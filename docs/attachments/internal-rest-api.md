# MCAttach Internal REST API Contract

**Status:** Design contract (Step 1.6A), revised (second pass). The read-only endpoints of sub-stage 1.6A.2 (§5) are implemented in `api/api_attachments.py`. Of the mutation endpoints, the following are implemented (worker handlers in `meshsrv/attachments/service.py`, enqueued from `api/api_attachments.py`): the three Step 1.6A.3A lifecycle actions — `POST /api/attachments/{id}/retry`, `/download`, and `/reject` (§7.3); the Step 1.6A.3B idempotent multipart create — `POST /api/attachments` (§7.2); the two Step 1.6A.3C mutations — `POST /api/attachments/{id}/cancel` (§7.4) and `POST /api/mca/contacts/{contact_id}/request-key` (§7.10); and the eight Step 1.6A.4 provider onboarding & management mutations — `POST /api/mca/providers/probe`, `POST /api/mca/providers`, `PATCH /api/mca/providers/{provider_id}`, `POST /api/mca/providers/{provider_id}/default`, `DELETE /api/mca/providers/{provider_id}`, `PUT`/`DELETE /api/mca/providers/{provider_id}/upload-token`, and `POST /api/mca/providers/{provider_id}/check` (§7.11/§7.12). and the four Step 1.6A.5 content/action endpoints — `GET /api/attachments/{id}/content` (§7.14) plus `POST /api/attachments/{id}/save`, `POST /api/attachments/{id}/revoke`, and `DELETE /api/attachments/{id}/local-content` (§7.3), the latter three as worker commands `attachment_save` / `attachment_revoke` / `attachment_delete_local_content` while content is served synchronously from the internal `ContentDescriptor` (§3.7). The remaining Stage 1 and Multi-transport endpoints (contacts enumeration, manual import, copy-code, add-delivery, connectors) remain design-only and do not exist yet.
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

### 2.3 CSRF — finalized (one mechanism, no alternatives)

The audited codebase has **no CSRF mechanism today**: there is no `csrf_token` in any JS or Python file, and `server.py` sets no `SESSION_COOKIE_SAMESITE`, no `SESSION_COOKIE_HTTPONLY`, and no `MAX_CONTENT_LENGTH` (verified by grep — zero hits for all of these). Because MCAttach introduces the project's first `multipart/form-data` surface and its first write-heavy mutation set, the following project-wide CSRF contract is a **required prerequisite** — implemented once in `server.py`/`api_auth.py`, shared by every feature, not invented per-MCAttach.

The mechanism is **fixed** — a session-bound CSRF token, always required, with a `SameSite=Lax` cookie as a defence-in-depth layer. `SameSite` alone is **not** accepted as CSRF protection (the framework default is not sufficient for state-changing requests), and the earlier "`SameSite=Strict` alone" alternative is removed.

1. **Cookie settings.** `SESSION_COOKIE_HTTPONLY = True`; `SESSION_COOKIE_SAMESITE = "Lax"`; `SESSION_COOKIE_SECURE` **defaults to `False`** in `config.example.py` (dev runs over plain HTTP on the Pi) and is set `True` only behind HTTPS — a single `app.config` change, not MCAttach-specific.
2. **Token issuance.** Mint a per-session random token (≥ 128 bits, `secrets.token_hex`/`token_urlsafe`), store it in the session, and expose it to the page via `<meta name="csrf-token">`. The token is **not** returned by any API read endpoint. Because `AUTH_ENABLED` may be `False` (an unprotected instance has no login to rotate from), issuance is **not login-only**: the token is minted lazily the first time the main app page renders (a `context_processor`), and rotated at login for the authenticated path. A pre-existing session (created before CSRF shipped) gains a token the same lazy way on its next page render.
3. **Token rotation.** Regenerate the token on every successful **login** (discarding any pre-auth token). Logout **validates** the token first, then clears the session (and with it the token). Page refreshes do **not** rotate the token.
4. **Token check.** One `before_request` hook rejects any `POST`/`PUT`/`PATCH`/`DELETE` (and any non-GET/HEAD) `/api/` request whose `X-CSRF-Token` header does not match the session token — compared with `secrets.compare_digest()` (constant-time) — returning `{"ok": false, "error": "CSRF token missing or invalid", "error_code": "csrf_invalid"}` with **403**. GET/HEAD are exempt.
5. **Client usage.** The frontend sends the token as `X-CSRF-Token` on every mutating `fetch`/`XMLHttpRequest`. Custom headers are settable on multipart `FormData` requests, so the `multipart/form-data` create endpoint is covered; JSON and form mutations carry it identically.

Every mutation endpoint in this contract is marked `CSRF: required`. Until the project-wide mechanism above exists, **no MCAttach mutation may ship** — recorded as the first gap in §13 and as sub-stage 1.6A.0 in §5.

### 2.4 JSON envelope

- **Success:** `{"ok": true, ...}` with domain fields.
- **Error:** `{"ok": false, "error": "<human text>", "error_code": "<snake_case_code>"}` plus the correct HTTP status. Codes are snake_case: existing examples `waypoint_not_found`, `radio_busy`, `auth_required`, `password_too_short`.

`handle_errors` (server.py:496) turns an uncaught exception into a 500 envelope; MCAttach routes must catch domain errors and return a stable `error_code` rather than leaking `str(e)` (see §10).

### 2.5 Request body

- JSON: `request.get_json(force=True)`.
- File upload: `multipart/form-data` (see §7.3 — the one place MCAttach needs a content-type the existing codebase does not yet use).

### 2.6 Status codes

