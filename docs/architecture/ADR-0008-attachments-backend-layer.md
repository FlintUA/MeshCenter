# ADR-0008: Attachments backend layer for Step 1.6A (worker, connectivity, provider registry v2, contacts)

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2` (sections 15.1, 15.2, 16.3-16.5, 17.4-17.6, 18); ADR-0006 (sender state machine), ADR-0007 (receiver state machine)

## Context

Execution Plan Step 1.6 (Files tab UI) was split into 1.6A (backend vertical slice) and 1.6B (UI), per the decision already made and agreed with the user, because a UI with nothing driving `sender.py`/`receiver.py`'s state machines behind it would be dead code. Before 1.6A's REST endpoints can be written, four backend gaps have to be resolved — none of which Steps 1.4/1.5 needed, since their own tests drive `run_step()` directly rather than through a running server:

1. **Nothing calls `run_step()`/`reconcile_pending()`/`resume_pending()` outside of tests.** Both state machines are pure functions of (DB row, caller-supplied inputs) — by design (ADR-0006/ADR-0007), neither owns a scheduler. Something in `server.py`'s process has to actually drive them forward over time and in response to events, or every attachment sits frozen in whatever state it reached on the last direct call.
2. **`network_available` has no real source.** Both state machines take it as a plain `bool` parameter. The only existing connectivity signal in the codebase, `api_system.py`'s `/api/system/network`, is a single `ping -c 1 -W 1 8.8.8.8` — it says nothing about whether a specific Relay origin is actually reachable over HTTPS, which is the only connectivity question either state machine actually needs answered.
3. **`ProviderRegistry.register(..., is_default=True)` does not enforce "at most one default per workspace"** — confirmed directly against the current code: the `ON CONFLICT` clause updates only the upserted row's own `is_default`, and `get_default()`'s `SELECT ... LIMIT 1` has no `ORDER BY`, so two default rows would make the choice silently non-deterministic. The profile model is also too thin for 17.6's Settings requirements (no `enabled`/health/kind/limits/last-checked fields) and stores nothing for a Relay's upload credential at all yet — `create_upload()`'s `upload_access_token` has never had a persisted home because Steps 1.1/1.4/1.5 always passed it in directly from a test fixture.
4. **No backend surface exists for `mca_recipient_bindings`' TOFU/key-change state**, even though the schema (Step 1.2) already tracks it (`tofu_confirmed_at`, `pending_public_identity`, `pending_key_epoch`, `pending_detected_at`). Without at least the internal functions wrapping these transitions, the send form's "Кому" field (17.4) has nothing to call to tell a `trusted` contact from a `confirmation_required` or `key_changed` one.

## Decision

### 1. `meshsrv/attachments/service.py` — one `AttachmentsService` per workspace, no job queue

A single class, constructed once at server startup and held for the process's lifetime — mirrors `KeyExchangeCoordinator`'s "one instance per (workspace, adapter)" shape, not a new architectural pattern:

```python
class AttachmentsService:
    def __init__(self, conn, workspace_manager, principal, provider_registry,
                 key_exchange, connectivity, *, adapter_id, tick_seconds=12):
        ...
    def start(self) -> None: ...   # spawns the daemon thread, called once from start_runtime()
    def wake(self) -> None: ...    # sets the wake Event; safe to call from any thread
    def stop(self) -> None: ...    # for tests / graceful shutdown
