# MCAttach Internal REST API Contract

**Status:** Design contract (Step 1.6A), revised. This document defines the HTTP surface — it does **not** implement it. No endpoint below exists yet; no runtime code, migration, test, JS, HTML or CSS was changed in the Step 1.6A change that added/revised this document.
**Canonical source:** the Russian system design spec (section 18 primary, sections 17/19/20 and the state machines also consulted). That spec is reference-only and is not committed to the repository.
**Audience:** a future implementation task, split into sub-stages (§5).

---

## 1. Scope and non-goals

This document specifies the **internal** REST API MeshCenter exposes to its own browser UI for the MCAttach (file-transfer-over-Meshtastic) subsystem. "Internal" means: browser-session-authenticated, same-origin, never a public or machine-to-machine API. It does not specify the **Relay** wire API (that lives in `meshsrv/attachments/relay_client.py`) and does not specify the **mesh** wire format (ADR-0001 / `meshsrv/attachments/codec.py`).

Everything here is bounded by the MVP narrowing that is already in force across the backend layer:

- **Single transport:** Meshtastic direct text (`MCA1_TEXT`) only. `MeshtasticTextAdapter` reports `supports_channel=False` and incoming only for direct messages.
- **Single recipient:** one sender → one recipient per attachment. `attachment_recipients` is schema-capable of more than one row, but no code path exercises it; `AttachmentsService._resolve_recipient_identities()`'s TOFU-binding check is scoped to `DIRECT` routes only.
- **No automatic download:** receiving never starts without an explicit user action (`receiver.begin_download()` is never called automatically; `WAITING_CONSENT` is excluded from `receiver.AUTOMATIC_STATES`).
- **No automatic Relay failover, no per-Relay upload quota, no broadcast/group delivery.**

---

## 2. Conventions inherited from the existing codebase

These are the conventions already in force in `server.py` / `api/*.py`, which the MCAttach routes must follow for consistency. Each is cited.

### 2.1 Route registration

DI-by-parameter-list pattern, not Blueprints (CLAUDE.md): one plain function

```python
def register_mca_routes(app, state_lock, handle_errors, mca, ...):
```

called from `server.py`, closing over the shared objects it needs. The single object every handler calls through is a request-facing **facade** (§3), never `server.py`'s raw globals and never a raw `sqlite3.Connection`.

### 2.2 Browser-session authentication

A single shared password (no usernames/roles). `api/api_auth.py`'s `before_request` hook returns, for any `/api/` path with no `session["authenticated"]`:

```json
{"ok": false, "error": "Authentication required", "error_code": "auth_required"}
```

with HTTP **401**. Every MCAttach endpoint is an `/api/` path and inherits this with **no extra work** — do not add a second auth layer. Exempt paths are only `/login` and `/static/`, so no MCAttach endpoint is reachable unauthenticated.

### 2.3 CSRF (project-wide contract — a prerequisite, not a per-endpoint detail)

The audited codebase has **no CSRF mechanism today**: there is no `csrf_token` in any JS or Python file, and `server.py` sets no `SESSION_COOKIE_SAMESITE`, no `SESSION_COOKIE_HTTPONLY`, and no `MAX_CONTENT_LENGTH` (verified by grep — zero hits for all of these). The Flask session cookie therefore relies on the framework default (effectively `SameSite=Lax` in a browser), which is **not** sufficient protection for state-changing requests.

Because MCAttach introduces the project's first `multipart/form-data` surface and its first write-heavy mutation set, the following project-wide CSRF contract is a **required prerequisite** — implemented once in `server.py`/`api_auth.py`, shared by every feature, not invented per-MCAttach:

1. **Cookie hardening.** Set `SESSION_COOKIE_SAMESITE = "Strict"` (or `"Lax"` *combined with* an explicit CSRF token — one of the two, not neither) and `SESSION_COOKIE_HTTPONLY = True`. This is a single `app.config` change, not MCAttach-specific.
2. **Token issuance.** At login, mint a per-session random token (≥ 128 bits), store it in the session, and expose it to the page (e.g. a `<meta name="csrf-token">`). The token is **not** returned by any API read endpoint.
3. **Token check.** One `before_request` hook rejects any non-GET/HEAD `/api/` request whose `X-CSRF-Token` header does not equal the session token, returning `{"ok": false, "error": "CSRF token missing or invalid", "error_code": "csrf_invalid"}` with **403**.
4. **Client usage.** The frontend sends the token as `X-CSRF-Token` on every mutating `fetch`/`XMLHttpRequest`. Custom headers are settable on multipart `FormData` requests, so the `multipart/form-data` create endpoint is covered; JSON and form mutations carry it identically.

Every mutation endpoint in this contract is marked `CSRF: required`. Until the project-wide mechanism above exists, **no MCAttach mutation may ship** — recorded as the first gap in §13 and as sub-stage 1.6A.0 in §5.

### 2.4 JSON envelope

- **Success:** `{"ok": true, ...}` with domain fields.
- **Error:** `{"ok": false, "error": "<human text>", "error_code": "<snake_case_code>"}` plus the correct HTTP status. Codes are snake_case: existing examples `waypoint_not_found`, `radio_busy`, `auth_required`, `password_too_short`.

`handle_errors` (server.py:496) turns an uncaught exception into a 500 envelope; MCAttach routes must catch domain errors and return a stable `error_code` rather than leaking `str(e)` (see §10).

### 2.5 Request body

- JSON: `request.get_json(force=True)`.
- File upload: `multipart/form-data` (see §7.2 — the one place MCAttach needs a content-type the existing codebase does not yet use).

### 2.6 Status codes

