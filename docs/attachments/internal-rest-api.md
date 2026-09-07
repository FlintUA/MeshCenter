# MCAttach Internal REST API Contract

**Status:** Design contract (Step 1.6A). This document defines the HTTP surface — it does **not** implement it. No endpoint below exists yet; no runtime code was changed in the Step 1.6A change that added this document.
**Canonical source:** the Russian system design spec (section 18 primary, sections 17/19/20 and the state machines also consulted). That spec is reference-only and is not committed to the repository.
**Audience:** a future implementation task. The contract is written to be precise enough to implement against without re-deriving decisions.

---

## 1. Scope and non-goals

This document specifies the **internal** REST API MeshCenter exposes to its own browser UI for the MCAttach (file-transfer-over-Meshtastic) subsystem. "Internal" means: browser-session-authenticated, same-origin, never a public or machine-to-machine API. It does not specify the **Relay** wire API (that is `docs/architecture/ADR-0003`-adjacent and lives in `meshsrv/attachments/relay_client.py`) and does not specify the **mesh** wire format (ADR-0001 / `meshsrv/attachments/codec.py`).

Everything here is bounded by the MVP narrowing that is already in force across the backend layer:

- **Single transport:** Meshtastic direct text (`MCA1_TEXT`) only. `MeshtasticTextAdapter.capabilities()` reports `supports_channel=False` and `supports_incoming` only for direct messages (design spec §19.1/§19.3).
- **Single recipient:** one sender → one recipient per attachment (design spec §3.2 / Multi-Relay requirements §4). `attachment_recipients` is schema-capable of more than one row, but no code path exercises it; `AttachmentsService._resolve_recipient_identities()`'s TOFU-binding check is scoped to `DIRECT` routes only.
- **No automatic download:** receiving never starts without an explicit user action (design spec §13; `receiver.begin_download()` is never called automatically).
- **No automatic Relay failover, no per-Relay upload quota, no broadcast/group delivery** (Multi-Relay requirements §4).

---

## 2. Conventions inherited from the existing codebase

These are **not** new decisions — they are the conventions already in force in `server.py` / `api/*.py`, which the MCAttach routes must follow for consistency. Each is cited to its source.

### 2.1 Route registration

Follow the DI-by-parameter-list pattern, not Blueprints (CLAUDE.md "Architecture"): one plain function

```python
def register_mca_routes(app, state_lock, handle_errors, mca, ...):
```

called from `server.py`'s `__main__` block, closing over the shared objects it needs. The single object every handler should call through is a request-facing **facade** (see §5), not `server.py`'s raw globals.

### 2.2 Browser-session authentication

A single shared password (no usernames/roles). `api/api_auth.py`'s `before_request` hook returns, for any `/api/` path with a valid session but no `session["authenticated"]`:

```json
{"ok": false, "error": "Authentication required", "error_code": "auth_required"}
```

with HTTP **401**. Every MCAttach endpoint is an `/api/` path and inherits this with **no extra work** — do not add a second auth layer. Exempt paths are only `/login` and `/static/`, so no MCAttach endpoint is reachable unauthenticated.

### 2.3 CSRF

**The existing codebase has no CSRF token mechanism** — there is no `csrf_token` in any JS or Python file, and no `SameSite` override on the session cookie. This is an existing, pre-MCAttach gap, not something Step 1.6A introduces or is expected to fix by itself. The design spec's section 18 requirement "CSRF-защита для mutation endpoints" is therefore **currently unmet across the whole app**, not just MCAttach.

For this contract:

- Mark every **mutation** endpoint `CSRF: required (project-wide gap)`. This records the requirement without pretending the mechanism already exists.
- The implementation task should **not** invent a bespoke MCAttach CSRF token. If CSRF protection is to be added, it must be added once, project-wide (e.g. a session-bound `X-CSRF-Token` checked in a single `before_request` hook and issued to the frontend), and this document flags that as an open decision (§11), not a per-endpoint design detail.

### 2.4 JSON envelope

Two shapes, both already standard:

- **Success:** `{"ok": true, ...}` with domain-specific fields.
- **Error:** `{"ok": false, "error": "<human text>", "error_code": "<snake_case_code>"}` plus the correct HTTP status. Error codes are snake_case: existing examples are `waypoint_not_found`, `invalid_waypoint_id`, `radio_busy`, `radio_released`, `auth_required`, `password_too_short`.

`handle_errors` (server.py:496) already wraps every route and turns an uncaught exception into `{"ok": false, "error": str(e), ...}` with HTTP 500 — MCAttach routes must **not** leak internal exception text; where a domain error has a stable code, catch it and return the stable `error_code` instead of relying on the `str(e)` fallback (see §8.1).

### 2.5 Request body

- JSON bodies: `request.get_json(force=True)` (or `silent=True` where the existing style tolerates a missing body).
- File upload: **`multipart/form-data`** (see §4.2 — this is the one place MCAttach needs a content-type the existing codebase does not yet use).

### 2.6 Status codes

Use the codebase's established vocabulary: `200` (read), `201`/`202` (created/accepted — see §4.2), `400` (validation), `401` (auth, inherited), `404` (not found), `409` (conflict — e.g. wrong state, already exists), `429` (rate-limited), `500` (unexpected), `503` (radio/relay unavailable). Where an action is queued rather than completed synchronously, prefer `202 Accepted` over `200`.

### 2.7 Pagination

The only existing list route (`GET /api/waypoints`) returns everything plus a `total` count and does **not** paginate. For MCAttach's list endpoint, follow that shape (`total` + full list) for the MVP and add `limit`/`offset` as **optional** query params with server-defined defaults, because attachment history can grow unboundedly (metadata retained 90 days by default, design spec §16.5). No cursor-based pagination — out of scope.

