"""meshsrv/attachments/command_registry.py

The in-memory command-result store (internal-rest-api.md §3.4; Execution
Plan Step 1.6A.1). MIT-licensed Core code - stdlib only, never
`meshtastic`, never Flask.

A mutation returns `202` + `command_id`; the client learns the *outcome*
by polling `GET /api/mca/commands/{command_id}` (§7.9). This registry is
the thing that poll reads, and it is deliberately **not** a worker-
published immutable snapshot: the worker has not necessarily run when the
first `GET` arrives, so the request thread itself must be able to record
"queued" *before* the command is enqueued (so the worker can never dequeue
a command whose registry entry does not yet exist, and an immediate `GET`
sees `queued`, never `404`).

Ownership split (the whole point of the threading model, §3.1/§3.2):

- the **request thread** registers `queued` (via `register()`, called
  *before* the queue write);
- the **worker** transitions `running` → `succeeded`/`failed` as it
  dequeues and executes (`mark_running()` / `mark_succeeded()` /
  `mark_failed()`);
- both are guarded by one *short, dedicated* lock - not the tick lock, and
  with **no SQLite** anywhere in this module.

Eviction is **terminal-only** (§3.4): `queued`/`running` entries are never
evicted (a client polling an in-flight command must keep seeing it), and
their count is naturally bounded by the bounded command queue plus the
per-tick drain. Terminal entries (`succeeded`/`failed`) are evicted on
either a TTL (older than `COMMAND_RESULT_TTL_SECONDS`) or, when more than
`COMMAND_RESULT_MAX_ENTRIES` terminal entries accumulate, least-recently-
used first. Eviction is *lazy* - it runs on the next registry access
rather than from a background sweeper - which is the honest, simplest
correct behavior for an in-memory cache and is documented here rather than
hidden.

Restart-amnesic (§3.4): nothing is persisted. On restart every
`command_id` is forgotten and `GET` returns `404 command_not_found`. A
command still `queued`/`running` at crash never executes, so the client
must re-issue (or re-run a probe) - safe because each command is either
idempotent (create) or re-drivable (the action endpoints), and the
single-owner SQLite transaction model guarantees a crash mid-command
leaves a consistent on-disk state. `command_not_found` does **not** prove
a side effect did not occur; a re-issued Relay-facing command must first
reconcile persisted/remote state (documented in §3.4, and enforced by the
worker's execution, not by this registry).

The result payload is *caller-produced and caller-validated*: the worker
sets `result`/`resource_id`/`error_code` and is responsible for ensuring
`result` carries no secrets, no absolute paths, no ciphertext (§3.4 /
§7.9). This registry stores what it is given and keeps its own `repr`
free of the payload, but it does not second-guess the payload's contents -
with one exception: it enforces a hard *size* bound
(`COMMAND_RESULT_MAX_PAYLOAD_BYTES`) so a misbehaving worker cannot retain
an unbounded blob per terminal entry and defeat the `COMMAND_RESULT_MAX_
ENTRIES` memory bound. Size is a mechanical limit, not a content judgment,
so it does not re-open the "caller validates content" split - and it never
*raises* out of the worker: an oversized or non-serializable result is
recorded as a terminal `failed` with a bounded `error_code` and no payload,
so a command can never be left stuck in `running` (§15.2).
"""

from __future__ import annotations

import collections
import dataclasses
import json
import threading
import time
import types
from typing import Any, Dict, Mapping, Optional

from meshsrv.attachments.commands import Command

# ---- status values (§7.9) ------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

_TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})

# ---- eviction constants (§15.2: chosen from the Step 1.6A.1 benchmark
# on a real Pi Zero 2 W - see scripts/benchmark_mca_snapshots.py) --------

# Upper bound on the number of *terminal* (succeeded/failed) entries kept
# at once. queued/running entries are outside this budget (never evicted).
COMMAND_RESULT_MAX_ENTRIES = 1000

# A terminal entry older than this (measured from `updated_at`, when it
# reached its terminal state) is evicted on the next registry access. One
# hour is a long enough window for a client to poll a completed command's
# outcome after a burst (§3.4); kept as-is (§15.2).
COMMAND_RESULT_TTL_SECONDS = 3600

