"""The radio inbound worker: polls a transport's `drain_received()` and hands
every event to the shared Core ingest (meshsrv/inbound_events.py).

Poll, not push: the adapter IPC is strictly one response per request, so the
adapter buffers what the library receives (a bounded queue) and Core drains it
about once a second. `drain_received()` reads adapter memory only - it never
talks to the radio - so a 1 s poll costs the radio nothing.

Ingest happens only while the accepted radio is TCP *and* its identity is
MATCH (`eligibility()` decides, per tick, from live state - so a Settings switch
to or from TCP needs no restart). Anything else waits quietly. Failures that are
just "the link is busy / reconnecting / the adapter is restarting" are expected
and never become an ERROR every second; a broken event is dropped on its own,
never the batch it arrived in; and nothing here logs message text.

Serial keeps its own source (the `--listen` CLI parser); both feed the same
ingest, so dedup, chat routing, MCAttach and the waypoint store behave the same.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from meshsrv.radio_transport import (
    ReceivedTextEvent,
    ReceivedWaypointEvent,
    TransportError,
    TransportErrorCode,
)

POLL_INTERVAL_S = 1.0
IDLE_INTERVAL_S = 5.0          # not a TCP radio / identity not MATCH: no need to look often
DRAIN_LIMIT = 100              # a batch; a full one is followed immediately by another
MAX_BATCHES_PER_TICK = 5
DRAIN_TIMEOUT_S = 5.0

# Errors that only mean "not now": the router is busy with a switch/reconnect,
# the link is down, the adapter process is (re)starting. Waited out silently.
_QUIET_CODES = {
    TransportErrorCode.BUSY,
    TransportErrorCode.NOT_CONNECTED,
    TransportErrorCode.ADAPTER_UNAVAILABLE,
    TransportErrorCode.TIMEOUT,
    TransportErrorCode.UNSUPPORTED,
}

WARN_INTERVAL_S = 60.0         # rate limit for overflow / unexpected-error messages
QUIET_LOG_INTERVAL_S = 300.0   # a long outage is mentioned once in a while, never every second


class InboundWorker:
    def __init__(
        self,
        *,
        drain: Callable[..., Any],
        eligibility: Callable[[], Optional[str]],
        ingest_text: Callable[[ReceivedTextEvent], Any],
        ingest_waypoint: Callable[[ReceivedWaypointEvent], Any],
        log: Callable[..., Any] = print,
        log_system_event: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self._drain = drain
        self._eligibility = eligibility
        self._ingest_text = ingest_text
        self._ingest_waypoint = ingest_waypoint
        self._log = log
        self._log_system_event = log_system_event
        self._clock = clock
        self._sleep = sleep

        self._lock = threading.Lock()
        self._stats = {
            "ticks": 0,
            "drains": 0,
            "drained_events": 0,
            "text_events": 0,
            "waypoint_events": 0,
            "ingest_errors": 0,
            "malformed_events": 0,          # undecodable on the adapter or on this side
            "overflow_dropped": 0,          # discarded by the adapter's bounded queue
            "discarded_on_identity_refusal": 0,
            "soft_errors": 0,
        }
        self._status = "idle"
        self._waiting_reason: Optional[str] = None
        self._last_drain_at: Optional[float] = None
        self._connection_generation: Optional[int] = None
        self._last_warn_at = float("-inf")
        self._last_quiet_log_at = float("-inf")

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """Counters for observability. Counts and states only - never content."""
        with self._lock:
            snapshot = dict(self._stats)
            snapshot["status"] = self._status
            snapshot["waiting_reason"] = self._waiting_reason
            snapshot["last_drain_age_s"] = (
                None if self._last_drain_at is None else round(self._clock() - self._last_drain_at, 1)
            )
            snapshot["connection_generation"] = self._connection_generation
        return snapshot

    def _bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[name] += amount

    def _set_status(self, status: str, reason: Optional[str] = None) -> None:
        with self._lock:
            self._status = status
            self._waiting_reason = reason

    def _rate_limited(self, attribute: str, interval: float) -> bool:
        """True (and stamps now) if at least `interval` passed since the last time."""
        now = self._clock()
        with self._lock:
            if now - getattr(self, attribute) < interval:
                return False
            setattr(self, attribute, now)
            return True

    # ------------------------------------------------------------------
    def tick(self) -> str:
        """One poll cycle. Returns "ingesting", "waiting" or "idle" (for tests
        and for run_forever's pacing). Never raises."""
        self._bump("ticks")
        try:
            reason = self._eligibility()
        except Exception as error:  # a broken gate must never take the thread down
            self._set_status("waiting", "eligibility_error")
            self._warn(f"[INBOUND] eligibility check failed: {type(error).__name__}")
            return "waiting"
        if reason:
            self._set_status("idle", reason)
            return "idle"

        for _ in range(MAX_BATCHES_PER_TICK):
            try:
                batch = self._drain(limit=DRAIN_LIMIT, timeout=DRAIN_TIMEOUT_S)
            except TransportError as error:
                return self._on_transport_error(error)
            except Exception as error:
                self._bump("soft_errors")
                self._set_status("waiting", "drain_error")
                self._warn(f"[INBOUND] drain failed unexpectedly: {type(error).__name__}")
                return "waiting"

            self._set_status("ingesting")
            self._handle_batch(batch)
            if len(batch.events) < DRAIN_LIMIT:
                break  # drained dry; a full batch means there may be more, so go round again
        return "ingesting"

    def _on_transport_error(self, error: TransportError) -> str:
        self._bump("soft_errors")
        self._set_status("waiting", error.code.value)
        if error.code in _QUIET_CODES:
            if self._rate_limited("_last_quiet_log_at", QUIET_LOG_INTERVAL_S):
                self._log(f"[INBOUND] waiting for the radio link ({error.code.value})", flush=True)
            return "waiting"
        self._warn(f"[INBOUND] drain_received failed: {error.code.value}")
        return "waiting"

    def _warn(self, message: str) -> None:
        if self._rate_limited("_last_warn_at", WARN_INTERVAL_S):
            self._log(message, flush=True)

    def _handle_batch(self, batch) -> None:
        with self._lock:
            self._stats["drains"] += 1
            self._stats["drained_events"] += len(batch.events)
            self._stats["overflow_dropped"] += batch.dropped
            self._stats["malformed_events"] += batch.malformed
            self._last_drain_at = self._clock()
            self._connection_generation = batch.connection_generation

        if batch.dropped and self._rate_limited("_last_warn_at", WARN_INTERVAL_S):
            message = (
                f"{batch.dropped} inbound TCP event(s) were dropped by the adapter's queue "
                "(overflow) - MeshCenter is not draining fast enough"
            )
            self._log(f"[INBOUND] {message}", flush=True)
            if self._log_system_event:
                self._log_system_event(title="Inbound queue overflow", level="WARNING", details=message, source="radio")

        for event in batch.events:
            try:
                if isinstance(event, ReceivedTextEvent):
                    self._bump("text_events")
                    self._ingest_text(event)
                elif isinstance(event, ReceivedWaypointEvent):
                    self._bump("waypoint_events")
                    self._ingest_waypoint(event)
                else:
                    self._bump("malformed_events")
            except Exception as error:
                # One bad event costs that event only. The exception type says
                # what broke; message text can be in the exception, so it is not logged.
                self._bump("ingest_errors")
                self._warn(f"[INBOUND] dropped an event that could not be ingested ({type(error).__name__})")

    # ------------------------------------------------------------------
    def discard_pending(self, drain: Callable[..., Any], *, max_batches: int = 20) -> int:
        """Empty the adapter's queue and drop what is in it, without ingesting.
        Used when a session is torn down because its radio failed identity
        verification: whatever that session captured must not survive to be
        mixed with a later, legitimate connection (plan section 46). Best
        effort - a failure just means there was nothing reachable to discard."""
        discarded = 0
        for _ in range(max_batches):
            try:
                batch = drain(limit=DRAIN_LIMIT, timeout=DRAIN_TIMEOUT_S)
            except Exception:
                break
            discarded += len(batch.events)
            if len(batch.events) < DRAIN_LIMIT:
                break
        if discarded:
            self._bump("discarded_on_identity_refusal", discarded)
        return discarded

    def run_forever(self) -> None:
        self._log("[INBOUND] worker started", flush=True)
        while True:
            state = self.tick()
            self._sleep(POLL_INTERVAL_S if state in ("ingesting", "waiting") else IDLE_INTERVAL_S)