```

**No `mca_jobs` table.** That table exists in the schema (migration 1, Step 0.1) but nothing has ever written to it — Steps 1.4/1.5 deliberately made `run_step()` re-derive "what work is left" from the attachment's own `state` column plus its supporting state table (`mca_sender_state`/`mca_receiver_state`), not from a separate queue row that could drift out of sync with it. Introducing a job queue now would mean keeping two sources of truth consistent for a workload that, on a Pi Zero 2 W in a single household's mesh, is a handful of attachments at a time. Each worker tick instead does a direct, cheap SQL scan:

```sql
SELECT id, direction FROM attachments
WHERE workspace_id = ? AND state NOT IN (<terminal states>)
```

and calls `sender.run_step()` or `receiver.run_step()` per row depending on `direction`, exactly like `resume_pending()`/`reconcile_pending()` already do internally — the worker's tick body *is* those two functions, called back to back, not a reimplementation. `mca_jobs` stays reserved/unused (as it already effectively was after Step 1.4), and this ADR does not populate it. Revisit only if a future stage needs true priority scheduling or cross-attachment ordering, which nothing in Stage 1's scope requires.

**Wake sources**, each just a `service.wake()` call (never a direct `run_step()` call from a request handler — see below):

- a new outgoing draft is created (`POST /api/attachments`);
- an inbound OFFER/ACK/key-exchange message is ingested (existing `delivery/base.py` `ingest()` path, extended to call `service.wake()` after handing the message to `key_exchange`/`receiver.handle_offer()`);
- a user action: `Скачать` / `Повторить` / `Отклонить` / `Отменить` / `Отозвать`;
- `ConnectivityMonitor` reports a transition into `online` for a Relay origin some attachment is waiting on;
- a `threading.Event.wait(timeout=tick_seconds)` simply times out — the periodic safety-net pass (handles `retry_at`, and anything a missed wake left stranded).

**Concurrency model**, sized for a single-process Flask app on a Pi Zero 2 W, not a distributed system:

- One daemon thread per workspace. A per-workspace `threading.Lock` held for the duration of one tick (scan + drive every eligible row to its next automatic stopping point) rules out two ticks overlapping if a wake arrives mid-tick; the woken call simply blocks briefly on the lock rather than needing a separate SQLite lease. This is sufficient specifically because MeshCenter is one process — a lease would only earn its complexity if multiple OS processes could touch the same `attachments.db`, which ADR-0003 already rules out.
- Within one tick, attachments are processed one at a time, in `id` order — no thread pool. `_step_encrypting()`/`_step_downloading()` are CPU-bound (AEAD over the whole file) and I/O-bound (one Relay HTTP call at a time each); serializing them is the "one crypto worker" the developer discussion asked for, and is simply what calling `run_step()` in a plain `for` loop already gives for free.
- A tick caps itself at `MAX_ATTACHMENTS_PER_TICK = 8` non-terminal rows advanced per pass (a config constant, not a hard architectural limit) so one very active workspace can't starve a wake-driven event from being handled within `tick_seconds`; anything left over is picked up on the next tick or the next wake.
- "Maximum two concurrent network transfers" from the developer discussion is satisfied by the above without extra bookkeeping: there is exactly one thread calling `run_step()` at a time, so the true concurrency ceiling is 1, not 2 — tighter than asked, and simpler. Revisit if a later stage genuinely needs parallel uploads.

**API handlers never do slow work inline.** Every mutating endpoint (`POST /api/attachments`, `.../download`, `.../reject`, `.../retry`, `.../cancel`) does exactly: validate input, call the one non-blocking domain function that changes state synchronously and cheaply (`sender.create_draft()`, `receiver.begin_download()`, `receiver.reject()`, a new `sender.cancel()`/`retry()` call — all of these are already just a few SQLite statements, not the encrypt/upload/download work itself), call `service.wake()`, and return `202 Accepted`. The worker thread does the encrypt/upload/download/decrypt/verify work asynchronously afterward. This is what keeps a Flask request from blocking on AEAD-over-5-MiB or a slow Relay round trip.

**Startup sequence** (in `start_runtime()`, alongside the existing `threading.Thread(target=..., daemon=True).start()` calls it already has for `radio_health_worker` etc.):

1. Open/migrate the MCA workspace DB (already done today, just needs to happen before the next steps if it doesn't already).
2. Construct `ProviderRegistry`, `KeyExchangeCoordinator`, `AttachmentsService`.
3. Call `sender.resume_pending()` and `receiver.reconcile_pending()` once, synchronously, before the service's own thread starts — this is the "restart never loses a job" guarantee from Steps 1.4/1.5, now actually wired into the real process instead of only into tests.
4. `service.start()` — spawns the daemon thread, which does an immediate first tick, then waits on `tick_seconds` or a wake.

### 2. `meshsrv/connectivity_monitor.py` — a single snapshot, HTTPS-based, per-Relay granularity

```python
class ConnectivityMonitor:
    def __init__(self, provider_registry, *, session=None, now_fn=time.time): ...
    def snapshot(self) -> ConnectivitySnapshot: ...          # cheap, returns the last computed result
    def refresh(self, *, force=False) -> ConnectivitySnapshot: ...  # does the actual HTTP calls, rate-limited internally
    def can_attempt_relay(self, provider_id: str) -> bool: ...