`200` (read), `201`/`202` (created/accepted — see §3.4), `400` (validation), `401` (auth, inherited), `403` (CSRF), `404` (not found), `409` (conflict / wrong state / idempotency), `429` (rate-limited / queue full), `500` (unexpected), `503` (radio/relay unavailable at an action's runtime). See §3.4 for the uniform async rule.

### 2.7 Pagination

The only existing list route (`GET /api/waypoints`) returns everything plus `total` and does not paginate. MCAttach's list endpoint keeps `total` and adds `limit`/`offset` as optional query params with server defaults (metadata retained 90 days by default). No cursor pagination.

### 2.8 Idempotency

Job creation is idempotent by **client request ID**. Full semantics are in §3.5 and §7.2: identical replay returns the original result; the same `client_request_id` with different canonical content returns **409**.

### 2.9 Secrets, absolute paths, and logging

- Relay credentials (upload/revoke tokens, receipt secrets, private keys) are **never** returned — replaced by `upload_token_configured` / `configured` booleans.
- Responses never expose absolute filesystem paths (§7.4 replaces `saved_path` with a `saved` boolean).
- Logs omit plaintext filename/comment, keys, full MCA pointers, and tokens.

---

## 3. Threading and data-access model (the architectural core)

This is the one real architectural addition Step 1.6A requires, and it supersedes the earlier revision's "dedicated read-only connection" idea — which is **rejected**: a read-only connection is still a second thread reaching into SQLite, and routing reads through the worker's `tick` lock would serialize them behind the network-bound tick. The model below preserves the single-owner invariant *and* keeps request threads off both `conn` and the tick lock.

### 3.1 The invariant (already true of the backend)

The single `sqlite3.Connection` is touched by exactly two threads over the process lifetime:

1. the **startup thread** — construction + `migrate()` + the eager `ConnectivityMonitor._refresh_profile_snapshot()` and `sender.resume_pending()`/`receiver.reconcile_pending()` inside `mca_runtime.ensure_service()`;
2. the **worker thread** (`AttachmentsService._run`) — every tick.

`AttachmentsService._lock` exists **only** to make `tick()` re-entrant (its own docstring says so), *not* to arbitrate between two owners. A Flask request thread must therefore never call `tick()`, take `_lock`, or touch `conn`. `ConnectivityMonitor.snapshot()` and `evaluate_upload_decision()` are the worked example: they read an atomically-published in-memory `_profile_snapshot`/`_relay_statuses`, never SQLite — and a dedicated thread-identity test already asserts a simulated REST call performs zero SQLite operations.

### 3.2 Two request-thread-safe surfaces

**Reads — immutable snapshots.** The worker publishes, at the end of each tick (and once at startup), immutable projections built by copy (a fresh `dict`/`list`, swapped in with one reference assignment, atomic under the GIL — the exact pattern `ConnectivityMonitor._refresh_profile_snapshot()` already documents). The request thread reads only these:

- **connectivity snapshot** — exists: `ConnectivityMonitor.snapshot()`.
- **provider snapshot** — exists: `ConnectivityMonitor._profile_snapshot` (private; expose a read-only accessor).
- **attachments snapshot** — **new**: a worker-built projection of `attachments` in the public row shape of §7.4 (no `saved_path`, no protocol-internal columns), keyed by `id`, plus an ordered list for the list endpoint. Built each tick from the same row-scan the worker already performs, so it stays fresh without extra queries.
- **idempotency index snapshot** — **new**: `client_request_id → {attachment_id, canonical_hash, created_at}` (§3.5).

**Writes — a bounded command queue.** Every mutation is an immutable, frozen `Command` dataclass validated on the request thread (using **pure functions only** — no `conn`, no file I/O beyond the create endpoint's spool write, §7.2), then enqueued on a bounded queue the same way the existing inbound queue works:

- `queue.Queue(maxsize=COMMAND_QUEUE_MAXSIZE)`, `put_nowait()`; `queue.Full` → **429** `command_queue_full` (never block the request thread).
- The worker drains up to `MAX_COMMANDS_PER_TICK` commands per tick (before the row-scan), executing each on its own thread — the single owner — then `wake()`s itself. A command that raises is caught and logged like a bad attachment; it never kills the worker.
- Command execution does the real domain work: `sender.create_draft()`, `sender.run_step()` (for retry), `receiver.begin_download()`/`reject()`, the Relay revoke, the workspace file move, `ProviderRegistry.register()`/`update_profile()`/`set_default()`/`set_upload_token()`/`remove_or_disable()`, etc.

The set of commands is enumerated per endpoint in §7. `wake()` remains the handler's only post-action call *after* enqueueing (it flags the worker without touching DB/network).

### 3.3 The one allowed file read on the request thread

`GET /api/attachments/{id}/content` serves a **file from disk**, not a SQLite row. Files written by the worker (decrypted `cache/incoming/` blob, or a persisted `files/` copy) are immutable once written, so the content endpoint may read them directly on the request thread, gated on the attachments snapshot for state (`content_available` / `saved`). It must not open `conn`.

### 3.4 Sync vs async: the uniform response model

This resolves the earlier contradiction between a "blanket 202" and synchronous-looking provider/action endpoints. There are exactly two response classes:

- **Reads** → `200` + snapshot data (or `404`). No command, no queue.
- **Mutations** → `202 Accepted` + `{"ok": true, "command_id": "<uuid>"}`. Every mutation — attachment *and* provider/registry — is a worker command, because the registry and the attachments service share the same single-owner `conn`; a "synchronous" registry write from the request thread would violate §3.1 just as surely as an attachment write would.

Synchronous validation that produces a **4xx** still happens on the request thread (pure functions: `normalize_origin`, `compute_provider_id`, key-length checks, snapshot-state preconditions) so the client gets an immediate, deterministic error. The persistence is always the deferred command. The client observes success/state-change by polling the read snapshot (design spec §18: polling, not streaming).

### 3.5 Idempotency and synchronous result resolution

`create` (and provider `register`) must answer three cases deterministically even though the write is deferred:

1. **new** `client_request_id` → `202` + `command_id`.
2. **identical replay** (same `client_request_id`, same canonical content) → `200` + the *original* result (`attachment_id`) read from the worker-published **idempotency index snapshot**.
3. **same `client_request_id`, different canonical content** → **409** `idempotency_conflict`.

Canonical content for create is `SHA-256(file bytes ‖ recipient source_address ‖ comment ‖ hard_ttl_seconds ‖ download_grace_seconds ‖ provider_id ‖ route_id)`. The idempotency index is another immutable snapshot (§3.2), so cases 2/3 resolve without touching `conn`. This requires two new `attachments` columns (`client_request_id`, `canonical_hash`) — listed in §13.

---

## 4. Domain-layer inventory (what the worker executes)

All real work lives in `meshsrv/attachments/` and `meshsrv/connectivity_monitor.py`. The endpoints are a thin translation layer; the worker is the only executor. Names below are verbatim.

### 4.1 Service and runtime

- `AttachmentsService` (`service.py`): `start()`, `stop()`, `wake()`, `evaluate_upload_readiness()`, `enqueue_inbound()`, `tick()`. `wake()`'s docstring names it "the only method API handlers … are meant to call after a domain-layer action."
- `_MCARuntimeState` singleton (`mca_runtime.py`): builds and holds `conn`, `principal`, `provider_registry`, `connectivity_monitor`, `coordinator`, `service`; reached via `mca_runtime._get_state(data_dir)`.
- `start_attachments_service()` (`mca_runtime.py`), called once from `server.py`'s `start_runtime()`.

### 4.2 Sender (`sender.py`)

States: `DRAFT → VALIDATING → ENCRYPTING → QUEUED_UPLOAD → UPLOADING → READY_TO_SEND → SENT → RECEIVED → DOWNLOADED`; terminal `EXPIRED`, `REVOKED`, `CANCELLED`, `FAILED_VALIDATION`, `FAILED_UPLOAD`, `FAILED_RADIO`.

- `TERMINAL_STATES = {DOWNLOADED, EXPIRED, REVOKED, CANCELLED, FAILED_VALIDATION, FAILED_UPLOAD, FAILED_RADIO}`.
- `AUTOMATIC_STATES = {DRAFT, VALIDATING, ENCRYPTING, QUEUED_UPLOAD, UPLOADING, READY_TO_SEND}`. `SENT`/`RECEIVED` are intentionally excluded (event-driven via `on_ack_received`/`on_ack_downloaded`).
- `create_draft(conn, workspace_manager, principal, *, workspace_id, source_path, file_name, mime_type, recipients, adapter_id, connector_profile_id, route_type, route_id, provider_id, kind, comment, hard_ttl_seconds, download_grace_seconds, now)` → `attachment_id`. **Does no file I/O** (`source_path` is recorded, not opened — a draft can exist before the file/radio are ready).
- `run_step(...)` → exactly one transition; no-op for terminal/`SENT`/`RECEIVED`/`DOWNLOADED`.
- `cancel(conn, attachment_id)` → `CANCELLED`; raises for terminal states and `SENT`/`RECEIVED`/`DOWNLOADED`.
- `resume_pending(...)`, `on_ack_received(...)`, `on_ack_downloaded(...)`.
- **Retry semantics:** `run_step` is the only forward driver and only makes progress from `AUTOMATIC_STATES`. `FAILED_VALIDATION`/`FAILED_UPLOAD` are terminal by design (retrying unchanged would fail identically forever); `FAILED_RADIO` is reserved/unreached. `READY_TO_SEND` on a radio-send failure stays `READY_TO_SEND` (retryable). So **retry is valid only for `AUTOMATIC_STATES`** — see §8.

`RecipientTarget` = `{public_identity: bytes, key_id: hex16}` — resolved by the caller from `mca_recipient_bindings`, **not** supplied as a raw public key by the browser.

### 4.3 Receiver (`receiver.py`)

States: `OFFER_RECEIVED → WAITING_KEY → WAITING_PROVIDER → WAITING_NETWORK → WAITING_CONSENT → DOWNLOADING → VERIFYING → AVAILABLE`; terminal `EXPIRED`, `REJECTED`, `FAILED`.

- `TERMINAL_STATES = {AVAILABLE, EXPIRED, REJECTED, FAILED}`.
- `AUTOMATIC_STATES = {WAITING_KEY, WAITING_PROVIDER, WAITING_NETWORK, DOWNLOADING}`. `WAITING_CONSENT`, `VERIFYING`, `OFFER_RECEIVED` are **not** automatic.
- `begin_download(conn, id)` → only from `WAITING_CONSENT` (else raises); `reject(conn, id)` → only from `WAITING_CONSENT`. `handle_offer(...)`, `run_step(...)`, `reconcile_pending(...)`.

### 4.4 Provider registry (`provider_registry.py`)

`ProviderProfile` fields: `provider_id` (Base64URL, 11 chars — the table primary key), `display_name`, `origin`, `service_public_key` (32 bytes), `tls_required`, `upload_allowed`, `download_allowed`, `max_ciphertext_bytes`, `is_default`, `added_at`, `kind` (`own`|`third_party`), `enabled`, `min_ttl_seconds`, `max_ttl_seconds`, `protocol_version`, `upload_token_configured`, `last_checked_at`, `last_check_result`, `last_latency_ms`, `last_error_code`.

Methods: `register(...)`, `set_default(provider_id)`, `update_profile(...)` (with a `CLEAR` sentinel for the nullable TTL/`protocol_version` fields), `record_check_result(...)`, `remove_or_disable(provider_id, workspace_manager, principal_id)` (→ `"deleted"`|`"disabled"`), `list_enabled()`, `get_upload_candidates()`, `get_download_profile()`, `set_upload_token(...)`, `get_upload_token(...)`, `resolve(provider_id)` (the SSRF boundary — a miss returns `None`, no DNS/HTTP), `list_providers()`, `get_default()`.

Key facts for the contract:

- `compute_provider_id(origin, service_public_key)` = Base64URL of the first 8 bytes of `SHA-256(origin + "\n" + raw 32-byte Ed25519 key)`; `normalize_origin(base_url)` enforces HTTPS-only, a hostname, no credentials, and a bare origin (no path/query/fragment). These two are pure functions — safe on the request thread for validation.
- **There is no separate `profile_id`.** `mca_provider_profiles.provider_id` is the sole primary key and the sole public identifier (§9).
- **No `clear_upload_token()` method exists** — token removal is only reachable as a side effect of `remove_or_disable()` (`_delete_upload_token_file`, private). §7.9 requires a new public method (gap).
- **No "check now" method** — the health/info probe lives in `ConnectivityMonitor.refresh(force=True)` (worker-thread only). §7.9 exposes it as a worker command.

### 4.5 Connectivity (`connectivity_monitor.py`)

`InternetStatus` {`unknown`,`online`,`offline`,`limited`}; `RelayState` {`unknown`,`online`,`degraded`,`unreachable`,`identity_mismatch`,`incompatible`,`disabled`}; `UploadReadiness` {`ready`,`upload_token_missing`,`upload_disabled`}; `UploadRejectionReason` (11 values, §10); `UploadDecision(ready, reason, detail)`; `RelayStatus(provider_id, state, upload_readiness, checked_at, latency_ms, error_code)`; `ConnectivitySnapshot(internet, relays)`.

- `snapshot()` and `evaluate_upload_decision()` are the two documented thread-safe reads (no SQLite, no network). `refresh(force=False)` is the **only** method that performs network I/O and is worker-only.
- Probing: `/health` every `RELAY_HEALTH_INTERVAL_SECONDS` (backoff to `RELAY_HEALTH_BACKOFF_CEILING_SECONDS`), `/v1/info` every `RELAY_INFO_MIN_INTERVAL_SECONDS` (identity + protocol re-check), `MAX_CONCURRENT_RELAY_PROBES = 2`, `DEFAULT_TIMEOUT_SECONDS = 5.0`. Per-probe `requests.Session` (never shared), closed deterministically.

### 4.6 Contacts, key exchange, delivery, identity

- `contacts.ContactStatus` {`key_unknown`,`confirmation_required`,`trusted`,`key_changed`}; `contact_status(...)`, `confirm_binding(...)`, `accept_key_change(...)`, `reject_key_change(...)`. **No "list all bindings" method** (keyed by address only — §13).
- `key_exchange.KeyExchangeCoordinator`: `build_key_request()`, `force_announce()`, `get_binding()`, `get_binding_by_key_id()`, `get_status()`, `confirm_tofu()`, `accept_pending_key_change()`, `reject_pending_key_change()`.
- `delivery.DeliveryAdapter` ABC: `adapter_id`, `connector_profile_id`, `capabilities()`, `resolve_route()`, `encode()`, `send()`, `ingest()`. `WireFormat` {`MCA1_TEXT`,`MCA1_CBOR`}; `RouteType` {`DIRECT`,`CHANNEL`,`CHAT`,`MANUAL`}; `ConnectorState` {`READY`,`DEGRADED`,`UNAVAILABLE`}; `AckSemantics` {`NONE`,`BEST_EFFORT`,`CONFIRMED`}; `Route(route_type, route_id, destination_address?)`.
- `identity.MCAPrincipal`: `workspace_id`, `principal_id` (hex16), `key_id` (hex16), `epoch`, `public_identity` (32B), `public_x25519`, `private_key_file`, `created_at`, `status`. `compute_key_id(public_identity)` → hex16.

### 4.7 Relay client (`relay_client.py`)

`RelayClient(base_url, upload_access_token=None, ...)`. Uses a `requests.Session` with a fixed `timeout`; `_url(path) = f"{base_url}{path}"`. **No `allow_redirects=False`, no IP-range/DNS-rebinding validation, no TLS pinning beyond `requests`' default** — this is the precise SSRF surface corrected in §12.

---

## 5. Sub-stage split (Step 1.6A → implementable increments)

Each sub-stage is independently implementable and shippable against the existing domain layer; later stages depend only on §3's facade plumbing.

| Sub-stage | Content | Endpoints | Domain prerequisite |
|---|---|---|---|
| **1.6A.0** | Project-wide CSRF contract (§2.3) + facade plumbing (§3): command queue, attachments/idempotency snapshots, `clear_upload_token()`. | — (infrastructure) | none — prerequisite for every mutation |
| **1.6A.1** | Read-only surface | list, detail, deliveries, connectivity, providers, providers/{id}, upload-readiness, identity, delivery-adapters, connectors, contacts | §3 snapshots |
| **1.6A.2** | Create + idempotency + import | `POST /api/attachments`, `POST /api/mca/import` | `client_request_id`/`canonical_hash` columns + multipart staging (§7.2) |
| **1.6A.3** | Attachment actions | retry, download, save, reject, revoke, delete-local-content, request-key | §3 command queue |
| **1.6A.4** | Provider onboarding & management | probe, register, patch, default, delete, upload-token (PUT/DELETE), check | two-phase bootstrap (§12), `clear_upload_token()` |
| **1.6A.5** | Content download/preview + copy-code (Stage 1) | content, copy-code | pointer persistence for copy-code (§13) |

---

## 6. Endpoint inventory (31)

Legend: **1.6A.N** = sub-stage target; **Stage 1** = within MVP but deferred; **Multi-transport** = Stage 3+ (needs a non-Meshtastic/direct transport).

| # | Method | Endpoint | Class | Stage |
|---|---|---|---|---|
| 1 | GET | `/api/attachments` | read | 1.6A.1 |
| 2 | GET | `/api/attachments/{id}` | read | 1.6A.1 |
| 3 | GET | `/api/attachments/{id}/deliveries` | read | 1.6A.1 |
| 4 | GET | `/api/attachments/{id}/content` | read (file) | 1.6A.5 |
| 5 | GET | `/api/mca/contacts` | read | 1.6A.1 |
| 6 | GET | `/api/mca/delivery-adapters` | read | 1.6A.1 |
| 7 | GET | `/api/mca/connectors` | read | Multi-transport |
| 8 | GET | `/api/mca/providers` | read | 1.6A.1 |
| 9 | GET | `/api/mca/providers/{id}` | read | 1.6A.1 |
| 10 | GET | `/api/mca/providers/{id}/upload-readiness` | read | 1.6A.1 |
| 11 | GET | `/api/mca/connectivity` | read | 1.6A.1 |
| 12 | GET | `/api/mca/identity` | read | 1.6A.1 |
| 13 | POST | `/api/attachments` | mutation | 1.6A.2 |
| 14 | POST | `/api/attachments/{id}/retry` | mutation | 1.6A.3 |
| 15 | POST | `/api/attachments/{id}/download` | mutation | 1.6A.3 |
| 16 | POST | `/api/attachments/{id}/save` | mutation | 1.6A.3 |
| 17 | POST | `/api/attachments/{id}/reject` | mutation | 1.6A.3 |
| 18 | POST | `/api/attachments/{id}/revoke` | mutation | 1.6A.3 |
| 19 | DELETE | `/api/attachments/{id}/local-content` | mutation | 1.6A.3 |
| 20 | POST | `/api/attachments/{id}/deliveries` | mutation | Stage 1 |
| 21 | POST | `/api/attachments/{id}/copy-code` | mutation | Stage 1 |
| 22 | POST | `/api/mca/import` | mutation | 1.6A.2 |
| 23 | POST | `/api/mca/contacts/{contact_id}/request-key` | mutation | 1.6A.3 |
| 24 | POST | `/api/mca/providers/probe` | mutation | 1.6A.4 |
| 25 | POST | `/api/mca/providers` | mutation | 1.6A.4 |
| 26 | PATCH | `/api/mca/providers/{id}` | mutation | 1.6A.4 |
| 27 | POST | `/api/mca/providers/{id}/default` | mutation | 1.6A.4 |
| 28 | DELETE | `/api/mca/providers/{id}` | mutation | 1.6A.4 |
| 29 | PUT | `/api/mca/providers/{id}/upload-token` | mutation | 1.6A.4 |
| 30 | DELETE | `/api/mca/providers/{id}/upload-token` | mutation | 1.6A.4 |
| 31 | POST | `/api/mca/providers/{id}/check` | mutation | 1.6A.4 |

Rows 1–8, 13–23 (19 rows) are the design spec's section-18 endpoints; 4, 9–12, 24–31 (12 rows) are extensions. See §14 for the adaptation list.

---

## 7. Per-endpoint contracts

Every mutation returns `202` + `command_id` (§3.4) unless a synchronous validation error applies. `CSRF: required` on every mutation; `Auth: yes (inherited)` on all. "Read" endpoints return `200` from a snapshot. `State precondition` is validated synchronously from the attachments snapshot.

### 7.1 Reads (1.6A.1)

**`GET /api/attachments`** — list.
- Query: `direction` (`sent`|`received`|`all`, default `all`); `state` (one state or `all`); `filter` (`pending`|`errors`|`saved`|`all`); `limit` (default 100, max 500); `offset` (default 0).
- Response: `{"ok": true, "attachments": [<public projection §7.4>], "total": <int>}`.
- Errors: `400 invalid_direction` / `invalid_state` / `invalid_filter`.

**`GET /api/attachments/{id}`** — detail + timeline.
- Response: `{"ok": true, "attachment": {…§7.4…}, "timeline": [{"event_type", "detail", "created_at"}]}` (timeline from `attachment_events`, redacted per §12).
- Errors: `400 invalid_attachment_id`, `404 attachment_not_found`.

**`GET /api/attachments/{id}/deliveries`** — delivery-route states.
- Response: `{"ok": true, "deliveries": [{"id", "adapter_id", "connector_profile_id", "route_type", "route_id", "state", "external_message_id", "sent_at"}]}`.
- Errors: as above.

**`GET /api/mca/contacts`** — MCA compatibility of known bindings.
- Response: `{"ok": true, "contacts": [{"source_address", "key_id", "status": "trusted|confirmation_required|key_unknown|key_changed"}]}`.
- Gap: needs a new enumeration method (§13).

**`GET /api/mca/delivery-adapters`** — capabilities/state of the one adapter.
- Response: `{"ok": true, "adapters": [{"adapter_id": "meshtastic", "connector_profile_id": "meshtastic", "capabilities": {"wire_formats": ["MCA1_TEXT"], "max_payload_bytes": 180, "supports_direct": true, "supports_channel": false, "supports_incoming": true, "ack_semantics": "CONFIRMED", "connector_state": "READY"}}]}`.

**`GET /api/mca/connectors`** — Multi-transport placeholder; MVP returns the single Meshtastic connector plus its BLE receive-blindness flag.

**`GET /api/mca/providers`** — registry.
- Response: `{"ok": true, "providers": [<public provider projection §7.10>]}` — includes `state`/`upload_readiness`/`latency_ms`/`error_code` joined from the connectivity snapshot by `provider_id`.

**`GET /api/mca/providers/{id}`** — one profile (public projection §7.10). `404 provider_not_found`.

**`GET /api/mca/providers/{id}/upload-readiness`** — delegates to `AttachmentsService.evaluate_upload_readiness()`.
- Query (optional): `ciphertext_bytes`, `requested_ttl_seconds`.
- Response: `{"ok": true, "ready": bool, "reason": "<UploadRejectionReason|null>", "detail": null}`.

**`GET /api/mca/connectivity`** — `{"ok": true, "internet": "...", "relays": {provider_id: {"state","upload_readiness","checked_at","latency_ms","error_code"}}}`.

**`GET /api/mca/identity`** — `{"ok": true, "principal_id", "key_id", "epoch", "fingerprint": "<hex>", "status": "ACTIVE"}`. `fingerprint` is the full-key fingerprint, distinct from the 64-bit `key_id`.

### 7.2 `POST /api/attachments` — create outgoing send (1.6A.2)

- **Content-Type:** `multipart/form-data`. **CSRF:** required. **Idempotency:** `client_request_id` (§3.5).
- **Form parts:**
  - `file` (binary, required): ≤ 5 MiB plaintext (MVP); MIME in the allowlist (jpeg/png/webp/pdf/txt/log/csv/json).
  - `metadata` (JSON string, required): `{client_request_id, recipient: {source_address}, route?, provider_id?, comment?, hard_ttl_seconds?, download_grace_seconds?}`.
- **Validation (synchronous, pure):** `client_request_id` `[A-Za-z0-9_-]{1,64}`; `comment` ≤ 1000 bytes UTF-8; `provider_id` resolves in the provider snapshot; `route_type` must be `DIRECT`; recipient binding must be `trusted` (else `recipient_not_trusted`); `hard_ttl_seconds` within the provider's `[min,max]`.
- **Flow:** the route stages the bytes to `spool/outgoing/<uuid>` (server path, never the client filename), sniffs MIME from magic bytes, then enqueues `CreateDraftCommand` (worker runs `create_draft` + immediate `run_step` + `wake`). Encryption/upload/send happen on the worker.
- **Responses:** `202 {"ok": true, "command_id"}`; idempotent replay `200 {"ok": true, "attachment_id", "state"}`; `409 idempotency_conflict`; `400 mime_not_allowed` / `file_too_large` / `recipient_not_found` / `recipient_not_trusted` / `provider_not_found` / `ttl_out_of_range` / `invalid_metadata`.
- **Radio availability is NOT a precondition.** The draft is created and queued even with no radio connected; only the `READY_TO_SEND → SENT` step needs the radio, and it parks in `READY_TO_SEND` (retryable) until the radio returns (§4.2). No `503 radio_unavailable` here.

### 7.3 Attachment action endpoints (1.6A.3) — command + state matrix

| Endpoint | Command (worker executes) | State precondition | Result |
|---|---|---|---|
| `POST /api/attachments/{id}/retry` | `NudgeAttachmentCommand` → `run_step` | `state ∈ sender.AUTOMATIC_STATES ∪ receiver.AUTOMATIC_STATES` | next step (no new `transfer_id`) |
| `POST /api/attachments/{id}/download` | `BeginDownloadCommand` → `receiver.begin_download` | `WAITING_CONSENT` only | `DOWNLOADING` |
| `POST /api/attachments/{id}/save` | `SaveToFilesCommand` → move `cache/incoming/` → `files/` | `AVAILABLE` only | `saved=true` (idempotent via `unique_file_name`) |
| `POST /api/attachments/{id}/reject` | `RejectCommand` → `receiver.reject` | `WAITING_CONSENT` only | `REJECTED` |
| `POST /api/attachments/{id}/revoke` | `RevokeCommand` → `relay_client.revoke` | `SENT`/`RECEIVED`/`DOWNLOADED` | `REVOKED` |
| `DELETE /api/attachments/{id}/local-content` | `DeleteLocalContentCommand` → delete `files/` copy | `saved=true` | `saved=false` (history kept) |

Common errors: `404 attachment_not_found`; `409 invalid_state_transition` (with the current state) for any violated precondition; `503 relay_unreachable` (revoke at runtime); `409 not_saved` (local-content when `saved=false`).

### 7.4 Public attachment projection (all read endpoints)

```json
{
  "id": "<uuid hex>", "direction": "sent|received", "state": "<state>",
  "file_name": "photo.jpg|null", "mime_type": "image/jpeg|null",
  "plain_size": 12345, "cipher_size": 16728,
  "created_at": 1750000000, "hard_expires_at": 1750259200,
  "download_grace_seconds": 3600, "provider_id": "<base64url>",
  "saved": false, "content_available": false,
  "primary_delivery_id": "<uuid|null>", "error_code": "relay_unreachable|null",
  "recipients": [{"key_id": "<hex16>", "principal_id": "<hex16>"}],
  "deliveries": [ ... ]
}
```

`content_available` is `true` only for a received attachment in `AVAILABLE` (decrypted+verified blob present) or a sent attachment with `saved=true`. **No `saved_path`, no `include_raw`** — both removed from this revision (finding 11). `file_name` is `null` until the manifest is opened for a received attachment (design spec §17.2).

### 7.5 `POST /api/mca/import` (1.6A.2)

- **CSRF:** required. **Content-Type:** `application/json`. **Body:** `{"envelope": "<MCA1:... | base64url CBOR>", "wire_format": "MCA1_TEXT"}`.
- **Flow:** synchronous validation via `codec.peek_message_type` (pure), then enqueue `ImportEnvelopeCommand` → worker ingests through a `MANUAL` route (no real adapter) and dispatches as if it were an inbound event. Signature verification, known-provider resolution, and consent still gate any download.
- **Responses:** `202 {"ok": true, "command_id"}`; `400 invalid_mca_envelope`; `409 duplicate_transfer`; `429 admission_rejected` (per-source/global pending caps).

### 7.6 `POST /api/attachments/{id}/copy-code` (Stage 1)

- **CSRF:** required. **Precondition:** `READY_TO_SEND`/`SENT`/`RECEIVED`/`DOWNLOADED`.
- **Response:** `202` + command; the signed `MCA1-TEXT` is produced by the worker. Gap: the signed pointer is not persisted for re-read today (§13).

### 7.7 `POST /api/attachments/{id}/deliveries` (Stage 1) — add a route without re-upload. No domain method yet (§13).

### 7.8 `POST /api/mca/contacts/{contact_id}/request-key` (1.6A.3)

- **CSRF:** required. **Body:** `{"route": {"adapter_id", "route_id"}}` (optional; defaults to the binding's route).
- **Flow:** `RequestKeyCommand` → `key_exchange.build_key_request()` → send via the adapter. Rate-limited by `key_exchange`'s per-address gate.
- **Responses:** `202`; `429 rate_limited`; `409 key_already_known`; `503 radio_unavailable` (at command runtime).

### 7.9 Provider management (1.6A.4)

All mutations are worker commands (§3.4); synchronous validation uses `normalize_origin`/`compute_provider_id`/key-length checks (pure).

- **`POST /api/mca/providers/probe`** (phase 1 of bootstrap, §12): body `{"base_url"}`. Pure-validation of the origin, then a worker command that resolves DNS, classifies the address, and fetches `/v1/info` (async probe). Returns `202` + `command_id`; the probe **result** is read back via `GET /api/mca/providers/{id}`'s `last_check_result`/`last_error_code` (or a probe-specific read). No persistence.
- **`POST /api/mca/providers`** (phase 2 commit): body `{display_name, base_url, service_public_key (b64url), kind, tls_required, upload_allowed, download_allowed, max_ciphertext_bytes, min_ttl_seconds?, max_ttl_seconds?, protocol_version?, fingerprint_confirmation}`. The server re-derives `provider_id` and requires `fingerprint_confirmation` to match the full-key fingerprint shown in phase 1; a mismatch is `400 provider_id_mismatch`. Enqueues `RegisterProviderCommand` → `register()`.
- **`PATCH /api/mca/providers/{id}`**: partial update of non-identity fields; `CLEAR` (JSON `null` with an explicit `clear: true` companion, or a documented sentinel) clears the TTL/`protocol_version` fields. Enqueues `UpdateProviderCommand` → `update_profile()`. Identity fields (`origin`, `service_public_key`) are **not** editable — changing them is a new registration.
- **`POST /api/mca/providers/{id}/default`**: `SetDefaultProviderCommand` → `set_default()` (transactional single-default).
- **`DELETE /api/mca/providers/{id}`**: `RemoveProviderCommand` → `remove_or_disable()` → returns `{"action": "deleted"|"disabled"}` once applied.
- **`PUT /api/mca/providers/{id}/upload-token`**: body `{"upload_token": "<secret>"}`; `SetUploadTokenCommand` → `set_upload_token()` (0600 file). Response never echoes the token.
- **`DELETE /api/mca/providers/{id}/upload-token`**: `ClearUploadTokenCommand` → new `clear_upload_token()` (§13 gap). Response `upload_token_configured: false`.
- **`POST /api/mca/providers/{id}/check`**: `CheckProviderCommand` → `connectivity.refresh(force=True)` (worker). Response `202`; the fresh status is observable in the connectivity snapshot.

### 7.10 Public provider projection (all provider reads)

```json
{
  "provider_id": "<base64url>", "display_name": "...", "origin": "https://...",
  "service_key_fingerprint": "<hex>", "kind": "own|third_party",
  "tls_required": true, "upload_allowed": true, "download_allowed": true,
  "max_ciphertext_bytes": 5242880, "is_default": true, "enabled": true,
  "min_ttl_seconds": null, "max_ttl_seconds": null, "protocol_version": null,
  "upload_token_configured": false,
  "last_checked_at": null, "last_check_result": null, "last_latency_ms": null,
  "last_error_code": null,
  "state": "online|...", "upload_readiness": "ready|..."
}
```

**No raw `service_public_key` bytes** (replaced by `service_key_fingerprint` = hex SHA-256 of the 32-byte key), no `upload_token_file`, no token.

### 7.11 `GET /api/attachments/{id}/content` (1.6A.5)

- **CSRF:** n/a (GET). **Precondition:** `content_available` (§7.4). **Auth:** yes.
- **Headers:** `X-Content-Type-Options: nosniff` always; `Content-Disposition` = `attachment` for non-preview types, `inline` only for decoded-and-verified allowlisted image/text/PDF.
- **Content-Type:** sniffed MIME for preview types; `application/octet-stream` otherwise.
- **Never serves ciphertext.** Never inline-serves SVG/HTML/JS/archives.
- **Errors:** `404 attachment_not_found`; `409 not_available`; `404 content_missing`.
- Path is resolved by validated filename against the controlled workspace root (the `api_camera.py` screenshot pattern) — one path validation, not two independent resolutions.

---

## 8. State / action matrix (corrected)

Allowed actions per state. `retry` is valid **only** for `AUTOMATIC_STATES`; `REJECTED` and permanent failures (`FAILED_VALIDATION`, `FAILED_UPLOAD`, `FAILED_RADIO`) are **not** retryable — the user re-creates a draft (sender) or the offer is terminal (receiver).

| State | retry | download | save | reject | revoke | copy-code | local-content | cancel* |
|---|---|---|---|---|---|---|---|---|
| DRAFT / VALIDATING / ENCRYPTING / QUEUED_UPLOAD / UPLOADING / READY_TO_SEND | ✓ | — | — | — | — | — | — | ✓ |
| SENT / RECEIVED / DOWNLOADED | — | — | — | — | ✓ | ✓ | ✓(if saved) | — |
| FAILED_VALIDATION / FAILED_UPLOAD / FAILED_RADIO | — | — | — | — | — | — | — | — |
| EXPIRED / REVOKED / CANCELLED | — | — | — | — | — | — | — | — |
| OFFER_RECEIVED | ✓ | — | — | — | — | — | — | — |
| WAITING_KEY / WAITING_PROVIDER / WAITING_NETWORK / DOWNLOADING | ✓ | — | — | — | — | — | — | — |
| WAITING_CONSENT | — | ✓ | — | ✓ | — | — | — | — |
| VERIFYING | ✓ | — | — | — | — | — | — | — |
| AVAILABLE | — | — | ✓ | — | — | ✓ | ✓ | — |
| REJECTED / FAILED / EXPIRED (receiver) | — | — | — | — | — | — | — | — |

`*` cancel is not a section-18 endpoint (the outbound card offers "Отозван", not "Отменён"); listed for completeness only — if a cancel endpoint is ever added it uses `sender.cancel()`, whose own guard (`terminal ∪ {SENT,RECEIVED,DOWNLOADED}`) this row encodes.

`retry` never mutates state directly — it re-dispatches `run_step`/`reconcile_pending` for a state the tick already drives, forcing immediacy (e.g. `READY_TO_SEND` after the radio returns, `QUEUED_UPLOAD` after the network returns). It never mints a new `transfer_id`.

---

## 9. `provider_id` vs `profile_id` — the schema decision

`mca_provider_profiles.provider_id` (Base64URL, 11 chars, derived from `origin + "\n" + service_public_key`) is the **primary key and the sole public identifier** in this contract. There is no separate `profile_id`/autoincrement id exposed anywhere.

- **Why not a local `profile_id`:** the MVP has no operation that needs a stable identity decoupled from the derived `provider_id`. A profile is identified end-to-end (wire OFFER field, `attachments.provider_id`, the Relay's self-reported `/v1/info` `provider_id`, and the registry key) by the same derived value; introducing a second local id would fork the identity space with no consumer.
- **The honest consequence:** changing `origin` or `service_public_key` changes `provider_id`, so "re-key a Relay" is a *new registration* (a fresh trust-bootstrap), never an edit — this is already the behavior `update_profile()` deliberately enforces (it refuses `origin`/`service_public_key`).
- **Open (recorded, not decided):** whether a later stage needs a separate local `profile_id` to keep a stable UI reference across a re-key, or to support multiple profiles for one origin. This is left as an explicit unresolved decision (§15.4); the contract commits to `provider_id`-only for the MVP.

---

## 10. Error code reference

Stable, snake_case, additive.

| Status | `error_code` | Meaning |
|---|---|---|
| 400 | `invalid_metadata` / `invalid_attachment_id` / `invalid_direction` / `invalid_state` / `invalid_filter` | malformed input |
| 400 | `mime_not_allowed` / `file_too_large` | file validation |
| 400 | `recipient_not_found` / `recipient_not_trusted` | binding missing or not `trusted` |
| 400 | `provider_not_found` / `ttl_out_of_range` / `provider_id_mismatch` | provider/registration |
| 400 | `invalid_mca_envelope` | not a parseable MCA message |
| 401 | `auth_required` | inherited (existing) |
| 403 | `csrf_invalid` | CSRF token missing/mismatch (§2.3) |
| 404 | `attachment_not_found` / `contact_not_found` / `provider_not_found` | — |
| 409 | `invalid_state_transition` | action not valid in current state |
| 409 | `idempotency_conflict` | same `client_request_id`, different canonical content |
| 409 | `duplicate_transfer` | `transfer_id` already known |
| 409 | `key_already_known` | request-key for an already-trusted address |
| 409 | `not_saved` | local-content when `saved=false` |
| 429 | `rate_limited` | key-exchange/ACK quota |
| 429 | `admission_rejected` | inbound pending cap |
| 429 | `command_queue_full` | worker command queue at capacity |
| 503 | `radio_unavailable` / `relay_unreachable` | runtime unavailability at an action's execution |

`UploadRejectionReason` maps 1:1 to `error_code`s on upload-readiness: `profile_not_found`, `profile_disabled`, `upload_not_allowed`, `upload_token_missing`, `relay_not_yet_checked`, `relay_unreachable`, `relay_identity_mismatch`, `relay_incompatible`, `ciphertext_too_large`, `ttl_below_minimum`, `ttl_above_maximum`.

---

## 11. Security and privacy protections

1. **Auth on everything** — every endpoint is `/api/`, inheriting `_enforce_auth` (401 `auth_required`).
2. **CSRF** — §2.3, a project-wide prerequisite; mutations fail closed (`403`) until it exists.
3. **Single-owner SQLite** — §3.1; no request thread touches `conn` or the tick lock; a dedicated read-only connection is explicitly rejected.
4. **No absolute paths** — `saved_path` removed from the projection; content served by validated filename against a controlled root.
5. **No secret egress** — upload/revoke tokens, receipt secret, private keys never in a response; `upload_token_configured`/`configured` only; token-write/clear endpoints never echo the token.
6. **No secret logging** — stable `error_code`s, sanitized messages, no plaintext filename/comment/keys/pointers/tokens.
7. **SSRF** — see §12 (corrected): runtime lookups go through `resolve()` (a miss is `None`, no network), but the onboarding origin is browser-supplied and needs DNS/IP/redirect validation that does not exist yet.
8. **Recipient identity never client-derived** — the create endpoint takes a transport address; the server resolves the trusted Ed25519 key from `mca_recipient_bindings` and enforces the TOFU address binding before encrypting (§4.2).
9. **Preview policy** — §7.11; `nosniff` always; inline only for decoded-and-verified allowlisted image/text/PDF; everything else `application/octet-stream` + attachment.
10. **Bounded admission** — inbound caps (per-source/global) and a bounded command queue (§3.2), both surfaced as `429` rather than silent drops.

---

## 12. SSRF: corrected claim, and safe Relay onboarding

**Correction of the earlier revision:** the statement "the browser never supplies a Relay URL — only a provider_id" was **false**. It is true for the *runtime* path (send/receive resolve a pinned origin from `provider_id` via `ProviderRegistry.resolve()`, which returns `None` on a miss and never performs DNS/HTTP). But **onboarding** (`POST /api/mca/providers`, `POST /api/mca/providers/probe`) accepts a browser-supplied `base_url`/`origin` that the server later fetches. The SSRF surface is therefore the onboarding origin plus the worker's subsequent fetch of it.

**What exists today:**
- `normalize_origin()` enforces HTTPS-only, a hostname, no credentials, and a bare origin — pure, request-thread-safe, but does **not** resolve DNS or classify the address.
- `resolve()` never turns an unknown `provider_id` into a network attempt.

**What does not exist (all gaps, §13):**
- DNS resolution + address classification (public vs loopback/link-local/site-local/multicast) at onboarding.
- Redirect pinning: `relay_client` and `connectivity_monitor` call `requests.Session.request(...)` with no `allow_redirects=False`, so a Relay can redirect a request to an internal address (SSRF via redirect). Redirects must be either disabled or re-validated against the pinned origin on every hop.
- DNS-rebinding protection: the resolved IP must be re-resolved and re-validated on each request, not trusted from onboarding time.
- A gate between "origin entered" and "origin persisted" that performs an asynchronous probe before trust is committed.

**Safe onboarding (two-phase, §7.9), required before any provider can be registered:**

1. **Phase 1 — probe (no persistence).** `POST /api/mca/providers/probe` accepts `base_url`. The request thread runs `normalize_origin` (pure). The worker then: resolves DNS; rejects loopback/link-local/site-local/multicast/unspecified addresses unless the operator has opted into the LAN policy (below); fetches `GET {origin}/v1/info` with redirects disabled; compares the Relay's self-reported `provider_id` and `service_public_key` against the values derived from the pinned origin+key; and records the outcome (and a full-key fingerprint) for the UI to show. Nothing is persisted.
2. **Phase 2 — commit (fingerprint confirmation).** `POST /api/mca/providers` requires the fingerprint shown in phase 1 to match (`fingerprint_confirmation`); the server re-derives `provider_id` and persists via `register()` only on match.

The trust-bootstrap trust is thus: **operator-confirmed fingerprint + probe-verified identity**, not a blind origin.

**Open decision (private-IP / self-hosted policy).** A blanket ban on private IPs would break a self-hosted LAN Relay, which the design spec explicitly wants to support. The final policy — whether private/LAN origins are (a) allowed only with an explicit per-profile "this is a LAN relay" opt-in, (b) allowed for `https` with a pinned cert, or (c) gated on a config flag — is **not yet decided** and is recorded in §15.3. The contract states the mechanism (resolve → classify → probe → confirm) and leaves the private-range *policy* open; the implementation must not silently apply either extreme.

---

## 13. Implementation gaps (resolve before/with the named sub-stage)

1. **No request-facing facade / queue / snapshots.** `AttachmentsService` has no CRUD methods and no command queue; the §3 model (attachments snapshot, idempotency snapshot, command queue, `clear_upload_token()`) must be built first (1.6A.0/1).
2. **No `client_request_id` / `canonical_hash` columns** on `attachments` (migration needed) — idempotency (§3.5) cannot work without them.
3. **No multipart staging** — no existing route accepts `multipart/form-data`; spool write, magic-byte sniff, 5 MiB cap are new.
4. **Signed pointer not persisted for re-read** — `copy-code` (§7.6) needs the canonical `MCA1-TEXT` (or its inputs) persisted at commit.
5. **No contact enumeration** — `GET /api/mca/contacts` needs a "list all bindings" method.
6. **No add-route / save-to-files / revoke / clear-token domain methods** — `deliveries` POST, `save`, `revoke`-via-facade, and `clear_upload_token()` all need new worker-executed methods.
7. **SSRF hardening (§12)** — DNS/IP classification, redirect pinning, DNS-rebinding re-validation, async onboarding probe: all absent.
8. **No connector registry** — `connector_profile_id` is a fixed `"meshtastic"` string; `GET /api/mca/connectors` is a Multi-transport placeholder.
9. **CSRF mechanism absent project-wide** (§2.3) — a project prerequisite, not MCAttach-specific.
10. **`MAX_CONTENT_LENGTH` absent** — the 5 MiB upload cap must be enforced server-side (Flask `MAX_CONTENT_LENGTH` or an explicit streaming check), not only by the client.

---

## 14. Section-18 adaptations (deliberate deviations, updated)

1. **Added `GET /api/mca/connectivity`** — the spec implies connectivity state but never names an endpoint; `ConnectivityMonitor.snapshot()` is the ready-made, thread-safe source.
2. **Expanded `GET /api/mca/providers` into a full CRUD set (9, 24–31)** — §17.6's settings list (profiles, default, limits, quotas) requires write routes the single `GET` doesn't provide; `ProviderRegistry` already has every method except `clear_upload_token()` and the probe.
3. **Added `GET /api/mca/identity`** — the principal/fingerprint the UI and the bootstrap flow both need.
4. **`POST /api/attachments` is `multipart/form-data`** — the spec leaves the upload mechanism unstated; multipart is chosen for the MVP (≤ 5 MiB, one request, standard `FormData`).
5. **Recipient is an address, never a key** — the server resolves the trusted key from the TOFU binding; nothing in the audited code accepts a client-derived public key.
6. **All mutations are worker commands (202), reads are snapshots (200)** — the uniform §3.4 model, replacing the earlier mixed sync/async description.
7. **`retry` restricted to `AUTOMATIC_STATES`** — `REJECTED`/`FAILED_*` are terminal, not universally retryable (corrected per finding 10).
8. **`cancel` omitted** — not in §18; the sender-side lifecycle action is `revoke` (Relay-side).
9. **`GET /api/mca/connectors` deferred to Multi-transport** — the MVP has one hardcoded connector.
10. **`retry` does not mint a new `transfer_id`** — the spec's §22.1 rule is pinned into the endpoint contract.
11. **`saved_path` and `include_raw` removed** from responses; replaced by `saved`/`content_available` (§7.4).

---

## 15. Unresolved decisions

1. **Command/query queue architecture** — the exact queue mechanics (single queue vs. separate command and query queues; `MAX_COMMANDS_PER_TICK`; whether create's `attachment_id` is minted at enqueue-time or worker-time) should be chosen **after** measuring the worker tick's current runtime on real hardware. §3 is the required *shape*; the constants are tunable, not fixed.
2. **File upload in one request vs. two-step stage-then-create** — §7.2 commits to one multipart request; the stage-then-create alternative is recorded (better resumability, an extra round-trip) and may be revisited if resumable uploads become a requirement.
3. **Private-IP / self-hosted Relay policy** — §12, left open (blanket ban would break self-hosted LAN Relays).
4. **Whether a separate local `profile_id` is needed** — §9, left open for a later re-key/multi-profile stage.
5. **CSRF mechanism shape** — `SameSite=Strict` alone vs. `SameSite=Lax` + token vs. token-only (§2.3) — a project-level decision, not MCAttach's.
6. **Progress polling cadence / whether list-detail returns a `progress` field** — left to the UI task.

---

## 16. Validation notes

This contract was produced against a full read of: `api/api_auth.py`, `api/api_settings.py`, `api/api_waypoints.py`, `api/api_camera.py`, `server.py` (auth/CSRF/cookie/error/route paths), `meshsrv/attachments/{service,sender,receiver,provider_registry,contacts,identity,key_exchange,workspace,relay_client,codec,manifest,crypto,mime_allowlist,mca_runtime}.py`, `meshsrv/attachments/delivery/{base,meshtastic,fakes}.py`, `meshsrv/attachments/db/migrations.py`, `meshsrv/connectivity_monitor.py`, and design spec sections 12–22. State names, enum values, `AUTOMATIC_STATES`/`TERMINAL_STATES` sets, and method signatures in §4 are verbatim from `main`. Specific verification this revision relied on: `sender.cancel()`'s `terminal ∪ {SENT,RECEIVED,DOWNLOADED}` guard, `sender`'s `FAILED_*` terminal semantics, `receiver.TERMINAL_STATES`/`AUTOMATIC_STATES`, `provider_registry.normalize_origin()`/`compute_provider_id()`/`resolve()` semantics, `ConnectivityMonitor._profile_snapshot` build-swap publication and `refresh(force=)` signature, and `relay_client._request()`'s absent `allow_redirects`.