### 2.8 Idempotency

Job creation is idempotent by **client request ID** (design spec §18 "создание job идемпотентно по client request ID"). Use a `client_request_id` field (see §4.2). The database already models per-delivery idempotency (`attachment_deliveries.idempotency_key` unique constraint, design spec §16.4), but there is **no** client-request-id column on `attachments` yet — that is a schema gap (§9).

### 2.9 Secrets and absolute paths

- Messenger/Relay credentials (upload tokens, revoke tokens, receipt secrets, private keys) are **never** returned to the browser. They are replaced by a boolean `configured`/`upload_token_configured` flag (design spec §18 "messenger credentials никогда не выдаются browser API и заменяются признаком `configured`").
- Responses must never expose absolute filesystem paths. `saved_path` is returned only as a path **relative to the controlled workspace root**, or omitted entirely in favour of a generated download URL (§4.6). This mirrors `api_camera.py`'s screenshot pattern (serves by validated filename, never by absolute path).
- Never log plaintext filename/comment, keys, full MCA pointers, or bearer/revoke credentials (design spec §22.2).

---

## 3. Domain-layer inventory (what the endpoints call)

All real work lives in `meshsrv/attachments/` and `meshsrv/connectivity_monitor.py`. The endpoints are a thin translation layer over these — they must **never** import the `meshtastic` package, touch the radio directly, or perform Relay/network I/O on the request thread (design spec §19.1/§19.2).

### 3.1 Service and runtime

| Object | Module | Role |
|---|---|---|
| `AttachmentsService` | `meshsrv/attachments/service.py` | The single-owner worker. Public surface today: `start()`, `stop()`, `wake()`, `evaluate_upload_readiness(...)`, `enqueue_inbound(event)`, `tick()`. **It has no CRUD methods** — see §5. |
| `_MCARuntimeState` (singleton) | `meshsrv/attachments/mca_runtime.py` | Builds and holds `conn`, `principal`, `provider_registry`, `connectivity_monitor`, `coordinator` (key exchange), `service`. Reached via `mca_runtime._get_state(data_dir)`. |
| `start_attachments_service()` | `meshsrv/attachments/mca_runtime.py` | Called once from `server.py`'s `start_runtime()` (before the listener thread). |

### 3.2 Sender (`meshsrv/attachments/sender.py`)

States: `DRAFT → VALIDATING → ENCRYPTING → QUEUED_UPLOAD → UPLOADING → READY_TO_SEND → SENT → RECEIVED → DOWNLOADED`, plus terminal `EXPIRED`, `REVOKED`, `CANCELLED`, `FAILED_VALIDATION`, `FAILED_UPLOAD`, `FAILED_RADIO`. `AUTOMATIC_STATES` excludes `SENT`/`RECEIVED`/`DOWNLOADED` and all terminal states.

Key functions (all take a raw `sqlite3.Connection`):

- `create_draft(conn, workspace_manager, principal, *, workspace_id, source_path, file_name, mime_type, recipients, adapter_id, connector_profile_id, route_type, route_id, provider_id, kind, comment, hard_ttl_seconds, download_grace_seconds, now)` → returns `attachment_id`. **Does no file I/O** — `source_path` is recorded, not opened.
- `run_step(conn, *, workspace_manager, principal, recipient_identities, relay_client, delivery_adapter, network_available, attachment_id, now)` → one state transition.
- `cancel(conn, attachment_id)` → `CANCELLED` (refuses terminal states and `SENT`/`RECEIVED`/`DOWNLOADED`).
- `resume_pending(...)`, `on_ack_received(...)`, `on_ack_downloaded(...)`.

`RecipientTarget` = `{public_identity: bytes, key_id: str}` — resolved by the caller from `mca_recipient_bindings`, **not** supplied as a raw public key by the browser.

### 3.3 Receiver (`meshsrv/attachments/receiver.py`)

States: `OFFER_RECEIVED → WAITING_KEY → WAITING_PROVIDER → WAITING_NETWORK → WAITING_CONSENT → DOWNLOADING → VERIFYING → AVAILABLE`, plus terminal `EXPIRED`, `REJECTED`, `FAILED`. `WAITING_CONSENT` is **excluded** from auto-advance; `begin_download()` is the only transition out of it (→ `DOWNLOADING`) and `reject()` (→ `REJECTED`) is the only other action valid there.

Key functions: `handle_offer(...)`, `run_step(...)`, `begin_download(conn, attachment_id)` (only from `WAITING_CONSENT`), `reject(conn, attachment_id)` (only from `WAITING_CONSENT`), `reconcile_pending(...)`.

### 3.4 Provider registry (`meshsrv/attachments/provider_registry.py`)

`ProviderProfile` fields: `provider_id` (Base64URL, 11 chars), `display_name`, `origin`, `service_public_key` (32 bytes), `tls_required`, `upload_allowed`, `download_allowed`, `max_ciphertext_bytes`, `is_default`, `added_at`, `kind` (`own`|`third_party`), `enabled`, `min_ttl_seconds`, `max_ttl_seconds`, `protocol_version`, `upload_token_configured`, `last_checked_at`, `last_check_result`, `last_latency_ms`, `last_error_code`.

Methods: `register(...)`, `set_default(provider_id)`, `update_profile(...)` (with a `CLEAR` sentinel for nullable fields), `record_check_result(...)`, `remove_or_disable(...)`, `list_enabled()`, `get_upload_candidates()`, `get_download_profile(provider_id)`, `set_upload_token(...)`, `get_upload_token(...)`, `resolve(provider_id)` (the SSRF boundary — a miss returns `None`, never a network attempt), `list_providers()`, `get_default()`.