```

```python
@dataclass(frozen=True)
class ConnectivitySnapshot:
    internet: InternetStatus
    relays: dict[str, RelayStatus]   # keyed by provider_id
```

**Internet state** (`unknown`/`checking`/`online`/`offline`/`limited`) is derived, not independently probed with a second ICMP call: it is `online` if *any* registered, enabled Relay's `/health` succeeded within the last check window, or a lightweight HTTPS HEAD to a fixed, documented fallback URL succeeds when no Relay is registered yet (first-run state, before any provider profile exists). This directly answers the developer discussion's objection to the existing single-ping check — an HTTPS success against a real endpoint MCAttach actually depends on is strictly more informative than ICMP reachability to `8.8.8.8`, and reuses a call the monitor has to make anyway rather than adding a second kind of probe. The check always runs on the Raspberry Pi itself (this is backend code called from the worker thread, not `navigator.onLine` in the browser) — `GET /api/mca/connectivity` (1.6A) only ever serves the monitor's last computed snapshot, it does not trigger a probe from a request.

**Per-Relay state** (`unknown`/`checking`/`online`/`degraded`/`unreachable`/`identity_mismatch`/`incompatible`/`disabled`), checked independently per registered profile:

- `GET <origin>/health` — the fast, frequent probe (every `RELAY_HEALTH_INTERVAL_SECONDS`, default 60, backing off to 300 after consecutive failures — the "backoff to 5 minutes" from the developer discussion).
- `GET <origin>/v1/info` — the slow, rare probe, only on: profile added, profile's `base_url` edited, explicit user `Проверить`, or after a `provider_id`/`service_public_key` mismatch is suspected. A mismatch between what `/v1/info` reports and the profile's pinned `provider_id`/`service_public_key` sets `identity_mismatch` — a distinct, more severe state than `unreachable`, never silently downgraded to a generic offline indicator (per the developer discussion's explicit requirement).
- Upload capability is a *separate* boolean-ish field on the same `RelayStatus`, not folded into the state enum: `upload_readiness: ready|upload_token_missing|upload_disabled|limit_exceeded`. A Relay can be `online` for download while `upload_token_missing` for sending — exactly the case the developer discussion called out — and the two must never be collapsed into one indicator.

`can_attempt_relay(provider_id)` is the one call site the worker/state machines actually need: `True` only when that Relay's state is `online` or `degraded` (both are "an HTTP attempt is worth making"; `unreachable`/`identity_mismatch`/`incompatible`/`disabled` are not). **This ADR does not change `sender.run_step()`'s or `receiver.run_step()`'s existing `network_available: bool` parameter signatures** — `AttachmentsService` computes that bool per call as `connectivity.can_attempt_relay(row["provider_id"])` before invoking `run_step()` for that specific row. A real HTTPS attempt inside `run_step()` itself remains the final authority on success or failure regardless of what the monitor last reported (a stale `unreachable` reading must never permanently block an attempt — only skip *this tick's* attempt and let the next natural retry/backoff try again), matching the developer discussion's explicit requirement that the probe is for scheduling, not a hard gate.

Check intervals (`RELAY_HEALTH_INTERVAL_SECONDS`, the backoff ceiling, `tick_seconds` above) are named constants in `connectivity_monitor.py`/`service.py`, not hardcoded inline — Step 1.9's real Pi Zero 2 W hardware pass is the right place to confirm they hold up under real load, per the developer discussion's own flagged uncertainty.

### 3. Provider Registry v2

**Migration 8** extends `mca_provider_profiles` and fixes the multi-default bug:

```sql
ALTER TABLE mca_provider_profiles ADD COLUMN kind TEXT NOT NULL DEFAULT 'own';           -- 'own' | 'third_party'
ALTER TABLE mca_provider_profiles ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE mca_provider_profiles ADD COLUMN min_ttl_seconds INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN max_ttl_seconds INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN protocol_version TEXT;
ALTER TABLE mca_provider_profiles ADD COLUMN upload_token_file TEXT;      -- filename under this workspace's keys/, or NULL
ALTER TABLE mca_provider_profiles ADD COLUMN last_checked_at INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN last_check_result TEXT;      -- mirrors RelayStatus.state, cached
ALTER TABLE mca_provider_profiles ADD COLUMN last_latency_ms INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN last_error_code TEXT;