# Hard cap on the serialized size of a single command's `result` payload
# (JSON bytes, §7.9). This is what bounds the *worst-case* memory of
# `COMMAND_RESULT_MAX_ENTRIES` terminal entries: 1000 entries x at most
# ~16 KiB of payload ~= 16 MiB of retained result bytes, plus the small
# frozen-dataclass overhead per entry and the two index dicts. That is NOT
# negligible next to the attachment snapshot: the Step 1.6A.1 benchmark
# measured the retained attachment snapshot's Python-object half at
# ~14.5 MiB, so a fully-loaded command registry (~16 MiB) is actually the
# *larger* of the two retained structures. The honest worst case is their
# *sum* (~30 MiB of retained state) - a real budget line that still fits the
# 415 MiB of a Pi Zero 2 W, but not "memory-trivial". A worker that builds a
# larger result is a bug (every §7.9 shape is tiny: an `attachment_id`, a
# `provider_id`, a probe summary); `mark_succeeded` converts an oversized or
# non-serializable result into a terminal FAILED with a bounded error_code
# and no payload, rather than raising out of the worker (see
# `_result_payload_error`).
COMMAND_RESULT_MAX_PAYLOAD_BYTES = 16 * 1024


def _result_payload_error(result: Optional[Mapping[str, Any]], max_payload_bytes: int) -> Optional[str]:
    """Return a bounded error code if `result` cannot be safely retained as a
    JSON-serializable, size-bounded payload; `None` if it is fine. Two
    distinct codes so a poller can tell them apart:

    - `result_payload_not_serializable` - the result cannot be JSON-encoded
      (a circular structure is the realistic case; `json.dumps` raises
      `ValueError` for those, and `default=str` cannot rescue them);
    - `result_payload_too_large` - the result serializes, but its JSON byte
      length exceeds `max_payload_bytes`.

    JSON is what the facade serves to pollers (§7.9), so the JSON byte length
    is the honest proxy for both wire and retained cost."""
    if result is None:
        return None
    try:
        encoded = json.dumps(result, default=str, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return "result_payload_not_serializable"
    if len(encoded) > max_payload_bytes:
        return "result_payload_too_large"
    return None


@dataclasses.dataclass(frozen=True)
class CommandResult:
    """The immutable read shape `get()` returns - the §7.9 projection,
    without the JSON envelope (the facade adds `{"ok": true, "command":
    ...}`). Frozen; `result` (when present) is frozen to a read-only
    mapping so neither a poller nor a later mutation can alter an already-
    published result. `repr()` omits `result` - same no-secret-in-logs
    rule as `Command`."""

    command_id: str
    kind: str
    status: str
    created_at: float
    updated_at: float
    resource_id: Optional[str] = None
    result: Optional[Mapping[str, Any]] = None
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        if self.result is not None:
            object.__setattr__(self, "result", types.MappingProxyType(dict(self.result)))

    def __repr__(self) -> str:
        return (
            f"CommandResult(command_id={self.command_id!r}, kind={self.kind!r}, "
            f"status={self.status!r}, error_code={self.error_code!r})"
        )


# The only legal status transitions (a strict lifecycle - the worker is the
# sole transitioner and should never produce anything else; an illegal
# transition is a bug, so it raises rather than being silently coerced).
_ALLOWED_TRANSITIONS = {
    STATUS_QUEUED: frozenset({STATUS_RUNNING}),
    STATUS_RUNNING: frozenset({STATUS_SUCCEEDED, STATUS_FAILED}),
}


class CommandRegistry:
    """See the module docstring for the full contract. Thread-safe via a
    dedicated short lock; injected clock (`now_fn`) so TTL eviction is
    deterministic under test; restart-amnesic by construction (all state
    is instance-local)."""

    def __init__(
        self,
        *,
        max_entries: int = COMMAND_RESULT_MAX_ENTRIES,
        ttl_seconds: float = COMMAND_RESULT_TTL_SECONDS,
        max_payload_bytes: int = COMMAND_RESULT_MAX_PAYLOAD_BYTES,
        now_fn=time.time,
    ):
        self._lock = threading.Lock()
        self._entries: Dict[str, CommandResult] = {}
        # LRU order of *terminal* entries only - least-recently-used at the
        # front, most-recently-used at the back (OrderedDict.popitem(last=
        # False) evicts the LRU). Non-terminal entries are tracked only in
        # `_entries`.
        self._terminal_lru: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._max_payload_bytes = max_payload_bytes
        self._now = now_fn

    # ---- request thread: register queued (before the queue write) -------

    def register(self, command: Command) -> None:
        """Records a command as `queued`. Called by the request thread
        *before* `CommandQueue.put_nowait()` (§3.4 / §3.6 step 4), so the
        worker can never dequeue a command whose entry does not yet exist
        and an immediate `GET` sees `queued`, never `404`.

        Raises `ValueError` on a duplicate `command_id` - command ids are
        fresh UUIDs, so a collision here is a caller bug (e.g. re-
        registering an idempotent-replay id), never a benign event."""
        now = self._now()
        with self._lock:
            self._evict_locked(now)
            if command.command_id in self._entries:
                raise ValueError(f"command already registered: {command.command_id!r}")
            self._entries[command.command_id] = CommandResult(
                command_id=command.command_id,
                kind=command.kind,
                status=STATUS_QUEUED,
                created_at=command.created_at,
                updated_at=now,
            )

    # ---- worker thread: transition the lifecycle ------------------------

    def mark_running(self, command_id: str) -> None:
        now = self._now()
        with self._lock:
            self._transition_locked(command_id, STATUS_RUNNING, now)

    def mark_succeeded(
        self,
        command_id: str,
        *,
        resource_id: Optional[str] = None,
        result: Optional[Mapping[str, Any]] = None,
    ) -> None:
        # A successful command carries its `result` payload. An oversized or
        # non-serializable result is a worker bug (every §7.9 shape is tiny),
        # but it must NOT raise out of the worker and leave the command stuck
        # in `running` - it becomes a terminal `failed` with a bounded
        # error_code and no payload (the payload is the thing that was bad, so
        # nothing sensitive is retained). The result is measured *before*
        # taking the lock (it is caller-provided and immutable), keeping the
        # lock hold to the transition itself.
        error_code = _result_payload_error(result, self._max_payload_bytes)
        if error_code is not None:
            now = self._now()
            with self._lock:
                self._transition_locked(command_id, STATUS_FAILED, now, error_code=error_code)
            return
        now = self._now()
        with self._lock:
            self._transition_locked(
                command_id, STATUS_SUCCEEDED, now,
                resource_id=resource_id, result=result,
            )

    def mark_failed(self, command_id: str, *, error_code: str) -> None:
        now = self._now()
        with self._lock:
            self._transition_locked(command_id, STATUS_FAILED, now, error_code=error_code)

    def _transition_locked(
        self,
        command_id: str,
        status: str,
        now: float,
        *,
        resource_id: Optional[str] = None,
        result: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> None:
        current = self._entries.get(command_id)
        if current is None:
            raise ValueError(f"unknown command_id: {command_id!r}")
        if status not in _ALLOWED_TRANSITIONS.get(current.status, frozenset()):
            raise ValueError(f"illegal command transition {current.status!r} -> {status!r}")
        self._entries[command_id] = CommandResult(
            command_id=command_id,
            kind=current.kind,
            status=status,
            created_at=current.created_at,
            updated_at=now,
            resource_id=resource_id if resource_id is not None else current.resource_id,
            result=result if result is not None else current.result,
            error_code=error_code if error_code is not None else current.error_code,
        )
        if status in _TERMINAL_STATUSES:
            # Move to MRU position (or insert); a terminal entry's LRU
            # clock starts ticking at the moment it becomes terminal.
            self._terminal_lru[command_id] = None
            self._terminal_lru.move_to_end(command_id)
            self._evict_locked(now)

    # ---- any thread: read ------------------------------------------------

    def get(self, command_id: str) -> Optional[CommandResult]:
        """Returns the current result for `command_id`, or `None` if it is
        unknown or was evicted (terminal and past TTL / LRU). Safe to call
        from any thread.

        A successful read of a *terminal* entry refreshes its recency in
        the LRU order (a client actively polling a finished command keeps
        it from being the LRU-eviction victim) - TTL still bounds it."""
        now = self._now()
        with self._lock:
            self._evict_locked(now)
            entry = self._entries.get(command_id)
            if entry is not None and entry.status in _TERMINAL_STATUSES:
                self._terminal_lru.move_to_end(command_id)
            return entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def terminal_count(self) -> int:
        """Number of terminal (succeeded/failed) entries - for tests."""
        with self._lock:
            return len(self._terminal_lru)

    # ---- eviction (terminal-only) ---------------------------------------

    def _evict_locked(self, now: float) -> None:
        # TTL: drop terminal entries past their age. Iterate over the LRU
        # order's keys (a snapshot, since we mutate during the loop).
        for command_id in list(self._terminal_lru.keys()):
            entry = self._entries.get(command_id)
            if entry is None:
                self._terminal_lru.pop(command_id, None)
                continue
            if now - entry.updated_at > self._ttl_seconds:
                self._entries.pop(command_id, None)
                self._terminal_lru.pop(command_id, None)
        # LRU: while over capacity, evict the least-recently-used terminal.
        while len(self._terminal_lru) > self._max_entries:
            lru_id, _ = self._terminal_lru.popitem(last=False)
            self._entries.pop(lru_id, None)