### 3.5 Connectivity (`meshsrv/connectivity_monitor.py`)

- `InternetStatus`: `unknown | online | offline | limited`.
- `RelayState`: `unknown | online | degraded | unreachable | identity_mismatch | incompatible | disabled`.
- `UploadReadiness`: `ready | upload_token_missing | upload_disabled`.
- `UploadRejectionReason`: `profile_not_found | profile_disabled | upload_not_allowed | upload_token_missing | relay_not_yet_checked | relay_unreachable | relay_identity_mismatch | relay_incompatible | ciphertext_too_large | ttl_below_minimum | ttl_above_maximum`.
- `UploadDecision`: `{ready: bool, reason: UploadRejectionReason|None, detail: str|None}`.
- `ConnectivitySnapshot`: `{internet: InternetStatus, relays: {provider_id: RelayStatus}}`; `RelayStatus` = `{provider_id, state, upload_readiness, checked_at, latency_ms, error_code}`.
- Methods: `snapshot()`, `refresh(force=False)`, `can_attempt_relay(provider_id)`, `can_upload_to(provider_id)`, `evaluate_upload_decision(provider_id, *, ciphertext_bytes, requested_ttl_seconds)`.

`snapshot()` and `evaluate_upload_decision()` are documented as **safe to call from a request thread** (they read an atomically-published in-memory snapshot, never SQLite, never network). These are the two connectivity surfaces the REST layer may call directly.

### 3.6 Contacts and key exchange

- `contacts.ContactStatus`: `key_unknown | confirmation_required | trusted | key_changed` (mapped 1:1 from `key_exchange.AddressStatus`).
- `contacts.contact_status(coordinator, source_address)`, `confirm_binding(...)`, `accept_key_change(...)`, `reject_key_change(...)`.
- `key_exchange.KeyExchangeCoordinator`: `build_key_request()`, `force_announce(source_address)`, `get_binding(source_address)`, `get_binding_by_key_id(sender_key_id)`, `get_status(source_address)`, `confirm_tofu(...)`, `accept_pending_key_change(...)`, `reject_pending_key_change(...)`.
- **No "list all bindings" method exists** — contacts are keyed by transport address, and `contacts.py`'s own docstring says a browsable directory is out of scope (§9).

### 3.7 Delivery (`meshsrv/attachments/delivery/`)

- `DeliveryAdapter` ABC: `capabilities()`, `resolve_route(user_selection)`, `encode(logical_message, route)`, `send(wire_payload, route, idempotency_key)`, `ingest(transport_event)`; attributes `adapter_id`, `connector_profile_id`.
- `DeliveryCapabilities`: `wire_formats`, `max_payload_bytes`, `supports_direct`, `supports_channel`, `supports_incoming`, `ack_semantics`, `connector_state`.
- `WireFormat`: `MCA1_TEXT | MCA1_CBOR`. `RouteType`: `DIRECT | CHANNEL | CHAT | MANUAL`. `ConnectorState`: `READY | DEGRADED | UNAVAILABLE`. `AckSemantics`: `NONE | BEST_EFFORT | CONFIRMED`.
- `Route`: `{route_type, route_id, destination_address?}`.

### 3.8 Identity (`meshsrv/attachments/identity.py`)

`MCAPrincipal`: `workspace_id`, `principal_id` (hex 16), `key_id` (hex 16), `epoch`, `public_identity` (32 bytes), `public_x25519` (32 bytes), `private_key_file`, `created_at`, `status` (`ACTIVE`). `compute_key_id(public_identity)` → hex 16.

---

## 4. Endpoint inventory

### 4.0 Stage classification legend

| Tag | Meaning |
|---|---|
| **1.6A** | Target of the Step 1.6A implementation (the REST layer itself). Achievable against the existing domain layer plus the new facade of §5. |
| **Stage 1** | Within the MVP vertical slice but deferred within it (needs a small domain addition). |
| **Multi-transport** | Needs a non-Meshtastic or non-direct transport (MeshCore / Telegram / WhatsApp / channel) — Stage 3+. |

### 4.1 Summary table