CREATE UNIQUE INDEX idx_mca_provider_profiles_one_default
    ON mca_provider_profiles(workspace_id) WHERE is_default = 1;
```

The partial unique index makes "at most one default per workspace" a database-enforced invariant, not just an application convention — it turns the bug the developer discussion found into something that fails loudly (`sqlite3.IntegrityError`) instead of silently, if anything ever tries to violate it again. `ProviderRegistry.set_default(provider_id)` is the one sanctioned way to change the default, and does it transactionally:

```python
def set_default(self, provider_id: str) -> ProviderProfile:
    with self._conn:  # sqlite3's own transaction context: commits on success, rolls back on exception
        self._conn.execute(
            "UPDATE mca_provider_profiles SET is_default = 0 WHERE workspace_id = ? AND is_default = 1",
            (self._workspace_id,),
        )
        cur = self._conn.execute(
            "UPDATE mca_provider_profiles SET is_default = 1 WHERE workspace_id = ? AND provider_id = ?",
            (self._workspace_id, provider_id),
        )
        if cur.rowcount == 0:
            raise ProviderRegistryError(f"no such provider_id in this workspace: {provider_id!r}")
    return self.resolve(provider_id)
```

`register(..., is_default=True)` is changed to call this same clear-then-set sequence instead of relying on `ON CONFLICT`'s per-row update, so the two entry points can't diverge. **Changing the default only affects new drafts** — `sender.create_draft()` already takes `provider_id` as an explicit argument and stores it on the attachment row at creation time (confirmed unchanged by this ADR); nothing in Steps 1.4/1.5 ever re-reads "the current default" mid-transfer, so this guarantee already holds and just needs to be named as a decision here rather than left implicit.

**Upload token storage.** `mca_provider_profiles` gains `upload_token_file` (a filename, not a path — same convention as `mca_principal.private_key_file`) instead of storing the token itself. The actual secret is written to `<workspace>/keys/relay_upload_token_<provider_id>.secret`, `0o600`, inside the already-`0o700` `keys/` directory `WorkspacePaths`/`identity.py` already establish and chmod — this is not a new trust boundary, it is the existing one Step 1.2's private-key storage already relies on. `ProviderRegistry` gains:

```python
def set_upload_token(self, provider_id: str, workspace_manager: MCAWorkspaceManager, principal_id: str, token: str) -> None
def get_upload_token(self, provider_id: str, workspace_manager: MCAWorkspaceManager, principal_id: str) -> Optional[str]
```

`get_upload_token()` is called only from `sender.py`'s own `run_step()` path (building a `RelayClient`), never from an HTTP handler — `GET /api/mca/providers` (1.6A) returns `upload_token_configured: bool` computed from `upload_token_file IS NOT NULL`, exactly as the developer discussion specified, and the token itself is never serialized to JSON or written to any log line (existing `meshsrv` logging already redacts by never logging raw secret material — this follows the same rule `identity.py`'s signing keys already follow, not a new one).

**Delete vs. disable.** `remove_or_disable(provider_id)`: if no `attachments` row anywhere in the workspace references this `provider_id`, delete the row and its token file outright; otherwise set `enabled = 0` and leave history intact — `resolve()` is unchanged (still returns disabled profiles; `enabled` is a UI/worker-facing filter, not a trust decision, since an in-flight `DOWNLOADING` attachment against a since-disabled Relay must still be able to finish or fail cleanly rather than erroring on a `None` resolve).

Also added, all thin wrappers the 1.6A endpoints call directly: `update_profile()` (display name, TTL bounds, enabled — never `service_public_key`/`origin`, which are identity-defining and require the same re-confirmation flow as adding a new profile, not a silent edit), `list_enabled()`, `get_upload_candidates()` (enabled + `upload_allowed` + `upload_token_file IS NOT NULL`), `get_download_profile(provider_id)` (unchanged from today's `resolve()`, aliased for read-path clarity).

### 4. Minimal contacts backend (no new schema — `mca_recipient_bindings` already has everything)

New `meshsrv/attachments/contacts.py`, thin wrappers around `key_exchange.py` state already being written by its existing `handle_incoming()` path — this module adds no new persistence, only read/decision surface for 1.6A's HTTP layer to call:

```python
def contact_status(binding: Optional[RecipientBinding]) -> str:
    # 'key_unknown' | 'confirmation_required' | 'trusted' | 'key_changed'
    # (mirrors RecipientBinding.status from key_exchange.py almost exactly -
    # this function exists only to also fold in the "no binding row at all
    # yet" case, which key_exchange.py's own `.status` property, being an
    # instance method, cannot represent.)

