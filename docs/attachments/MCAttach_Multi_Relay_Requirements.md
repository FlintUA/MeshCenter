# MCAttach: Multi-Relay Requirements

**Status:** Living document, current as of the PR #231 review hardening pass (Stage 1, `mcattach-adr-0008-hardening` branch).
**Scope:** What "supporting multiple Relay profiles" actually means for MCAttach in this MVP, what is guaranteed today, and what is explicitly deferred. Written in support of PR #231 review section 13. See ADR-0005 (Relay identity/`provider_id`), ADR-0007 (receiver state machine), ADR-0008 (backend layer, including its PR #231 amendment).

## 1. What a "Relay" is, in this codebase

A Relay is an HTTPS service, external to MeshCenter, that stores encrypted attachment objects on behalf of a workspace so a Meshtastic-carried `MCA1-TEXT`/`MCA1-CBOR` message never has to carry file bytes itself (the LoRa payload ceiling makes that impossible for anything but a trivial file). MeshCenter never invents, guesses, or resolves a Relay's URL from anything received over the mesh - `ProviderRegistry.resolve()` is the only sanctioned `provider_id -> endpoint` lookup, and a miss returns `None`, never a network attempt (`meshsrv/attachments/provider_registry.py`'s own module docstring; this is the project's primary SSRF defense for this subsystem).

Each Relay a workspace knows about is one row in `mca_provider_profiles`, described by a `ProviderProfile`: `provider_id` (a stable 8-byte value derived from `origin + service_public_key`, ADR-0005), `origin`, `service_public_key`, `kind` (`own` | `third_party`), `enabled`, upload/download permission flags, TTL bounds, and cached health-check results.

## 2. Multiple Relays per workspace - what's actually supported today

- **Registration:** a workspace can register up to `MAX_PROVIDER_PROFILES` (8) distinct Relay profiles. The cap is enforced by `ProviderRegistry.register()` worker-side, in the **same transaction** as the insert (Step 1.6A.4) — a registration that would exceed it fails with `provider_limit_reached` and consumes no slot. Re-registering an already-registered identity (the same `provider_id`, derived from `origin + service_public_key`) is idempotent and does **not** consume a slot.
- **Exactly one default:** `set_default()` is the only sanctioned way to change which profile is the default for new drafts, and it is transactional - it always clears every other row's `is_default` in the same transaction before setting the new one, and a partial unique index (`idx_mca_provider_profiles_one_default`, migration 8) makes "at most one default" a database-enforced invariant, not just an application convention.
- **Independent connectivity tracking:** `ConnectivityMonitor` tracks each registered profile's `RelayState` independently - `unknown|online|degraded|unreachable|identity_mismatch|incompatible|disabled` - keyed by `provider_id`. One Relay being unreachable never marks another Relay (or the workspace's own internet status) as down; `refresh()`'s internet-status derivation falls back to the generic connectivity probe whenever no Relay has confirmed `ONLINE` evidence, regardless of why - none registered, every registered Relay disabled, every registered Relay failing, or a mix (see ADR-0008's PR #231 amendment for the full history, including a 2nd-pass fix: a workspace whose only Relays were all *disabled* used to never re-check general internet status at all, leaving it permanently stale).
- **Contextual upload readiness:** `ConnectivityMonitor.evaluate_upload_decision(provider_id, *, ciphertext_bytes=None, requested_ttl_seconds=None)` returns a structured `UploadDecision(ready, reason, detail)` - not a bare boolean - checking profile existence/`enabled`/`upload_allowed`/token configured, the Relay's live state (a distinct reason each for never-checked/unreachable/identity-mismatched/protocol-incompatible), and, when supplied, the actual ciphertext size against `max_ciphertext_bytes` and a requested TTL against `min_ttl_seconds`/`max_ttl_seconds`. `can_upload_to(provider_id)` remains as a thin `bool` wrapper for callers that only need yes/no. `ProviderRegistry.get_upload_candidates()` alone only checks local config and has no live-state awareness - it is not a substitute for this.
- **Independent health-check cadence:** each profile has its own `/health` interval and exponential backoff (`_consecutive_failures`, keyed per `provider_id`) - one flaky Relay backing off does not affect another's check schedule.
- **Bounded concurrent probing:** when more than one Relay is due for a health/info check in the same `refresh()` pass, up to `MAX_CONCURRENT_RELAY_PROBES` (2) are probed concurrently, so a single slow/unreachable Relay does not delay probing another due Relay behind it. Every state update and the SQLite write this produces still happens only on `AttachmentsService`'s own worker thread, never inside a probe's own worker thread.
- **Per-attachment Relay selection:** which Relay a given outgoing attachment uses is chosen once, at draft-creation time (`sender.create_draft(..., provider_id=...)`), and stored on the attachment row. Changing the workspace's default afterward never retroactively changes an in-flight attachment's Relay (ADR-0008 §3, unchanged by the hardening pass).
- **Explicit disable, not deletion, when in use:** `remove_or_disable()` deletes a profile outright only when no `attachments` row anywhere in the workspace still references it; otherwise it disables the profile (`enabled = 0`) and keeps history and the profile row intact, so an in-flight transfer against a since-disabled Relay can still finish or fail cleanly rather than hitting a broken lookup mid-transfer. See ADR-0008's amendment for the full disabled-Relay policy.
- **Cleanup on deletion:** `ConnectivityMonitor.refresh()` prunes its own per-provider tracking state (`_relay_statuses`, `_consecutive_failures`, `_last_info_check`) for any `provider_id` that no longer exists in the registry, so a long-running instance's memory does not grow without bound as Relays are added and removed over the workspace's lifetime.
- **Backend field validation:** `register()`/`update_profile()` validate `kind`, `display_name`, `max_ciphertext_bytes`, the TTL pair (both positive, `min <= max`, checked against the *effective* post-update pair), and `protocol_version` (non-empty when given) - a malformed profile is rejected at registration/edit time, not left to fail later, mid-transfer. `update_profile()` also supports an explicit `CLEAR` sentinel for nullable TTL/`protocol_version` fields, distinct from "not mentioned" (a real bug in the original `COALESCE`-based implementation, fixed in the hardening pass).
- **Protocol-version compatibility check:** each Relay's `/v1/info` response is periodically re-checked (rare cadence, `RELAY_INFO_MIN_INTERVAL_SECONDS`) against `SUPPORTED_RELAY_PROTOCOL_VERSIONS`; a Relay reporting a protocol version this client cannot speak is marked `RelayState.INCOMPATIBLE`, distinct from `IDENTITY_MISMATCH` (the Relay is who it claims to be identity-wise; this client simply cannot use it).
- **Inbound admission is bounded independently of any one Relay.** A flood of inbound OFFERs (each naming a distinct, attacker-chosen `transfer_id`, hence not caught by the existing repeat-`transfer_id` dedup) is capped per source address (`MAX_PENDING_RECEIVED_PER_SOURCE=20`) and per workspace (`MAX_PENDING_RECEIVED_GLOBAL=200`), counting only *live* (non-terminal) received attachments - independent of which Relay(s) any of those OFFERs happen to reference.