`200` (read), `201`/`202` (created/accepted — see §3.4), `400` (validation), `401` (auth, inherited), `403` (CSRF), `404` (not found), `409` (conflict / wrong state / idempotency), `429` (rate-limited / queue full), `500` (unexpected), `503` (radio/relay unavailable at an action's runtime). See §3.4 for the uniform async rule.

### 2.7 Pagination

The only existing list route (`GET /api/waypoints`) returns everything plus `total` and does not paginate. MCAttach's list endpoint keeps `total` and adds `limit`/`offset` as optional query params with server defaults (metadata retained 90 days by default). No cursor pagination.

### 2.8 Idempotency

Job creation is idempotent by **client request ID**. Full semantics are in §3.5 (canonical hash) and §3.6 (atomic reservation); the endpoint contract is §7.3. Identical replay returns the original result; the same `client_request_id` with different canonical content returns **409**.

### 2.9 Secrets, absolute paths, and logging

- Relay credentials (upload/revoke tokens, receipt secrets, private keys) are **never** returned — replaced by `upload_token_configured` / `configured` booleans.
- Responses never expose absolute filesystem paths (§7.5 replaces `saved_path` with a `saved` boolean; file location is an internal `ContentDescriptor`, §3.7).
- Logs omit plaintext filename/comment, keys, full MCA pointers, and tokens.

---

## 3. Threading and data-access model (the architectural core)

This is the one real architectural addition Step 1.6A requires, and it supersedes the earlier revisions' "dedicated read-only connection" idea — which is **rejected**: a read-only connection is still a second thread reaching into SQLite, and routing reads through the worker's `tick` lock would serialize them behind the network-bound tick. The model below preserves the single-owner invariant *and* keeps request threads off both `conn` and the tick lock.

### 3.1 The invariant (already true of the backend)

The single `sqlite3.Connection` is touched by exactly two threads over the process lifetime:

1. the **startup thread** — construction + `migrate()` + the eager `ConnectivityMonitor._refresh_profile_snapshot()` and `sender.resume_pending()`/`receiver.reconcile_pending()` inside `mca_runtime.ensure_service()`;
2. the **worker thread** (`AttachmentsService._run`) — every tick.

`AttachmentsService._lock` exists **only** to make `tick()` re-entrant (its own docstring says so), *not* to arbitrate between two owners. A Flask request thread must therefore never call `tick()`, take `_lock`, or touch `conn`. `ConnectivityMonitor.snapshot()` and `evaluate_upload_decision()` are the worked example: they read an atomically-published in-memory `_profile_snapshot`/`_relay_statuses`, never SQLite — and a dedicated thread-identity test already asserts a simulated REST call performs zero SQLite operations.

### 3.2 Two request-thread-safe surfaces

**Reads — immutable snapshots.** The worker publishes, at the end of each tick (and once at startup), immutable projections built by copy (a fresh `dict`/`list`, swapped in with one reference assignment, atomic under the GIL — the exact pattern `ConnectivityMonitor._refresh_profile_snapshot()` already documents). The request thread reads only these:

- **connectivity snapshot** — exists: `ConnectivityMonitor.snapshot()`.
- **provider snapshot** — exists: `ConnectivityMonitor._profile_snapshot` (private; expose a read-only accessor).
- **attachments snapshot** — **new**: see §3.3.
- **idempotency index snapshot** — **new**: `client_request_id → {attachment_id, canonical_hash, created_at}` (§3.5, §3.6).
- **command-result registry** — **new**: `command_id → CommandResult` (§3.4); a separate thread-safe in-memory registry, **not** a worker-published snapshot (the request thread publishes `queued`, the worker publishes `running`/`succeeded`/`failed`).
- **probe snapshot** — **new**: `probe_id → ProbeRecord` (§7.10).

The two concrete types: **`AttachmentsFacade`** (`facade.py`) is the request thread's only touchpoint — one write (`submit(command)`) plus the read-only accessors, each a lock-guarded read of an in-memory component (never `conn`, never the tick lock, never filesystem/network — §3.1 held by construction). The complete read surface is: the snapshot-backed reads **`attachments_snapshot()`** / **`get_attachment()`** / **`committed_idempotency()`**, and the component reads **`get_command()`** / **`get_probe()`** / **`connectivity_snapshot()`** / **`provider_snapshot()`** / **`evaluate_upload_readiness()`** / **`identity_snapshot()`**. **`CommandDispatcher`** (`dispatch.py`) is the worker's kind→`CommandHandler` table — the *only* extension point for command execution, so `Command` stays a closed typed enumeration with no arbitrary callables in its payload.

The facade also carries an **explicit readiness signal** (a shared `threading.Event`), so a request thread can distinguish "the runtime is not up yet" from "there is nothing to return". Readiness is `true` only once the service has **started** *and* the **first attachments snapshot has published successfully**; it is **cleared** the moment `stop()` runs. `mca_runtime.get_attachments_facade()` returns `None` (an explicit not-ready signal, mapped to `503` by a request handler) until startup has constructed the facade — it never lazy-creates the SQLite runtime from a request thread. Once the facade exists but is not yet ready (startup in progress, or a first publish that failed), the snapshot-backed reads (`attachments_snapshot()`, `get_attachment()`, `committed_idempotency()`) and the write (`submit()`) raise **`FacadeNotReady`** (`error_code: "mca_not_ready"`) — never an empty snapshot, never an empty idempotency mapping, never a false "attachment not found", and `submit()` rejects *without* registering or enqueueing the command — which a request handler maps to **`503 mca_not_ready`**. The pure component reads above are deliberately **not** readiness-gated: they resolve to components that are safe to read from the moment the facade is constructed, so they work even before the first snapshot publishes.

**Writes — a bounded command queue.** Every mutation is an immutable, frozen `Command` dataclass validated on the request thread (using **pure functions only** — no `conn`, no file I/O beyond the create endpoint's spool write, §7.3), then enqueued on a bounded queue the same way the existing inbound queue works:

- `queue.Queue(maxsize=COMMAND_QUEUE_MAXSIZE)`, `put_nowait()`; `queue.Full` → **429** `command_queue_full` (never block the request thread).
- The worker drains up to `MAX_COMMANDS_PER_TICK` commands per tick (before the row-scan) and executes each through the `CommandDispatcher` on its own thread — the single owner. An enumerated kind with no wired handler becomes a terminal **failed** result (`error_code: unsupported_command_kind`); a handler that raises is caught by the drain loop and recorded as **failed** (`error_code: command_execution_failed`). Either way it never kills the worker.
- Command execution does the real domain work: `sender.create_draft()`, `sender.run_step()` (for retry), `receiver.begin_download()`/`reject()`, the Relay revoke, the workspace file move, `ProviderRegistry.register()`/`update_profile()`/`set_default()`/`set_upload_token()`/`remove_or_disable()`, etc.

The set of commands is enumerated per endpoint in §7. `wake()` remains the handler's only post-action call *after* enqueueing (it flags the worker without touching DB/network).

### 3.3 Dedicated worker-owned snapshot publisher (attachments / recipients / deliveries / events)

The attachments snapshot is **not** derived from the worker's existing automatic-state row scan. That scan walks only the bounded set of `AUTOMATIC_STATES` rows the tick must advance and does **not** contain full history, `recipients`, `deliveries`, or the (redacted) event timeline. A **dedicated snapshot publisher** is therefore required: after the worker has drained commands and run state transitions each tick, it performs a read-only pass over:

- `attachments` (the public projection of §7.5),
- `attachment_recipients` (recipient rows joined per attachment),
- `attachment_deliveries` (delivery-route state per attachment),
- `attachment_events` (**safely filtered/redacted**, §11),
- the idempotency index (`client_request_id`, `canonical_hash`, `attachment_id`),

and atomically swaps the results in as fresh snapshots. It is this publisher — not the row scan — that makes `GET /api/attachments`, `GET /api/attachments/{id}` (with timeline) and `GET /api/attachments/{id}/deliveries` correct. The publisher is worker-owned (runs on the worker thread, the single owner of `conn`) and its output is what request threads read.

**Publication cost is bounded.** A full re-read of 90 days of history, `recipients`, `deliveries` and `events` on every tick would be wasteful on a Pi Zero 2 W. The publisher therefore:

- re-publishes **incrementally**: the four projected tables carry AFTER triggers (migration 12) that transactionally record the affected `attachment_id` in a `mca_dirty_attachments` table; the worker drains and deduplicates those dirty ids each tick and rebuilds **only the affected attachment's projection** (and its bounded timeline), never the whole O(N) snapshot. Deleting an attachment removes its projection. The complete immutable snapshot reference is then swapped atomically, so one relevant *rebuild* is O(1) — with one honest caveat: each publish still materializes a fresh immutable container (`records` tuple + `by_id`/`idempotency` dicts), a residual O(N) shallow reference copy measured ~22 ms at N=5000 on a Pi Zero 2 W (vs ~3.5 s for a full build), never the O(N) full re-read. Unrelated MCA writes (ACK quota, Relay health, reply outbox) record no dirty id and never force a rebuild. The first publish is a full `build_attachments_snapshot()`; a full build is retained only as the first-publish path and the legacy un-migrated-DB fallback, not the per-tick path;
- keeps the **list** projection compact — with `recipients`/`deliveries` summarized — and loads the **bounded timeline only for the detail** projection (`GET /api/attachments/{id}`), where the per-attachment event history is bounded **in SQL** to `MAX_DETAIL_EVENTS = 200` (most-recent-first, `occurred_at DESC, id DESC`), never the full 90-day history. Note the precise split: the in-memory `AttachmentRecord` the snapshot holds *always* retains that bounded timeline per attachment (so a detail read needs no extra `conn` pass), while the **list serializer** (`serialize_attachment_public(include_timeline=False)`) **omits the `timeline` field from the JSON entirely** — the timeline is present in the snapshot, absent from the list response, and only the detail serializer (`include_timeline=True`) emits the bounded, redacted one;
- is covered by a **mandatory benchmark** in sub-stage 1.6A.1 (§5) measuring snapshot build time (full and incremental) and memory on target hardware before the read API ships.

### 3.4 Sync vs async: the uniform response model, and the command-result endpoint

This resolves the earlier contradiction between a "blanket 202" and synchronous-looking provider/action endpoints. There are exactly two response classes:

- **Reads** → `200` + snapshot data (or `404`). No command, no queue.
- **Mutations** → `202 Accepted` + `{"ok": true, "command_id": "<uuid>"}`. Every mutation — attachment *and* provider/registry — is a worker command, because the registry and the attachments service share the same single-owner `conn`; a "synchronous" registry write from the request thread would violate §3.1 just as surely as an attachment write would.

Synchronous validation that produces a **4xx** still happens on the request thread (pure functions: `normalize_origin`, `compute_provider_id`, key-length checks, snapshot-state preconditions, probe/fingerprint checks against the probe snapshot) so the client gets an immediate, deterministic error. The persistence is always the deferred command.

**The result of a command is observable, not just inferable.** `202` returns only a `command_id`; the client learns the *outcome* by polling:

```
GET /api/mca/commands/{command_id}
```

which returns the command's status and a safe result payload (§7.9). This is mandatory because several commands can fail without mutating the domain (a failed provider registration creates no provider; a failed save/revoke/delete leaves the attachment unchanged; an upload-token write can fail) — polling the domain snapshots alone cannot distinguish "queued/running" from "failed".

**`CommandRegistry` — a separate thread-safe in-memory store.** The command-result store is **not** a worker-published immutable snapshot (the worker has not necessarily run when the first `GET` arrives). It is a dedicated `CommandRegistry`:

- **request thread** registers `queued` **before** `put_nowait()` (so the worker can never dequeue a command whose registry entry does not yet exist, and a `GET` that lands right away sees `queued`, never `404`);
- **worker** transitions `running` → `succeeded`/`failed` as it dequeues and executes;
- guarded by a short dedicated lock (not the tick lock); **no SQLite**;
- **LRU/TTL eviction only of terminal results** (`succeeded`/`failed`); `queued` and `running` entries are **never** evicted (a client polling an in-flight command must keep seeing it);
- **safe** — `result` payloads contain no secrets, no absolute paths, no ciphertext; `error_code` is a stable snake_case code;
- **internal-recovery fail-safe** — a distinct, registry-owned terminalization path (`record_internal_failure`), separate from the strict lifecycle below. It exists because the *normal* `running → succeeded/failed` transition can itself fail (a worker bug or an impossible state). When a `mark_running`/`mark_succeeded`/`mark_failed` call raises, the worker falls back to this fail-safe, which — under the registry's own lock and **without** going through the strict transition table — (a) recovers a still-`queued`/`running` entry to terminal `failed` with `error_code: command_execution_failed` and no `resource_id`/`result`, inserted into the terminal TTL/LRU bookkeeping; (b) leaves an already-terminal entry unchanged (never overwriting a valid `succeeded`/`failed`); or (c) materializes a *missing* entry as a fresh terminal `failed` from the immutable `Command` metadata. It never exposes the exception text, payload, or command input. This is an **invariant-recovery** path, not an alternative public transition API. It is best-effort, not absolute: if the registry itself is corrupted such that the fail-safe raises, that command cannot be made to produce a terminal polling result — that residual case is logged and documented honestly, never silently claimed as terminal;
- **restart-amnesic** — in-memory only. On restart, all `command_id`s are forgotten and `GET` returns `404 command_not_found`; a command that was still `queued`/`running` never executes, so the client must **re-issue** (or, for `provider_probe`, re-run the probe rather than looking for its result in a domain snapshot). (This is safe: each command is either idempotent — create — or re-drivable — the action endpoints — and the single-owner SQLite transaction model guarantees a crash mid-command leaves a consistent on-disk state.)
- **`command_not_found` does not prove a side effect did not occur.** A command may have been dequeued and executed — including a Relay-facing side effect (upload, revoke, abort) — before the crash wiped the registry. A reissued Relay-facing command must therefore first **reconcile persisted/remote state** (the stored upload-session/revoke-token state of §7.4) and remain **idempotent**, rather than blindly repeating an external call.

Command lifecycle: `queued` (request thread) → `running` (worker) → `succeeded`/`failed` (worker). `command_id` and, for `attachment_create`, `attachment_id` are minted on the request thread (§3.6). This is the **strict** lifecycle: the transition table rejects any other sequence. Separately, the internal-recovery fail-safe (above) terminalizes a command when a *strict transition itself* fails — it does not relax the strict table, and a transition failure that is merely *logged* is never treated as "now terminal": terminality is a registry write (a normal transition or the fail-safe), never a log line.

### 3.5 Idempotency — canonical hash

`create` must answer these cases deterministically even though the write is deferred:

1. **new** `client_request_id` → `202` + `command_id` (and, for create, `attachment_id` — both minted before enqueue, §3.6).
2. **identical replay while the command is still pending** (`queued`/`running`) → `202` + the **same** `command_id`/`attachment_id` with `replayed: true`.
3. **identical replay after success** → `200` + the *original* `attachment_id` (from the committed idempotency index).
4. **same `client_request_id`, different canonical content** → **409** `idempotency_conflict`.

The canonical content hash is **versioned and boundary-unambiguous** — not a concatenation of variable-length fields:

```
canonical_hash = hex(
    SHA-256(
        "MCA-IDEMPOTENCY-v1\0" ||
        file_sha256_ascii ||
        canonical_json_bytes
    )
)
```

- `file_sha256_ascii` = lowercase hex `SHA-256(file bytes)`, computed on the request thread over the spooled bytes during staging.
- `canonical_json_bytes` = UTF-8 of a deterministic JSON serialization (object keys sorted lexicographically, no insignificant whitespace) of the following **complete** semantic field set:

```json
{
  "recipient": {"source_address": "..."},
  "adapter_id": "meshtastic",
  "connector_profile_id": "meshtastic",
  "route_type": "DIRECT",
  "route_id": "<node id>",
  "provider_id": "<base64url>",
  "comment": "<string>",
  "hard_ttl_seconds": 259200,
  "download_grace_seconds": 3600,
  "source_name": "<sanitized file name>",
  "mime_type": "image/jpeg"
}
```

Every field that influences the result is included; `source_name` is the sanitized (safe) filename and `mime_type` the magic-byte-sniffed MIME — both resolved at staging, not trusted from the client filename/extension. This requires two new `attachments` columns (`client_request_id`, `canonical_hash`) — listed in §13.

### 3.6 Idempotency — atomic pending reservation

Two concurrent Flask threads could both observe "this `client_request_id` is absent" and both enqueue a create before the worker publishes a new idempotency snapshot. To close that race, creation reserves the id before enqueueing:

1. **Mint IDs.** On the request thread, generate both `attachment_id` and `command_id` **before** enqueueing (so a replay can return them).
2. **Compute** `canonical_hash` (§3.5).
3. **Reserve.** Under a small dedicated lock, consult an in-memory `pending_reservations: client_request_id → {canonical_hash, attachment_id, command_id}` map (and the committed idempotency index):
   - if already **reserved or committed** with the same `canonical_hash` → idempotent replay: if still pending, return `202` with the **same** `command_id`/`attachment_id` and `replayed: true`; if committed, return `200` with the existing `attachment_id`;
   - if already **reserved or committed** with a different `canonical_hash` → `409 idempotency_conflict`;
   - otherwise → insert the reservation (with all three ids) and release the lock.
4. **Register the command as `queued`** in the `CommandRegistry` (§3.4) — **before** the queue write, so the worker can never dequeue a command whose registry entry does not yet exist.
5. **Enqueue** the command (`put_nowait`). On `queue.Full`, **remove both** the `CommandRegistry` entry **and** the `pending_reservations` entry, then return `429 command_queue_full`. The registration + enqueue + reservation form one submission transaction (Finding 2): a registration failure (e.g. a duplicate `command_id`) also removes the fresh reservation, and any other enqueue failure removes both too — each rollback uses a compare-and-remove (`remove_if_matches`) that never drops a reservation another command now owns, and the worker wake is best-effort (a wake failure cannot turn an accepted enqueue into a failure).
6. Return `202` + `command_id` (+ `attachment_id` for create) **only after** the command was successfully enqueued.

The reservation is transient (in-memory only). The worker, on successfully committing the row, **publishes the committed idempotency entry *before* releasing the in-memory reservation** (`publish-before-release`, Finding 1): it tracks the just-committed create as a `(client_request_id, attachment_id, canonical_hash)` record (not a bare string) and holds its reservation until the snapshot publisher has actually published an idempotency entry matching that exact triple, only then removing it from `pending_reservations`. This covers **both** a newly-inserted row *and* a matching-hash duplicate recovered through the `IntegrityError` path — in the duplicate case the reservation is atomically promoted to the *original* row's `attachment_id` before release, so a replay in the window returns the original attachment, never the colliding request's id nor a fresh enqueue. This closes the commit-to-snapshot race — without it, a concurrent replay arriving in the tiny window between the row commit and the snapshot publish would observe the id as *neither* pending *nor* committed and enqueue a duplicate. On a failed create (nothing committed) the reservation is dropped immediately — no such window exists. On restart, `pending_reservations` is discarded and the committed idempotency index is **restored from the database** (the worker reads `client_request_id`/`canonical_hash`/`attachment_id` from `attachments` at startup). A **unique index on `(workspace_id, client_request_id)`** in `attachments` is the final, database-level backstop against any duplicate that slips past the in-memory reservation (§13).

### 3.7 Internal `ContentDescriptor` (file location without SQLite)

The content route serves a **file from disk**, not a SQLite row. `saved`/`content_available` booleans alone are **not** enough for the request thread to locate the file safely. The snapshot publisher therefore attaches to each attachment an internal, **immutable** `ContentDescriptor`:

```python
ContentDescriptor {
    attachment_id: str,
    locator: str,          # workspace-root-relative path, internal only
    mime_type: str,        # sniffed, or "application/octet-stream"
    disposition: "inline" | "attachment",
    plain_size: int,
}
```

- `locator` is **never serialized to JSON** — it is stripped by the read-projection serializer; only `content_available`/`saved` reach the browser.
- The content route reads the descriptor from the attachments snapshot (pure), re-validates `locator` against the controlled workspace root (the `api_camera.py` screenshot pattern — one path validation, not two independent resolutions), and streams the bytes. No `conn` access.
- Files written by the worker (decrypted `cache/incoming/` blob, or a persisted `files/` copy) are immutable once written, so direct request-thread reads are safe.

---

## 4. Domain-layer inventory (what the worker executes)

All real work lives in `meshsrv/attachments/` and `meshsrv/connectivity_monitor.py`. The endpoints are a thin translation layer; the worker is the only executor. Names below are verbatim.

### 4.1 Service and runtime

- `AttachmentsService` (`service.py`): `start()`, `stop()`, `wake()`, `evaluate_upload_readiness()`, `enqueue_inbound()`, `tick()`. `wake()`'s docstring names it "the only method API handlers … are meant to call after a domain-layer action." The worker's tick drains inbound events, then up to `MAX_COMMANDS_PER_TICK` commands (before the row-scan), then refreshes the snapshot (§3.2).
- `_MCARuntimeState` singleton (`mca_runtime.py`): builds and holds `conn`, `principal`, `provider_registry`, `connectivity_monitor`, `coordinator`, `service`, and the six facade components (`command_queue`, `command_registry`, `pending_reservations`, `probe_registry`, `snapshot_publisher`, `wake_event`) plus `dispatcher`, `facade`, and the shared `ready_event` (the readiness signal §3.2 documents); reached via `mca_runtime._get_state(data_dir)`.
- `start_attachments_service()` (`mca_runtime.py`), called once from `server.py`'s `start_runtime()`; `get_attachments_facade()` returns the request-facing facade, or `None` before startup (never lazy-creating the SQLite runtime, §3.2).
- `AttachmentsFacade` (`facade.py`): the request-thread surface — `submit()` plus the read-only accessors over the in-memory components (`command_queue`, `command_registry`, `pending_reservations`, `probe_registry`, `snapshot_publisher`) and the injected `connectivity_monitor`/`principal` for the pure `connectivity_snapshot()`/`provider_snapshot()`/`evaluate_upload_readiness()`/`identity_snapshot()` reads, with the `FacadeNotReady` (`mca_not_ready`) readiness gate on the snapshot-backed reads and `submit()` (§3.2).
- `CommandDispatcher` / `CommandOutcome` / `CommandHandler` (`dispatch.py`): the worker's kind→handler table; unwired kind → `unsupported_command_kind`, raising handler → `command_execution_failed` (caught by the drain loop, §3.2/§3.4).
- `Command` / `CommandQueue` (`commands.py`), `CommandRegistry` (`command_registry.py`), `PendingReservations` (`idempotency.py`), `ProbeRegistry` (`probe_registry.py`), `AttachmentsSnapshotPublisher` + projection types (`snapshots.py`): the §3.2–§3.7 in-memory plumbing.

### 4.2 Sender (`sender.py`)

States: `DRAFT → VALIDATING → ENCRYPTING → QUEUED_UPLOAD → UPLOADING → READY_TO_SEND → SENT → RECEIVED → DOWNLOADED`; terminal `EXPIRED`, `REVOKED`, `CANCELLED`, `FAILED_VALIDATION`, `FAILED_UPLOAD`, `FAILED_RADIO`.

- `TERMINAL_STATES = {DOWNLOADED, EXPIRED, REVOKED, CANCELLED, FAILED_VALIDATION, FAILED_UPLOAD, FAILED_RADIO}`.
- `AUTOMATIC_STATES = {DRAFT, VALIDATING, ENCRYPTING, QUEUED_UPLOAD, UPLOADING, READY_TO_SEND}`. `SENT`/`RECEIVED` are intentionally excluded (event-driven via `apply_ack()` on a verified inbound ACK — ADR-0009).
- `create_draft(conn, workspace_manager, principal, *, workspace_id, source_path, file_name, mime_type, recipients, adapter_id, connector_profile_id, route_type, route_id, provider_id, kind, comment, hard_ttl_seconds, download_grace_seconds, now)` → `attachment_id`. **Does no file I/O** (`source_path` is recorded, not opened — a draft can exist before the file/radio are ready). **ADR-0009 fail-closed:** each `RecipientTarget` must carry a 32-byte `public_identity` whose `compute_key_id()` equals its `key_id`, and Stage 1 requires exactly one recipient — anything else raises `SenderError`; the identity is then pinned on `attachment_recipients.recipient_public_identity` for later inbound-ACK verification.
- `run_step(...)` → exactly one transition; no-op for terminal/`SENT`/`RECEIVED`/`DOWNLOADED`.
- `cancel(conn, attachment_id)` → `CANCELLED`; raises for terminal states and `SENT`/`RECEIVED`/`DOWNLOADED`. **Note:** `cancel()` does **not** itself clear the spool or revoke an already-committed Relay object — the `cancel` endpoint composes that (see §7.4 and §13).
- `resume_pending(...)`, `apply_ack(conn, attachment_id, message_type, now=None)` → resulting state (ADR-0009). Dispatches `ACK_RECEIVED`/`ACK_DOWNLOADED`/`ACK_PROVIDER_UNKNOWN`; a duplicate/out-of-order/stale ACK is **dropped** (returns the unchanged state), and only an unknown `attachment_id` or a non-ACK `message_type` raises `SenderError`. `ACK_DOWNLOADED` deletes the transient `mca_sender_state` row but **retains** `mca_sender_revoke_state`; `ACK_PROVIDER_UNKNOWN` sets `error_code = recipient_provider_unknown` and is non-terminal (`SENT` is preserved).
- **Retry semantics:** `run_step` is the only forward driver and only makes progress from `AUTOMATIC_STATES`. `FAILED_VALIDATION`/`FAILED_UPLOAD` are terminal by design (retrying unchanged would fail identically forever); `FAILED_RADIO` is reserved/unreached. `READY_TO_SEND` on a radio-send failure stays `READY_TO_SEND` (retryable). So **retry is valid only for `AUTOMATIC_STATES`** — see §8. Retry from a terminal `FAILED_*` is a future state-machine change, not promised here.

`RecipientTarget` = `{public_identity: bytes, key_id: hex16}` — resolved by the caller from `mca_recipient_bindings`, **not** supplied as a raw public key by the browser.

### 4.3 Receiver (`receiver.py`)

States: `OFFER_RECEIVED → WAITING_KEY → WAITING_PROVIDER → WAITING_NETWORK → WAITING_CONSENT → DOWNLOADING → VERIFYING → AVAILABLE`; terminal `EXPIRED`, `REJECTED`, `FAILED`.

- `TERMINAL_STATES = {AVAILABLE, EXPIRED, REJECTED, FAILED}`.
- `AUTOMATIC_STATES = {WAITING_KEY, WAITING_PROVIDER, WAITING_NETWORK, DOWNLOADING}`. `WAITING_CONSENT`, `VERIFYING`, `OFFER_RECEIVED` are **not** automatic.
- `begin_download(conn, id)` → only from `WAITING_CONSENT` (else raises); `reject(conn, id)` → only from `WAITING_CONSENT`. `handle_offer(...)`, `run_step(...)`, `reconcile_pending(...)`.

### 4.4 Provider registry (`provider_registry.py`)

`ProviderProfile` fields: `provider_id` (Base64URL, 11 chars — the table primary key), `display_name`, `origin`, `service_public_key` (32 bytes), `tls_required`, `upload_allowed`, `download_allowed`, `max_ciphertext_bytes`, `is_default`, `added_at`, `kind` (`own`|`third_party`), `enabled`, `min_ttl_seconds`, `max_ttl_seconds`, `protocol_version`, `upload_token_configured`, `last_checked_at`, `last_check_result`, `last_latency_ms`, `last_error_code`.

Methods: `register(...)`, `set_default(provider_id)`, `update_profile(...)` (with a `CLEAR` sentinel for the nullable TTL/`protocol_version` fields), `record_check_result(...)`, `remove_or_disable(provider_id, workspace_manager, principal_id)` (→ `"deleted"`|`"disabled"`), `list_enabled()`, `get_upload_candidates()`, `get_download_profile()`, `set_upload_token(...)`, `get_upload_token(...)`, `clear_upload_token(...)`, `resolve(provider_id)` (the SSRF boundary — a miss returns `None`, no DNS/HTTP), `list_providers()`, `get_default()`.

Key facts for the contract:

- `compute_provider_id(origin, service_public_key)` = Base64URL of the first 8 bytes of `SHA-256(origin + "\n" + raw 32-byte Ed25519 key)`; `normalize_origin(base_url)` enforces HTTPS-only, a hostname, no credentials, and a bare origin (no path/query/fragment). These two are pure functions — safe on the request thread for validation.
- **There is no separate `profile_id`.** `mca_provider_profiles.provider_id` is the sole primary key and the sole public identifier (§9).
- **`clear_upload_token()` now exists** (added in Step 1.6A.1) — the public token-removal method §7.11 requires, no longer only reachable as a side effect of `remove_or_disable()`'s private `_delete_upload_token_file`.
- **No "check now" method** — the health/info probe lives in `ConnectivityMonitor.refresh(force=True)` (worker-thread only). §7.11 exposes it as a worker command.

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

`RelayClient(base_url, upload_access_token=None, ...)`. Uses a `requests.Session` with a fixed `timeout`; `_url(path) = f"{base_url}{path}"`. **No `allow_redirects=False`, no IP-range/DNS-rebinding validation, no TLS pinning beyond `requests`' default** — this is the precise SSRF surface closed by §12.

---

## 5. Sub-stage split (Step 1.6A → implementable increments)

Each sub-stage is independently implementable and shippable against the existing domain layer. Sub-stages 1.6A.0–1.6A.1 are the facade plumbing everything else depends on; 1.6A.2 is the read-only surface; 1.6A.3–1.6A.5 are the mutation surfaces. Contacts enumeration, manual import, copy-code and add-delivery are **later Stage 1** (not part of the 1.6A series), and connectors are **Multi-transport** — those endpoints are excluded from the sub-stages that depend on them.

| Sub-stage | Content | Endpoints | Domain prerequisite |
|---|---|---|---|
| **1.6A.0** | Project-wide CSRF contract (§2.3). | — (infrastructure) | none — prerequisite for every mutation |
| **1.6A.1** | Facade plumbing (§3): command queue + `CommandRegistry`, attachments/provider/idempotency/probe snapshots, dedicated snapshot publisher (§3.3), `ContentDescriptor` (§3.7), idempotency migration + unique index (§3.6), `clear_upload_token()`, and a **mandatory snapshot-cost benchmark** (§3.3). | — (infrastructure; `commands/{command_id}` read lands in 1.6A.2) | none |
| **1.6A.2** | Read-only API | list, detail, deliveries, connectivity, providers, providers/{id}, upload-readiness, identity, delivery-adapters, commands/{command_id} | §3 snapshots |
| **1.6A.3** | Attachment lifecycle — create / download / reject / cancel / retry / request-key | `POST /api/attachments`, `POST /api/attachments/{id}/download`, `POST /api/attachments/{id}/reject`, `POST /api/attachments/{id}/cancel`, `POST /api/attachments/{id}/retry`, `POST /api/mca/contacts/{contact_id}/request-key` | §3 command queue, idempotency (§3.5/§3.6) |
| **1.6A.4** | Provider onboarding & management | probe, register, patch, default, delete, upload-token (PUT/DELETE), check | two-phase bootstrap via `probe_id` (§7.10/§12), `clear_upload_token()` |
| **1.6A.5** | Content / save / revoke | content, save, revoke, delete-local-content | `ContentDescriptor` (§3.7) |
| **Stage 1 (later)** | Contacts enumeration, manual import, copy-code, add-delivery | contacts, import, copy-code, deliveries-POST | contact enumeration method, pointer persistence (§13) |
| **Multi-transport (later)** | Connector registry | connectors | a real connector registry |

---

## 6. Endpoint inventory (33)

Legend: **1.6A.N** = sub-stage target; **Stage 1** = later (within MVP but deferred); **Multi-transport** = Stage 3+ (needs a non-Meshtastic/direct transport).

| # | Method | Endpoint | Class | Stage |
|---|---|---|---|---|
| 1 | GET | `/api/attachments` | read | 1.6A.2 |
| 2 | GET | `/api/attachments/{id}` | read | 1.6A.2 |
| 3 | GET | `/api/attachments/{id}/deliveries` | read | 1.6A.2 |
| 4 | GET | `/api/attachments/{id}/content` | read (file) | 1.6A.5 |
| 5 | GET | `/api/mca/contacts` | read | Stage 1 |
| 6 | GET | `/api/mca/delivery-adapters` | read | 1.6A.2 |
| 7 | GET | `/api/mca/connectors` | read | Multi-transport |
| 8 | GET | `/api/mca/providers` | read | 1.6A.2 |
| 9 | GET | `/api/mca/providers/{id}` | read | 1.6A.2 |
| 10 | GET | `/api/mca/providers/{id}/upload-readiness` | read | 1.6A.2 |
| 11 | GET | `/api/mca/connectivity` | read | 1.6A.2 |
| 12 | GET | `/api/mca/identity` | read | 1.6A.2 |
| 13 | GET | `/api/mca/commands/{command_id}` | read | 1.6A.2 |
| 14 | POST | `/api/attachments` | mutation | 1.6A.3 |
| 15 | POST | `/api/attachments/{id}/retry` | mutation | 1.6A.3 |
| 16 | POST | `/api/attachments/{id}/download` | mutation | 1.6A.3 |
| 17 | POST | `/api/attachments/{id}/save` | mutation | 1.6A.5 |
| 18 | POST | `/api/attachments/{id}/reject` | mutation | 1.6A.3 |
| 19 | POST | `/api/attachments/{id}/cancel` | mutation | 1.6A.3 |
| 20 | POST | `/api/attachments/{id}/revoke` | mutation | 1.6A.5 |
| 21 | DELETE | `/api/attachments/{id}/local-content` | mutation | 1.6A.5 |
| 22 | POST | `/api/attachments/{id}/deliveries` | mutation | Stage 1 |
| 23 | POST | `/api/attachments/{id}/copy-code` | mutation | Stage 1 |
| 24 | POST | `/api/mca/import` | mutation | Stage 1 |
| 25 | POST | `/api/mca/contacts/{contact_id}/request-key` | mutation | 1.6A.3 |
| 26 | POST | `/api/mca/providers/probe` | mutation | 1.6A.4 |
| 27 | POST | `/api/mca/providers` | mutation | 1.6A.4 |
| 28 | PATCH | `/api/mca/providers/{id}` | mutation | 1.6A.4 |
| 29 | POST | `/api/mca/providers/{id}/default` | mutation | 1.6A.4 |
| 30 | DELETE | `/api/mca/providers/{id}` | mutation | 1.6A.4 |
| 31 | PUT | `/api/mca/providers/{id}/upload-token` | mutation | 1.6A.4 |
| 32 | DELETE | `/api/mca/providers/{id}/upload-token` | mutation | 1.6A.4 |
| 33 | POST | `/api/mca/providers/{id}/check` | mutation | 1.6A.4 |

Rows 1–4, 6, 8–10, 14–21, 24–25, 27 (24 rows) correspond to the design spec's section-18 endpoints plus the action lifecycle; 5, 7, 11–13, 22–23, 26, 28–33 (9 rows) are explicit extensions. See §14 for the adaptation list.

---

## 7. Per-endpoint contracts

Every mutation returns `202` + `command_id` (§3.4) unless a synchronous validation error applies; its outcome is then observed via `GET /api/mca/commands/{command_id}` (§7.9). `CSRF: required` on every mutation; `Auth: yes (inherited)` on all. "Read" endpoints return `200` from a snapshot. `State precondition` is validated synchronously from the attachments snapshot.

### 7.1 Reads (1.6A.2)

**`GET /api/attachments`** — list.
- Query: `direction` (`sent`|`received`|`all`, default `all`); `state` (one state or `all`); `filter` (`pending`|`errors`|`saved`|`all`); `limit` (default 100, range 1–500); `offset` (default 0, ≥ 0). A present `limit`/`offset` must be a strict ASCII decimal integer within bounds — malformed or out-of-range values are a hard `400 invalid_pagination`, never silently clamped or coerced to a default.
- Response: `{"ok": true, "attachments": [<public projection §7.5>], "total": <int>}`.
- Errors: `400 invalid_direction` / `invalid_state` / `invalid_filter` / `invalid_pagination`.

**`GET /api/attachments/{id}`** — detail + timeline.
- Response: `{"ok": true, "attachment": {…§7.5…}, "timeline": [{"event_type", "detail", "created_at"}]}` (timeline from `attachment_events`, redacted per §11).
- Errors: `400 invalid_attachment_id`, `404 attachment_not_found`.

**`GET /api/attachments/{id}/deliveries`** — delivery-route states.
- Response: `{"ok": true, "deliveries": [{"id", "adapter_id", "connector_profile_id", "route_type", "route_id", "state", "external_message_id", "sent_at"}]}`.
- Errors: as above.

**`GET /api/mca/contacts`** — MCA compatibility of known bindings. **Stage 1** (needs a new enumeration method, §13).
- Response: `{"ok": true, "contacts": [{"source_address", "key_id", "status": "trusted|confirmation_required|key_unknown|key_changed"}]}`.

**`GET /api/mca/delivery-adapters`** — capabilities/state of the one adapter.
- Response: `{"ok": true, "adapters": [{"adapter_id": "meshtastic", "connector_profile_id": "meshtastic", "capabilities": {"wire_formats": ["MCA1_TEXT"], "max_payload_bytes": 180, "supports_direct": true, "supports_channel": false, "supports_incoming": true, "ack_semantics": "CONFIRMED", "connector_state": "UNKNOWN"}}]}`.
- `connector_state` is always `"UNKNOWN"` on this read endpoint: the real adapter's `ConnectorState` (`READY`/`DEGRADED`/`UNAVAILABLE`) is derived from the live radio transport's `get_connection_info()` (§4.6), which a request thread must not touch (§3.1), and there is no request-thread-safe immutable snapshot of it. It must never be conflated with internet/Relay status (those live in `connectivity` above), and it is never fabricated as `READY`.

**`GET /api/mca/connectors`** — Multi-transport placeholder; MVP returns the single Meshtastic connector plus its BLE receive-blindness flag.

**`GET /api/mca/providers`** — registry.
- Response: `{"ok": true, "providers": [<public provider projection §7.12>]}` — includes `state`/`upload_readiness`/`latency_ms`/`error_code` joined from the connectivity snapshot by `provider_id`.

**`GET /api/mca/providers/{id}`** — one profile (public projection §7.12). The `{id}` is canonical-validated via the registry's own `decode_provider_id`/`encode_provider_id` (round-trip equality), so a padded/wrong-length/wrong-alphabet/non-canonical id is `400 invalid_provider_id`. A canonical-but-unregistered id is `404 provider_not_found`.

**`GET /api/mca/providers/{id}/upload-readiness`** — delegates to `AttachmentsService.evaluate_upload_readiness()`.
- `{id}` is canonical-validated as in the provider detail endpoint (`400 invalid_provider_id`); a canonical-but-unregistered id returns `200` `{"ready": false, "reason": "profile_not_found"}`.
- Query (optional): `ciphertext_bytes` (ASCII decimal, ≥ 0); `requested_ttl_seconds` (ASCII decimal, > 0). A present value that is blank/malformed/signed/fractional/out-of-range — including a zero or negative `requested_ttl_seconds` — is a `400 invalid_query`. `ciphertext_bytes: 0` is valid.
- Response: `{"ok": true, "ready": bool, "reason": "<UploadRejectionReason|null>", "detail": null}`.

**`GET /api/mca/connectivity`** — `{"ok": true, "internet": "...", "relays": {provider_id: {"state","upload_readiness","checked_at","latency_ms","error_code"}}}`.

**`GET /api/mca/identity`** — `{"ok": true, "principal_id", "key_id", "epoch", "fingerprint": "<hex>", "status": "ACTIVE"}`. `fingerprint` is the full-key fingerprint, distinct from the 64-bit `key_id`.

### 7.2 `POST /api/attachments` — create outgoing send (1.6A.3)

- **Content-Type:** `multipart/form-data`. **CSRF:** required. **Idempotency:** `client_request_id` (§3.5/§3.6).
- **Form parts:**
  - `file` (binary, required): ≤ 5 MiB plaintext (MVP); MIME in the allowlist (jpeg/png/webp/pdf/txt/log/csv/json).
  - `metadata` (JSON string, required): `{client_request_id, recipient: {source_address}, route?, provider_id?, comment?, hard_ttl_seconds?, download_grace_seconds?}`.
- **Validation (synchronous, pure):** `client_request_id` `[A-Za-z0-9_-]{1,64}`; `comment` ≤ 1000 bytes UTF-8; `provider_id` resolves in the provider snapshot; `route_type` must be `DIRECT`; `hard_ttl_seconds` within the provider's `[min,max]`; `recipient.source_address` is a non-empty string. **Recipient trust is a two-level check (Finding 7).** The *synchronous* level reads the worker-published immutable recipient-binding snapshot (`facade.recipient_snapshot()`, §3.2) — a read-only `MappingProxyType` keyed by `transport_address`, carrying only public identifiers (never the `public_identity` key bytes) — and rejects with `400 recipient_not_found` (no binding for the address) / `400 recipient_not_trusted` (a binding exists but is not `MCA_READY`). The *worker* level re-reads the authoritative binding from `key_exchange.get_binding` (a SQLite read, §3.1) immediately before draft commit and fails the command with the same codes if the binding was absent or stopped being `MCA_READY` after the snapshot was taken; the synchronous check is fast feedback only, the worker check is authoritative.
- **Provider policy (synchronous, from the immutable snapshot):** the request thread rejects with the *same stable codes and reasons* as `evaluate_upload_decision()`'s local checks, using the immutable provider snapshot (§3.1), in order: an explicit `provider_id` that does not resolve to a profile → `400 provider_not_found` (the create path never returns `invalid_provider_id` — a malformed or non-canonical id simply fails to resolve, exactly like an unregistered one; `invalid_provider_id` is a provider-read-route-only code, §7.1); a disabled profile (`enabled=false`) → `400 provider_disabled`; `upload_allowed=false` → `400 upload_not_allowed`; an unconfigured upload token (`upload_token_configured=false`) → `400 upload_token_missing`. **Reachability is deliberately NOT a hard create precondition**: current Internet/Relay reachability and radio availability are not consulted here — offline creation and queuing remain possible (§7.2 "Radio availability" below), and the relay-state rejection reasons (`relay_*`) never appear as synchronous `400`s on create.
- **Size policy (synchronous):** the staged plaintext must satisfy `ciphertext_size(plain_size) ≤ max_ciphertext_bytes`, where `ciphertext_size(plain_size) = plain_size + chunk_count * TAG_BYTES` (the exact, deterministic data-ciphertext size under the chunk AEAD construction — chunking overhead is fully deterministic, so this is a *conservative upper bound* checked before any encryption, not a post-hoc estimate; the manifest header ciphertext is a separate `manifest_size` object and is correctly excluded). A file whose deterministic ciphertext bound exceeds the limit is rejected `400 ciphertext_too_large` and its staged bytes discarded. The **authoritative post-encryption limit check** — the Relay `create_upload` `total_size` against `max_ciphertext_bytes` — remains in place, unchanged; this pre-encryption bound only rejects earlier and cheaper, it never relaxes that check. The worker-side pre-encryption bound is deliberately best-effort: if the staged spool file is unexpectedly gone at commit time, that check is skipped and the authoritative Relay-side `total_size` check (plus the independent `VALIDATING` failure on a missing source) is the remaining guard — the bound is an early, cheap rejection, never the sole enforcement point.
- **Content validation (full-stream, Finding 8):** MIME is *sniffed* from the first `_SNIFF_HEAD_BYTES` (512) magic bytes, but that head is only the fast path — a `text/*` or `application/json` file is then validated over its **entire** stream via an incremental `TextStreamValidator` (whole-stream UTF-8 + NUL scan, never buffered whole), and a JSON document must parse in full (`validate_json_document` — a leading `{`/`[` is not enough; truncated documents and trailing garbage are rejected). A text-family file whose full content contains a NUL or invalid UTF-8 (including after byte 512), or a JSON file that fails full parse, is rejected `400 mime_not_allowed`; the sniffed head alone never suffices. An **empty (zero-byte) file** has no magic bytes to sniff and is rejected `400 mime_not_allowed` (an empty file can never establish a content type). The staged `file_name` is normalized so its extension matches the *content* MIME (`normalize_file_name_for_mime`), never the reverse; normalization also caps the final `source_name` at 255 Unicode code points (`MAX_SOURCE_NAME_CODE_POINTS`), truncating only the stem so the extension survives and the name stays within the worker's `_bounded_text(..., max_len=255)` re-validation bound.
- **Staging is chunked, never whole-file buffered (Finding 4):** `_stage_spool_file` streams the multipart file in fixed `_STAGING_CHUNK_BYTES` (64 KiB) chunks, so the 5 MiB cap is enforced by *stopping after at most one chunk past it* (`file_too_large`) — an oversized file is never read into memory in full. The total multipart request body is capped per-request via `request.max_content_length` (`_MAX_REQUEST_BYTES` = file cap + metadata cap + 8 KiB slack), so Werkzeug rejects an oversized body with `413 request_too_large` **before** any part is spooled — set per-request, never via the global `MAX_CONTENT_LENGTH`, so no other endpoint's limit is affected. Per-request `request.max_content_length` is a Flask 3.1+ attribute (the 3.0 request object did not expose it), so `requirements.txt` pins `Flask>=3.1.0,<4.0.0` — a 3.0 install would silently skip the bound and fall through to the endpoint's own chunked file-size check. The `metadata` part alone is bounded at `_MAX_METADATA_BYTES` (16 KiB) → `400 metadata_too_large`.
- **Worker-side provider recheck:** immediately before draft commit, `_command_create` re-resolves the provider from the worker-owned registry and re-runs the *same local-configuration* checks (existence, `enabled`, `upload_allowed`, `upload_token_configured`, and the ciphertext size bound). If the profile was removed/disabled or the upload policy/token changed after the request snapshot was taken, the command fails safely (`provider_not_found` / `provider_disabled` / `upload_not_allowed` / `upload_token_missing` / `ciphertext_too_large`) and cleans the staged file — the request accepted from a stale snapshot never proceeds to commit.
- **Worker transaction + orphan cleanup:** `_command_create` runs its draft commit inside a single worker-owned SQLite transaction (§3.1) — any pre-commit failure rolls back with no partial rows, then removes the reservation and the staged spool file, so a failed create leaves nothing behind. Separately, a bounded orphan-staging recovery (`_recover_orphaned_spool`, Finding 5) reclaims `spool/outgoing/` files left unreferenced by a process crash. It runs **cadence-gated**, not on every tick (Finding 5): the first tick after startup runs it, then at most once per `ORPHAN_RECOVERY_CADENCE_SECONDS` (300 s) — so the O(N) referenced-row scan is amortized to ≤ once per 300 s instead of once per tick, while still cleaning promptly after startup. When it does run, it deletes only names matching the two known conventions (a dot-prefixed `.tmp` staging temp, or a bare 32-hex committed id), only once older than `ORPHAN_SPOOL_MIN_AGE_SECONDS`, and only when unreferenced by any persisted row, pending reservation, or queued command; it never recurses or follows symlinks and is capped per tick (`MAX_ORPHAN_SCAN_PER_TICK` / `MAX_ORPHAN_DELETE_PER_TICK`), so an active request's fresh file and a committed attachment's file are both preserved. A failed recovery pass is logged and skipped, never allowing the worker tick to stop.
- **Flow:** the route stages the bytes to `spool/outgoing/<uuid>` (server path, never the client filename), sniffs MIME from magic bytes, mints `attachment_id`/`command_id`, computes `file_sha256` and `canonical_hash` (§3.5), performs the atomic reservation (§3.6), then enqueues an `attachment_create` command. The worker (`AttachmentsService._command_create`) resolves the trusted recipient binding and calls `sender.create_draft()` with the already-minted ids/hash, leaving the row in `DRAFT` for the normal tick to advance; encryption/upload/send happen on the worker. On every non-fresh/failed path the request thread removes the just-staged file it wrote.
- **Responses:** `202 {"ok": true, "command_id", "attachment_id"}`; pending idempotent replay `202 {"ok": true, "command_id", "attachment_id", "replayed": true}` (same ids); committed idempotent replay `200 {"ok": true, "attachment_id", "state"}`; `409 idempotency_conflict`; `400 mime_not_allowed` / `file_too_large` / `metadata_too_large` / `provider_not_found` / `provider_disabled` / `upload_not_allowed` / `upload_token_missing` / `ciphertext_too_large` / `ttl_out_of_range` / `invalid_metadata` / `recipient_not_found` / `recipient_not_trusted`; `413 request_too_large` (total multipart body over the per-request cap, Finding 4); `429 command_queue_full`. `recipient_not_found` / `recipient_not_trusted` are **synchronous** `400`s when the recipient snapshot already lacks a `MCA_READY` binding for the address (Finding 7); a binding that was trusted at snapshot time but stops being `MCA_READY` before commit surfaces as an **asynchronous** command-result error with the same code (§7.9), observed by polling `GET /api/mca/commands/{command_id}` — the worker's `get_binding` re-check is authoritative. A provider removed/disabled or an upload policy/token changed *after* a stale request snapshot was accepted surfaces as an **asynchronous** command failure with the same `provider_not_found` / `provider_disabled` / `upload_not_allowed` / `upload_token_missing` / `ciphertext_too_large` code (§7.9), because the worker re-checks before draft commit.
- **Radio availability is NOT a precondition.** The draft is created and queued even with no radio connected; only the `READY_TO_SEND → SENT` step needs the radio, and it parks in `READY_TO_SEND` (retryable) until the radio returns (§4.2). No `503 radio_unavailable` here.

### 7.3 Attachment action endpoints (retry / download / save / reject / revoke / local-content)

| Endpoint | Command (worker executes) | State precondition | Result |
|---|---|---|---|
| `POST /api/attachments/{id}/retry` | `NudgeAttachmentCommand` → `run_step` | `state ∈ sender.AUTOMATIC_STATES ∪ receiver.AUTOMATIC_STATES` | next step (no new `transfer_id`) |
| `POST /api/attachments/{id}/download` | `BeginDownloadCommand` → `receiver.begin_download` | `WAITING_CONSENT` only | `DOWNLOADING` |
| `POST /api/attachments/{id}/save` | `SaveToFilesCommand` → move `cache/incoming/` → `files/` | `AVAILABLE` only | `saved=true` (genuinely idempotent, §7.3 note) |
| `POST /api/attachments/{id}/reject` | `RejectCommand` → `receiver.reject` | `WAITING_CONSENT` only | `REJECTED` |
| `POST /api/attachments/{id}/revoke` | `RevokeCommand` → `relay_client.revoke` | `SENT`/`RECEIVED`/`DOWNLOADED` | `REVOKED` |
| `DELETE /api/attachments/{id}/local-content` | `DeleteLocalContentCommand` → delete `files/` copy | `saved=true` | `saved=false` (history kept) |

- **Implementation status (Steps 1.6A.3A and 1.6A.5):** all six rows of this table are implemented as worker commands in `meshsrv/attachments/service.py`, enqueued from `api/api_attachments.py` with the same synchronous-validation-then-202 model — `attachment_retry` / `attachment_download` / `attachment_reject` (Step 1.6A.3A), and `attachment_save` / `attachment_revoke` / `attachment_delete_local_content` (Step 1.6A.5).
- **`/reject` is a local state transition only, not a wire-level rejection.** `receiver.reject()` marks the local row `REJECTED` and commits; it does **not** enqueue or send a signed `MessageType.REJECTED` frame back to the sender, so the sender is not yet told about the rejection. Signed `REJECTED` generation, durable outbox delivery, sender-side source/key verification, and sender-state handling are deferred to the still-pending inbound control-message / ADR-0009 work. The sender-to-recipient rejection round trip is therefore **not** complete after Step 1.6A.3A.
- **`/retry` is limited to `AUTOMATIC_STATES`.** Retrying a terminal `FAILED_*` (e.g. `FAILED_UPLOAD`) is a **future** state-machine change and is **not** promised by this contract (§4.2, §13, §15). The endpoint returns `409 invalid_state_transition` for any non-`AUTOMATIC_STATES` state.
- Common errors: `404 attachment_not_found`; `409 invalid_state_transition` for any violated precondition — the synchronous 409 body carries the single safe `state` field with the current state: `{"ok": false, "error": "invalid state transition", "error_code": "invalid_state_transition", "state": "<current state>"}` (only `state` is added — never direction, ids, paths, comments, filenames, keys, tokens, or exception text); `503 relay_unreachable` (revoke at runtime, observed via the command result); `409 not_saved` (local-content when `saved=false`).
- **`/save` is genuinely idempotent, not merely deduplicated.** `unique_file_name()` only resolves a name collision at **first** save — it is **not** an idempotency mechanism (a second save would otherwise create a second copy under a new name). The command instead: if `saved=true` and the file exists → return the prior result (`saved=true`, same `file_name`) **without copying**; if `saved=true` but the file is missing → `content_missing` (or a separately-specified re-save recovery), never a silent duplicate; `unique_file_name()` is applied **only** on the first save to resolve a name conflict.

### 7.4 `POST /api/attachments/{id}/cancel` — cancel an outgoing send (1.6A.3C, implemented)

Cancel the send before it is `SENT`. **Implementation status:** implemented as the worker command `attachment_cancel` (§4.2, `meshsrv/attachments/service.py`), enqueued from `api/api_attachments.py` with the same synchronous-validation-then-202 model as §7.3.

**Synchronous (request thread):** `503 mca_not_ready` when the facade is missing/not ready; `400 invalid_attachment_id` for a non-canonical id (32 lowercase hex); `404 attachment_not_found` when absent from the published snapshot; `409 invalid_state_transition` (with the safe `state` field) unless the row is **outgoing** (`direction == "sent"`) and in `sender.AUTOMATIC_STATES` (`DRAFT`/`VALIDATING`/`ENCRYPTING`/`QUEUED_UPLOAD`/`UPLOADING`/`READY_TO_SEND`) — received rows and terminal / `SENT` / `RECEIVED` / `DOWNLOADED` / `REJECTED` / `EXPIRED` / `REVOKED` / `CANCELLED` rows are never cancellable. Otherwise `202 {"ok": true, "command_id"}`.

**Worker (`_command_cancel`)** re-reads the persisted row (never trusts the snapshot) and runs two halves strictly in order:

1. **Remote first** — decided by persisted `mca_sender_state` (`upload_id`/`revoke_token`), not by the state name: if neither was ever persisted → local-only cancel; if a session exists but `revoke_token` is missing → `relay_unreachable` (cannot safely revoke; the row, sender state, and spool are all preserved); otherwise resolve the Relay client from the row's persisted `provider_id` (never a default) and call `RelayClient.revoke(bytes.fromhex(transfer_id), revoke_token)`. A Relay **404 is confirmed absence** (the object is already gone → the remote half is satisfied); any other `RelayHTTPError`/`RelayError`, or a missing/disabled provider → `relay_unreachable` with the row, sender state, and spool all **preserved** for a manual retry (a committed-but-unreachable object is never silently abandoned).
2. **Local second**, only after the remote half resolves: unlink **only** `spool/outgoing/<attachment_id>` (derived from the validated id, never `saved_path`; a missing spool is clean; an unlink `OSError` → `spool_cleanup_failed` with the row unchanged), clear `saved_path` to `NULL`, then `sender.cancel()` in the same transaction (a `SenderError` from a state that raced out of `AUTOMATIC_STATES` rolls back → `invalid_state_transition`).

Success result (only when both halves completed): state `CANCELLED`, `saved_path` NULL, no `mca_sender_state` row, no spool file, history preserved — `result: {"attachment_id": ..., "state": "CANCELLED"}`.

### 7.5 Public attachment projection (all read endpoints)

```json
{
  "id": "<uuid hex>", "direction": "sent|received", "state": "<state>",
  "file_name": "photo.jpg|null", "mime_type": "image/jpeg|null",
  "plain_size": 12345, "cipher_size": 16728,
  "created_at": 1750000000, "hard_expires_at": 1750259200,
  "download_grace_seconds": 3600, "provider_id": "<base64url>",
  "saved": false, "content_available": false,
  "primary_delivery_id": "<uuid|null>", "error_code": "relay_unreachable|null",
  "counterparty_contact_id": "!1a2b3c4d|null",
  "recipients": [{"key_id": "<hex16>", "principal_id": "<hex16>"}],
  "deliveries": [ ... ]
}
```

`content_available` is `true` only for a received attachment in `AVAILABLE` (decrypted+verified blob present) or a sent attachment with `saved=true`. **No `saved_path`, no `include_raw`, no `ContentDescriptor.locator`** — the file location is internal-only (§3.7). `file_name` is `null` until the manifest is opened for a received attachment (design spec §17.2).

`counterparty_contact_id` is the canonical transport address of the counterparty for that transfer, in the same `!`+8-lowercase-hex contact-id namespace the UI's contact merge builds (`mergeContacts`), so the archive/detail/search views can map an attachment back to a contact. It is derived **only** from persisted routing data, never from `recipient.principal_id` (the recipient's MCA *principal* id — a different 16-hex namespace) nor from `envelope_id` (a key id, not a node id):

- **sent `DIRECT`** → the single applicable `attachment_deliveries.route_id`; exactly one `DIRECT` delivery, else `null` (ambiguous).
- **received `DIRECT`** → the persisted `attachments.reply_route_id`, only when `reply_route_type == "DIRECT"`.

Missing, invalid (wrong length / non-hex), ambiguous, or non-`DIRECT` routing all yield `null`, so the UI falls back to "no contact mapping" rather than guessing. The field is computed in the snapshot publisher (`meshsrv/attachments/snapshots.py`) and is present on **both** the list and detail projections; it is a read-only projection with **no DB migration** (the persisted `recipients`/`deliveries`/`reply_route_*` columns are unchanged).

### 7.6 `POST /api/mca/import` (Stage 1)

- **CSRF:** required. **Content-Type:** `application/json`. **Body:** `{"envelope": "<MCA1:... | base64url CBOR>", "wire_format": "MCA1_TEXT"}`.
- **Flow:** synchronous validation via `codec.peek_message_type` (pure), then enqueue `ImportEnvelopeCommand` → worker ingests through a `MANUAL` route (no real adapter) and dispatches as if it were an inbound event. Signature verification, known-provider resolution, and consent still gate any download.
- **Responses:** `202 {"ok": true, "command_id"}`; `400 invalid_mca_envelope`; `409 duplicate_transfer`; `429 admission_rejected` (per-source/global pending caps).

### 7.7 `POST /api/attachments/{id}/copy-code` (Stage 1)

- **CSRF:** required. **Precondition:** `READY_TO_SEND`/`SENT`/`RECEIVED`/`DOWNLOADED`.
- **Response:** `202` + command; the signed `MCA1-TEXT` is produced by the worker and returned in the command result (`result.mca1_text`). Gap: the signed pointer is not persisted for re-read today (§13).

### 7.8 `POST /api/attachments/{id}/deliveries` (Stage 1) — add a route without re-upload. No domain method yet (§13).

### 7.9 `GET /api/mca/commands/{command_id}` — command result (1.6A.2)

- **Stage:** 1.6A.2. **Auth:** yes. **CSRF:** n/a (GET).
- **Response (`200`):**
  ```json
  {
    "ok": true,
    "command": {
      "command_id": "...",
      "type": "provider_probe",
      "status": "queued|running|succeeded|failed",
      "resource_id": null,
      "result": null,
      "error_code": null,
      "created_at": 0,
      "updated_at": 0
    }
  }
  ```
- **Field semantics:** `type` is the command kind (§3.4 list below); `status` is one of `queued`/`running`/`succeeded`/`failed`; `resource_id` is the primary domain id produced on success (`attachment_id` or `provider_id`), else `null`; `result` is a **safe** payload (no secrets/absolute paths/ciphertext), else `null`; `error_code` is a stable snake_case code when `status=failed`, else `null`.
- **Command types:** `attachment_create`, `attachment_retry`, `attachment_download`, `attachment_save`, `attachment_reject`, `attachment_cancel`, `attachment_revoke`, `attachment_delete_local_content`, `attachment_import`, `attachment_copy_code`, `attachment_add_delivery`, `contact_request_key`, `provider_probe`, `provider_register`, `provider_update`, `provider_set_default`, `provider_remove`, `provider_set_upload_token`, `provider_clear_upload_token`, `provider_check`.
- **Safe `result` examples:** `attachment_create` → `{"attachment_id"}`; `provider_register` → `{"provider_id"}`; `provider_probe` → `{"probe_id", "provider_id", "origin", "service_key_fingerprint", "protocol_version", "max_ciphertext_bytes", "min_ttl_seconds", "max_ttl_seconds", "expires_at"}`; `provider_remove` → `{"action": "deleted"|"disabled"}`.
- **Errors:** `400 invalid_command_id`; `404 command_not_found` (unknown id, expired, or lost to restart). After a restart the client falls back to polling the domain snapshots (§3.4).
- **Bounded/immutable/restart behavior:** §3.4.

### 7.10 `POST /api/mca/contacts/{contact_id}/request-key` (1.6A.3C, implemented)

Ask a contact to announce its MCA key over the fixed DIRECT route. **Implementation status:** implemented as the worker command `contact_request_key` (§4.2, `meshsrv/attachments/service.py`), enqueued from `api/api_attachments.py`.

- **Contact id (Stage 1):** the canonical Meshtastic transport address — `!` followed by exactly 8 lowercase hex digits (e.g. `!756f9960`). Any other shape → `400 {"ok": false, "error": "invalid contact id", "error_code": "invalid_contact_id"}`.
- **CSRF:** required. **Body:** empty, or `{"route": {"adapter_id": "meshtastic", "route_id": "<contact_id>"}}`. A missing `route` defaults to DIRECT (the only supported route); a supplied `route` must name `adapter_id == "meshtastic"` and `route_id == contact_id` — anything else → `400 invalid_contact_id`.
- **Synchronous (request thread):** `503 mca_not_ready`; `400 invalid_contact_id`; `409 key_already_known` when the snapshot binding is already `MCA_READY`; otherwise `202 {"ok": true, "command_id"}`. `KEY_UNKNOWN`/`KEY_UNVERIFIED`/`KEY_CHANGED` may request.
- **Worker (`_command_request_key`)** re-validates id/adapter/route (never trusts the queue), re-reads the live binding (a contact that became `MCA_READY` since the snapshot read → `key_already_known`), checks the persisted outgoing rate limit, then `build_key_request()` + `encode()` + `send()` over a fixed DIRECT `Route(route_type=DIRECT, route_id=contact_id, destination_address=contact_id)` with `command.command_id` as the idempotency key. A missing delivery adapter, a `DeliveryError`, or a receipt with `sent != True` → `radio_unavailable` (no quota consumed). On `sent == True`, the quota timestamp is persisted and the result is `{"contact_id": ..., "status": "requested"}`.
- **Rate limit:** one accepted outgoing key request per `(workspace_id, adapter_id, source_address)` per 600 s, persisted in `mca_key_exchange_contact_state.last_request_sent_at` (migration 13) so it survives a restart; the timestamp is recorded **only after** `DeliveryReceipt.sent == True`, so a failed send never consumes the quota. A second request inside the window → `rate_limited` (command result).
- **Responses:** `202`; `400 invalid_contact_id`; `409 key_already_known`; `429 command_queue_full`; command-result failures `rate_limited` / `radio_unavailable`.

### 7.11 Provider onboarding — two-phase via `probe_id` (1.6A.4, implemented)

Onboarding is **two-phase and trust-anchored on a single-use probe record**, so registration never re-trusts Relay parameters supplied by the browser. All mutations are worker commands (§3.4); synchronous validation uses pure functions and the probe snapshot.

**Phase 1 — `POST /api/mca/providers/probe`.**
- **Body:** `{"base_url": "<origin>"}`. **CSRF:** required.
- **Synchronous validation:** `normalize_origin(base_url)` (HTTPS-only, hostname, no credentials, bare origin) — pure; `400 invalid_origin` on failure.
- **Worker command** (`provider_probe`): resolves DNS, classifies the address (§12), fetches `GET {origin}/v1/info` with redirects disabled, compares the Relay's self-reported identity, and records a **`ProbeRecord`**:
  ```
  ProbeRecord {
      probe_id,               # short-lived, single-use
      origin,                 # normalized
      provider_id,            # computed from origin + fetched key
      service_public_key,     # fetched from /v1/info
      service_key_fingerprint, # hex SHA-256 of the key (for user confirmation)
      protocol_version,       # advertised
      max_ciphertext_bytes,   # advertised limit
      min_ttl_seconds, max_ttl_seconds,  # advertised limits
      expires_at,             # short TTL (minutes)
      status                  # probed | failed
  }
  ```
  Nothing is persisted to SQLite. The record lives in the **probe snapshot** (§3.2) and expires after a short TTL.
- **Result:** the `provider_probe` command result carries `{probe_id, provider_id, origin, service_key_fingerprint, protocol_version, max_ciphertext_bytes, min_ttl_seconds, max_ttl_seconds, expires_at}` (§7.9) — the browser shows the fingerprint and hands `probe_id` + `fingerprint_confirmation` to phase 2. A failed probe returns `error_code` (`relay_unreachable`, `relay_identity_mismatch`, `relay_incompatible`, `origin_not_routable`, …).

**Phase 2 — `POST /api/mca/providers`.**
- **Body:** `{probe_id, display_name, policy: {kind, upload_allowed, download_allowed, max_ciphertext_bytes?}, fingerprint_confirmation}`.
- **What the browser does NOT supply:** `base_url`/`origin`, `service_public_key`, `protocol_version`, `min_ttl_seconds`/`max_ttl_seconds`, the derived `provider_id`, and `tls_required`. All of those come from the `ProbeRecord`, so a corrupted frontend cannot fabricate a consistent set — it can only reference a probe the server already performed.
- **`tls_required` is not browser-controllable.** For the globally-routable HTTPS-only MVP, `tls_required` is **always `true`** with TLS certificate verification **always enabled**; the policy body cannot set it to `false`. Relaxing this is only possible via a future ADR (e.g. the deferred LAN-Relay advanced mode).
- **Synchronous validation (pure, against the probe snapshot):** `probe_id` must exist and be unexpired (`400 probe_id_not_found` / `400 probe_id_expired`); `fingerprint_confirmation` must equal the probe's `service_key_fingerprint` (`400 provider_id_mismatch`); `max_ciphertext_bytes` (if supplied) must not exceed the probe's advertised limit.
- **Worker command** (`provider_register`): atomically **checks-and-consumes** `probe_id` (marks it used) and calls `register()` with the probe's fields. A second register with the same `probe_id` fails with `probe_id_used` in the command result.
- **Responses:** `202 {"ok": true, "command_id"}`; `400 probe_id_not_found` / `probe_id_expired` / `provider_id_mismatch` / `invalid_metadata`.

### 7.12 Remaining provider management (1.6A.4, implemented)

All mutations are worker commands (§3.4); synchronous validation uses pure functions and the probe snapshot (§7.11). The six management command kinds are `provider_update` / `provider_set_default` / `provider_remove` / `provider_set_upload_token` / `provider_clear_upload_token` / `provider_check` (joining the two onboarding kinds `provider_probe`/`provider_register` from §7.11), enqueued from `api/api_attachments.py` and executed by the matching worker handlers in `meshsrv/attachments/service.py`.

- **`PATCH /api/mca/providers/{id}`**: partial update of non-identity fields. For the clearable TTL/`protocol_version` fields, a JSON `null` on a **present** key maps to the `CLEAR` sentinel (explicitly back to unset), while an **absent** key leaves the field alone — the `provider_registry.CLEAR` sentinel is what crosses the command payload, never a bare `None` (which the worker treats as "leave alone"). Enqueues `provider_update` → `update_profile()`. Identity fields (`origin`, `service_public_key`) are **not** editable — changing them is a new registration. A body with no recognized field (only the id) is `400 invalid_metadata`.
- **`POST /api/mca/providers/{id}/default`**: `provider_set_default` → `set_default()` (transactional single-default).
- **`DELETE /api/mca/providers/{id}`**: `provider_remove` → `remove_or_disable()` → result `{"action": "deleted"|"disabled"}`.
- **`PUT /api/mca/providers/{id}/upload-token`**: body `{"upload_token": "<secret>"}`; `provider_set_upload_token` → `set_upload_token()` (0600 file, ≤ `MAX_UPLOAD_TOKEN_BYTES` = 4096 UTF-8 bytes). Response never echoes the token; an empty token is `400 invalid_metadata`, an overlong one `400 upload_token_too_long`.
- **`DELETE /api/mca/providers/{id}/upload-token`**: `provider_clear_upload_token` → `clear_upload_token()` (idempotent). Result `upload_token_configured: false`.
- **`POST /api/mca/providers/{id}/check`**: `provider_check` → `connectivity.refresh(force=True)` (worker). Result `202`; the fresh status is observable in the connectivity snapshot.

### 7.13 Public provider projection (all provider reads)

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

**No raw `service_public_key` bytes** (replaced by `service_key_fingerprint`), no `upload_token_file`, no token. `tls_required` is always `true` for the MVP (read-only; see §7.11).

### 7.14 `GET /api/attachments/{id}/content` (1.6A.5, implemented)

- **CSRF:** n/a (GET). **Precondition:** `content_available` (§7.5). **Auth:** yes.
- **Location:** read from the internal `ContentDescriptor` (§3.7) — no SQLite; `locator` never serialized.
- **Headers (mandatory):**
  - `X-Content-Type-Options: nosniff` always;
  - `Cache-Control: no-store` always;
  - `Content-Security-Policy: sandbox` always (no scripts, no same-origin escalation on preview);
  - `Content-Type`: sniffed MIME for preview types; `application/octet-stream` otherwise;
  - `Content-Disposition`: `inline` **only** for decoded-and-verified `image/jpeg`, `image/png`, `image/webp`, and (optionally) strict `text/plain`; `attachment` for everything else, **including PDF**.
- **Content-Disposition filename:** generated as a safe ASCII fallback plus an RFC 5987 `filename*` for non-ASCII — `Content-Disposition: attachment; filename="<ascii-fallback>"; filename*=UTF-8''<pct-encoded-safe-name>`. The name is derived from the sanitized safe name (§7.5), never from the client-supplied path.
- **Never serves ciphertext.** Never inline-serves SVG/HTML/JS/archives/PDF.
- **Errors:** `404 attachment_not_found`; `409 not_available`; `404 content_missing`.
- Path is re-validated against the controlled workspace root (the `api_camera.py` screenshot pattern) — one path validation, not two independent resolutions.

---

## 8. State / action matrix (corrected)

Allowed actions per state. `retry` is valid **only** for `AUTOMATIC_STATES` — `OFFER_RECEIVED`, `WAITING_CONSENT` and `VERIFYING` are **not** automatic and are therefore **not** retryable; `REJECTED` and permanent failures (`FAILED_VALIDATION`, `FAILED_UPLOAD`, `FAILED_RADIO`) are likewise **not** retryable. `cancel` is keyed off persisted remote state (see §7.4).

| State | retry | download | save | reject | revoke | cancel | copy-code | local-content |
|---|---|---|---|---|---|---|---|---|
| DRAFT / VALIDATING / ENCRYPTING / QUEUED_UPLOAD | ✓ | — | — | — | — | ✓ (no remote session: local + spool) | — | — |
| UPLOADING | ✓ | — | — | — | — | ✓ (persisted session: revoke object) | — | — |
| READY_TO_SEND | ✓ | — | — | — | — | ✓ (committed: revoke object) | — | — |
| SENT / RECEIVED / DOWNLOADED | — | — | — | — | ✓ | — (use revoke) | ✓ | ✓(if saved) |
| FAILED_VALIDATION / FAILED_UPLOAD / FAILED_RADIO | — | — | — | — | — | — | — | — |
| EXPIRED / REVOKED / CANCELLED | — | — | — | — | — | — | — | — |
| OFFER_RECEIVED | — | — | — | — | — | — | — | — |
| WAITING_KEY / WAITING_PROVIDER / WAITING_NETWORK / DOWNLOADING | ✓ | — | — | — | — | — | — | — |
| WAITING_CONSENT | — | ✓ | — | ✓ | — | — | — | — |
| VERIFYING | — | — | — | — | — | — | — | — |
| AVAILABLE | — | — | ✓ | — | — | — | ✓ | ✓ |
| REJECTED / FAILED / EXPIRED (receiver) | — | — | — | — | — | — | — | — |

`cancel` semantics (keyed off persisted remote state, §7.4): no persisted `upload_id`/`revoke_token` → local cancel + clear spool → `CANCELLED`; a persisted session without a `revoke_token` → `relay_unreachable` (cannot safely revoke — row preserved for manual retry); a persisted `revoke_token` → revoke the object (a Relay 404 is confirmed absence) then local cancel → `CANCELLED` (any other remote-cleanup failure ⇒ not `CANCELLED`, `error_code: relay_unreachable`; an unlink `OSError` ⇒ `spool_cleanup_failed`); `SENT/RECEIVED/DOWNLOADED` → use `/revoke`, not `/cancel`.

`retry` never mutates state directly — it re-dispatches `run_step`/`reconcile_pending` for a state the tick already drives, forcing immediacy (e.g. `READY_TO_SEND` after the radio returns, `QUEUED_UPLOAD` after the network returns). It never mints a new `transfer_id`, and it does **not** recover a terminal `FAILED_*` (that is a future state-machine change).

---

## 9. `provider_id` vs `profile_id` — the schema decision

`mca_provider_profiles.provider_id` (Base64URL, 11 chars, derived from `origin + "\n" + service_public_key`) is the **primary key and the sole public identifier** in this contract. There is no separate `profile_id`/autoincrement id exposed anywhere.

- **Why not a local `profile_id`:** the MVP has no operation that needs a stable identity decoupled from the derived `provider_id`. A profile is identified end-to-end (wire OFFER field, `attachments.provider_id`, the Relay's self-reported `/v1/info` `provider_id`, and the registry key) by the same derived value; introducing a second local id would fork the identity space with no consumer.
- **The honest consequence:** changing `origin` or `service_public_key` changes `provider_id`, so "re-key a Relay" is a *new registration* (a fresh trust-bootstrap), never an edit — this is already the behavior `update_profile()` deliberately enforces (it refuses `origin`/`service_public_key`).
- **Open (recorded, not decided):** whether a later stage needs a separate local `profile_id` to keep a stable UI reference across a re-key, or to support multiple profiles for one origin. This is left as an explicit unresolved decision (§15.1); the contract commits to `provider_id`-only for the MVP.

---

## 10. Error code reference

Stable, snake_case, additive.

| Status | `error_code` | Meaning |
|---|---|---|
| 400 | `invalid_metadata` / `invalid_attachment_id` / `invalid_direction` / `invalid_state` / `invalid_filter` / `invalid_command_id` / `invalid_origin` / `invalid_pagination` / `invalid_provider_id` / `invalid_query` / `invalid_contact_id` | malformed input |
| 400 | `mime_not_allowed` / `file_too_large` / `metadata_too_large` / `ciphertext_too_large` | file/size validation |
| 400 | `recipient_not_found` / `recipient_not_trusted` | binding missing or not `trusted` |
| 400 | `provider_not_found` / `provider_disabled` / `upload_not_allowed` / `upload_token_missing` / `upload_token_too_long` / `ttl_out_of_range` / `provider_id_mismatch` | provider/registration |
| 400 | `probe_id_not_found` / `probe_id_expired` | onboarding probe reference invalid/stale |
| 400 | `invalid_mca_envelope` | not a parseable MCA message |
| 401 | `auth_required` | inherited (existing) |
| 403 | `csrf_invalid` | CSRF token missing/mismatch (§2.3) |
| 404 | `attachment_not_found` / `contact_not_found` / `provider_not_found` / `command_not_found` | — |
| 409 | `invalid_state_transition` | action not valid in current state — the synchronous 409 body adds a `state` field with the current safe public state (§7.3) |
| 409 | `idempotency_conflict` | same `client_request_id`, different canonical content |
| 409 | `duplicate_transfer` | `transfer_id` already known |
| 409 | `key_already_known` | request-key for an already-trusted address |
| 409 | `not_saved` | local-content when `saved=false` |
| 409 | `probe_id_used` | single-use `probe_id` already consumed (command result) |
| 413 | `request_too_large` | total multipart body over the create endpoint's per-request cap (§7.2, Finding 4) |
| 429 | `rate_limited` | key-exchange/ACK quota |
| 429 | `admission_rejected` | inbound pending cap |
| 429 | `command_queue_full` | worker command queue at capacity |
| 503 | `radio_unavailable` / `relay_unreachable` | runtime unavailability at an action's execution |
| 503 | `mca_not_ready` | the attachments facade exists but readiness is unset (service not yet started, or the first snapshot publish failed); snapshot-backed reads and `submit()` reject until readiness (§3.2) |
| 500 | `internal_error` | an unexpected exception was sanitized by the MCAttach read endpoint's local error boundary — a stable envelope with no exception text, class, traceback, or path (§11) |

Command-result `error_code`s reuse this table plus the probe failure codes (`origin_not_routable`, `relay_identity_mismatch`, `relay_incompatible`), the provider-cap code `provider_limit_reached` (a `provider_register` whose write would exceed `MAX_PROVIDER_PROFILES` = 8 — the cap is enforced worker-side in the *same* transaction as the insert, so the slot is never consumed; re-registering an already-registered identity is idempotent and does **not** consume a slot), the two worker-side execution codes `unsupported_command_kind` (an enumerated kind with no wired handler — a terminal `failed`, never a crash) and `command_execution_failed` (a wired handler raised; the drain loop records it as a terminal `failed`), and the cancel-specific worker code `spool_cleanup_failed` (an unlink `OSError` on `spool/outgoing/<attachment_id>` — §7.4). `upload_token_too_long` (an upload token exceeding `MAX_UPLOAD_TOKEN_BYTES` = 4096 UTF-8 bytes) is enforced synchronously as a `400` in the API layer *and* authoritatively worker-side in `set_upload_token()`. `UploadRejectionReason` maps 1:1 to `error_code`s on upload-readiness: `profile_not_found`, `profile_disabled`, `upload_not_allowed`, `upload_token_missing`, `relay_not_yet_checked`, `relay_unreachable`, `relay_identity_mismatch`, `relay_incompatible`, `ciphertext_too_large`, `ttl_below_minimum`, `ttl_above_maximum`.

---

## 11. Security and privacy protections

1. **Auth on everything** — every endpoint is `/api/`, inheriting `_enforce_auth` (401 `auth_required`).
2. **CSRF** — §2.3 (finalized): mandatory session-bound `X-CSRF-Token` + `SameSite=Lax`/`HttpOnly`/conditional `Secure`, `compare_digest`, login rotation, logout clearing; mutations fail closed (`403`) until it exists.
3. **Single-owner SQLite** — §3.1; no request thread touches `conn` or the tick lock; a dedicated read-only connection is explicitly rejected; reads go through immutable snapshots and an internal `ContentDescriptor` for files.
4. **No absolute paths** — `saved_path` removed; `ContentDescriptor.locator` is internal-only, never serialized.
5. **No secret egress** — upload/revoke tokens, receipt secret, private keys never in a response or command result; `upload_token_configured`/`configured` only; token-write/clear endpoints never echo the token.
6. **No secret logging** — stable `error_code`s, sanitized messages, no plaintext filename/comment/keys/pointers/tokens.
7. **SSRF** — §12 (finalized): MVP allows only globally-routable HTTPS Relay origins (`is_global` + IPv4-mapped-IPv6 check); special/private address ranges, redirects, and unvalidated re-resolution are blocked, and the fetch is IP-pinned (§12.2).
8. **Recipient identity never client-derived** — the create endpoint takes a transport address; the server resolves the trusted Ed25519 key from `mca_recipient_bindings` and enforces the TOFU address binding before encrypting (§4.2).
9. **Preview policy** — §7.14; `nosniff`, `Cache-Control: no-store`, `Content-Security-Policy: sandbox` always; inline only for decoded-and-verified JPEG/PNG/WebP (and optionally strict `text/plain`); PDF and everything else `application/octet-stream` + attachment with a safe ASCII/`filename*` disposition.
10. **Bounded admission** — inbound caps (per-source/global), a bounded command queue (§3.2), and a bounded command-result/probe store (§3.4), all surfaced as `429`/`404` rather than silent drops.

---

## 12. SSRF: corrected claim, and the finalized MVP Relay policy

**Correction of the earlier revision:** the statement "the browser never supplies a Relay URL — only a provider_id" was **false**. It is true for the *runtime* path (send/receive resolve a pinned origin from `provider_id` via `ProviderRegistry.resolve()`, which returns `None` on a miss and never performs DNS/HTTP). But **onboarding** (`POST /api/mca/providers/probe`) accepts a browser-supplied `base_url`/`origin` that the server later fetches. The SSRF surface is therefore the onboarding origin plus the worker's subsequent fetch of it.

### 12.1 Finalized MVP policy: globally-routable HTTPS only

For the MVP, only a Relay whose origin resolves to a **globally routable** address over HTTPS is accepted. The concrete check is `ipaddress.ip_address(addr).is_global == True` (which correctly classifies `0.0.0.0/32` and `::/128` as non-global *without* blacklisting the entire internet the way `0.0.0.0/0`/`::/0` would), **plus** an explicit IPv4-mapped-IPv6 check (`::ffff:a.b.c.d` is rejected when the embedded IPv4 address is not global). The following are **blocked** at probe time and re-checked on every subsequent fetch:

- loopback (`127.0.0.0/8`, `::1`);
- private / site-local (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`);
- link-local (`169.254.0.0/16`, `fe80::/10`);
- multicast (`224.0.0.0/4`, `ff00::/8`);
- unspecified (`0.0.0.0/32`, `::/128`);
- reserved / special-use ranges;
- IPv4-mapped IPv6 addresses whose embedded IPv4 address falls in the above special ranges;
- **redirects** — `allow_redirects=False` everywhere; any 3xx is an error, never followed.

This does **not** hinder a user's own Hostinger-hosted public HTTPS Relay, and it substantially simplifies safe implementation. **LAN / self-hosted Relay (a private-origin Relay) is deferred to a future "advanced mode" with its own ADR.** In this contract, "own Relay" means a *user-owned public HTTPS Relay* (a globally-routable origin the user controls), not a LAN-only service.

### 12.2 What exists today vs. what is required

- **Exists:** `normalize_origin()` (HTTPS-only, hostname, no credentials, bare origin — pure, request-thread-safe); `resolve()` (a miss is `None`, no network).
- **Required (all gaps, §13):** DNS resolution + the §12.1 address classification at probe time; **IP pinning** on every subsequent fetch (see below); `allow_redirects=False` in `relay_client`/`connectivity_monitor`; and a gate between "origin probed" and "origin persisted" that runs the probe asynchronously before trust is committed.

**DNS-rebinding protection requires IP pinning, not "re-resolve then request."** The sequence "resolve DNS, check the IP, then issue a `requests` call by hostname" still leaves a window — `requests` performs its **own independent DNS resolution** internally, which a rebinding attacker can answer differently. The contract therefore requires the fetch path to:

1. resolve **all** addresses for the hostname;
2. **reject** the origin if **any** resolved address is not globally routable per §12.1;
3. connect **to one of the already-validated IPs** (the HTTP client is pinned to a specific IP — no second, independent DNS lookup inside the client);
4. while verifying the **TLS certificate and SNI against the original hostname** (so the pinned IP does not break hostname verification);
5. and forbid redirects.

If this IP-pinning approach is judged too involved for the MVP, the document must **not** claim DNS-rebinding protection is fully designed — the honest fallback is to state that rebinding protection is *deferred*, and that mitigation is limited to the probe-time blocklist. Either way, the runtime `resolve()` lookup and every Relay fetch must enforce §12.1.

### 12.3 Trust bootstrap (two-phase, §7.11)

1. **Phase 1 — probe.** `POST /api/mca/providers/probe` accepts `base_url`; the request thread runs `normalize_origin` (pure); the worker resolves DNS, applies the §12.1 blocklist, fetches `GET {origin}/v1/info` with redirects disabled, and records a single-use `ProbeRecord` (§7.11). Nothing is persisted to SQLite.
2. **Phase 2 — commit.** `POST /api/mca/providers` consumes `probe_id` and requires `fingerprint_confirmation` to match the probe's `service_key_fingerprint`; the worker persists via `register()` only on match, using the probe's fields.

The trust is thus **operator-confirmed fingerprint + server-side probe-verified identity**, never a blind origin or a browser-replayed parameter set.

---

## 13. Implementation gaps (resolve before/with the named sub-stage)

1. **Facade / queue / snapshots — built in Step 1.6A.1.** The §3 plumbing now exists: `CommandQueue`, `CommandRegistry`, `PendingReservations`, `ProbeRegistry`, `AttachmentsSnapshotPublisher` (+ `ContentDescriptor`), the `AttachmentsFacade` request surface, and the `CommandDispatcher` handler table — wired into `_MCARuntimeState` and drained by the worker (§3.2/§3.3/§3.4). Seventeen command handlers are now implemented: the Step 1.6A.3A lifecycle handlers (`attachment_retry` / `attachment_download` / `attachment_reject`), the Step 1.6A.3B create handler (`attachment_create` → `sender.create_draft`), the two Step 1.6A.3C handlers (`attachment_cancel` → remote-revoke-then-`sender.cancel`, §7.4; `contact_request_key` → rate-limited KEY_REQUEST send, §7.10), and the eight Step 1.6A.4 provider handlers (`provider_probe` / `provider_register` / `provider_update` / `provider_set_default` / `provider_remove` / `provider_set_upload_token` / `provider_clear_upload_token` / `provider_check`, §7.11/§7.12), and the three Step 1.6A.5 handlers (`attachment_save` → save-to-files, `attachment_revoke` → remote-revoke-then-`REVOKED`, `attachment_delete_local_content` → delete-files-copy-keep-history, §7.3). The three command kinds for the deferred Stage 1 additions (`attachment_import`, `attachment_copy_code`, `attachment_add_delivery`) are enumerated in `COMMAND_KINDS` but not yet wired to handlers.
2. **`client_request_id` / `canonical_hash` columns — done (migration 11).** The two columns plus the partial unique index on `(workspace_id, client_request_id) WHERE client_request_id IS NOT NULL` now exist (§3.5/§3.6).
3. **Multipart staging — done (Step 1.6A.3B).** `POST /api/attachments` now accepts `multipart/form-data`, stages the plaintext to `spool/outgoing/<uuid>` in bounded 64 KiB chunks (never whole-file buffered), sniffs MIME from magic bytes plus full-stream text/JSON validation and filename normalization, computes `file_sha256` + `canonical_hash`, and enforces the 5 MiB cap with a per-request total-body cap (`413 request_too_large`) (§7.2).
4. **Cancel orchestration — done (Step 1.6A.3C).** `sender.cancel()` still does not itself clear the spool or revoke an in-flight Relay object; the `attachment_cancel` worker command now composes both halves — the persisted-remote-state Relay revoke (with 404-as-confirmed-absence) followed by the spool unlink + `saved_path` clear + `sender.cancel()` (§7.4).
5. **`ProbeRegistry` built (Step 1.6A.1); `probe_id`→`register()` wiring done (Step 1.6A.4).** The single-use in-memory `ProbeRecord` store with its TTL/check-and-consume semantics (§7.11) is built and TTL-enforced. The `provider_register` handler now check-and-consumes the probe record (a replay or an expired probe → `probe_id_used` in the command result), and `register()` materializes the profile from the probe's server-fetched identity fields — never re-trusting browser-supplied Relay parameters (§7.11).
6. **No contact enumeration** — `GET /api/mca/contacts` needs a "list all bindings" method.
7. **`clear_upload_token()` done (Step 1.6A.1); save-to-files and revoke done (Step 1.6A.5); add-route still open.** `save` (save-to-files) and `revoke`-via-facade are now implemented as worker commands (`attachment_save` / `attachment_revoke`, §7.3). The remaining domain method — `deliveries` POST (add-route) — still needs a worker-executed method (Stage 1, deferred).
8. **SSRF hardening (§12) — done (Step 1.6A.4).** DNS/IP classification (`is_global` + IPv4-mapped-IPv6), IP pinning with hostname/SNI TLS verification (no independent client-side DNS), redirect pinning (`allow_redirects=False`), and the async onboarding probe now exist in `meshsrv/attachments/relay_http.py` (the shared §12 module — `resolve_host_ips` / `is_globally_routable` / `validate_origin_routable` / `SecureSession` / `_PinnedHTTPSAdapter` / `build_secure_session`), exercised by the `provider_probe`/`provider_check` worker handlers via `RelayClient`.
9. **No connector registry** — `connector_profile_id` is a fixed `"meshtastic"` string; `GET /api/mca/connectors` is a Multi-transport placeholder.
10. **CSRF mechanism — done (Step 1.6A.0, PR #233).** The project-wide `X-CSRF-Token` + `SameSite=Lax` cookie contract now exists (§2.3); this prerequisite is satisfied, not MCAttach-specific.
11. **5 MiB upload cap — enforced by the create endpoint (Step 1.6A.3B), not `MAX_CONTENT_LENGTH`.** The endpoint streams the file in fixed `_STAGING_CHUNK_BYTES` (64 KiB) chunks and rejects `file_too_large` after at most one chunk beyond `_MAX_FILE_BYTES` — an oversized file is never buffered whole. The total multipart body is additionally capped per-request via `request.max_content_length` (`_MAX_REQUEST_BYTES`), so Werkzeug rejects an oversized body with `413 request_too_large` *before* spooling it — set per-request, never via the global `MAX_CONTENT_LENGTH`, so no other endpoint's upload limit is affected. The global `MAX_CONTENT_LENGTH` is still deliberately not set (a per-request cap is the correct scope for a single endpoint's limit); the earlier "residual hardening" note for this gap is now closed by the per-request cap + chunked staging.
12. **Signed pointer not persisted for re-read** — `copy-code` (§7.7) needs the canonical `MCA1-TEXT` (or its inputs) persisted at commit.

---

## 14. Section-18 adaptations (deliberate deviations, updated)

1. **Added `GET /api/mca/connectivity`** — the spec implies connectivity state but never names an endpoint; `ConnectivityMonitor.snapshot()` is the ready-made, thread-safe source.
2. **Added `GET /api/mca/commands/{command_id}`** — the command-result endpoint the async 202 model requires; the spec's polling model had no way to observe a mutation's outcome directly.
3. **Expanded `GET /api/mca/providers` into a full CRUD set (8–10, 26–33)** — §17.6's settings list (profiles, default, limits, quotas) requires write routes the single `GET` doesn't provide; `ProviderRegistry` already has every method except `clear_upload_token()` and the probe.
4. **Added `GET /api/mca/identity`** — the principal/fingerprint the UI and the bootstrap flow both need.
5. **`POST /api/attachments` is `multipart/form-data`** — the spec leaves the upload mechanism unstated; multipart is chosen for the MVP (≤ 5 MiB, one request, standard `FormData`).
6. **Recipient is an address, never a key** — the server resolves the trusted key from the TOFU binding; nothing in the audited code accepts a client-derived public key.
7. **All mutations are worker commands (202), reads are snapshots (200)** — the uniform §3.4 model, with `GET /api/mca/commands/{command_id}` as the outcome surface.
8. **`retry` restricted to `AUTOMATIC_STATES`** — `REJECTED`/`FAILED_*` are terminal; terminal-failure retry is a future state-machine change, not promised here.
9. **`cancel` added** (not in §18) — the outbound card's pre-`SENT` lifecycle action, keyed off persisted upload-session/revoke-token state (§7.4).
10. **Provider onboarding via `probe_id`** — the spec's "trust bootstrap + fingerprint confirm" is pinned to a single-use probe record so registration never re-trusts browser parameters (§7.11).
11. **`GET /api/mca/connectors` deferred to Multi-transport** — the MVP has one hardcoded connector.
12. **`retry` does not mint a new `transfer_id`** — the spec's §22.1 rule is pinned into the endpoint contract.
13. **`saved_path` and `include_raw` removed** from responses; replaced by `saved`/`content_available` and an internal `ContentDescriptor` (§7.5, §3.7).
14. **Contacts enumeration, manual import, copy-code, and add-delivery moved to later Stage 1** — they each need a domain addition that is not part of the 1.6A series (§5).

---

## 15. Unresolved decisions

1. **Whether a separate local `profile_id` is needed** — §9; open for a later re-key/multi-profile stage.
2. **Command-queue topology constants** — `MAX_COMMANDS_PER_TICK`, `COMMAND_RESULT_MAX_ENTRIES`/`COMMAND_RESULT_TTL_SECONDS`/`COMMAND_RESULT_MAX_PAYLOAD_BYTES`: the §3 model fixes the *shape* (reads use immutable snapshots, not a query queue; ids are minted on the request thread). **Chosen in Step 1.6A.1** from the snapshot-cost benchmark: `COMMAND_RESULT_MAX_ENTRIES = 256` terminal entries, `COMMAND_RESULT_TTL_SECONDS = 3600`, and a `COMMAND_RESULT_MAX_PAYLOAD_BYTES = 4 KiB` cap (every documented §7.9 result shape is well under 1 KiB). The final benchmark re-ran the combined memory check on a Pi Zero 2 W at these constants: the worst-case registry (256 near-max-size payloads) retained **~1.1 MiB** of Python objects, and the combined worst case (a 5000-attachment snapshot + the full registry, then one incremental publish and one defensive full build) peaked at **92.3 MiB live RSS** with **~100 MiB MemAvailable remaining, no swap increase, and zero OOM kills** — the registry is now a negligible contributor. The snapshot-republish cadence that once fed this list was superseded by incremental dirty-id publication in Step 1.6A.1 — see §3.3 — so it is no longer an open constant.
3. **File upload in one request vs. two-step stage-then-create** — §7.2 commits to one multipart request; the stage-then-create alternative is recorded (better resumability, an extra round-trip) and may be revisited if resumable uploads become a requirement.
4. **Terminal-failure retry state-machine change** — retrying `FAILED_UPLOAD` (and other terminal failures) is deferred to a separate change (§4.2, §7.3); the exact new transition is out of scope for this contract.
5. **Progress polling cadence / whether list-detail returns a `progress` field** — left to the UI task.

(Decisions that were previously open and are now **finalized**: CSRF mechanism — §2.3; Relay private-IP/LAN policy — §12.1.)

---

## 16. Validation notes

This contract was produced against a full read of: `api/api_auth.py`, `api/api_settings.py`, `api/api_waypoints.py`, `api/api_camera.py`, `server.py` (auth/CSRF/cookie/error/route paths), `meshsrv/attachments/{service,sender,receiver,provider_registry,contacts,identity,key_exchange,workspace,relay_client,codec,manifest,crypto,mime_allowlist,mca_runtime}.py`, `meshsrv/attachments/delivery/{base,meshtastic,fakes}.py`, `meshsrv/attachments/db/migrations.py`, `meshsrv/connectivity_monitor.py`, and design spec sections 12–22. State names, enum values, `AUTOMATIC_STATES`/`TERMINAL_STATES` sets, and method signatures in §4 are verbatim from `main`. Specific verification this revision relied on: `sender.cancel()`'s guard and its lack of spool/Relay cleanup; `sender`'s `FAILED_*` terminal semantics; `receiver.TERMINAL_STATES`/`AUTOMATIC_STATES`; `provider_registry.normalize_origin()`/`compute_provider_id()`/`resolve()` semantics; `ConnectivityMonitor._profile_snapshot` build-swap publication and `refresh(force=)` signature; and `relay_client._request()`'s absent `allow_redirects`.
