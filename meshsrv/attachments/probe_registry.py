"""meshsrv/attachments/probe_registry.py

The in-memory, single-use Provider onboarding probe store (internal-rest-
api.md §3.2/§3.4/§7.11; Execution Plan Step 1.6A.1). MIT-licensed Core
code - stdlib only, never `meshtastic`, never Flask.

Provider onboarding is **two-phase and trust-anchored on a single-use
probe record** (§7.11), so registration never re-trusts Relay parameters
supplied by the browser. Phase 1 (`POST /api/mca/providers/probe`) is a
worker command that resolves/fetches the Relay's own `/v1/info` and
records a `ProbeRecord` here; phase 2 (`POST /api/mca/providers`) hands
`probe_id` + `fingerprint_confirmation` back, and the worker **checks-
and-consumes** the probe atomically before calling `register()` with the
probe's fields. The browser can only ever reference a probe the server
already performed - it cannot fabricate a consistent
`origin`/`service_public_key`/`protocol_version`/TTL set.

Threading model (§3.2), the same split as `CommandRegistry`:

- the **worker** (the single owner of `conn`) is the only thing that
  calls `add()` (after a completed probe) and `consume()` (phase 2's
  atomic check-and-consume);
- the **request thread** calls `get()` only - a pure, non-consuming read
  used for synchronous phase-2 validation (`probe_id_not_found` /
  `probe_id_expired` / `provider_id_mismatch`), which must never block on
  or mutate worker state;
- guarded by one *short, dedicated* lock - not the tick lock, and with
  **no SQLite** anywhere in this module.

Nothing is ever persisted to SQLite or disk (§7.11): the record lives in
this in-memory store and expires after a short TTL. Restart-amnesic by
construction - a `probe_id` is forgotten on restart, which is exactly the
desired behavior for a single-use nonce (the client simply re-runs the
probe rather than looking for its result in a domain snapshot, §3.4).

Single-use semantics are *destructive*: `consume()` removes the record,
so a second `consume()` (or a replayed `provider_register` with the same
`probe_id`) returns `None` - the worker maps that to `probe_id_used`,
never a re-registration. `get()` is deliberately non-destructive so the
request thread can validate a `probe_id` repeatedly without consuming it.

The constants below are named, single-source placeholders chosen *after*
the Step 1.6A.1 snapshot-cost benchmark (§15.2), not measured numbers.
"""

from __future__ import annotations

import collections
import dataclasses
import secrets
import threading
import time
from typing import Dict, Optional

# ---- status values (§7.11) ------------------------------------------------

PROBE_STATUS_PROBED = "probed"
PROBE_STATUS_FAILED = "failed"

_PROBE_STATUSES = frozenset({PROBE_STATUS_PROBED, PROBE_STATUS_FAILED})

# ---- eviction constants (§15.2: placeholders, chosen after the Step
# 1.6A.1 benchmark - not measured yet) ------------------------------------

# Upper bound on the number of probe records held at once (a bounded
# store, §3.4 item 10: bounded admission, surfaced as 429/404 rather than
# silent drops). Probe records are single-use and short-lived, so this
# budget is small by design.
PROBE_MAX_ENTRIES = 64

# A probe record's short lifetime (minutes, §7.11: "expires after a short
# TTL"). A `provider_probe` is a fresh, expensive network operation; the
# browser must complete phase 2 within this window or the probe is stale
# and must be re-run.
PROBE_TTL_SECONDS = 300


def mint_probe_id() -> str:
    """A fresh, unpredictable probe id: `secrets.token_hex(16)` (32 hex
    chars, 128 random bits). Deliberately `secrets` (cryptographic
    entropy) rather than `uuid4` - a `probe_id` is a *trust-anchoring
    nonce* the browser must not be able to guess or pre-image, unlike a
    `command_id`, whose only requirement is uniqueness. Pure - no state,
    no I/O."""
    return secrets.token_hex(16)


@dataclasses.dataclass(frozen=True)
class ProbeRecord:
    """One completed Relay probe (§7.11), immutable once recorded. The
    `service_public_key` (raw 32 Ed25519 bytes, fetched from `/v1/info`)
    is what phase 2 feeds back into `register()`; `service_key_fingerprint`
    is the hex SHA-256 the browser shows for human confirmation. `status`
    is `probed` (reached the Relay and read its info) or `failed`.

    `repr()` omits `service_public_key` (raw key bytes have no place in a
    log line, same no-secret-in-logs rule as `Command`/`CommandResult`).
    The fingerprint *is* shown - it is exactly the value the user is asked
    to confirm, not a secret."""

    probe_id: str
    origin: str
    provider_id: str
    service_public_key: bytes
    service_key_fingerprint: str
    protocol_version: Optional[str]
    max_ciphertext_bytes: int
    min_ttl_seconds: Optional[int]
    max_ttl_seconds: Optional[int]
    expires_at: float
    status: str

    def __post_init__(self) -> None:
        if self.status not in _PROBE_STATUSES:
            raise ValueError(f"status must be one of {sorted(_PROBE_STATUSES)}, got {self.status!r}")
        if not self.probe_id:
            raise ValueError("probe_id must not be empty")

    def __repr__(self) -> str:
        # No service_public_key - the one line of defence that keeps an
        # accidentally-logged ProbeRecord from leaking raw key bytes.
        return (
            f"ProbeRecord(probe_id={self.probe_id!r}, origin={self.origin!r}, "
            f"provider_id={self.provider_id!r}, status={self.status!r})"
        )