## 3. Trust bootstrap - unchanged, still exactly three paths

Adding a Relay to a workspace's registry is always one of exactly three admin-driven paths (`provider_registry.py`'s own module docstring, unchanged by this hardening pass):

1. One built-in default provider shipped with a MeshCenter release.
2. Manual admin entry of a base URL, with explicit full-fingerprint confirmation.
3. Import of a signed `.mcaprovider`/QR profile, again with explicit admin fingerprint confirmation.

`ProviderRegistry.register()` never performs its own trust bootstrap - it assumes the caller (the eventual Step 1.6A UI, or a test) has already gone through one of these three paths. There is no fourth, mesh-carried way to register a Relay, by design.

## 4. Explicitly out of scope for this MVP

Per PR #231 review section 21's exclusion list, none of the following exist yet and are not addressed by this document or the current implementation:

- **Automatic Relay switching / failover.** If a draft's assigned Relay becomes `unreachable`/`disabled`, the attachment retries against that same Relay (via `sender.run_step()`'s own retry/backoff), it is never silently re-pointed at a different registered Relay.
- **Simultaneous multi-radio delivery.** A single MeshCenter instance has one active radio profile at a time (see `meshsrv/instance_manager.py`); Relay multiplicity is orthogonal to, and does not change, that constraint.
- **A general, browsable multi-recipient contacts directory.** The send flow's recipient field is pre-filled from the already-open direct chat's own contact (ADR-0008 §4); channel/broadcast delivery to multiple simultaneous recipients over one Relay-hosted object is architecturally anticipated (`attachment_recipients` already supports more than one row per attachment) but not exercised by any real code path in this MVP - `AttachmentsService._resolve_recipient_identities()`'s TOFU-address-binding check (PR #231 review section 8) is explicitly scoped to `DIRECT` routes only for exactly this reason.
- **Per-Relay upload quota enforcement.** `UploadReadiness.LIMIT_EXCEEDED` was removed rather than implemented (PR #231 review section 10) - nothing in this codebase tracks or computes a per-Relay upload quota today.
- **A REST API surface for any of this.** This is no longer fully out of scope: sub-stage 1.6A.4 (Step 1.6A.4) implemented the Provider Registry management surface — `POST /api/mca/providers/probe` (two-phase onboarding), `POST /api/mca/providers` (register), `PATCH /api/mca/providers/{id}`, `POST /api/mca/providers/{id}/default`, `DELETE /api/mca/providers/{id}`, `PUT`/`DELETE /api/mca/providers/{id}/upload-token`, and `POST /api/mca/providers/{id}/check` — as worker commands enqueued from `api/api_attachments.py`. What remains out of scope is the Step 1.6A.5 content/save/revoke surface and any browser UI (still planned, not built).

## 5. Where to look in the code

| Concern | Module |
|---|---|
| Relay profile storage, validation, trust bootstrap | `meshsrv/attachments/provider_registry.py` |
| Two-phase onboarding probe records (single-use, in-memory) | `meshsrv/attachments/probe_registry.py` |
| §12 SSRF network policy (DNS/IP classification, IP pinning, redirects off) | `meshsrv/attachments/relay_http.py` |
| Provider onboarding & management REST routes (1.6A.4) | `api/api_attachments.py` |
| Provider command handlers (worker-side executor) | `meshsrv/attachments/service.py` |
| Per-Relay and internet-wide connectivity tracking | `meshsrv/connectivity_monitor.py` |
| Per-attachment Relay selection at draft time | `meshsrv/attachments/sender.py` (`create_draft()`) |
| TOFU binding-to-transport-address enforcement before encrypting | `meshsrv/attachments/service.py` (`_resolve_recipient_identities()`) |
| Schema (`mca_provider_profiles`, migration 8) | `meshsrv/attachments/db/migrations.py` |
