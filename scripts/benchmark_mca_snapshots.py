"""scripts/benchmark_mca_snapshots.py

Step 1.6A.1 mandatory snapshot-cost benchmark (internal-rest-api.md §3.3
and §15.2). Measures the two per-tick costs the read API (Step 1.6A.2)
will add to the worker, on a synthetic database, so the §15.2 topology
constants can be chosen from *measured* numbers on the real deployment
hardware rather than guessed.

This is a measurement harness, not a test, and it is deliberately
**stdlib-only**: it imports `meshsrv.attachments.{snapshots,workspace,db.
migrations}` (and, for the command-drain microbenchmark,
`commands`/`command_registry`), none of which pull in `nacl`/`cbor2`/
`meshtastic`/Flask. It therefore runs in a bare Python venv on a target Pi
with no `pip install` step, which is exactly what the isolated-temporary-
clone workflow (per the Step 1.6A.1 verification brief) wants.

What it measures, for each N in `--counts` (default 0, 100, 1000, 5000):

1. **Snapshot build (full)** — `build_attachments_snapshot()`, the §3.3
   "read-only pass" over `attachments`/`attachment_recipients`/
   `attachment_deliveries`/`attachment_events` plus the idempotency index.
   This is the O(N) cost the *first* publish and the legacy un-migrated
   fallback pay; it is NOT the per-tick path, which is incremental (below).
   It is reported to show the cost the incremental design eliminates.
2. **Incremental publication** — `AttachmentsSnapshotPublisher.refresh()`
   with exactly one attachment dirtied (a relevant write) each call: the
   publisher drains that one dirty id, rebuilds that one projection + its
   bounded timeline, and swaps the snapshot atomically. This is the true
   per-tick cost under a relevant-write workload. The *rebuild* is O(1) in N
   (only the one dirty projection is re-fetched), but each publish still
   materializes a fresh immutable container — the `records` tuple plus the
   `by_id`/`idempotency` dicts — so the complete snapshot stays
   reference-atomic; that shallow reference copy is O(N). Measured
   (~22 ms at N=5000 vs ~3.6 s for the full build on a Pi Zero 2 W) the
   residual is a flat O(1) rebuild plus a linear reference copy, not a full
   O(N) rebuild.
3. **Worker-tick row scan** — the existing `_due_rows()` query (a
   `LIMIT`-bounded scan over the AUTOMATIC_STATES rows), reproduced
   verbatim here so the benchmark does not need to import `sender`/
   `receiver` (which would drag in `nacl`/`cbor2`). `LIMIT` bounds only the
   *rows returned*; the `ORDER BY created_at` sort is not covered by an
   index, so the query still walks/sorts every matching row — it scales
   with N (measured ~linear). Reported to show that this pre-existing scan
   (not the snapshot publisher) is the other N-dependent per-tick cost.

Plus one fixed (N-independent) microbenchmark: the **command drain**
(register `queued` → `running` → `succeeded` for `MAX_COMMANDS_PER_TICK`
commands through `CommandRegistry`), which sizes `MAX_COMMANDS_PER_TICK`.

Two *integrated* measurements close out the verification brief (they are
not arithmetically derived from the per-N numbers above — each is its own
directly-timed/directly-read sample):

4. **Integrated-tick saturation workload** (`--active-count` /
   `--duration` / `--relevant-every`): the *whole* worker tick — the
   `_due_rows()` scan, the unrelated ACK-quota + Relay-health writes, a
   relevant state/event write, and the snapshot publish — timed as one
   sample per tick in a tight loop (no `DEFAULT_TICK_SECONDS` sleep). This
   is a **saturation** figure (how fast the worker *could* churn); the
   operational duty cycle is `per-tick / 5 s` (the publish alone is
   `per-publish / 5 s`), not this loop's publication-fraction-of-wall-time,
   and the report prints that caveat.
5. **Combined worst-case memory**: a full `--active-count` snapshot *and* a
   worst-case `COMMAND_RESULT_MAX_ENTRIES`-entry / max-size-payload command
   registry retained simultaneously, then one incremental publish and one
   defensive full build on top. Reports baseline / retained / live-peak /
   process-peak RSS plus `/proc/meminfo` MemAvailable and SwapUsed and
   `/proc/vmstat` `oom_kill` before/after — all directly read, never summed
   from the separate measurements.

Memory is reported as peak RSS (`resource.getrusage`, Linux-only; `None` on
Windows) plus a `tracemalloc` peak for the Python-object half of the
snapshot build. The timing figures (median + p95 over `--iterations`
repetitions) are the primary deliverable; memory is the fit-in-RAM check
that matters most on a 415 MiB Pi Zero 2 W.

The synthetic rows are *representative*, not adversarial: a realistic mix
of sent/received directions and states, 1-2 recipients and 1-2 deliveries
per attachment, 3-6 bounded timeline events each (a few rows carry 300 to
exercise the `MAX_DETAIL_EVENTS` slice), a subset of sent rows carrying a
`client_request_id`/`canonical_hash` for the idempotency index, and
`received`/`AVAILABLE` rows carrying a `files/`-relative `saved_path` so
`ContentDescriptor` construction runs. All values are synthetic; no real
attachment, identity, key, token, or path is ever touched.

Usage (run from the repo root, or anywhere — the script bootstraps its own
import path):

    python3 scripts/benchmark_mca_snapshots.py
    python3 scripts/benchmark_mca_snapshots.py --counts 0,100,1000,5000 --iterations 30 --json /tmp/mca_bench.json
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sqlite3
import statistics
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

# Bootstrap: make `meshsrv` importable when this script is run as
# `python3 scripts/benchmark_mca_snapshots.py` from the repo root (Python
# puts `scripts/`, not the root, on sys.path for a directly-executed file).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from meshsrv.attachments.commands import (  # noqa: E402
    MAX_COMMANDS_PER_TICK,
    Command,
    CommandQueue,
    mint_command_id,
)
from meshsrv.attachments.command_registry import (  # noqa: E402
    COMMAND_RESULT_MAX_ENTRIES,
    COMMAND_RESULT_MAX_PAYLOAD_BYTES,
    COMMAND_RESULT_TTL_SECONDS,
    CommandRegistry,
)
from meshsrv.attachments.db.migrations import migrate  # noqa: E402
from meshsrv.attachments.snapshots import (  # noqa: E402
    AttachmentsSnapshotPublisher,
    build_attachments_snapshot,
)
from meshsrv.attachments.workspace import MCAWorkspaceManager  # noqa: E402

# ---- worker-tick constants, reproduced verbatim from the source modules
# so this benchmark stays stdlib-only (importing sender/receiver would pull
# in nacl/cbor2). The values below are read directly from the module
# constants where possible; the state *sets* are hardcoded because they live
# in sender.py/receiver.py, which are not stdlib-only.
SENDER_AUTOMATIC_STATES = ("DRAFT", "VALIDATING", "ENCRYPTING", "QUEUED_UPLOAD", "UPLOADING", "READY_TO_SEND")
RECEIVER_AUTOMATIC_STATES = ("WAITING_KEY", "WAITING_PROVIDER", "WAITING_NETWORK", "DOWNLOADING")
MAX_ATTACHMENTS_PER_TICK = 8  # meshsrv/attachments/service.py:76

PRINCIPAL_ID = "0123456789abcdef"
WORKSPACE_ID = "bench-workspace"

# Representative sent/received state mixes (a superset of the automatic
# states above, plus the terminal states a real 90-day window accumulates).
_SENT_STATES = ("DRAFT", "VALIDATING", "ENCRYPTING", "QUEUED_UPLOAD", "READY_TO_SEND", "SENT", "CANCELLED", "FAILED_VALIDATION")
_RECEIVED_STATES = ("WAITING_KEY", "WAITING_NETWORK", "DOWNLOADING", "AVAILABLE", "EXPIRED", "FAILED")

_WIRE_FORMATS = ("MCA1_TEXT", "MCA1_CBOR")


# ---------------------------------------------------------------------------
# environment capture
# ---------------------------------------------------------------------------

def _read_text(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_meminfo() -> Dict[str, int]:
    """Parse /proc/meminfo into {MemTotal_kb: ..., MemAvailable_kb: ...,
    SwapTotal_kb, SwapFree_kb}."""
    out: Dict[str, int] = {}
    text = _read_text("/proc/meminfo")
    if text is None:
        return out
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split()[0] if rest.strip() else "0"
        if key in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
            try:
                out[key + "_kb"] = int(value)
            except ValueError:
                pass
    return out


def _read_oom_kill_count() -> Optional[int]:
    """The running oom_kill counter from /proc/vmstat (None if unavailable),
    so a combined-memory run can report whether the kernel OOM-killed anything
    while the retained snapshot + registry + full build were alive."""
    text = _read_text("/proc/vmstat")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("oom_kill "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def capture_environment() -> Dict[str, object]:
    mem = _read_meminfo()
    env: Dict[str, object] = {
        "model": _read_text("/proc/device-tree/model"),
        "architecture": platform.machine(),
        "system": platform.system(),
        "platform": platform.platform(),
        "kernel_release": platform.release(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load_avg": _read_text("/proc/loadavg"),
    }
    if "MemTotal_kb" in mem:
        env["mem_total_mib"] = round(mem["MemTotal_kb"] / 1024, 1)
    if "MemAvailable_kb" in mem:
        env["mem_available_mib"] = round(mem["MemAvailable_kb"] / 1024, 1)
    os_release = _read_text("/etc/os-release")
    if os_release is not None:
        for line in os_release.splitlines():
            if line.startswith("PRETTY_NAME="):
                env["os_pretty_name"] = line.split("=", 1)[1].strip('"')
    return env


# ---------------------------------------------------------------------------
# synthetic database (representative, never adversarial)
# ---------------------------------------------------------------------------

def _synthetic_db(n: int, workspace_manager: MCAWorkspaceManager) -> sqlite3.Connection:
    """Build a fresh migrated schema and populate it with `n` representative
    attachments (plus recipients, deliveries, events, and an idempotency
    subset). In-memory so the measurement is CPU-bound, not disk-bound, and
    reproducible across runs/devices."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    paths = workspace_manager.paths(PRINCIPAL_ID)

    base_ts = 1_750_000_000.0
    window_s = 90 * 24 * 3600.0  # 90 days of history, spread over created_at
    attachments: List[tuple] = []
    recipients: List[tuple] = []
    deliveries: List[tuple] = []
    events: List[tuple] = []

    # Deterministic per-run shape, no RNG (so median/p95 across iterations
    # are comparable, and a 5000-row run is a strict superset of the 100-row
    # one modulo scale, not a different random draw).
    for i in range(n):
        attachment_id = f"att-{i:08d}"
        direction = "sent" if i % 5 < 3 else "received"  # ~60/40 mix
        created_at = base_ts - (i * window_s / max(n, 1))
        hard_expires_at = created_at + 259_200.0  # 3-day hard TTL
        if direction == "sent":
            state = _SENT_STATES[i % len(_SENT_STATES)]
            file_name = f"capture_{i}.jpg" if i % 3 else f"note_{i}.txt"
            mime_type = "image/jpeg" if i % 3 else "text/plain"
            saved_path = None  # sent rows are not saved until content is kept
        else:
            state = _RECEIVED_STATES[i % len(_RECEIVED_STATES)]
            file_name = f"incoming_{i}.jpg"
            mime_type = "image/jpeg"
            # received/AVAILABLE rows carry a servable files/ locator; the
            # rest carry nothing (exercises both descriptor branches). The
            # path is derived from the workspace manager's real `files/`
            # directory so it is absolute on every platform (exercising the
            # ContentDescriptor branch on Linux *and* Windows).
            saved_path = str(paths.files / file_name) if state == "AVAILABLE" else None

        # ~10% of sent rows carry an idempotency pair (exercises the index).
        client_request_id = None
        canonical_hash = None
        if direction == "sent" and i % 10 == 0:
            client_request_id = f"req-{i:06d}-abcdef"
            canonical_hash = "ab" * 32

        primary_delivery_id = f"del-{i:08d}" if direction == "sent" else None

        attachments.append((
            attachment_id, WORKSPACE_ID, f"transfer-{i:08d}", direction,
            PRINCIPAL_ID, None,  # sender_principal_id
            f"prov-{i % 7:02d}", state, None, file_name, mime_type,
            (i % 4096) + 1024, (i % 4096) + 4096,  # plain_size, cipher_size
            None,  # plain_sha256
            created_at, hard_expires_at, 3600,  # download_grace_seconds
            saved_path, None, None,  # error_code, retry_at
            primary_delivery_id, None, None,  # pending_offer_cbor, draft_comment
            None, None, None, None,  # reply_route_type, reply_route_id, reply_adapter_id, reply_connector_profile_id
            None,  # reply_destination_address
            client_request_id, canonical_hash,
        ))

        # recipients: 1-2 per attachment
        for r in range(1 + (i % 2)):
            recipients.append((
                f"rec-{i:08d}-{r}", attachment_id,
                f"key-{((i + r) % 100):02d}", f"princ-{(i + r) % 100:02d}",
            ))

        # deliveries: 1 per sent attachment (received rows have none)
        if direction == "sent":
            deliveries.append((
                primary_delivery_id, attachment_id, "meshtastic", "meshtastic",
                "DIRECT", f"!node{i % 50:02d}", _WIRE_FORMATS[i % 2],
                f"ext-{i:08d}" if state in ("SENT",) else None,
                f"idem-{i:08d}", "SENT" if state == "SENT" else "PENDING",
                created_at + 60.0, None, None, None,
            ))

        # events: 3-6 per attachment; a few rows carry 300 to exercise the
        # MAX_DETAIL_EVENTS slice (bounded timeline).
        n_events = 300 if i % 1000 == 0 else 3 + (i % 4)
        for e in range(n_events):
            events.append((
                f"evt-{i:08d}-{e}", attachment_id, created_at + e,
                "state_changed" if e % 2 == 0 else "progress",
                json.dumps({"to": state, "error_code": None}),
            ))

    conn.executemany(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, "
        "principal_id, sender_principal_id, provider_id, state, recipient_key_epoch, "
        "file_name, mime_type, plain_size, cipher_size, plain_sha256, created_at, "
        "hard_expires_at, download_grace_seconds, saved_path, error_code, retry_at, "
        "primary_delivery_id, pending_offer_cbor, draft_comment, reply_route_type, "
        "reply_route_id, reply_adapter_id, reply_connector_profile_id, "
        "reply_destination_address, client_request_id, canonical_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        attachments,
    )
    conn.executemany(
        "INSERT INTO attachment_recipients (id, attachment_id, envelope_id, recipient_principal_id) "
        "VALUES (?,?,?,?)",
        recipients,
    )
    conn.executemany(
        "INSERT INTO attachment_deliveries (id, attachment_id, adapter_id, connector_profile_id, "
        "route_type, route_id, wire_format, external_message_id, idempotency_key, state, sent_at, "
        "ack_at, error_code, retry_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        deliveries,
    )
    conn.executemany(
        "INSERT INTO attachment_events (id, attachment_id, occurred_at, event_type, detail_json) "
        "VALUES (?,?,?,?,?)",
        events,
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# timing helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_values: Sequence[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _median_p95(samples: List[float]) -> Dict[str, float]:
    ordered = sorted(samples)
    return {
        "median_ms": statistics.median(ordered) * 1000.0,
        "p95_ms": _percentile(ordered, 0.95) * 1000.0,
        "min_ms": ordered[0] * 1000.0,
        "max_ms": ordered[-1] * 1000.0,
        "iterations": len(samples),
    }


def _time_fn(fn: Callable[[], None], iterations: int) -> List[float]:
    samples: List[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def _peak_rss_kb() -> Optional[int]:
    """Peak RSS in KiB (POSIX only); None on Windows, where `resource` is
    absent and the number would not be meaningful anyway."""
    if os.name == "nt":
        return None
    import resource  # POSIX-only, imported lazily so Windows still runs
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _current_rss_kb() -> Optional[int]:
    """Current RSS (VmRSS) in KiB from /proc/self/status - the *incremental*
    memory signal, not the process peak. None on Windows/without /proc."""
    if os.name == "nt":
        return None
    text = _read_text("/proc/self/status")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# per-N benchmark
# ---------------------------------------------------------------------------

def _bench_n(conn: sqlite3.Connection, workspace_manager: MCAWorkspaceManager, iterations: int) -> Dict[str, object]:
    paths = workspace_manager.paths(PRINCIPAL_ID)

    def build():
        build_attachments_snapshot(
            conn,
            workspace_id=WORKSPACE_ID,
            workspace_manager=workspace_manager,
            principal_id=PRINCIPAL_ID,
            now=time.time(),
        )

    # Warm up once (page cache + first-object allocation), then measure the
    # steady-state per-tick cost — the honest number the cadence constant
    # is sized from.
    build()

    # --- snapshot publication (raw build) --------------------------------
    # Timing is measured WITHOUT tracemalloc (which would inflate it ~2x);
    # the Python-object peak is a single, separate traced build below.
    build_samples = _time_fn(build, iterations)
    build_result = _median_p95(build_samples)
    tracemalloc.start()
    build()
    _, build_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    build_result["peak_python_kib"] = round(build_peak_bytes / 1024, 1)

    # --- incremental publication (dirty one attachment, rebuild it) ------
    # The per-tick path under a relevant-write workload: one attachment is
    # dirtied (its UPDATE fires the migration-12 trigger), refresh() drains
    # that one dirty id and rebuilds only that projection (O(1) in N). The
    # atomic publish still materializes a fresh O(N) container (records tuple
    # + by_id/idempotency dicts), so the measured cost is a flat O(1) rebuild
    # plus a linear reference copy - not the O(N) full build above.
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM attachments WHERE workspace_id = ? ORDER BY id", (WORKSPACE_ID,)
        ).fetchall()
    ]
    publisher = AttachmentsSnapshotPublisher(now_fn=time.time)
    publisher.refresh(conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)  # first publish (full build)
    counter = {"i": 0}

    def incremental_refresh():
        i = counter["i"]
        counter["i"] += 1
        if ids:
            aid = ids[i % len(ids)]
            conn.execute("UPDATE attachments SET state = state WHERE id = ?", (aid,))
            conn.commit()
        publisher.refresh(conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    incremental_refresh()  # warm up
    refresh_samples = _time_fn(incremental_refresh, iterations)
    refresh_result = _median_p95(refresh_samples)

    # --- worker-tick row scan (the existing _due_rows() query) -----------
    sent_ph = ",".join("?" for _ in SENDER_AUTOMATIC_STATES)
    recv_ph = ",".join("?" for _ in RECEIVER_AUTOMATIC_STATES)
    scan_sql = (
        "SELECT id, direction, provider_id FROM attachments "
        f"WHERE workspace_id = ? AND ((direction = 'sent' AND state IN ({sent_ph})) "
        f"OR (direction = 'received' AND state IN ({recv_ph}))) "
        "ORDER BY created_at LIMIT ?"
    )
    scan_params = (
        WORKSPACE_ID,
        *SENDER_AUTOMATIC_STATES,
        *RECEIVER_AUTOMATIC_STATES,
        MAX_ATTACHMENTS_PER_TICK,
    )

    def scan():
        conn.row_factory = sqlite3.Row
        conn.execute(scan_sql, scan_params).fetchall()

    scan_samples = _time_fn(scan, iterations)
    scan_result = _median_p95(scan_samples)

    peak_rss = _peak_rss_kb()
    return {
        "snapshot_build": build_result,
        "snapshot_incremental": refresh_result,
        "worker_tick_scan": scan_result,
        "peak_rss_kib": peak_rss,
    }


def _bench_command_drain(iterations: int) -> Dict[str, float]:
    """The fixed, N-independent cost of draining MAX_COMMANDS_PER_TICK
    commands through the queue + registry lifecycle (queued → running →
    succeeded). Sizes MAX_COMMANDS_PER_TICK."""
    queue = CommandQueue(maxsize=MAX_COMMANDS_PER_TICK * 2)
    registry = CommandRegistry()
    kinds = ("attachment_create", "provider_check")

    def drain():
        commands = [
            Command(command_id=mint_command_id(), kind=kinds[i % len(kinds)], payload={}, created_at=time.time())
            for i in range(MAX_COMMANDS_PER_TICK)
        ]
        for c in commands:
            registry.register(c)
            queue.put_nowait(c)
        for _ in range(MAX_COMMANDS_PER_TICK):
            c = queue.get_nowait()
            registry.mark_running(c.command_id)
            registry.mark_succeeded(c.command_id, resource_id=f"res-{c.command_id}")

    drain()  # warm up
    samples = _time_fn(drain, iterations)
    return _median_p95(samples)


# ---------------------------------------------------------------------------
# active-write workload (§15.2 correction, requirement #4)
# ---------------------------------------------------------------------------

def _bench_active_writes(
    conn: sqlite3.Connection,
    workspace_manager: MCAWorkspaceManager,
    *,
    duration_seconds: float,
    relevant_every: int,
) -> Dict[str, object]:
    """A saturation active-write workload, not idle steady-state.

    Runs the *whole* worker tick in a tight loop (no `DEFAULT_TICK_SECONDS`
    sleep between ticks) for `duration_seconds`, so the per-tick timings here
    are a **saturation** figure — how fast the worker *could* churn if a
    relevant write arrived every single tick — not the operational duty
    cycle. A real Meshtastic network cannot produce thousands of relevant
    writes per minute; operationally the worker runs one tick per
    `DEFAULT_TICK_SECONDS` (5 s), so the operational duty cycle is
    `per-tick / 5 s` (of which the publish alone is `per-publish / 5 s`,
    ~0.3%). This saturation loop, by contrast, never sleeps, so its
    publication time is a large fraction of wall time; the two numbers
    answer different questions — saturation is the *ceiling*, the /5 s
    ratio is the *real cadence*.

    Each timed tick runs, in order, the integrated worker path in one sample:
    (0) the `_due_rows()` row scan (reproduced verbatim, including its
    unindexed `ORDER BY created_at` sort — see the `worker_tick_scan`
    measurement), (1) the two *unrelated* writes every tick (ACK quota +
    Relay health — no snapshot trigger, so they must NOT force a rebuild),
    (2) on every `relevant_every`-th tick a *relevant* write (advance an
    attachment state + append an event) that DOES dirty that attachment and
    force an incremental rebuild of just it, and (3) the snapshot publish
    (drain dirty ids → rebuild the one dirty projection → materialize and
    swap the immutable snapshot). Reports ticks, publications (ticks that
    actually rebuilt at least one projection), total publication time, and
    per-tick/per-publish duration max/median/p95.
    """
    # Seed one Relay-health row once (mca_provider_profiles has no snapshot
    # trigger - it is a provider/connectivity snapshot, not an attachment
    # snapshot - so its health updates are exactly the "unrelated write" this
    # workload is meant to include).
    conn.execute(
        "INSERT INTO mca_provider_profiles "
        "(provider_id, workspace_id, origin, service_public_key_b64url, "
        "max_ciphertext_bytes, hard_expiry_default_seconds, added_at) "
        "VALUES ('prov-bench', ?, 'https://relay.example', 'AAAA', 4096, 3600, 0)",
        (WORKSPACE_ID,),
    )
    # Seed the ACK quota row once (unique on workspace_id+scope); the loop
    # below increments it, which is the realistic per-tick ACK-quota write.
    conn.execute(
        "INSERT INTO mca_ack_quota (workspace_id, scope, window_start_at, count) "
        "VALUES (?, '__global__', 0, 0)",
        (WORKSPACE_ID,),
    )
    conn.commit()

    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM attachments WHERE workspace_id = ? ORDER BY id", (WORKSPACE_ID,)
        ).fetchall()
    ]

    # The `_due_rows()` scan, reproduced verbatim (the same `ORDER BY
    # created_at` + `LIMIT` as service.py), so the integrated tick includes
    # its real unindexed-sort cost in the same timing sample as the writes
    # and the publish.
    sent_ph = ",".join("?" for _ in SENDER_AUTOMATIC_STATES)
    recv_ph = ",".join("?" for _ in RECEIVER_AUTOMATIC_STATES)
    scan_sql = (
        "SELECT id, direction, provider_id FROM attachments "
        f"WHERE workspace_id = ? AND ((direction = 'sent' AND state IN ({sent_ph})) "
        f"OR (direction = 'received' AND state IN ({recv_ph}))) "
        "ORDER BY created_at LIMIT ?"
    )
    scan_params = (
        WORKSPACE_ID,
        *SENDER_AUTOMATIC_STATES,
        *RECEIVER_AUTOMATIC_STATES,
        MAX_ATTACHMENTS_PER_TICK,
    )
    conn.row_factory = sqlite3.Row

    publisher = AttachmentsSnapshotPublisher(now_fn=time.time)
    # First publish (so `snapshot()` is non-None for the publication detection).
    publisher.refresh(conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    tick = 0
    publications = 0
    publish_times_s: List[float] = []
    tick_times_s: List[float] = []
    start = time.perf_counter()
    deadline = start + duration_seconds

    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        tick += 1

        # (0) the `_due_rows()` row scan - the integrated tick's first cost.
        conn.execute(scan_sql, scan_params).fetchall()

        # (1) unrelated writes, every tick: ACK quota + Relay health.
        conn.execute(
            "UPDATE mca_ack_quota SET count = count + 1, window_start_at = ? "
            "WHERE workspace_id = ? AND scope = '__global__'",
            (tick, WORKSPACE_ID),
        )
        conn.execute(
            "UPDATE mca_provider_profiles SET last_checked_at = ?, last_latency_ms = ?, last_check_result = 'ok' "
            "WHERE provider_id = 'prov-bench'",
            (tick, (tick % 50) + 1),
        )

        # (2) a relevant write on every `relevant_every`-th tick.
        if ids and tick % relevant_every == 0:
            aid = ids[tick % len(ids)]
            conn.execute("UPDATE attachments SET state = 'SENT' WHERE id = ?", (aid,))
            conn.execute(
                "INSERT INTO attachment_events (id, attachment_id, occurred_at, event_type, detail_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"evt-live-{tick:08d}", aid, tick, "state_changed", '{"to": "SENT"}'),
            )
        conn.commit()

        # (3) publish - rebuilds only the dirty projections (a relevant
        # write); unrelated writes leave no dirty ids and short-circuit.
        prev = publisher.snapshot()
        t_refresh = time.perf_counter()
        publisher.refresh(conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
        refresh_s = time.perf_counter() - t_refresh
        if publisher.snapshot() is not prev:
            publications += 1
            publish_times_s.append(refresh_s)

        tick_times_s.append(time.perf_counter() - t0)

    total_publish_s = sum(publish_times_s)
    return {
        "duration_seconds": round(time.perf_counter() - start, 2),
        "relevant_every": relevant_every,
        "ticks": tick,
        "publications": publications,
        "publication_fraction": round(publications / tick, 4) if tick else 0.0,
        "total_publish_time_ms": round(total_publish_s * 1000.0, 2),
        "tick": _median_p95(tick_times_s),
        "publish": _median_p95(publish_times_s),
    }


# ---------------------------------------------------------------------------
# memory (baseline + incremental RSS; §15.2 correction, requirement #5)
# ---------------------------------------------------------------------------

def _bench_memory(counts: Sequence[int], workspace_manager: MCAWorkspaceManager) -> Dict[str, object]:
    """Baseline RSS *before* snapshot construction, then the *incremental*
    RSS delta after building + holding a snapshot at each N. The delta - not
    the process peak - is the snapshot's memory cost; the baseline already
    includes the in-memory SQLite pages for those N rows."""
    incremental: Dict[str, object] = {}
    for n in counts:
        conn = _synthetic_db(n, workspace_manager)
        gc.collect()
        baseline = _current_rss_kb()
        snapshot = build_attachments_snapshot(
            conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager,
            principal_id=PRINCIPAL_ID, now=time.time(),
        )
        after = _current_rss_kb()
        incremental[str(n)] = {
            "baseline_rss_kib": baseline,
            "after_rss_kib": after,
            "snapshot_delta_kib": (after - baseline) if (baseline is not None and after is not None) else None,
        }
        del snapshot
        conn.close()
    return incremental


# ---------------------------------------------------------------------------
# command-result registry worst-case memory (§15.2 correction, requirement #6)
# ---------------------------------------------------------------------------

def _bench_command_registry_memory() -> Dict[str, object]:
    """Worst-case retained memory of a full command-result registry:
    `COMMAND_RESULT_MAX_ENTRIES` terminal entries, each carrying a result
    payload at (just under) the `COMMAND_RESULT_MAX_PAYLOAD_BYTES` bound.
    Measures the Python-object footprint (tracemalloc *current*, not peak)
    and the RSS delta on Linux - the number that proves
    COMMAND_RESULT_MAX_PAYLOAD_BYTES actually bounds the
    COMMAND_RESULT_MAX_ENTRIES worst case."""
    registry = CommandRegistry()
    gc.collect()
    baseline_rss = _current_rss_kb()

    tracemalloc.start()
    for i in range(COMMAND_RESULT_MAX_ENTRIES):
        cid = f"cmd-{i:04d}"
        # A *fresh* payload string per entry (not a shared reference), so the
        # measurement is the honest worst case of N distinct max-size results.
        body = "x" * (COMMAND_RESULT_MAX_PAYLOAD_BYTES - 128)
        registry.register(Command(command_id=cid, kind="attachment_create", payload={}, created_at=float(i)))
        registry.mark_running(cid)
        registry.mark_succeeded(cid, resource_id=f"res-{i:04d}", result={"data": body, "idx": i})
    _, current_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    after_rss = _current_rss_kb()
    return {
        "entries": COMMAND_RESULT_MAX_ENTRIES,
        "max_payload_bytes": COMMAND_RESULT_MAX_PAYLOAD_BYTES,
        "payload_json_bytes_each": len(
            json.dumps({"data": "x" * (COMMAND_RESULT_MAX_PAYLOAD_BYTES - 128), "idx": 0}, separators=(",", ":"))
        ),
        "traced_current_kib": round(current_bytes / 1024, 1),
        "rss_delta_kib": (after_rss - baseline_rss) if (baseline_rss is not None and after_rss is not None) else None,
    }


# ---------------------------------------------------------------------------
# combined worst-case memory (Step 1.6A.1 verification brief, requirement #4)
# ---------------------------------------------------------------------------

def _bench_combined_memory(active_count: int, workspace_manager: MCAWorkspaceManager) -> Dict[str, object]:
    """The single combined memory test the verification brief asks for: hold a
    full `active_count`-attachment snapshot *and* a worst-case
    (`COMMAND_RESULT_MAX_ENTRIES`-entry, max-size-payload) command-result
    registry in memory at the same time, then
    run one incremental publication and one defensive full build on top, and
    report whether the whole thing fits in RAM on the target device.

    All figures are *directly measured* system metrics, not arithmetic sums of
    the separate per-N / registry measurements: baseline VmRSS before anything
    is built, retained VmRSS with the snapshot + registry + in-memory SQLite
    pages all alive, live-peak VmRSS while a transient *second* full snapshot
    (the defensive build) is simultaneously materialized on top of the
    retained one, process-wide ru_maxrss, and /proc/meminfo MemAvailable +
    SwapUsed plus /proc/vmstat `oom_kill` before/after — so a run that
    exhausted RAM or touched swap is visible as such, not inferred."""
    gc.collect()
    baseline_rss = _current_rss_kb()
    baseline_mem = _read_meminfo()
    baseline_oom = _read_oom_kill_count()

    def _mem_available_kib(mem: Dict[str, int]) -> Optional[int]:
        return mem.get("MemAvailable_kb")

    def _swap_used_kib(mem: Dict[str, int]) -> Optional[int]:
        total = mem.get("SwapTotal_kb")
        free = mem.get("SwapFree_kb")
        if total is None or free is None:
            return None
        return total - free

    # Retain a full snapshot (the publisher's first publish is a full build,
    # and it is kept alive on the publisher for the rest of this test).
    conn = _synthetic_db(active_count, workspace_manager)
    publisher = AttachmentsSnapshotPublisher(now_fn=time.time)
    publisher.refresh(conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    # Retain a worst-case command-result registry: COMMAND_RESULT_MAX_ENTRIES
    # terminal entries, each a *distinct* near-max-size payload string.
    registry = CommandRegistry()
    for i in range(COMMAND_RESULT_MAX_ENTRIES):
        cid = f"cmd-{i:04d}"
        body = "x" * (COMMAND_RESULT_MAX_PAYLOAD_BYTES - 128)
        registry.register(Command(command_id=cid, kind="attachment_create", payload={}, created_at=float(i)))
        registry.mark_running(cid)
        registry.mark_succeeded(cid, resource_id=f"res-{i:04d}", result={"data": body, "idx": i})

    # Retained VmRSS: snapshot + registry + in-memory SQLite pages all alive.
    retained_rss = _current_rss_kb()
    retained_mem = _read_meminfo()

    # One incremental publication (dirty exactly one attachment → rebuild only
    # that projection → materialize + swap the immutable snapshot).
    aid_row = conn.execute(
        "SELECT id FROM attachments WHERE workspace_id = ? ORDER BY id LIMIT 1", (WORKSPACE_ID,)
    ).fetchone()
    if aid_row is not None:
        conn.execute("UPDATE attachments SET state = state WHERE id = ?", (aid_row[0],))
        conn.commit()
    publisher.refresh(conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    # One defensive full build, held long enough to measure the live peak with
    # the retained snapshot + registry + a *second* full snapshot coexisting.
    full = build_attachments_snapshot(
        conn, workspace_id=WORKSPACE_ID, workspace_manager=workspace_manager,
        principal_id=PRINCIPAL_ID, now=time.time(),
    )
    live_peak_rss = _current_rss_kb()
    process_peak_rss = _peak_rss_kb()
    del full

    after_mem = _read_meminfo()
    after_oom = _read_oom_kill_count()

    return {
        "attachments": active_count,
        "registry_entries": COMMAND_RESULT_MAX_ENTRIES,
        "baseline_rss_kib": baseline_rss,
        "retained_rss_kib": retained_rss,
        "live_peak_rss_kib": live_peak_rss,
        "process_peak_rss_kib": process_peak_rss,
        "mem_available_before_kib": _mem_available_kib(baseline_mem),
        "mem_available_retained_kib": _mem_available_kib(retained_mem),
        "mem_available_after_kib": _mem_available_kib(after_mem),
        "swap_used_before_kib": _swap_used_kib(baseline_mem),
        "swap_used_after_kib": _swap_used_kib(after_mem),
        "oom_kill_before": baseline_oom,
        "oom_kill_after": after_oom,
    }


# ---------------------------------------------------------------------------
# report + main
# ---------------------------------------------------------------------------

def run_benchmark(
    counts: Sequence[int],
    iterations: int,
    *,
    active_count: int,
    duration_seconds: float,
    relevant_every: int,
) -> Dict[str, object]:
    env = capture_environment()
    workspace_manager = MCAWorkspaceManager("/tmp/bench-data")
    result: Dict[str, object] = {
        "environment": env,
        "constants": {
            "MAX_COMMANDS_PER_TICK": MAX_COMMANDS_PER_TICK,
            "COMMAND_RESULT_MAX_ENTRIES": COMMAND_RESULT_MAX_ENTRIES,
            "COMMAND_RESULT_MAX_PAYLOAD_BYTES": COMMAND_RESULT_MAX_PAYLOAD_BYTES,
            "COMMAND_RESULT_TTL_SECONDS": COMMAND_RESULT_TTL_SECONDS,
        },
        "command_drain": _bench_command_drain(iterations),
        "attachments": {},
    }
    for n in counts:
        conn = _synthetic_db(n, workspace_manager)
        result["attachments"][str(n)] = _bench_n(conn, workspace_manager, iterations)
        conn.close()

    # Active-write workload on its own synthetic DB of `active_count`.
    active_conn = _synthetic_db(active_count, workspace_manager)
    result["active_writes"] = _bench_active_writes(
        active_conn, workspace_manager,
        duration_seconds=duration_seconds, relevant_every=relevant_every,
    )
    active_conn.close()

    result["memory"] = _bench_memory(counts, workspace_manager)
    result["command_registry_memory"] = _bench_command_registry_memory()
    # Combined worst-case (snapshot + registry retained together, plus one
    # incremental publish and one defensive full build) - run last so the
    # process-wide ru_maxrss peak already reflects the heavier earlier phases
    # and the combined live-peak VmRSS is the *marginal* signal here.
    result["combined_memory"] = _bench_combined_memory(active_count, workspace_manager)
    return result


def _fmt_ms(v: Dict[str, float]) -> str:
    return f"median {v['median_ms']:7.2f} ms   p95 {v['p95_ms']:7.2f} ms"


def _fmt_kib(v: Optional[int]) -> str:
    if v is None:
        return "n/a (non-Linux)"
    return f"{v / 1024:.1f} MiB"


def _print_report(result: Dict[str, object]) -> None:
    env = result["environment"]
    print("=" * 72)
    print("MCAttach Step 1.6A.1 snapshot-cost benchmark")
    print("=" * 72)
    print(f"model        : {env.get('model')}")
    print(f"arch/system  : {env.get('architecture')} / {env.get('system')}")
    print(f"OS           : {env.get('os_pretty_name')} ({env.get('kernel_release')})")
    print(f"Python       : {env.get('python_version')}  cpus={env.get('cpu_count')}")
    print(f"RAM          : total {env.get('mem_total_mib')} MiB, available {env.get('mem_available_mib')} MiB")
    print(f"load avg     : {env.get('load_avg')}")
    print()

    print("Per-attachment-count timings (steady-state, post warm-up):")
    header = f"{'N':>6} | {'snapshot build (full)':^40} | {'incremental publish':^40} | {'tick row-scan':^40}"
    print(header)
    print("-" * len(header))
    att = result["attachments"]
    for n in sorted(att, key=int):
        row = att[n]
        print(
            f"{n:>6} | {_fmt_ms(row['snapshot_build']):^40} | "
            f"{_fmt_ms(row['snapshot_incremental']):^40} | {_fmt_ms(row['worker_tick_scan']):^40}"
        )
        rss = row.get("peak_rss_kib")
        py = row["snapshot_build"].get("peak_python_kib")
        rss_s = f"peak RSS {rss / 1024:.1f} MiB" if rss else "peak RSS n/a (non-POSIX)"
        print(f"      |   python-obj peak {py} KiB   |   {rss_s}")

    print()
    print(f"command drain (MAX_COMMANDS_PER_TICK={result['constants']['MAX_COMMANDS_PER_TICK']}): "
          f"{_fmt_ms(result['command_drain'])}")

    # --- active-write workload (integrated tick) ------------------------
    aw = result.get("active_writes")
    if aw:
        print()
        print("Integrated-tick saturation workload (the full worker path in one")
        print(f"timed sample: _due_rows() scan + unrelated writes + a relevant write")
        print(f"every {aw['relevant_every']} tick(s) + snapshot publish, over {aw['duration_seconds']} s):")
        print(f"  worker ticks       : {aw['ticks']}")
        print(f"  publications       : {aw['publications']}  (publication fraction {aw['publication_fraction']:.2f})")
        print(f"  total publish time : {aw['total_publish_time_ms']:.2f} ms")
        print(f"  per-tick duration  : {_fmt_ms(aw['tick'])}")
        print(f"  per-publish dur.   : {_fmt_ms(aw['publish'])}")
        print("  NOTE: this is a SATURATION figure (tight loop, no 5 s tick sleep).")
        print("  A real radio network cannot produce a relevant write every tick; the")
        print("  operational worker runs one tick per DEFAULT_TICK_SECONDS (5 s), so the")
        print("  real duty cycle is per-tick / 5 s (the publish alone is per-publish / 5 s,")
        print("  ~0.3%). The saturation loop never sleeps, so its publication time is a")
        print("  large fraction of wall time only because it churns back-to-back.")

    # --- memory: baseline + incremental RSS -----------------------------
    mem = result.get("memory")
    if mem:
        print()
        print("Snapshot memory (baseline RSS before build, then incremental delta):")
        for n in sorted(mem, key=int):
            row = mem[n]
            base = row["baseline_rss_kib"]
            delta = row["snapshot_delta_kib"]
            base_s = f"{base / 1024:.1f} MiB" if base is not None else "n/a"
            delta_s = f"{delta / 1024:.1f} MiB" if delta is not None else "n/a (non-Linux)"
            print(f"  {n:>6} attachments : baseline {base_s:>10}   snapshot delta {delta_s:>10}")

    # --- command-result registry worst-case memory ----------------------
    crm = result.get("command_registry_memory")
    if crm:
        print()
        print("Command-result registry worst case "
              f"({crm['entries']} entries x ~{crm['payload_json_bytes_each']} B payload):")
        traced = crm["traced_current_kib"]
        rss_delta = crm["rss_delta_kib"]
        traced_s = f"{traced / 1024:.1f} MiB" if traced is not None else "n/a"
        rss_s = f"{rss_delta / 1024:.1f} MiB" if rss_delta is not None else "n/a (non-Linux)"
        print(f"  python-object (tracemalloc current): {traced_s}")
        print(f"  RSS delta                          : {rss_s}")

    # --- combined worst-case memory -------------------------------------
    cm = result.get("combined_memory")
    if cm:
        print()
        print("Combined worst-case memory (snapshot + registry retained together, then")
        print(f"one incremental publish + one defensive full build; {cm['attachments']} attachments")
        print(f"and {cm['registry_entries']} max-size-payload registry entries):")
        base = cm["baseline_rss_kib"]
        ret = cm["retained_rss_kib"]
        live = cm["live_peak_rss_kib"]
        proc = cm["process_peak_rss_kib"]
        print(f"  baseline RSS            : {_fmt_kib(base)}")
        print(f"  retained RSS            : {_fmt_kib(ret)}")
        print(f"  live-peak RSS (full)    : {_fmt_kib(live)}")
        print(f"  process-peak RSS        : {_fmt_kib(proc)}")
        ma_b, ma_a = cm["mem_available_before_kib"], cm["mem_available_after_kib"]
        ma_r = cm["mem_available_retained_kib"]
        print(f"  MemAvailable before     : {_fmt_kib(ma_b)}")
        print(f"  MemAvailable retained   : {_fmt_kib(ma_r)}")
        print(f"  MemAvailable after      : {_fmt_kib(ma_a)}")
        sw_b, sw_a = cm["swap_used_before_kib"], cm["swap_used_after_kib"]
        print(f"  SwapUsed before -> after: {_fmt_kib(sw_b)} -> {_fmt_kib(sw_a)}")
        oom_b, oom_a = cm["oom_kill_before"], cm["oom_kill_after"]
        oom_s = "n/a" if (oom_b is None or oom_a is None) else f"{oom_b} -> {oom_a}"
        print(f"  oom_kill before -> after: {oom_s}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--counts", default="0,100,1000,5000",
                        help="comma-separated synthetic attachment counts (default: 0,100,1000,5000)")
    parser.add_argument("--iterations", type=int, default=30,
                        help="timing repetitions per measurement (default: 30)")
    parser.add_argument("--active-count", type=int, default=5000,
                        help="synthetic attachment count for the active-write workload (default: 5000)")
    parser.add_argument("--duration", type=float, default=60.0,
                        help="active-write workload wall-clock duration in seconds (default: 60)")
    parser.add_argument("--relevant-every", type=int, default=1,
                        help="perform a relevant (state/event) write every N ticks; unrelated writes happen every tick (default: 1)")
    parser.add_argument("--json", metavar="PATH",
                        help="also write the full result object to this file as JSON")
    args = parser.parse_args()

    counts = [int(x) for x in args.counts.split(",") if x.strip() != ""]
    result = run_benchmark(
        counts, args.iterations,
        active_count=args.active_count,
        duration_seconds=args.duration,
        relevant_every=args.relevant_every,
    )
    _print_report(result)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
        print(f"\n[JSON written to {args.json}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
