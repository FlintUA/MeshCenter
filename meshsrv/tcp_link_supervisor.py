"""TcpLinkSupervisor - the ONE place a TCP radio link is (re)established.

DK2. Before this, healing a dropped TCP link was spread over three bounded,
partly overlapping mechanisms (process_identity_recovery's 6 attempts,
process_transport_autorecovery's 3-per-30-min, and a Reconnect button that
went through the serial release path). After roughly 20 minutes of the radio
being away they all gave up and only `docker restart` recovered.

This module is deliberately small, stdlib-only and free of server.py globals:
everything it needs is injected, so the schedule, the single-flight rule and
the state machine are unit-testable with a fake clock.

States
  IDLE        link healthy, or nothing to heal (not a TCP profile, identity
              refused). Nothing is scheduled.
  WAITING     an attempt is scheduled for `next_at`.
  ATTEMPTING  an attempt is running right now.

Rules
  * Unbounded: while the active profile is TCP and identity is not
    MISMATCH/NOT_FOUND, attempts continue forever. Only the delay is capped.
  * Delays: 30, 60, 120, 240, then 300 s (+-10 % jitter, never below 1 s).
  * `trigger_now()` (the Reconnect button) resets the backoff and makes the
    next tick attempt immediately; it never starts a second concurrent attempt.
  * One attempt at a time, enforced by `lock` - the same RLock the boot-time
    identity probe takes, so startup and the supervisor cannot overlap.
  * `attempt()` returns "connected", "retry" (transient failure) or "stop"
    (identity refused - fail closed, stop until something changes).
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

BACKOFF_DELAYS_S = (30, 60, 120, 240, 300)
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
        self._attempts = 0            # failed attempts since the link was last healthy
        self._next_at: Optional[float] = None
        self._last_error = ""
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
            return {
                "state": self._state,
                # The attempt being made now / due next ("attempt 3, next in 12 s"
                # = the third try happens in 12 s); 0 when nothing is scheduled.
                "attempt": self._attempts + 1 if self._state != STATE_IDLE else 0,
                "failed_attempts": self._attempts,
                "next_in_s": next_in,
                "last_error": self._last_error,
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
        session). Safe to call while an attempt is already running (then it
        only resets the counter - the running attempt is never duplicated)."""
        with self._mutex:
            self._attempts = 0
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
                self._reset_locked()
            return

        with self._mutex:
            forced, self._force = self._force, False
        if not forced and not self._auto_enabled():
            # Automatic reconnect switched off in Settings: only an explicit
            # Reconnect (forced) acts.
            with self._mutex:
                self._reset_locked()
            return
        if not forced and self._link_connected():
            with self._mutex:
                if self._state != STATE_IDLE:
                    self._on_event("TCP link restored", "OK", f"after {self._attempts} failed attempt(s)")
                self._reset_locked()
            return

        with self._mutex:
            if self._state == STATE_IDLE and not forced:
                # First sight of a dead link: attempt soon, not after a full wait.
                self._state = STATE_WAITING
                self._next_at = self._clock()
                self._on_event(
                    "TCP link down - reconnecting",
                    "WARNING",
                    "background reconnect will keep trying until the radio answers",
                )
            if self._state != STATE_WAITING or self._clock() < (self._next_at or 0):
                return
            self._state = STATE_ATTEMPTING

        self._run_attempt()

    def _run_attempt(self) -> None:
        with self.lock:
            try:
                outcome = self._attempt()
                error = ""
            except Exception as exc:
                outcome = OUTCOME_RETRY
                error = str(exc)

        with self._mutex:
            if outcome == OUTCOME_CONNECTED:
                self._on_event("TCP link re-established", "OK", f"attempt {self._attempts + 1}")
                self._reset_locked()
            elif outcome == OUTCOME_STOP:
                self._on_event("TCP reconnect stopped", "ERROR", "radio identity was refused - not retrying")
                self._reset_locked()
            else:
                self._attempts += 1
                self._last_error = error or self._last_error
                delay = backoff_delay(self._attempts, rng=self._rng)
                self._state = STATE_WAITING
                self._next_at = self._clock() + delay

    def _reset_locked(self) -> None:
        self._state = STATE_IDLE
        self._attempts = 0
        self._next_at = None
        self._last_error = ""