def confirm_binding(conn, binding_id: str, now=None) -> None:
    # sets tofu_confirmed_at - the explicit TOFU accept action (17.4/17.6)

def accept_key_change(conn, binding_id: str, now=None) -> None:
    # promotes pending_public_identity/pending_key_epoch to the active
    # columns, clears the pending_* columns

def reject_key_change(conn, binding_id: str, now=None) -> None:
    # clears the pending_* columns only - keeps the old (still trusted) key active
```

Sending is permitted only for `trusted`. The send form's "Кому" field (17.4) is **not** a general contacts directory in this MVP — per the developer discussion, in a direct chat the recipient is already the open chat's own contact, so the field is pre-filled from that context, not a picker; the `contact_status()` result decides whether the form shows the normal send button or a `Запросить ключ`/`Проверить ключ` action instead. A general, browsable contacts list (needed once channels/multiple recipients exist) is out of scope here and stays deferred with the rest of 17.5's full wizard.

## Consequences

- New files: `meshsrv/attachments/service.py`, `meshsrv/connectivity_monitor.py`, `meshsrv/attachments/contacts.py`.
- Migration 8: `mca_provider_profiles` gains 9 columns and one partial unique index (see above). No changes to `attachments`/`attachment_recipients`/`attachment_deliveries`/`attachment_events` — Steps 1.4/1.5's schemas are untouched by this ADR.
- `provider_registry.py`: `set_default()`, `set_upload_token()`/`get_upload_token()`, `update_profile()`, `remove_or_disable()`, `list_enabled()`, `get_upload_candidates()`, `get_download_profile()` added; `register()`'s default-handling changed to call `set_default()` internally instead of relying on `ON CONFLICT`.
- `server.py`: `start_runtime()` gains the five-step startup sequence in Decision §1; one new `threading.Thread(target=attachments_service.start_loop, daemon=True)`-shaped addition alongside the existing worker threads (exact wiring is an implementation detail of 1.6A, not re-litigated here).
- `sender.py`/`receiver.py`: **no signature changes** — `network_available` stays a plain `bool`, computed by the caller (`AttachmentsService`) per attachment via `connectivity.can_attempt_relay(provider_id)`. `mca_jobs` remains unused.
- 1.6A's REST endpoints (`GET/POST /api/attachments...`, `GET /api/mca/contacts`, `GET /api/mca/providers`, `GET /api/mca/connectivity`, and the contacts/provider mutation routes from the developer discussion) are thin HTTP wrappers over exactly the functions this ADR adds or already exists — no new domain logic belongs in `api/api_attachments.py` itself beyond input validation, CSRF, session auth, and idempotency-key handling, per the developer discussion's explicit requirement that these ship in 1.6A rather than being deferred to 1.7.

## References

- `MCAttach System Design and Implementation Spec v1.2`, sections 15.1, 15.2, 16.3-16.5, 17.4-17.6, 18.
- ADR-0006 (sender state machine), ADR-0007 (receiver state machine) — both explicitly left "who drives `run_step()`" and "what is `network_available`" as the calling application's problem; this ADR is that answer.
- `provider_registry.py`'s existing docstring on the three MVP trust-bootstrap paths (unchanged by this ADR).
- `server.py`'s existing `start_runtime()` background-thread convention (`radio_health_worker`, `telemetry_worker`, `ack_timeout_worker`, etc.) and `identity.py`'s `0o600`/`0o700` secret-file convention — both reused here, not replaced.