| # | Method | Endpoint | Purpose | Stage |
|---|---|---|---|---|
| 1 | GET | `/api/attachments` | List with filters/pagination | 1.6A |
| 2 | POST | `/api/attachments` | Create outgoing send (file + metadata) | 1.6A |
| 3 | GET | `/api/attachments/{id}` | Detail + timeline | 1.6A |
| 4 | POST | `/api/attachments/{id}/retry` | Retry a failed/paused job | 1.6A |
| 5 | POST | `/api/attachments/{id}/download` | Begin receiving from Relay | 1.6A |
| 6 | POST | `/api/attachments/{id}/save` | Save to MeshCenter Files | 1.6A |
| 7 | GET | `/api/attachments/{id}/content` | Authorized download/preview | 1.6A |
| 8 | POST | `/api/attachments/{id}/reject` | Reject an incoming offer | 1.6A |
| 9 | POST | `/api/attachments/{id}/revoke` | Revoke the object on the Relay | 1.6A |
| 10 | DELETE | `/api/attachments/{id}/local-content` | Delete persistent file, keep history | 1.6A |
| 11 | GET | `/api/attachments/{id}/deliveries` | All delivery-route states | 1.6A |
| 12 | POST | `/api/attachments/{id}/deliveries` | Add a route without re-upload | Stage 1 |
| 13 | POST | `/api/attachments/{id}/copy-code` | Get verified `MCA1-TEXT` for manual transfer | Stage 1 |
| 14 | POST | `/api/mca/import` | Import an MCA text/binary envelope | 1.6A |
| 15 | GET | `/api/mca/contacts` | MCA compatibility of contacts | Stage 1 |
| 16 | POST | `/api/mca/contacts/{contact_id}/request-key` | Send `KEY_REQUEST` via a binding | 1.6A |
| 17 | GET | `/api/mca/delivery-adapters` | Adapter capabilities and state | 1.6A |
| 18 | GET | `/api/mca/connectors` | Configured connector profiles | Multi-transport |
| 19 | GET | `/api/mca/providers` | Provider registry (read) | 1.6A |
| 20 | GET | `/api/mca/connectivity` | Connectivity snapshot | 1.6A |
| 21 | POST | `/api/mca/providers` | Register a provider (trust bootstrap) | 1.6A |
| 22 | GET | `/api/mca/providers/{id}` | Provider detail | 1.6A |
| 23 | PATCH | `/api/mca/providers/{id}` | Update provider profile | 1.6A |
| 24 | POST | `/api/mca/providers/{id}/default` | Set default provider | 1.6A |
| 25 | DELETE | `/api/mca/providers/{id}` | Remove or disable provider | 1.6A |
| 26 | PUT | `/api/mca/providers/{id}/upload-token` | Set upload token (secret in, `configured` out) | 1.6A |
| 27 | GET | `/api/mca/providers/{id}/upload-readiness` | Contextual upload decision | 1.6A |
| 28 | GET | `/api/mca/identity` | Local MCA principal (fingerprint/epoch) | 1.6A |

Endpoint 20 (`/api/mca/connectivity`) is the explicit extension required beyond the spec's section 18; 21–28 are the multi-Relay profile management and identity surface the spec's settings section (§17.6) and `ProviderRegistry`/`MCAPrincipal` imply but section 18's single `GET /api/mca/providers` does not cover.

### 4.2 `POST /api/attachments` — create outgoing send

- **Stage:** 1.6A. **Auth:** yes (inherited). **CSRF:** required (project-wide gap).
- **Content-Type:** `multipart/form-data`. **Idempotency:** `client_request_id`.

**Form parts:**

| Part | Type | Required | Limits / notes |
|---|---|---|---|
| `file` | binary | yes | ≤ 5 MiB plaintext (MVP, design spec §16.5); MIME must be in the allowlist (`mime_allowlist.py`): jpeg/png/webp/pdf/txt/log/csv/json. |
| `metadata` | JSON string | yes | A single JSON object (see below). |

**`metadata` fields:**

| Field | Type | Required | Validation |
|---|---|---|---|
| `client_request_id` | string | yes | 1–64 chars, `[A-Za-z0-9_-]`; unique per create (idempotency key, §9 gap). |
| `recipient` | object | yes | `{source_address: string}` — the transport address of the open direct chat. The server resolves the trusted key itself; the browser **never** supplies a raw public key. |
| `route` | object | no | `{adapter_id, connector_profile_id, route_type, route_id}`. Defaults to the sole Meshtastic direct adapter. `route_type` must be `DIRECT` for the MVP. |
| `provider_id` | string | no | Base64URL provider id. Defaults to the registry default. |
| `comment` | string | no | ≤ 1000 bytes UTF-8 (`MAX_COMMENT_BYTES`). |
| `hard_ttl_seconds` | int | no | Within the chosen provider's `[min_ttl_seconds, max_ttl_seconds]`; default 259200 (72 h). |
| `download_grace_seconds` | int | no | Default 3600 (1 h). |

**Flow:** the route stages the uploaded bytes into `spool/outgoing/` (server-controlled path, filename derived from a UUID — never the client filename), sniffs MIME from magic bytes (never trusts the client filename/extension), then calls `sender.create_draft(...)` via the facade with `source_path` = the staged path. Encryption/upload/send happens on the worker thread, not the request thread.

**Responses:**

- `202 Accepted`:
  ```json
  {"ok": true, "attachment_id": "<uuid hex>", "state": "DRAFT"}
  ```
- `400` `mime_not_allowed`, `file_too_large`, `recipient_not_found`, `provider_not_found`, `ttl_out_of_range`, `invalid_metadata`.
- `409` `idempotency_conflict` — a draft for this `client_request_id` already exists; the response body carries the existing `attachment_id`.
- `503` `radio_unavailable` — no radio is currently connected (send cannot be queued for direct delivery).

