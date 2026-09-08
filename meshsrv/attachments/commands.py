"""meshsrv/attachments/commands.py

The command model and bounded command queue (internal-rest-api.md §3.2
and §3.4; Execution Plan Step 1.6A.1). MIT-licensed Core code - stdlib
only, never `meshtastic`, never Flask.

Every mutation the Step 1.6A REST surface offers is represented as a
`Command`: a frozen, immutable dataclass built and validated on the
*request thread* (using pure functions only - no `conn`, no network, no
file I/O beyond the create endpoint's own spool write, which §7.2 does
before this module is reached), then handed to the worker across a
bounded `queue.Queue`. The worker drains up to `MAX_COMMANDS_PER_TICK`
per tick and executes each on its own (the single owner of `conn`); the
request thread never touches SQLite, the tick lock, or the network.

Why a bounded queue with `put_nowait()` rather than an unbounded one:
the request thread must never block, and a burst of mutations must not
grow an unbounded in-memory backlog that a slow worker (a slow/unreachable
Relay) could never drain. `queue.Full` is the backpressure signal - the
facade maps it to `429 command_queue_full` (§3.4), and the idempotent
create path additionally rolls back its reservation (§3.6 step 5).

The command kinds below are the §7.9 enumeration, all twenty of them, so
`Command` can reject an unknown kind at construction rather than letting a
typo reach the worker and fail there. The *payload* shape per kind is
fleshed out per endpoint in §7 (and validated by the facade's typed
submit methods); this module only freezes whatever already-validated
payload it is given and keeps its own `repr` free of it.

The queue-size / per-tick constants are deliberately named, single-source
constants so the Step 1.6A.1 snapshot-cost benchmark (and a real Pi tick
measurement) can tune them once, with evidence, rather than every call
site carrying a magic number (§15.2 - the constants are chosen *after*
that benchmark, not before). The values here are conservative
placeholders that make the queue functional without committing to an
unmeasured number.
"""

from __future__ import annotations

import dataclasses
import queue
import types
import uuid
from typing import Any, Mapping

# ---- constants (§15.2: chosen from the Step 1.6A.1 benchmark on a real
# Pi Zero 2 W - see scripts/benchmark_mca_snapshots.py) ------------------

# §3.2: the bounded command queue's capacity. Mirrors the inbound queue's
# shape (service.INBOUND_QUEUE_MAXSIZE) - a hard backpressure bound so a
# burst of mutations cannot grow an unbounded in-memory backlog behind a
# slow worker. 64 is a deliberate multiple of `MAX_COMMANDS_PER_TICK`, so
# the queue absorbs a few ticks' worth of burst without the worker falling
# behind.
COMMAND_QUEUE_MAXSIZE = 64

# §3.2: the worker drains up to this many commands per tick, before the
# automatic-state row scan. The Step 1.6A.1 benchmark measured the drain of
# 16 commands (register -> running -> succeeded) at ~14 ms median on a Pi
# Zero 2 W - a small fraction of the snapshot build, so 16 is comfortably
# within the tick budget and is kept as-is (§15.2).
MAX_COMMANDS_PER_TICK = 16

# The §7.9 command-type enumeration, complete. `Command` rejects anything
# outside this set so an unknown kind fails at construction, on the request
# thread, with a clean error rather than surfacing mid-tick.
COMMAND_KINDS = frozenset({
    "attachment_create",
    "attachment_retry",
    "attachment_download",
    "attachment_save",
    "attachment_reject",
    "attachment_cancel",
    "attachment_revoke",
    "attachment_delete_local_content",
    "attachment_import",
    "attachment_copy_code",
    "attachment_add_delivery",
    "contact_request_key",
    "provider_probe",
    "provider_register",
    "provider_update",
    "provider_set_default",
    "provider_remove",
    "provider_set_upload_token",
    "provider_clear_upload_token",
    "provider_check",
})


class CommandQueueFull(Exception):
    """Raised (or surfaced) when a command cannot be enqueued because the
    bounded queue is at capacity. The facade maps this to the `429
    command_queue_full` envelope (§3.4); it is a *distinct* exception type
    rather than a bare `queue.Full` so callers can catch exactly this
    condition without also catching an unrelated `queue.Full` from some
    other queue in the process."""


def mint_command_id() -> str:
    """A fresh, unpredictable command id: `uuid.uuid4().hex` (32 hex
    chars, 122 random bits). Mined on the request thread *before* enqueue
    (§3.6), so a replay can return the same id. Pure - no state, no I/O."""
    return uuid.uuid4().hex


@dataclasses.dataclass(frozen=True)
class Command:
    """One mutation, frozen and immutable once built. Constructed on the
    request thread from an already-validated payload; the worker is the
    only executor.

    `payload` is frozen to a read-only mapping (`types.MappingProxyType`)
    in `__post_init__`, so neither the request thread nor the worker can
    mutate it after the fact - a command's meaning is fixed at enqueue, not
    subject to a later in-place edit. The mapping's *values* are expected
    to be JSON-safe primitives or small immutable structures; this module
    does a shallow freeze (deep-freezing nested structures is unnecessary
    for the payloads §7 defines, and would be speculative complexity).

    `repr()` deliberately omits `payload` (and everything else beyond the
    id and kind) - a `provider_set_upload_token` payload carries a raw
    upload token, so a Command that ever lands in a log line or a debugger
    must not print it. Any code that needs to *read* the payload does so
    via the `.payload` attribute, explicitly, never via `repr`/`str`."""

    command_id: str
    kind: str
    payload: Mapping[str, Any]
    created_at: float

    def __post_init__(self) -> None:
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"unknown command kind: {self.kind!r}")
        object.__setattr__(self, "payload", types.MappingProxyType(dict(self.payload)))

    def __repr__(self) -> str:
        # No payload, no created_at - only the two fields that can never
        # carry a secret. This is the single line of defence that keeps an
        # accidentally-logged Command from leaking a token.
        return f"Command(command_id={self.command_id!r}, kind={self.kind!r})"


class CommandQueue:
    """The §3.2 bounded command queue - a thin, intent-revealing wrapper
    around `queue.Queue(maxsize=COMMAND_QUEUE_MAXSIZE)` that translates the
    two states a caller actually cares about into domain-appropriate
    exceptions (`CommandQueueFull` / `queue.Empty`) instead of leaking
    `queue.Full`/`queue.Empty` semantics into the facade.

    `put_nowait()` never blocks: it raises `CommandQueueFull` when the
    queue is at capacity, so the request thread can answer `429
    command_queue_full` immediately (§3.4) rather than ever stalling on
    the worker. `get_nowait()` is the worker's drain; it raises
    `queue.Empty` when there is nothing to run (the normal idle case,
    caught by the drain loop - not an error)."""

    def __init__(self, maxsize: int = COMMAND_QUEUE_MAXSIZE):
        self._queue: "queue.Queue[Command]" = queue.Queue(maxsize=maxsize)

    def put_nowait(self, command: Command) -> None:
        """Enqueue a command without blocking. Raises `CommandQueueFull`
        (not `queue.Full`) when at capacity."""
        try:
            self._queue.put_nowait(command)
        except queue.Full as exc:
            raise CommandQueueFull() from exc

    def get_nowait(self) -> Command:
        """Dequeue one command, or raise `queue.Empty` when idle."""
        return self._queue.get_nowait()

    def qsize(self) -> int:
        """Approximate current size (informational only - the same
        best-effort semantics as `queue.Queue.qsize`)."""
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()
