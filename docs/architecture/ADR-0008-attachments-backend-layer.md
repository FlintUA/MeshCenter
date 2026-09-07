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

## Amendment (PR #231 review hardening pass): what actually shipped differs from this ADR in five places

An independent review of the first implementation of this ADR (PR #227/#231) found real defects and architectural gaps this Decision section did not anticipate. The fixes are substantial enough that several claims above are now factually wrong about the shipped code, not just imprecise about implementation detail - corrected here rather than silently left stale, per this project's own preference for an honest paper trail over a retroactively "clean" original document.

**1. Inbound message dispatch is not what §1's "wake sources" list describes.** That list says an inbound OFFER/key-exchange message reaches this ADR's worker via "existing `delivery/base.py` `ingest()` path, extended to call `service.wake()` after handing the message to `key_exchange`/`receiver.handle_offer()`" - implying `ingest()`/dispatch still runs on the radio listener thread, with only a `wake()` call added. **This is false for the shipped code.** The radio listener thread (`mca_runtime.handle_incoming_meshtastic_text()`) now does exactly one thing: build an immutable `service.InboundEvent` and hand it to `AttachmentsService.enqueue_inbound()`, a bounded (`INBOUND_QUEUE_MAXSIZE=256`), non-blocking `queue.Queue`. Every real dispatch step this ADR's §1 implies happens inline - CBOR decode/`ingest()`, message-type classification, `key_exchange.handle_incoming()`, `receiver.handle_offer()`, and any resulting reply-send - now runs only on `AttachmentsService`'s own worker thread, drained at the start of each `tick()` (`_drain_inbound_events()`/`_process_one_inbound_event()`, `service.py`). This is a genuine single-owner-SQLite redesign, not a wording tweak: before this fix, the radio listener thread and the worker thread could both reach the one `sqlite3.Connection` directly, serialized only by a shared `threading.Lock` - a real concurrency hazard for any future call site that forgot to hold it (which happened at least once, see `service.py`'s own `_lock` docstring history). After this fix, only the worker thread ever touches `conn`; the listener thread's only touchpoint is the thread-safe queue.

**2. §2's per-Relay state list still names `checking`, which does not exist.** `InternetStatus.CHECKING`/`RelayState.CHECKING` were removed (confirmed by repo-wide grep to have been set nowhere, ever) rather than implemented, since making them real would need thread-safety machinery for a consumer (concurrent `snapshot()` reads during an in-flight `refresh()`) that does not exist yet - Step 1.6A's REST layer, explicitly out of scope for the hardening pass. The real state sets are `unknown|online|offline|limited` (internet) and `unknown|online|degraded|unreachable|identity_mismatch|incompatible|disabled` (per-Relay).

**3. §2's fallback-internet-check trigger is incompletely described.** "a lightweight HTTPS HEAD to a fixed, documented fallback URL succeeds when no Relay is registered yet (first-run state, before any provider profile exists)" describes only half of `_check_fallback_internet()`'s actual trigger condition. The shipped `refresh()` also falls back to this same probe whenever every currently-registered Relay is failing (not `disabled` - a disabled Relay is a deliberate user choice, never itself treated as evidence of an outage) - otherwise a workspace with exactly one Relay, currently down, would report the *internet itself* as down even with everything else on the network working fine. `refresh()` additionally now (a) prunes `ConnectivityMonitor`'s own per-provider tracking dicts for any `provider_id` no longer in the registry, (b) reflects a just-disabled Relay's `DISABLED` status on the very next `refresh()` rather than waiting out the cached health-check interval, (c) validates `/v1/info`'s reported `protocol_version` against `SUPPORTED_RELAY_PROTOCOL_VERSIONS`, distinct from an identity mismatch (`RelayState.INCOMPATIBLE`, not `IDENTITY_MISMATCH` - the Relay is who it claims to be, this client just cannot speak its protocol), and (d) no longer treats a transient `/v1/info` network failure as a completed identity check (previously this could silently defer the *next real* check by up to `RELAY_INFO_MIN_INTERVAL_SECONDS`, an hour).

**4. §2's `upload_readiness` state set names `limit_exceeded`, which was removed.** Nothing in this codebase computes a per-Relay upload quota, so this state could never actually be returned - a branchable state a function can never produce is worse than not having it. The real set is `ready|upload_token_missing|upload_disabled`, computed by the now-public `evaluate_upload_readiness()` (previously a private `_upload_readiness_for()`).

**5. TOFU binding trust, as actually shipped, is bound to the delivery/transport address - a property this ADR never named as a requirement at all.** `key_exchange.get_binding_by_key_id()` (used for OFFER signature verification) is deliberately address-agnostic - correct for that use, since the signer's identity, not which physical address relayed the message, is what a signature verifies. An earlier hardening pass (PR #227 defect #6) reused that same lookup for the *sending* path (`AttachmentsService._resolve_recipient_identities()`), checking only that a binding was `MCA_READY` - never that its `transport_address` matched the attachment's own delivery destination. A recipient's binding pinned to one address and an attachment's `attachment_deliveries.route_id` pointing somewhere else were never cross-checked, silently decoupling "the key we trust" from "the address we're sending to" - precisely the link TOFU exists to establish. Fixed (PR #231 review, section 8): for the DIRECT-only MVP, a recipient is now excluded, fail-closed, whenever `binding.transport_address != attachment_deliveries.route_id` for that attachment - the same `fail_recipients_not_trusted()` path an unconfirmed/`KEY_UNVERIFIED` binding already took. Also newly persisted end to end: an inbound OFFER's reply route (`reply_adapter_id`/`reply_connector_profile_id`/`reply_route_type`/`reply_route_id`/`reply_destination_address`, `attachments` table) now records the *real* adapter/connector that received the OFFER (from the `DeliveryEnvelope` `ingest()` produced), not an assumed single global Meshtastic adapter, and the receiver-side ACK outbox dispatch now enforces real, persisted per-source and global rate limits (`mca_ack_quota` table) instead of the "N rows per SQL query" pseudo-limit this ADR never described in the first place.

**Consistent disabled-Relay policy** (also not named as a single policy anywhere above, stated here explicitly): a `disabled` Relay profile is never deleted, never silently excluded from `ProviderRegistry.list_providers()`/`resolve()`, and always surfaces as `RelayState.DISABLED` in `ConnectivityMonitor`'s snapshot - reflected immediately on the next `refresh()`, not after any cached health-check interval elapses (Amendment point 3 above). `resolve()`/`get_download_profile()` still resolve a disabled profile normally (§3's existing "not a trust decision" reasoning), so an in-flight `DOWNLOADING` attachment against a since-disabled Relay can still finish or fail cleanly. `evaluate_upload_readiness()` reports `UPLOAD_DISABLED` for a disabled profile regardless of its `upload_allowed` column (a disabled Relay is never upload-ready). `ConnectivityMonitor.can_attempt_relay()` returns `False` for a disabled profile's `provider_id` once at least one `refresh()` has run (a `disabled` state is not in `_ATTEMPTABLE_RELAY_STATES`), so `AttachmentsService` never attempts a network call against it - the one case `refresh()` performs literally zero network I/O for, by design (`_check_relay()`'s own short-circuit). A user re-enabling a disabled profile (`update_profile(enabled=True)`) takes effect on the Relay's *next* `refresh()`, same as any other configuration change - there is no separate "re-enable" code path to keep in sync with `update_profile()` itself.

## Amendment (2nd-pass review): the single-owner-SQLite redesign still had an indirect blocking path, plus five smaller corrections

A second independent review of the amendment above found that its point 1 ("only the worker thread ever touches `conn`; the listener thread's only touchpoint is the thread-safe queue") was true but incomplete: it said nothing about the *locks* the two threads still shared, which turned out to reintroduce the exact blocking hazard the redesign was meant to eliminate.

**The bug.** `handle_incoming_meshtastic_text()` called `mca_runtime.ensure_service()`/`_get_state()` on *every* invocation (not just the first), and both acquired `mca_runtime._lock` - the *same* lock object `AttachmentsService` was constructed with (`lock=_lock`) for `tick()`'s own re-entrancy guard. `tick()` holds that lock for its entire duration, including `ConnectivityMonitor.refresh()`'s real Relay HTTP calls - so a slow/unreachable Relay meant the radio listener thread could block on `_lock` for the same duration. A test written for the first amendment pass had even pinned this as correct behavior (`test_attachments_service_shares_the_runtime_lock_not_a_private_one`) instead of catching it.

**The fix**, four parts: (1) `server.py`'s `start_runtime()` now calls `start_attachments_service()` *before* starting the radio listener thread, not after - proven at the AST level by a new statement-ordering test, not just "unconditional" as the first amendment's point 1 already established; (2) `handle_incoming_meshtastic_text()` no longer calls `ensure_service()`/`_get_state()` at all after startup - it only reads the already-built module-level runtime state directly and hands the event straight to `enqueue_inbound()` (no lock, no database, no filesystem, no network); (3) if the runtime is somehow not ready (should never happen given the ordering in (1)), the event is logged and dropped rather than triggering initialization from the listener thread; (4) `AttachmentsService` now gets its own dedicated `tick_lock` (`_MCARuntimeState.__init__`), never `mca_runtime._lock` - the two are now genuinely independent objects, one guarding rare, fast, startup-only singleton creation, the other held for a tick's entire (potentially slow) duration. Proven by a new deterministic test (`test_handle_incoming_meshtastic_text_does_not_block_on_a_slow_tick`) that blocks a real tick inside a fake Relay call until released and asserts `handle_incoming_meshtastic_text()` still returns within one second from a different thread.

Five smaller corrections from the same review pass, detailed in `docs/attachments/MCAttach_File_Transfer_Technical.md` (§§4, 6, 7, 8, 10, 11) rather than repeated here in full:

- `mca_ack_quota` was missing from `migrations.py`'s `ALL_TABLE_NAMES` schema-completeness set, so no existing test ever actually verified its presence after a full migration run.
- The disabled-Relay policy above was itself incomplete: `refresh()`'s fallback-internet-probe trigger still excluded "every registered Relay is disabled" as a reason to run, leaving internet status frozen (typically `UNKNOWN` forever) for a workspace whose only Relays have always been disabled. Fixed to run the probe whenever no Relay has confirmed `ONLINE` evidence, for any reason.
- The persisted reply route (`reply_adapter_id`/`reply_connector_profile_id`/`reply_destination_address`, this amendment's point 5) was being written but never read by `_dispatch_outgoing_replies()` - it now fails closed (`UNDELIVERABLE`) on a confirmed adapter mismatch rather than silently sending through whichever adapter the service happens to be constructed with.
- Nothing bounded how many `attachments` rows a flood of OFFERs carrying distinct, attacker-chosen `transfer_id`s could create - each an unbounded, permanent DB row. Added per-source (20) and global (200) admission ceilings on live (non-terminal) received attachments.
- `ConnectivityMonitor.can_upload_to(provider_id)` was added - `RelayStatus.state`/`.upload_readiness` are deliberately independent for `RelayStatus` itself, but neither alone answers "is it actually sensible to upload here right now"; this is the missing combined predicate.
- `POST /api/updates/apply` now skips its automatic post-update restart when `apply_update()` detected a `requirements.txt` change - restarting into code that expects a dependency nothing has installed would very likely crash-loop the service.

## References

- `MCAttach System Design and Implementation Spec v1.2`, sections 15.1, 15.2, 16.3-16.5, 17.4-17.6, 18.
- ADR-0006 (sender state machine), ADR-0007 (receiver state machine) — both explicitly left "who drives `run_step()`" and "what is `network_available`" as the calling application's problem; this ADR is that answer.
- `provider_registry.py`'s existing docstring on the three MVP trust-bootstrap paths (unchanged by this ADR).
- `server.py`'s existing `start_runtime()` background-thread convention (`radio_health_worker`, `telemetry_worker`, `ack_timeout_worker`, etc.) and `identity.py`'s `0o600`/`0o700` secret-file convention — both reused here, not replaced.
