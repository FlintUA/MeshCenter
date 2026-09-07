# MCAttach: File Transfer - Technical Reference

**Status:** Living document, current as of the PR #231 review hardening pass (Stage 1, `mcattach-adr-0008-hardening` branch).
**Audience:** developers working on this codebase. For a non-technical explanation, see `MCAttach_File_Transfer_Plain_Language.md`. For multi-Relay specifics, see `MCAttach_Multi_Relay_Requirements.md`.
**References:** ADR-0001 (wire protocol), ADR-0003 (SQLite exception for MCA state), ADR-0005 (Relay identity), ADR-0006 (sender state machine), ADR-0007 (receiver state machine), ADR-0008 (backend layer, plus its PR #231 amendment).

## 1. What MCAttach is

MCAttach ("Meshtastic-Carried Attachments") lets two MeshCenter instances exchange files larger than a LoRa text message can carry, using a third-party **Relay** (an HTTPS object store, external to the mesh) to hold the encrypted file while the mesh itself only ever carries small, signed control messages describing where to find it and how to decrypt it. The mesh never carries file bytes; the Relay never sees plaintext or the decryption key.

Every MeshCenter instance has exactly one **MCA principal** per workspace (an Ed25519 signing keypair plus an X25519-derived encryption keypair, `meshsrv/attachments/identity.py`) - not tied to a specific radio profile, since the principal must survive a radio-profile swap. Trust between two principals is established via **TOFU** (trust-on-first-use): the first time a principal's public key is seen at a given transport address, it is recorded as a pending binding; a human explicitly confirms it before it becomes usable for sending.

## 2. Wire protocol (ADR-0001) - message types this codebase actually implements

All MCA/1 messages are CBOR-encoded, either inlined as Base64URL text prefixed `MCA1:` (`MCA1-TEXT`, used over Meshtastic - `meshsrv/attachments/delivery/meshtastic.py`) or as raw CBOR bytes (`MCA1-CBOR`, for a binary-channel transport). `meshsrv/attachments/codec.py` is the single place that encodes/decodes/verifies every message type:

| Code | Type | Purpose |
|---|---|---|
| 1 | `OFFER` | Sender announces a new attachment: `provider_id`, `transfer_id`, `sender_key_id`, size bucket, expiry. |
| 2 | `ACK_RECEIVED` | Receiver confirms it downloaded the object's ciphertext. |
| 3 | `ACK_DOWNLOADED` | Receiver confirms it decrypted and made the file available. |
| 4 | `ACK_PROVIDER_UNKNOWN` | Receiver reports it cannot resolve the OFFER's `provider_id`. |
| 5 | `CANCEL` | Sender withdraws an in-flight offer. |
| 6 | `KEY_REQUEST` | Requests the other side's current public identity. |
| 7 | `KEY_ANNOUNCE` | Announces a principal's public identity (signed). |
| 8 | `KEY_ACK` | Confirms receipt of a `KEY_ANNOUNCE`. |
| 9 | `REJECTED` | Receiver declined an offer. |
| 10 | `EXPIRED` | An offer's hard expiry passed before completion. |
| 11 | `KEY_ROTATE` | Announces a principal's key rotation. |

**Implemented today:** `OFFER`, `KEY_REQUEST`, `KEY_ANNOUNCE`, `KEY_ACK` have real dispatch logic (`KeyExchangeCoordinator.handle_incoming()` for the key-exchange types, `receiver.handle_offer()` for `OFFER`). **Not implemented** (flagged, not silently missing - `mca_runtime.py`'s own module docstring, unchanged by this hardening pass): `ACK_RECEIVED`/`ACK_DOWNLOADED`/`ACK_PROVIDER_UNKNOWN`/`CANCEL`/`REJECTED`/`EXPIRED`/`KEY_ROTATE` have no production dispatch path yet - `sender.py`'s SENT state does not yet advance off a real inbound ACK. This is explicitly out of scope for the PR #231 review (its own exclusion list names sender-side inbound ACK routing and new terminal states as future work, likely ADR-0009).

## 3. State machines

### Sender (`meshsrv/attachments/sender.py`, ADR-0006)

```
DRAFT -> VALIDATING -> ENCRYPTING -> QUEUED_UPLOAD -> UPLOADING -> READY_TO_SEND -> SENT
                                                                      (terminal: FAILED_VALIDATION / FAILED_UPLOAD / FAILED_RADIO)
```

`AUTOMATIC_STATES = {DRAFT, VALIDATING, ENCRYPTING, QUEUED_UPLOAD, UPLOADING, READY_TO_SEND}` - every non-terminal, non-`SENT` state `AttachmentsService`'s tick scan advances automatically via `run_step()`. `create_draft()` records the recipient key_id(s) and the delivery route (adapter/connector/route type/route id) at creation time; nothing after that call changes which Relay or which route an attachment uses.

### Receiver (`meshsrv/attachments/receiver.py`, ADR-0007)

```
WAITING_KEY -> WAITING_PROVIDER -> WAITING_NETWORK -> WAITING_CONSENT -> DOWNLOADING -> AVAILABLE
                                                                            (terminal: FAILED)
```

`AUTOMATIC_STATES = {WAITING_KEY, WAITING_PROVIDER, WAITING_NETWORK, DOWNLOADING}` - `WAITING_CONSENT` is deliberately excluded (a human decision, not something a tick should silently advance past). `handle_offer()` is the entry point for a real inbound `OFFER`; `AUTOMATIC_STATES` ordering mirrors WAITING_KEY's priority over the provider check - an OFFER from an unknown sender always lands in `WAITING_KEY` first, regardless of whether its `provider_id` is even resolvable.

## 4. Single-owner SQLite model (PR #231 review, section 2)

Every MCA-related table lives in one `sqlite3.Connection` per workspace (ADR-0003's documented exception to "no database, only JSON files"). Before the PR #231 hardening pass, both the radio listener thread and `AttachmentsService`'s worker thread could reach this connection directly, serialized only by a shared `threading.Lock` - a real hazard, since any new call site that forgot to acquire that lock would silently race. The shipped fix makes `AttachmentsService`'s own worker thread the **sole** owner of the connection at runtime:

- `mca_runtime.handle_incoming_meshtastic_text()` (called from the radio listener thread) does exactly one thing: build an immutable `service.InboundEvent` (raw text, source address, packet id, timestamp) and hand it to `AttachmentsService.enqueue_inbound()` - a bounded (`INBOUND_QUEUE_MAXSIZE=256`), non-blocking `queue.Queue`. `put_nowait()` either succeeds immediately or raises `queue.Full`, caught and logged as a dropped event (source address only, never message contents) - the radio listener thread never blocks on this call.
- `AttachmentsService._drain_inbound_events()` runs at the start of every `tick()`, draining up to `MAX_INBOUND_EVENTS_PER_TICK=16` queued events and dispatching each via `_process_one_inbound_event()` - the CBOR decode (`delivery_adapter.ingest()`), message-type classification, `KeyExchangeCoordinator.handle_incoming()` or `receiver.handle_offer()` call, and any resulting reply-send all happen here, on the worker thread, holding the one connection.
- `AttachmentsService.tick()` is guarded by a `threading.Lock` (shared with `mca_runtime`'s own singleton-bookkeeping lock, by construction rather than necessity - see that module's own comment) so repeated/concurrent calls to `tick()` itself are safely serialized; this is no longer about arbitrating two different threads' *direct* database access, since only the worker thread does that any more.
- The worker's first `tick()` runs immediately on `start()`, rather than waiting out the full `tick_seconds` interval - a freshly-started service (or one recovering from a restart) resumes pending work without an up-to-5-second idle gap.
- `AttachmentsService.stop(timeout=5.0)` now returns whether the worker thread actually stopped within the timeout; a caller that then closes the connection (`mca_runtime.reset_state_for_tests()`, the only real caller) checks this before doing so, rather than unconditionally closing out from under a still-running tick.

`AttachmentsService` is started unconditionally in `start_runtime()`, independent of whether the connected radio's identity matches the configured profile (PR #231 review, section 3) - a mismatched/unconfirmed identity blocks the *radio listener* (so no inbound message can arrive), but must never freeze previously-drafted/in-flight attachment work, which is unrelated to that question.

## 5. TOFU and encryption

`KeyExchangeCoordinator`/`mca_recipient_bindings` (`meshsrv/attachments/key_exchange.py`) track, per `(workspace, adapter, transport_address)`, a recipient's currently-trusted public identity and status: `KEY_UNKNOWN` (no binding at all) / `KEY_UNVERIFIED` (seen, not yet confirmed by a human) / `MCA_READY` (confirmed, trusted) / a pending `KEY_CHANGED` state when a conflicting `KEY_ANNOUNCE` arrives for an address whose key is already trusted (parked in `pending_public_identity` until explicitly accepted or rejected).

`get_binding_by_key_id()` is a second, address-agnostic index into the same table, used for `OFFER` signature verification - correct there, since a signature verifies the *signer's* identity regardless of which physical address relayed the message.

**TOFU-to-transport-address binding before encrypting (PR #231 review, section 8):** `AttachmentsService._resolve_recipient_identities()` resolves a draft's recipient key_id(s) to public identities for the `ENCRYPTING` step. An earlier fix (PR #227 defect #6) excluded any binding that was not `MCA_READY` - correct, but incomplete: it never checked that the binding's own `transport_address` matched the attachment's actual delivery destination (`attachment_deliveries.route_id`). Reusing the address-agnostic `get_binding_by_key_id()` lookup unchanged for the *sending* path would have silently decoupled "the key we trust" from "the address we're sending to" - exactly the property TOFU exists to bind together. The shipped fix additionally excludes any recipient (fail-closed, same `fail_recipients_not_trusted()` path as an unconfirmed binding) whose `transport_address` does not equal the attachment's own `DIRECT` route destination. Non-`DIRECT` routes (not reachable in this MVP) skip this check - a channel broadcast's `route_id` names the channel, not any one recipient's address.

## 6. Connectivity (`meshsrv/connectivity_monitor.py`, ADR-0008 §2 + PR #231 amendment)

`ConnectivityMonitor.refresh()` (called once per `AttachmentsService.tick()`, never from a request handler) probes every registered Relay profile's `/health` (frequent, backing off exponentially on consecutive failure, capped at `RELAY_HEALTH_BACKOFF_CEILING_SECONDS`) and, rarely, `/v1/info` (identity + protocol-version confirmation). See `MCAttach_Multi_Relay_Requirements.md` for the full multi-Relay behavior and `ADR-0008`'s amendment for the specific bugs this hardening pass fixed (fallback-check trigger, disabled-profile staleness, transient `/v1/info` failures, stale-provider pruning).

`can_attempt_relay(provider_id)` is advisory only - `AttachmentsService` uses it to decide whether a tick is worth attempting a real network call for a given attachment, but the real HTTPS attempt inside `sender.run_step()`/`receiver.run_step()` remains the final authority regardless of what the monitor last reported.

## 7. Receiver-side ACK outbox (`meshsrv/attachments/receiver.py`, PR #231 review section 4)

A queued receiver-side reply (`mca_outgoing_replies`) is dispatched by `AttachmentsService._dispatch_outgoing_replies()`, once per tick, after the row-scan:

- **Real reply-route persistence.** An inbound `OFFER`'s reply route is now persisted with the actual adapter/connector that received it (`reply_adapter_id`, `reply_connector_profile_id`, `reply_route_type`, `reply_route_id`, `reply_destination_address` - migration 10, extended in this hardening pass), built from the `DeliveryEnvelope` the ingest step produced, not assumed to be "the one global Meshtastic adapter".
- **`DeliveryReceipt.sent` is actually checked.** `adapter.send()` returning without raising an exception does not by itself mean the message was sent - `receipt.sent is False` is now treated exactly like a raised exception (stays `PENDING`, attempt counted, backoff scheduled).
- **Real, persisted rate limiting.** `check_and_record_reply_quota()` (backed by the `mca_ack_quota` table) enforces both a per-source-address window (`ACK_PER_SOURCE_LIMIT=5` per `ACK_PER_SOURCE_WINDOW_SECONDS=600`) and a global window (`ACK_GLOBAL_LIMIT=30` per `ACK_GLOBAL_WINDOW_SECONDS=3600`), replacing the previous "N rows per dispatch call" limit, which only ever bounded one SQL query's row count, not the actual send rate. A reply that would exceed either quota is left `PENDING` and retried later - never marked sent, never dropped, and neither counter is incremented on a rejected attempt.

## 8. Migrations (`meshsrv/attachments/db/migrations.py`)

`migrate()` applies each pending migration inside an explicit `BEGIN`/individual-`conn.execute()`-per-statement/`COMMIT` sequence, with `PRAGMA user_version` updated as part of the same transaction. This was changed in the PR #231 hardening pass after empirically confirming (direct testing, not assumption) that Python's `sqlite3.Connection.executescript()` does **not** honor an explicit outer transaction on this project's build - statements that executed successfully before a later failure in the same script stayed committed even after an explicit `conn.rollback()`. The fix splits each migration's SQL into individual statements (`_split_sql_statements()`, comment-stripping regex + `;`-split) run one at a time via `conn.execute()`, which does roll back correctly on failure - verified directly, including for `PRAGMA user_version` itself. `tests/test_mca_db_migrations.py` covers upgrade/downgrade/re-upgrade round trips per version, an injected-failure-then-retry pair, and migration-9's data-fixup precision.

## 9. Provider Registry v2 (`meshsrv/attachments/provider_registry.py`, ADR-0008 §3 + PR #231 hardening)

See `MCAttach_Multi_Relay_Requirements.md` for the full behavioral contract. Backend validation (`kind`, `display_name`, `max_ciphertext_bytes`, the TTL pair, `protocol_version`) and the `CLEAR` sentinel for explicitly nulling an optional field (distinct from "not mentioned", which the original `COALESCE`-based `update_profile()` could not express) were added in the PR #231 hardening pass.

## 10. Upload readiness (`meshsrv/connectivity_monitor.py`)

`evaluate_upload_readiness(profile)` (a public function; previously a private `_upload_readiness_for()`) computes `ready | upload_token_missing | upload_disabled` from a profile's local configuration alone (`upload_allowed`, `upload_token_configured`) - deliberately independent of live `RelayState`, since a Relay can be `upload_readiness=ready` while currently `UNREACHABLE`, or vice versa; the two are reported side by side, never merged. `LIMIT_EXCEEDED` was removed - nothing in this codebase computes a per-Relay upload quota.

## 11. Install-time dependencies

`cbor2`, `PyNaCl` (`nacl`), and `requests` are Core (not adapter) dependencies - MCAttach code imports them directly, and none of the three carry a GPLv3 license concern the way `meshtastic` does, so they live in the repo root `requirements.txt`, not under `adapters/`. `scripts/verify-install.sh` includes an import check for all three so a broken/incomplete `pip install` on a fresh Pi surfaces immediately rather than as a runtime `ImportError` the first time an MCAttach code path actually executes.
