# ADR-0007: Receiver state machine, descriptor-signature verification, and manifest AAD bootstrap

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2` (sections 11, 13-15.2, 16.3-16.4, 20.1); ADR-0001 (protocol), ADR-0002 (crypto suite), ADR-0006 (object encryption/manifest)

## Context

Execution Plan Step 1.5 asks for the receiver state machine (design spec 15.2) in full: `OfferReceived -> WaitingKey/WaitingProvider/WaitingNetwork/WaitingConsent -> Downloading -> Verifying -> Available`, plus `Expired`/`Rejected`/`Failed`. Unlike the sender (Step 1.4, ADR-0006), which is a single background job driven by repeated `run_step()` polling, the receiver's front door is inherently event-driven: an `OFFER` arrives once, over a `DeliveryAdapter`'s `ingest()` path, exactly like `key_exchange.py`'s `KEY_REQUEST`/`KEY_ANNOUNCE` handling already is. Three concrete design gaps had to be resolved before writing any code, none of which the execution plan or prior ADRs settle:

1. **Descriptor signature verification does not exist yet.** `relay_client.py` (Step 1.1) parses `ObjectDescriptor.service_signature`/`service_public_key` but never verifies them against anything. Nothing in the codebase currently checks a committed object's descriptor signature at all — the sender never needs to (it only reads back its own `commit()` response to confirm success), and no receiver code has existed until now. Criterion #18 ("измененный ... pointer signature или descriptor signature никогда не выдается пользователю как валидный файл") requires this to exist before `Verifying` can mean anything.
2. **Chicken-and-egg on the manifest header's AAD.** `crypto.decrypt_header()` requires `chunk_count`/`plain_size` as authenticated-additional-data inputs, but those are only meaningful *inside* the still-encrypted header — and the sealed `RecipientSecret` (ADR-0006) carries `chunk_count` but deliberately not `plain_size` (checked directly against `manifest.py`'s dataclass in this session — the field does not exist). Something else has to supply `plain_size` before the header can be decrypted at all.
3. **No existing lookup resolves an OFFER's `sender_key_id` to a public identity independent of transport address.** `mca_recipient_bindings` (Step 1.2) is keyed by `(workspace_id, adapter_id, transport_address)`; `key_exchange.py`'s only lookup (`get_binding`) takes a `source_address`. An OFFER's `sender_key_id` field is a lookup key in its own right (ADR-0001 section 3, key 4) and must resolve even without assuming the envelope's route address is the same one the sender's `KEY_ANNOUNCE` arrived on.

## Decision

### 1. Descriptor signature verification: new `relay_client.verify_descriptor_signature()`

Added to `relay_client.py` (not a new module — it already owns `ObjectDescriptor`'s shape and is where a future real-Relay client difference would need to be reconciled anyway):

```python
def verify_descriptor_signature(descriptor: ObjectDescriptor, service_public_key: bytes) -> None
```

Reconstructs the exact `signed` payload `meshsrv/attachments/relay/mock_server.py::descriptor_payload()` builds (`domain="MCA-RELAY-DESCRIPTOR-V1"`, `protocol="MCA/1"`, `provider_id`, `transfer_id` b64url, `total_size`, `ciphertext_sha256` hex, `manifest_size`, `manifest_sha256` hex, `chunks: [{index, size, sha256 hex}]`, `committed_at`, `hard_expires_at`, `delete_after`), canonicalizes it with the same algorithm (`_canonical_json`: recursively sort dict keys, compact separators, `ensure_ascii=False` — copied verbatim from the mock server, which itself documents mirroring the real Relay's `mca_canonical_json`), and verifies with `nacl.signing.VerifyKey(service_public_key).verify(...)`, raising a new `RelayVerificationError(RelayError)` on any mismatch or malformed signature.

**Critical: the caller must always pass the *pinned* `service_public_key` from the local `ProviderRegistry` (the key confirmed during one of the three MVP trust-bootstrap paths — provider_registry.py's own docstring), never `descriptor.service_public_key`.** The descriptor's self-reported key is attacker-controlled data from an untrusted Relay response; verifying a signature against a key the same response supplies proves nothing. `receiver.py` never reads `descriptor.service_public_key` for trust decisions — it exists on the dataclass only for logging/diagnostics.

### 2. `plain_size`/`chunk_count` for header AEAD come from the signed descriptor, not the sealed envelope

The receiver derives both values independently, before opening any envelope or decrypting anything, directly from `ObjectDescriptor.chunks` (already covered by the just-verified descriptor signature):

```
chunk_count = len(descriptor.chunks)
plain_size  = sum(c.size for c in descriptor.chunks) - chunk_count * crypto.TAG_BYTES
```

This works because every chunk is `plaintext + 16-byte Poly1305 tag` (XChaCha20-Poly1305 IETF, `crypto_aead_xchacha20poly1305_ietf_ABYTES = 16`) with no other overhead, and `descriptor.chunks[i].size` is the exact ciphertext size, already covered by the Relay's own signature (ADR-0005/ADR-0006's trust chain) independent of anything the sender's manifest claims. `crypto.py` gains a new public `TAG_BYTES` constant so this arithmetic has one source of truth instead of a bare `16` duplicated in `receiver.py`.

Once the header is decrypted, `receiver.py` asserts `header.chunk_count == chunk_count` and `header.plain_size == plain_size` as an explicit, clearly-erroring safety net — not because a mismatch could produce a *silently* wrong result (a wrong guess simply fails AEAD verification outright, since these values are baked into the AAD), but because a bare `CryptoError("header failed AEAD verification")` is a much worse diagnostic than an assertion naming exactly which independently-derived value disagreed, if this arithmetic (or a future manifest format) is ever wrong.

`manifest.py::decrypt_manifest_header()`'s docstring is corrected in the same change — it currently claims both `chunk_count` and `plain_size` "are the values from that same opened `RecipientSecret`", which was only ever true for `chunk_count` (verified directly against the `RecipientSecret` dataclass, which has no `plain_size` field). Fixed to describe the actual source (independently derived from the descriptor, per this ADR), since a stale docstring on a function this security-sensitive is itself a latent bug magnet.

### 3. `sender_key_id -> public_identity` lookup: new `key_exchange.get_binding_by_key_id()`

A small, additive lookup alongside the existing `get_binding(source_address)`:

```python
def get_binding_by_key_id(conn, workspace_id: str, adapter_id: str, sender_key_id: str) -> Optional[RecipientBinding]
```

Queries `mca_recipient_bindings` by `(workspace_id, adapter_id, sender_key_id)` instead of `transport_address`. This is deliberately **not** a replacement for the address-keyed lookup (key exchange's own rate-limiting/TOFU-pinning logic stays address-scoped, unchanged) — it is a second, independent index into the same table for the one case that genuinely needs it: resolving an inbound OFFER's `sender_key_id` field to a public identity for signature verification, regardless of whether this particular delivery arrived over the same route the binding was originally established on. If no binding row matches, the OFFER's signer is unknown and the attachment enters `WaitingKey` — this is expected and common for a brand-new contact's first file, not an error.

### 4. Receiver state machine: event-driven front door + explicit reconciliation, mirroring `key_exchange.py`'s reply convention

New module `meshsrv/attachments/receiver.py`. States (design spec 15.2, English constants matching `sender.py`'s naming style):

```
OFFER_RECEIVED -> WAITING_KEY | WAITING_PROVIDER | WAITING_NETWORK | WAITING_CONSENT
WAITING_KEY       -> WAITING_PROVIDER | WAITING_NETWORK | WAITING_CONSENT
WAITING_PROVIDER  -> WAITING_CONSENT | WAITING_NETWORK
WAITING_NETWORK   -> WAITING_CONSENT
WAITING_CONSENT   -> DOWNLOADING (explicit user action only - never automatic)
DOWNLOADING       -> VERIFYING | WAITING_NETWORK (transient error)
VERIFYING         -> AVAILABLE | FAILED
{OFFER_RECEIVED, WAITING_KEY, WAITING_PROVIDER, WAITING_NETWORK} -> EXPIRED
WAITING_CONSENT   -> REJECTED
```

Two entry points, deliberately separate because they answer different questions:

- **`handle_offer(conn, *, workspace_manager, principal, provider_registry, key_exchange, raw_offer, network_available, now=None) -> ReceiveResult`** — the one function a caller invokes once per inbound OFFER frame's already-`ingest()`-ed logical bytes (`key_exchange` is this workspace/adapter's own already-constructed `KeyExchangeCoordinator`, used here only for its read-only `get_binding_by_key_id()`). Decodes the OFFER *unverified* first (`codec.decode_offer(raw, verify_key=None)` — this is precisely why that parameter is optional, per its own docstring: "useful for a first-contact OFFER where the key isn't known yet"), dedups by `(workspace_id, transfer_id)` (a repeat OFFER for an attachment already past `OFFER_RECEIVED` returns the existing id and sends no new network request or ACK — ADR-0001 section 6's dedup rule, and the reason repeat delivery over a second adapter later can never re-trigger `WAITING_PROVIDER`'s single ACK), then looks up the signer via `key_exchange.get_binding_by_key_id()`. No binding -> create the row in `WAITING_KEY`, persisting the full raw OFFER bytes in a new `attachments.pending_offer_cbor` column (migration 6) so a later binding can finish verifying it without the sender re-delivering anything, and stop (no signature was checked yet, so nothing about the OFFER's other fields is trusted — only `transfer_id`/`sender_key_id` are used, both required merely to *file* the pending offer, never to fetch anything from the Relay). Binding found -> verify the signature immediately; a bad signature is a hard reject, no row is even created (this is the one path with no corresponding state — an unverifiable-when-verification-was-possible OFFER is treated as never having arrived, consistent with criterion #18's "never shown as valid" applying to the pointer signature just as much as the descriptor/chunk signatures). Good signature -> create the row in `OFFER_RECEIVED`, then immediately calls `run_step()` (below) to advance as far as it can in the same call, exactly like `sender.create_draft()` does not auto-advance but a UI/wiring layer calling both back-to-back is the expected pattern — the difference here is `handle_offer` *does* chain into `run_step()` itself, since an inbound message, unlike a user's outgoing draft, has no separate "now start it" action.
- **`run_step(conn, *, workspace_manager, principal, provider_registry, key_exchange, network_available, attachment_id, relay_client=None, now=None) -> ReceiveResult`** — advances one attachment as far as automatic logic allows from its *current* state. `WAITING_KEY` re-checks `key_exchange.get_binding_by_key_id()` against the parked `pending_offer_cbor` and, once a binding exists, finishes verifying and clears that column (design spec 15.2's "ключ получен" edge, without needing the OFFER re-delivered); `WAITING_PROVIDER`/`WAITING_NETWORK` re-check `ProviderRegistry.resolve()`/`network_available`; `DOWNLOADING`/`VERIFYING` are driven to completion synchronously. This is the function a later reconciliation pass (`reconcile_pending()`, mirroring `sender.resume_pending()`) calls for every non-terminal received attachment after an external event: a new `KEY_ANNOUNCE` binds a previously-unknown sender, an admin registers a new provider (`ProviderRegistry.register()`), or the network comes back. This satisfies the Step 1.5 DoD verbatim: "после ручного импорта provider profile pending offer становится доступным без повторной отправки по радио" — `reconcile_pending()` re-evaluates `WAITING_KEY`/`WAITING_PROVIDER`/`WAITING_NETWORK` rows purely from local state, no OFFER re-delivery involved, matching criterion #17 exactly.

Both return a small `ReceiveResult(attachment_id, state, replies: List[bytes])` — `replies` are encoded-and-signed logical MCA frames (`ACK_RECEIVED`/`ACK_PROVIDER_UNKNOWN`/`ACK_DOWNLOADED`) the caller still has to `encode()`/`send()` through whichever adapter/route the triggering envelope came from. Neither function imports `DeliveryAdapter` or sends anything itself — this mirrors `key_exchange.py`'s explicit "holds no transport of its own" design (its own docstring's phrase) rather than `sender.py`'s pattern of calling `delivery_adapter.send()` directly, because a receiver reacting to an inbound event is architecturally the same shape as key exchange's inbound handling, not the same shape as a sender's active outbound job queue.

### 5. ACK dedup: one row in `attachment_events`, checked before sending, never a separate rate-limit table

Design spec 9.3 marks `ACK_RECEIVED` and `ACK_PROVIDER_UNKNOWN` "rate-limited"; criterion #16 requires unknown-provider to trigger "один проверенный status reply" (exactly one). Rather than a new quota table (`key_exchange.py`'s `mca_key_exchange_quota` solves a *different* problem — bounding a stranger's ability to trigger unlimited announces from many distinct addresses — which does not apply here: an OFFER's `transfer_id` is already unique and already deduplicated), each of the three ACK types is sent **at most once per `attachment_id`**, gated by checking for a prior `attachment_events` row of that type before encoding a new one (`_ack_already_sent(conn, attachment_id, event_type)`). This is simpler than a time-window quota and correctly satisfies "exactly one" rather than merely "at most N per hour" — repeat OFFER delivery (over a second transport, or a re-broadcast of the same one) never produces a second `ACK_PROVIDER_UNKNOWN`, and `reconcile_pending()` re-running `WAITING_PROVIDER` logic after a provider import never re-sends the already-sent one either.

### 6. `Downloading` restarts from scratch on resume; no chunk-level resume in MVP

Symmetric with the sender's own explicitly-stated MVP simplification (design spec 14.1: "В MVP при файле до 5 MiB допустимо перезапускать незавершенный upload целиком; chunk resume можно включить на следующем этапе"). The spec's table row for the download side ("Сеть пропала во время download | Части остаются в temp; повтор после backoff") is read as *permitting* an implementation that keeps partial temp data, not *requiring* one — and `Downloading` here is a short, synchronous, explicit-user-initiated operation bounded by the same 5 MiB cap the upload side is, so re-fetching every chunk on any interruption costs at most the same ~100 ms of crypto work ADR-0002 measured and a handful of seconds of Meshtastic-independent HTTPS transfer, not a background job worth building genuine resume for in this pass. `run_step()`'s `Downloading` handler always writes a fresh temp file (named by `transfer_id`, under `cache/incoming/`) and always re-fetches the descriptor and every chunk; it does not consult or preserve any prior partial state. Genuine chunk-level download resume, like the sender's, is deferred to Stage 2 (reliability) if it turns out to matter in practice.

### 7. `Verifying -> Available`: quarantine on failure, controlled-root save on success

On a chunk hash mismatch (`descriptor.chunks[i].sha256`, checked *before* decryption, as defense in depth against a corrupted transfer independent of AEAD), an AEAD failure (`crypto.CryptoError`), or an overall `plain_sha256` mismatch after full reassembly, the partially-written temp file is moved to `quarantine/` (design spec 16.3: "объекты с ошибкой проверки") rather than deleted outright, purely for diagnostics — never re-read or trusted afterward — and the attachment moves to `FAILED` with a stable `error_code`. On success, the plaintext is moved into `files/` via the already-existing `MCAWorkspaceManager.unique_file_name()` (duplicate-name handling, per criterion in section 23.1's Storage block: "duplicate name") — never a name taken from anywhere the sender's header claims without going through this workspace-owned, traversal-safe resolver (design spec 20.1, "Path traversal" row: "Удалить путь из filename, controlled storage root, generated disk name" — `unique_file_name`/`resolve_saved_path` already strip any path separator and re-verify containment, exactly this row's requirement). Only then does `receiver.py` call `relay_client.complete(transfer_id, receipt_secret)` (the `receipt_secret` opened from this recipient's own sealed envelope) and emit `ACK_DOWNLOADED`.

## Consequences

- `relay_client.py` gains `verify_descriptor_signature()` and `RelayVerificationError` — the first code in the whole project that actually checks a descriptor signature. Existing sender-side tests are unaffected (the sender never calls this function; it only reads its own successful `commit()` response).
- `crypto.py` gains a public `TAG_BYTES` constant; no behavior change to any existing function.
- `key_exchange.py` gains `get_binding_by_key_id()`; no change to any existing function's behavior or the rate-limiting/TOFU logic.
- `manifest.py::decrypt_manifest_header()`'s docstring is corrected (no code change) to stop claiming `plain_size` comes from the `RecipientSecret`.
- New migration 6: `mca_receiver_state` (mirroring migration 5's shape and rationale for the analogous per-attachment mid-flight secret material a received attachment needs across a `Downloading`/`Verifying` restart: `data_key`, `nonce_prefix`, `receipt_secret`, and the resolved `chunk_count`/`plain_size` — though, per decision 6 above, a restart is also always safe to simply redo those network calls if this row is missing or absent; the current `receiver.py` pass does not yet write to this table at all, redoing `Downloading` from scratch every time per decision 6, so it exists now for a future genuine-resume pass to use without another schema change) and `attachments.pending_offer_cbor` (a nullable `BLOB` column holding the full raw OFFER bytes while `WAITING_KEY`, cleared once verified — see decision 4).
- New module `meshsrv/attachments/receiver.py`, new tests `tests/test_receiver.py`. No wiring into `mca_runtime.py`/`server.py` in this pass — same scope boundary Step 1.4 drew for `sender.py` (a pure Core module + tests; UI/API/runtime wiring is Steps 1.6/1.7).
- Nothing in this ADR changes `ADR-0001`'s wire format, `ADR-0002`'s crypto suite, or `ADR-0006`'s manifest schema — this is entirely receiver-side logic consuming those already-fixed formats.

## References

- MCAttach System Design and Implementation Spec, v1.2 — sections 11 (Relay API), 13 (receive sequence), 14 (network-loss table), 15.2 (receiver state machine), 16.3-16.4 (storage/schema), 20.1 (threat table), 24 (acceptance criteria #9, #10, #16, #17, #18).
- ADR-0001 section 3/5.1 (OFFER/simple-ack wire shapes), ADR-0002 (Ed25519->X25519 derivation), ADR-0006 (manifest blob schema, `RecipientSecret` fields).
- `meshsrv/attachments/relay/mock_server.py::descriptor_payload()`/`_canonical_json()` — the exact signed-payload shape and canonicalization `verify_descriptor_signature()` must reproduce.
- `meshsrv/attachments/key_exchange.py` — the "return reply bytes, don't send them" convention this module's `handle_offer()`/`run_step()` follow.