def serialize_probe_record(record: ProbeRecord) -> dict:
    """The §7.9 safe `provider_probe` result payload, built field-by-field
    (never `dataclasses.asdict`). Deliberately omits `service_public_key`
    (raw bytes) and `status` - a *successful* probe's result is exactly
    the fields the browser needs to show the fingerprint and hand back
    `probe_id` + confirmation; a failed probe returns an `error_code`
    instead of a result payload (§7.11)."""
    return {
        "probe_id": record.probe_id,
        "provider_id": record.provider_id,
        "origin": record.origin,
        "service_key_fingerprint": record.service_key_fingerprint,
        "protocol_version": record.protocol_version,
        "max_ciphertext_bytes": record.max_ciphertext_bytes,
        "min_ttl_seconds": record.min_ttl_seconds,
        "max_ttl_seconds": record.max_ttl_seconds,
        "expires_at": record.expires_at,
    }


class ProbeRegistry:
    """See the module docstring for the full contract. Thread-safe via a
    dedicated short lock; injected clock (`now_fn`) so TTL expiry is
    deterministic under test; restart-amnesic by construction (all state
    is instance-local); no SQLite/disk anywhere.

    Eviction is *lazy* (runs on the next access, like `CommandRegistry`):
    an expired record is dropped on `get()`/`consume()`/`add()`, and an
    `add()` that would exceed `PROBE_MAX_ENTRIES` evicts the oldest-
    inserted record (FIFO - a probe is only useful within its short TTL,
    so age-of-insertion is the right tiebreak, not recency-of-use)."""

    def __init__(
        self,
        *,
        max_entries: int = PROBE_MAX_ENTRIES,
        ttl_seconds: float = PROBE_TTL_SECONDS,
        now_fn=time.time,
    ):
        self._lock = threading.Lock()
        # Insertion-ordered map: oldest-inserted at the front, newest at
        # the back (popitem(last=False) evicts the oldest).
        self._records: "collections.OrderedDict[str, ProbeRecord]" = collections.OrderedDict()
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._now = now_fn

    # ---- worker thread: record / consume ---------------------------------

    def add(self, record: ProbeRecord) -> None:
        """Records a completed probe. Raises `ValueError` on a duplicate
        `probe_id` (probe ids are fresh, unpredictable nonces - a
        collision is a caller bug, never a benign event). Bounded: expires
        old records and, if still over `PROBE_MAX_ENTRIES`, evicts the
        oldest-inserted.

        The TTL is enforced *here, internally*: the registry - not its
        caller - caps `expires_at` at `now + ttl_seconds`, so a
        caller-supplied expiry (even one far in the future) can never
        extend a probe's lifetime beyond the registry's own bound. A caller
        may still set a *shorter* `expires_at` (e.g. bounded by the Relay's
        own max-TTL), which is honored as-is."""
        now = self._now()
        capped = now + self._ttl_seconds
        if record.expires_at > capped:
            record = dataclasses.replace(record, expires_at=capped)
        with self._lock:
            self._evict_locked(now)
            if record.probe_id in self._records:
                raise ValueError(f"probe already recorded: {record.probe_id!r}")
            self._records[record.probe_id] = record
            while len(self._records) > self._max_entries:
                self._records.popitem(last=False)

    def consume(self, probe_id: str) -> Optional[ProbeRecord]:
        """Atomically checks-and-consumes `probe_id` (§7.11): if it is
        present and unexpired, remove it and return it (single-use); a
        second `consume()` - or one for an expired/unknown id - returns
        `None`. This is the worker's phase-2 gate before `register()`;
        `None` maps to `probe_id_used` / `probe_id_not_found`."""
        now = self._now()
        with self._lock:
            self._evict_locked(now)
            return self._records.pop(probe_id, None)

    # ---- request thread: non-consuming read ------------------------------

    def get(self, probe_id: str) -> Optional[ProbeRecord]:
        """The request thread's pure, non-consuming read for phase-2
        synchronous validation. Returns the record if present and
        unexpired, else `None` (`probe_id_not_found` / `probe_id_expired`).

        Does **not** consume a *valid* (unexpired) record - a successful
        `get()` leaves the probe in place for a later `consume()`, because
        the request thread must never mutate worker-owned state. The one
        mutation it does perform, stated honestly: under its own short lock
        it lazily evicts *already-expired* entries (`_evict_locked`), so an
        expired probe is dropped by housekeeping, not by `get()` itself -
        the caller observes `None` either way, but the store's size is kept
        honest by that housekeeping rather than by a background sweeper."""
        now = self._now()
        with self._lock:
            self._evict_locked(now)
            return self._records.get(probe_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    # ---- eviction ---------------------------------------------------------

    def _evict_locked(self, now: float) -> None:
        # TTL: drop expired records. Iterate over a key snapshot (we
        # mutate during the loop).
        for probe_id in list(self._records.keys()):
            record = self._records[probe_id]
            if now >= record.expires_at:
                del self._records[probe_id]
