"""TcpLinkSupervisor - the ONE place a TCP radio link is (re)established.

DK2 introduced it; DK3 retuned it against the real T-Beam (ESP32, firmware
2.7.15). What the capture showed (docs/Docker_Installation.md, "Radio
behaviour"): the radio closes its API session on its own, at random, and after a
close it tends to refuse new sessions for a while (a new session closed ~1 s
after the config dump, then SYNs ignored for 1-2 minutes). So this module does
not fight that: it keeps trying forever, but never faster than the radio can
take, never counts a session that dies within seconds as a recovery, and says so
when the pattern looks like another client holding the radio's single slot.

This module is stdlib-only and free of server.py globals: everything it needs is
injected, so the schedule, the single-flight rule and the state machine are
unit-testable with a fake clock.

States
  IDLE        link healthy, or nothing to heal (not a TCP profile, identity
              refused). Nothing is scheduled.
  WAITING     an attempt is scheduled for `next_at`.
  ATTEMPTING  an attempt is running right now.

Rules
  * Unbounded: while the active profile is TCP and identity is not
    MISMATCH/NOT_FOUND, attempts continue forever. Only the delay is capped.
  * After a link that was up goes down, wait HOLD_OFF_S (25 s) before the first
    attempt - the radio needs that long to release its slot. A radio never
    reached since boot is tried at once. Later attempts back off 30/60/90/120 s
    (cap 120 s), +-10 % jitter.
  * A link only counts as recovered after it has stayed up UP_THRESHOLD_S (20 s).
    Until then the failure counter is NOT reset, so a radio that accepts a
    session and drops it a second later cannot make us hammer it ("attempt 6
    right after a success" in the DK2 NAS log).
  * Three or more sessions that die within UP_THRESHOLD_S inside five minutes
    flag `contention` ("the radio may be in use by another client") and put the
    delay floor at the cap.
  * `trigger_now()` (the Reconnect button) resets the backoff and attempts on
    the next tick, hold-off skipped; it never starts a second concurrent attempt.
  * One attempt at a time, enforced by `lock` - the same RLock the boot-time
    identity probe takes.
  * `attempt()` returns "connected", "retry" (transient failure) or "stop"
    (identity refused - fail closed, stop until something changes).
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, List, Optional

HOLD_OFF_S = 25
BACKOFF_DELAYS_S = (30, 60, 90, 120)   # after the 1st, 2nd, ... failed attempt; last value is the cap
MAX_DELAY_S = BACKOFF_DELAYS_S[-1]
UP_THRESHOLD_S = 20
CONTENTION_CLOSES = 3
CONTENTION_WINDOW_S = 300
LOSS_HISTORY_S = 3600
JITTER_RATIO = 0.10
TICK_INTERVAL_S = 5.0

OUTCOME_CONNECTED = "connected"
OUTCOME_RETRY = "retry"
OUTCOME_STOP = "stop"

STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_ATTEMPTING = "attempting"


def backoff_delay(attempt_index: int, *, rng: Callable[[], float] = random.random) -> float:
    """Delay after the `attempt_index`-th failed attempt (1-based), capped."""
    base = BACKOFF_DELAYS_S[min(max(attempt_index, 1), len(BACKOFF_DELAYS_S)) - 1]
    jitter = (rng() * 2.0 - 1.0) * JITTER_RATIO * base
    return max(1.0, base + jitter)


class TcpLinkSupervisor:
    def __init__(
        self,
        *,
        is_tcp_profile: Callable[[], bool],
        identity_refused: Callable[[], bool],
        link_connected: Callable[[], bool],
        attempt: Callable[[], str],
        auto_enabled: Optional[Callable[[], bool]] = None,
        lock: Optional[threading.RLock] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        on_event: Optional[Callable[[str, str, str], None]] = None,
    ) -> None:
        self._is_tcp_profile = is_tcp_profile
        self._identity_refused = identity_refused
        self._link_connected = link_connected
        self._attempt = attempt
        self._auto_enabled = auto_enabled or (lambda: True)
        self.lock = lock or threading.RLock()
        self._clock = clock
        self._rng = rng
        self._on_event = on_event or (lambda title, level, details: None)

        self._mutex = threading.Lock()  # guards the fields below only
        self._state = STATE_IDLE
        self._attempts = 0            # failed attempts / unstable sessions since the link was last stable
        self._next_at: Optional[float] = None
        self._last_error = ""
        self._up_since: Optional[float] = None   # when the current session was first seen up
        self._probation = False       # up, but not yet for UP_THRESHOLD_S
        self._ever_up = False         # a session has been up at least once in this process
        self._quick_closes: List[float] = []
        self._losses: List[float] = []
        self._force = False
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        with self._mutex:
            now = self._clock()
            next_in = None
            if self._state == STATE_WAITING and self._next_at is not None:
                next_in = max(0, int(round(self._next_at - now)))
            self._prune_locked(now)
            return {
                "state": self._state,
                # The attempt being made now / due next ("attempt 3, next in 12 s"
                # = the third try happens in 12 s); 0 when nothing is scheduled.
                "attempt": self._attempts + 1 if self._state != STATE_IDLE else 0,
                "failed_attempts": self._attempts,
                "next_in_s": next_in,
                "last_error": self._last_error,
                "contention": self._contention_locked(now),
                "link_losses_1h": len(self._losses),
                "up_for_s": int(now - self._up_since) if self._up_since is not None else None,
            }

    def is_recovering(self) -> bool:
        with self._mutex:
            return self._state in (STATE_WAITING, STATE_ATTEMPTING)

    # ----------------------------------------------------------------- control
    def set_error(self, message: str) -> None:
        """Called by the attempt callable to explain a transient failure."""
        with self._mutex:
            self._last_error = str(message or "")

    def trigger_now(self) -> dict:
        """Reconnect button: reset backoff, attempt on the next tick - even if
        the link looks healthy (an explicit user request forces a fresh
        session), hold-off skipped. Safe to call while an attempt is already
        running (then it only resets the counter - the running attempt is never
        duplicated)."""
        with self._mutex:
            self._attempts = 0
            self._quick_closes.clear()
            self._force = True
            if self._state != STATE_ATTEMPTING:
                self._state = STATE_WAITING
                self._next_at = self._clock()
        self._wake.set()
        return self.status()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="tcp-link-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    # --------------------------------------------------------------------- loop
    def _run(self) -> None:
        while not self._stopped:
            try:
                self.tick()
            except Exception as error:  # never let the loop die
                self._on_event("TCP link supervisor error", "WARNING", str(error))
            self._wake.wait(TICK_INTERVAL_S)
            self._wake.clear()

    def tick(self) -> None:
        """One scheduling decision (also called directly by tests)."""
        if not self._is_tcp_profile() or self._identity_refused():
            with self._mutex:
                self._reset_all_locked()
            return

        with self._mutex:
            forced, self._force = self._force, False
        if not forced and not self._auto_enabled():
            # Automatic reconnect switched off in Settings: only an explicit
            # Reconnect (forced) acts.
            with self._mutex:
                self._reset_all_locked()
            return

        connected = self._link_connected()
        now = self._clock()
        if connected and not forced:
            self._note_up(now)
            return
        if not connected:
            self._note_down(now)

        with self._mutex:
            if self._state == STATE_IDLE and not forced:
                # First sight of a dead link (never reached since boot): try at
                # once; a link that WAS up already got its hold-off in _note_down.
                self._state = STATE_WAITING
                self._next_at = now
                self._on_event(
                    "TCP link down - reconnecting",
                    "WARNING",
                    "background reconnect will keep trying until the radio answers",
                )
            if self._state != STATE_WAITING or now < (self._next_at or 0):
                return
            self._state = STATE_ATTEMPTING

        self._run_attempt()

    # ---------------------------------------------------------- link bookkeeping
    def _note_up(self, now: float) -> None:
        with self._mutex:
            if self._up_since is None:
                self._up_since = now
                self._ever_up = True
            if self._state != STATE_IDLE and self._state != STATE_ATTEMPTING:
                # A session is up (someone else connected it, or our attempt did):
                # nothing left to schedule, but the failure counter stays until
                # the session has proven itself.
                self._state = STATE_IDLE
                self._next_at = None
            if self._probation and now - self._up_since >= UP_THRESHOLD_S:
                self._on_event("TCP link restored", "OK", f"stable after {self._attempts} failed attempt(s)")
                self._probation = False
                self._attempts = 0
                self._last_error = ""
            elif not self._probation and self._attempts:
                # Up without our attempt (e.g. adapter-side recovery): same rule.
                self._probation = True

    def _note_down(self, now: float) -> None:
        with self._mutex:
            if self._up_since is None:
                return
            up_for = now - self._up_since
            self._up_since = None
            self._probation = False
            self._losses.append(now)
            quick = up_for < UP_THRESHOLD_S
            if quick:
                self._quick_closes.append(now)
                self._attempts += 1   # an unstable session is a failed attempt, not a recovery
            else:
                self._attempts = 0
            self._prune_locked(now)
            contention = self._contention_locked(now)
            delay = float(HOLD_OFF_S) if not quick else self._delay_locked(now)
            self._state = STATE_WAITING
            self._next_at = now + delay
            self._on_event(
                "TCP link lost",
                "WARNING",
                f"session lasted {int(up_for)}s - "
                + ("it dropped almost at once; " if quick else "")
                + (
                    "the radio may be in use by another client; "
                    if contention
                    else ""
                )
                + f"next attempt in {int(delay)}s",
            )

    def _prune_locked(self, now: float) -> None:
        self._losses = [t for t in self._losses if now - t < LOSS_HISTORY_S]
        self._quick_closes = [t for t in self._quick_closes if now - t < CONTENTION_WINDOW_S]

    def _contention_locked(self, now: float) -> bool:
        return len([t for t in self._quick_closes if now - t < CONTENTION_WINDOW_S]) >= CONTENTION_CLOSES

    def _delay_locked(self, now: float) -> float:
        delay = backoff_delay(self._attempts, rng=self._rng)
        if self._contention_locked(now):
            delay = max(delay, float(MAX_DELAY_S))
        return delay

    # ------------------------------------------------------------------ attempts
    def _run_attempt(self) -> None:
        with self.lock:
            try:
                outcome = self._attempt()
                error = ""
            except Exception as exc:
                outcome = OUTCOME_RETRY
                error = str(exc)

        now = self._clock()
        with self._mutex:
            if outcome == OUTCOME_CONNECTED:
                self._on_event("TCP link re-established", "OK", f"attempt {self._attempts + 1}; watching it for {UP_THRESHOLD_S}s")
                self._state = STATE_IDLE
                self._next_at = None
                self._up_since = now
                self._ever_up = True
                self._probation = True
                self._last_error = ""
            elif outcome == OUTCOME_STOP:
                self._on_event("TCP reconnect stopped", "ERROR", "radio identity was refused - not retrying")
                self._reset_all_locked()
            else:
                self._attempts += 1
                self._last_error = error or self._last_error
                self._state = STATE_WAITING
                self._next_at = now + self._delay_locked(now)

    def _reset_all_locked(self) -> None:
        self._state = STATE_IDLE
        self._attempts = 0
        self._next_at = None
        self._last_error = ""
        self._up_since = None
        self._probation = False
        self._quick_closes.clear()