### 4.3 `GET /api/attachments` — list

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** n/a (read).
- **Query params:** `direction` (`sent`|`received`|`all`, default `all`), `state` (one state or `all`), `filter` (one of `pending`/`errors`/`saved`/`all`), `limit` (default 100, max 500), `offset` (default 0), `include_raw` (`1`/`true`/`yes`, default off — drops protocol-level fields).
- **Response:** `{"ok": true, "attachments": [ ... ], "total": <int>}`. Each item is the public projection (see §4.4) **without** the timeline. No absolute paths; `file_name` is present only where it is already known (sender's file, or a received file that has been decrypted — otherwise `null` per design spec §17.2 "Зашифрованный файл" until the manifest is opened).

### 4.4 `GET /api/attachments/{id}` — detail + timeline

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** n/a.
- **Response:** `{"ok": true, "attachment": {...}, "timeline": [ ... ]}`.

Public projection of `attachments` (see §8.1 for what is **excluded**):

```json
{
  "id": "<uuid hex>",
  "direction": "sent | received",
  "state": "<state>",
  "file_name": "photo.jpg | null",
  "mime_type": "image/jpeg | null",
  "plain_size": 12345,
  "cipher_size": 16728,
  "created_at": 1750000000,
  "hard_expires_at": 1750259200,
  "download_grace_seconds": 3600,
  "provider_id": "<base64url>",
  "primary_delivery_id": "<uuid | null>",
  "error_code": "relay_unreachable | null",
  "recipients": [{"key_id": "<hex16>", "principal_id": "<hex16>"}],
  "deliveries": [ ... ]
}
```

`timeline` is `attachment_events` (`event_type`, `detail`, `created_at`) — redacted per §8.1.

### 4.5 Action endpoints (retry / download / save / reject / revoke / local-content)

| Endpoint | Domain op | Allowed from state(s) | Result state | Notes |
|---|---|---|---|---|
| `POST /api/attachments/{id}/retry` | `sender.run_step` re-entry / re-queue | any **non-terminal** sender state, or `FAILED_*` | next step | Does **not** mint a new `transfer_id` (design spec §22.1). |
| `POST /api/attachments/{id}/download` | `receiver.begin_download` | `WAITING_CONSENT` only | `DOWNLOADING` | 409 otherwise. |
| `POST /api/attachments/{id}/save` | workspace `files/` move | `AVAILABLE` only | (state unchanged; sets `saved_path`) | Copies from `cache/incoming/` to `files/`. |
| `POST /api/attachments/{id}/reject` | `receiver.reject` | `WAITING_CONSENT` only | `REJECTED` | 409 otherwise. |
| `POST /api/attachments/{id}/revoke` | `relay_client.revoke` via facade | `SENT` / `RECEIVED` / `DOWNLOADED` (sent direction) | `REVOKED` | Needs the revoke token (server-side; never returned). |
| `DELETE /api/attachments/{id}/local-content` | delete `saved_path` file | `AVAILABLE` (received) or any sent with `saved_path` | (keeps history; clears `saved_path`) | Deletes the persistent copy only. |

All six return `{"ok": true, "state": "<result-state>"}` on success (or `{"ok": true, ...}` for delete), `404 attachment_not_found`, `409 invalid_state_transition` with the current state, and 5xx codes for Relay/radio failures (§4.7).

### 4.6 `GET /api/attachments/{id}/content` — download/preview

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** n/a (GET). **Content-Type:** `application/octet-stream` for unknown types; the sniffed MIME for allowlisted preview types (image/*, text/*, application/pdf).
- **Headers (mandatory, design spec §20.3):** `Content-Disposition` (attachment for non-preview, inline only for decoded-and-verified images), `X-Content-Type-Options: nosniff`.
- **Behaviour:** serves the **decrypted** file only when the attachment is `AVAILABLE` (or a sent draft whose `saved_path` exists). Never serves ciphertext. SVG/HTML/JS/archives are never auto-previewed. `404 attachment_not_found` / `409 not_available` / `404 content_missing`.
- **Security:** the filename is validated against a controlled root (the `api_camera.py` screenshot pattern — a single `safe_*_path()` validation, not two independent path-resolution steps).

### 4.7 `POST /api/attachments/{id}/copy-code`

- **Stage:** Stage 1. **Auth:** yes. **CSRF:** required (project-wide gap).
- **Purpose:** return the verified, signed `MCA1-TEXT` string so the user can paste it into any messenger/email (design spec §19.5 "Manual share"). Only valid once the pointer exists (`READY_TO_SEND`/`SENT`/`RECEIVED`/`DOWNLOADED`).
- **Response:** `{"ok": true, "mca1_text": "MCA1:..."}`.
- **Gap:** the signed pointer bytes are currently produced at send time and not persisted for re-read; returning them here needs the pointer stored (or re-signable) after commit (§9).

### 4.8 `POST /api/mca/import`

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** required (project-wide gap). **Content-Type:** `application/json`.
- **Body:** `{"envelope": "<MCA1:... | base64url CBOR>", "wire_format": "MCA1_TEXT"}` (or `MCA1_CBOR`).
- **Flow:** the route validates the envelope is an MCA message (`codec.peek_message_type`), wraps it in a `DeliveryEnvelope` with `adapter_id="manual"`, `route_type=MANUAL`, and enqueues an `InboundEvent` on the service's bounded queue. Import **does not** start a download or Relay access without signature verification, a known provider, and user consent (design spec §17.5).
- **Responses:** `202 {"ok": true, "state": "<WAITING_*|OFFER_RECEIVED>"}`; `400 invalid_mca_envelope`; `409 duplicate_transfer` (already-known `transfer_id`); `429 admission_rejected` (per-source/global pending caps).

### 4.9 `GET /api/mca/contacts`

- **Stage:** Stage 1 (the enumeration is the missing piece — §9). **Auth:** yes. **CSRF:** n/a.
- **Response:** `{"ok": true, "contacts": [{"source_address": "...", "key_id": "<hex16|null>", "status": "trusted|confirmation_required|key_unknown|key_changed"}]}`.
- **Gap:** no domain method enumerates all bindings; a facade method must be added.

### 4.10 `POST /api/mca/contacts/{contact_id}/request-key`

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** required (project-wide gap).
- **Body:** `{"route": {"adapter_id": "meshtastic", "route_id": "<node id>"}}` (optional; defaults to the contact's known binding).
- **Flow:** `key_exchange.build_key_request()` → send via the delivery adapter; rate-limited by `key_exchange`'s own per-address gate (`RateLimited`).
- **Responses:** `202 {"ok": true}`; `429 rate_limited`; `409 key_already_known`; `503 radio_unavailable`.

### 4.11 `GET /api/mca/delivery-adapters`

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** n/a.
- **Response:** `{"ok": true, "adapters": [{"adapter_id": "meshtastic", "connector_profile_id": "meshtastic", "capabilities": {wire_formats: ["MCA1_TEXT"], max_payload_bytes: 180, supports_direct: true, supports_channel: false, supports_incoming: true, ack_semantics: "CONFIRMED", connector_state: "READY"}}]}`.
- Backed by `adapter.capabilities()`. For the MVP this is a one-element list.

### 4.12 `GET /api/mca/connectors`

- **Stage:** Multi-transport. **Auth:** yes. **CSRF:** n/a.
- **Purpose:** configured radio/messenger profiles with their MCA capabilities (design spec §17.6 "radio connectors и их MCA capabilities"). For the MVP, returns the single Meshtastic connector and its BLE receive-blindness flag (design spec §19.3 — the UI must surface "Отправка без подтверждения" for BLE). Full multi-connector surface is Stage 3+.

### 4.13 `GET /api/mca/providers` and provider management (19, 21–27)

- `GET /api/mca/providers` (**1.6A**): `{"ok": true, "providers": [<public ProviderProfile>]}`. Public projection excludes `service_public_key` raw bytes (return `service_key_fingerprint` instead), and never includes any token. Includes `state`/`upload_readiness`/`latency_ms`/`error_code` from the connectivity snapshot joined by `provider_id`.
- `POST /api/mca/providers` (**1.6A**): register. Body `{display_name, origin, service_public_key, kind, tls_required, upload_allowed, download_allowed, max_ciphertext_bytes, min_ttl_seconds?, max_ttl_seconds?, protocol_version?}`. Trust bootstrap is admin-driven and **requires explicit full-fingerprint confirmation** (Multi-Relay §3); the `origin` → `provider_id` derivation (`compute_provider_id(origin, service_public_key)`) is server-side, and a mismatch against the client-supplied fingerprint is a `400 provider_id_mismatch`. The built-in-default and signed `.mcaprovider`/QR import paths are separate and not yet exposed (Stage 1+).
- `PATCH /api/mca/providers/{id}` (**1.6A**): partial update; use `CLEAR` (`null` sentinel) to clear nullable TTL/`protocol_version` fields; same validation as `update_profile()`.
- `POST /api/mca/providers/{id}/default` (**1.6A**): `set_default()` — transactional single-default invariant.
- `DELETE /api/mca/providers/{id}` (**1.6A**): `remove_or_disable()` — deletes only if no attachment references it, otherwise disables (`enabled=0`) and returns `{"ok": true, "action": "disabled"}`.
- `PUT /api/mca/providers/{id}/upload-token` (**1.6A**): body `{"upload_token": "<secret>"}`; stored `0600`; response returns `{"ok": true, "upload_token_configured": true}` — the token is **never** echoed back.
- `GET /api/mca/providers/{id}/upload-readiness` (**1.6A**): body-less; delegates to `AttachmentsService.evaluate_upload_readiness(provider_id)`; returns `{"ok": true, "ready": bool, "reason": "<UploadRejectionReason|null>", "detail": null}`. Accepts optional query `ciphertext_bytes` and `requested_ttl_seconds` to surface `ciphertext_too_large` / `ttl_below_minimum` / `ttl_above_maximum`.

### 4.14 `GET /api/mca/connectivity`

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** n/a.
- **Response:** `{"ok": true, "internet": "online|offline|limited|unknown", "relays": {"<provider_id>": {"state": "...", "upload_readiness": "...", "checked_at": ..., "latency_ms": ..., "error_code": null}}}`.
- Backed by `ConnectivityMonitor.snapshot()` — read-only, thread-safe, no SQLite/network on the request thread.

### 4.15 `GET /api/mca/identity`

- **Stage:** 1.6A. **Auth:** yes. **CSRF:** n/a.
- **Response:** `{"ok": true, "principal_id": "<hex16>", "key_id": "<hex16>", "epoch": 0, "fingerprint": "<hex>", "status": "ACTIVE"}`. `fingerprint` is the full public-key fingerprint (`compute_key_id`'s parent, i.e. SHA-256 of the public key), distinct from the 64-bit lookup `key_id`. Backups/rotation of the private key are explicit user actions (design spec §17.6) and **not** part of this read endpoint.

---

## 5. The request-facing facade (the one real architectural addition)

The single-owner SQLite model (ADR-0008 + its PR #231 amendment) means **only `AttachmentsService`'s worker thread may touch `conn`**. The CRUD operations in `sender.py`/`receiver.py` are module-level functions taking a raw `conn` — so a Flask request thread must **not** call them directly.

`AttachmentsService` today exposes `wake()` explicitly as "the only method API handlers are meant to call after a domain-layer action", and `evaluate_upload_readiness()` as "the service-layer surface Step 1.6A's REST endpoints should call". The rest of the CRUD surface does not exist yet.

Step 1.6A therefore requires a **request-facing facade** — either new methods on `AttachmentsService` or a thin companion object that holds the same collaborators — exposing, at minimum:

- read paths: `list_attachments(filters)`, `get_attachment(id)`, `get_timeline(id)`, `get_deliveries(id)`, `get_content_path(id)` (read-only; may use a dedicated read connection or run under the service's lock);
- write paths that enqueue/delegate to the worker rather than touching `conn` from the request thread: `create_draft(...)` (stage file → enqueue), `retry(id)`, `begin_download(id)`, `save_to_files(id)`, `reject(id)`, `revoke(id)`, `delete_local_content(id)`, `import_envelope(...)`, `request_key(...)`;
- provider/connectivity/identity reads (delegate to `ProviderRegistry`/`ConnectivityMonitor`/`MCAPrincipal`).

Every mutating facade method must end by calling `service.wake()` (never block, never network) and return immediately with a `202`. This is the design spec §18 "события прогресса идут через существующий механизм обновления UI либо легкий polling" — the browser polls the list/detail endpoints rather than the API streaming progress.

---

## 6. State / action matrix

Allowed actions per state (sender states above the line, receiver below):

| State | retry | download | save | reject | revoke | copy-code | local-content | cancel* |
|---|---|---|---|---|---|---|---|---|
| DRAFT → READY_TO_SEND (sender) | — | — | — | — | — | — | — | ✓ |
| SENT / RECEIVED / DOWNLOADED | — | — | — | — | ✓ | ✓ | ✓(if saved) | — |
| FAILED_* | ✓ | — | — | — | — | — | — | — |
| EXPIRED / REVOKED / CANCELLED | — | — | — | — | — | — | — | — |
| OFFER_RECEIVED / WAITING_KEY / WAITING_PROVIDER / WAITING_NETWORK | ✓ | — | — | — | — | — | — | — |
| WAITING_CONSENT | ✓ | ✓ | — | ✓ | — | — | — | — |
| DOWNLOADING / VERIFYING | ✓ | — | — | — | — | — | — | — |
| AVAILABLE | — | — | ✓ | — | — | ✓ | ✓ | — |
| REJECTED / FAILED (receiver) | ✓ | — | — | — | — | — | — | — |

`*` cancel is not in the section-18 endpoint list (design spec's outbound card offers "Отозван", not "Отменён"); it is listed here for completeness and is **not** an endpoint in this contract.

`retry` is the universal "nudge the worker" action for any stuck non-terminal state, including receiver `WAITING_*` states — it never mutates state directly, only re-dispatches `run_step`/`reconcile_pending`.

---

## 7. Error code reference

Stable, snake_case, additive. Endpoints return these as `error_code` with the status shown.

| Status | `error_code` | Meaning |
|---|---|---|
| 400 | `invalid_metadata` | malformed `metadata` JSON |
| 400 | `invalid_attachment_id` | id is not a UUID |
| 400 | `mime_not_allowed` | MIME not in `mime_allowlist` |
| 400 | `file_too_large` | plaintext exceeds 5 MiB / provider max |
| 400 | `recipient_not_found` | no binding for the address |
| 400 | `recipient_not_trusted` | binding exists but not confirmed (`confirmation_required`/`key_changed`) |
| 400 | `provider_not_found` | `resolve()` returned `None` |
| 400 | `ttl_out_of_range` | TTL outside provider bounds |
| 400 | `invalid_mca_envelope` | not a parseable MCA message |
| 400 | `provider_id_mismatch` | derived provider id ≠ client fingerprint |
| 401 | `auth_required` | inherited (existing) |
| 404 | `attachment_not_found` / `contact_not_found` / `provider_not_found` | — |
| 409 | `invalid_state_transition` | action not valid in current state |
| 409 | `idempotency_conflict` | `client_request_id` already used |
| 409 | `duplicate_transfer` | `transfer_id` already known |
| 409 | `key_already_known` | `request-key` for an already-trusted address |
| 429 | `rate_limited` | key-exchange / ACK quota |
| 429 | `admission_rejected` | inbound pending cap hit |
| 503 | `radio_unavailable` / `radio_busy` | existing radio vocabulary |
| 503 | `relay_unreachable` | Relay down at call time |

`UploadRejectionReason` values map 1:1 to `error_code`s on the upload-readiness endpoint (`profile_not_found`, `profile_disabled`, `upload_not_allowed`, `upload_token_missing`, `relay_not_yet_checked`, `relay_unreachable`, `relay_identity_mismatch`, `relay_incompatible`, `ciphertext_too_large`, `ttl_below_minimum`, `ttl_above_maximum`).

---

## 8. Security and privacy protections

1. **Auth on everything:** every endpoint is `/api/`, inheriting `_enforce_auth` (401 with `auth_required`).
2. **CSRF:** required on mutations, but the mechanism is a project-wide gap (§2.3, §11).
3. **No absolute paths:** `saved_path` is workspace-relative or absent; content is served by validated name against a controlled root.
4. **No secret egress:** upload/revoke tokens, receipt secret, private keys, and messenger credentials never appear in a response; replaced by `configured` flags. Token-write endpoint returns `upload_token_configured` only.
5. **No secret logging:** `error` text in error envelopes is a sanitized message, not a raw exception; the stable `error_code` is the machine-readable surface. Logs omit plaintext filename/comment, keys, full pointers, tokens (design spec §22.2).
6. **SSRF:** the browser never supplies a Relay URL — only a `provider_id`, resolved server-side through `ProviderRegistry.resolve()` (a miss is `None`, never a network attempt).
7. **Recipient identity never client-derived:** the create endpoint takes a transport address; the server resolves the trusted Ed25519 key from `mca_recipient_bindings` and enforces TOFU-address binding (ADR-0008 §8) before encrypting. A `confirmation_required`/`key_changed`/`key_unknown` contact blocks create with `recipient_not_trusted`.
8. **Preview policy:** `X-Content-Type-Options: nosniff` always; auto-preview only for decoded-and-verified images/text/PDF; everything else `application/octet-stream` attachment disposition (design spec §20.3).
9. **Bounded inbound admission:** import/handle-offer path is capped per-source and globally (already in `receiver.py`); the import endpoint surfaces `admission_rejected` rather than silently dropping.

---

## 9. Implementation gaps (must be resolved before/with Step 1.6A)

This is the honest list of what does **not** exist yet and blocks a literal implementation of the contract. None of these are defects in the current backend; they are the deliberate Step 1.6A boundary.

1. **No request-facing facade.** `AttachmentsService` has no CRUD methods (only `start/stop/wake/evaluate_upload_readiness/enqueue_inbound/tick`). All of §4 requires the §5 facade to be built first.
2. **No `client_request_id` column** on `attachments`. Idempotent creation (§4.2) needs a new schema column + migration (the idempotency constraint today exists only on `attachment_deliveries`).
3. **No file-staging endpoint plumbing.** No existing route accepts `multipart/form-data`; the spool write path, magic-byte MIME sniff on the request thread, and the 5 MiB cap enforcement are all new.
4. **Signed pointer not persisted for re-read.** `copy-code` (§4.7) needs the canonical `MCA1-TEXT` (or its inputs) persisted at commit time so it can be returned on demand.
5. **No contact enumeration.** `contacts.py`/`key_exchange.py` key by address with no "list all bindings" method — `GET /api/mca/contacts` needs one added.
6. **No "add delivery route to existing attachment" operation.** `POST /api/attachments/{id}/deliveries` (Stage 1) has no domain method; `sender.create_draft` only creates deliveries at draft time.
7. **No "save to Files" domain method.** `receiver._step_downloading` writes decrypted bytes to `cache/incoming/`; a `cache → files/` move that sets `saved_path` is a new workspace-level operation.
8. **No revoke exposure.** `relay_client.revoke(transfer_id, revoke_token)` exists, but the revoke token is not surfaced through the facade and no service method invokes it.
9. **CSRF mechanism absent project-wide** (§2.3) — a project decision, not an MCAttach one.
10. **No connector registry.** `connector_profile_id` is a fixed `"meshtastic"` string; `GET /api/mca/connectors` is a placeholder until a real connector registry exists (Multi-transport).

---

## 10. Section-18 adaptations (deliberate deviations)

The spec's §18 inventory was adapted to the audited codebase rather than transcribed. Each deviation is deliberate:

1. **Added `GET /api/mca/connectivity`** — the spec's "provider registry + capabilities" implies connectivity state but never names an endpoint for it; `ConnectivityMonitor.snapshot()` is the ready-made, thread-safe source.
2. **Expanded `GET /api/mca/providers` into a full CRUD set (21–27)** — the spec's §17.6 settings list (provider profiles, default, upload/download limits, quotas) requires write/management routes the single `GET` does not provide; the domain (`ProviderRegistry`) already has every method.
3. **`POST /api/attachments` is `multipart/form-data`, not JSON** — the spec leaves the upload mechanism unstated; multipart is chosen for the MVP (≤5 MiB, single request, standard browser `FormData`), with a JSON `metadata` part carrying the non-file fields. Two-step staging is the recorded alternative (§11).
4. **Recipient is an address, never a key** — the spec's §7.2 form takes a recipient; the contract makes explicit that the browser supplies a transport address and the server resolves the trusted key (TOFU binding, §8.7), because nothing in the audited code accepts a client-derived public key.
5. **`retry` does not mint a new `transfer_id`** — the spec's §22.1 rule is pinned into the endpoint contract.
6. **`cancel` omitted** — not in §18; the sender-side lifecycle action is `revoke` (Relay-side) and the outbound card's "Отозван".
7. **`GET /api/mca/connectors` deferred to Multi-transport** — the MVP has one hardcoded connector; a registry is Stage 3+.

---

## 11. Unresolved decisions (for the implementation task)

1. **File upload in one request vs. two-step stage-then-create.** Chosen: one multipart request. Alternative: `POST /api/attachments/stage` (multipart) → `POST /api/attachments` (JSON referencing the staged token). The alternative gives resumability and clearer idempotency at the cost of a second round-trip and server-side staging-session bookkeeping.
2. **CSRF mechanism shape.** Project-wide token vs. `SameSite=Strict` cookie vs. both — a project-level decision; do not solve it inside MCAttach (§2.3).
3. **Idempotency-key form.** Header `X-Idempotency-Key` vs. the `client_request_id` body field specified here. Chosen: body field (matches the existing codebase's field-in-body style; no header precedent). The implementation may alias both.
4. **Read consistency for list/detail.** A dedicated read-only SQLite connection vs. all reads through the worker lock. Chosen-in-principle: read-only connection for queries (the worker's `conn` is `check_same_thread=False` but single-owner-by-discipline); must be validated against the single-owner model before committing.
5. **`save_to_files` collision semantics.** Whether saving an already-saved attachment is idempotent (`unique_file_name` dedup) or a 409. Chosen: idempotent via `workspace_manager.unique_file_name()`.
6. **Progress polling cadence.** The contract specifies polling (design spec §18); the interval and whether `GET /api/attachments/{id}` returns a `progress` field (upload/download bytes) are left to the UI task, constrained by §22.3's metrics.

---

## 12. Validation notes

This contract was produced against a full read of: `api/api_auth.py`, `api/api_settings.py`, `api/api_waypoints.py`, `api/api_camera.py`, `server.py` (auth/CSRF/error/route-registration paths), `meshsrv/attachments/{service,sender,receiver,provider_registry,contacts,identity,key_exchange,workspace,relay_client,codec,manifest,crypto,mime_allowlist,mca_runtime}.py`, `meshsrv/attachments/delivery/{base,meshtastic,fakes}.py`, `meshsrv/attachments/db/migrations.py`, `meshsrv/connectivity_monitor.py`, and the design spec sections 12–22 (the section-18 endpoint inventory and its surrounding state/storage/UI/security/diagnostics sections). Method names, enum values, and state names in §3 are taken verbatim from those modules as they exist on `main`.
