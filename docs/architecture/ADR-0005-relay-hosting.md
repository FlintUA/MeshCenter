# ADR-0005: Deployed Relay hosting - auth, rate limiting, key ownership

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** MCAttach System Design and Implementation Spec v1.2, section 11.4; Execution Plan Step 1.1; `MCAttach_Relay_0.1.0` source (`relay.php`, `common.php`, `storage.php`, `setup.php`, `database.php`, `SECURITY.md`, `docs/API.md`)

## Context

Step 1.1 required confirming that the already-deployed Relay at `https://mcattach.elektroniker.help` matches the MCA/1 protocol contract, and specifically documenting three things not visible from `/v1/info` alone: the auth model for `POST /v1/uploads`, rate limiting, and service-key ownership. `mcattach.elektroniker.help/robots.txt` disallows all crawlers (`Disallow: /`), so this could not be done by fetching the live site - it was done by reading the actual deployed source (`MCAttach_Relay_0.1.0.zip`, provided directly by the project owner), which is a strictly better source of truth than a black-box HTTP probe would have been.

This review found the real contract differs from the Step 0.5 mock relay's assumed shape in several concrete ways. Per the Execution Plan's own instruction ("если он не проходит один в один - это первое, что нужно поправить, а не расхождение, с которым можно жить"), `meshsrv/attachments/relay_client.py`, `meshsrv/attachments/relay/mock_server.py`, and `meshsrv/attachments/provider_registry.py` are being corrected to match this ADR, not the other way around.

## Decision

### Auth model (POST /v1/uploads and friends)

Three-tier bearer-token model, `Authorization: Bearer <token>` (or `X-MCA-Token: Bearer <token>` for environments that strip `Authorization`):

1. **Upload access token** - one single, long-lived token for the whole Relay instance (`mca_up_<43-char base64url>`), minted once at setup and shown to the admin exactly once. Required only on `POST /v1/uploads`. The Relay stores only its SHA-256 **hex** hash (`upload_access_token_hash`) in its own config file - never the token itself.
2. **Session upload token** - minted fresh per upload session (`mca_us_<43-char base64url>`), returned in the `201` response of `POST /v1/uploads`. Required on every subsequent write to that session: `GET .../uploads/{id}` (status), `PUT .../chunks/{n}`, `PUT .../manifest`, `POST .../commit`. Stored server-side only as a raw SHA-256 digest (`upload_token_hash BINARY(32)`), checked with `hash_equals`.
3. **Revoke token** - minted alongside the session token, returned in the same `201` response, required only by `DELETE /v1/objects/{transfer_id}`. Same raw-SHA-256-hash storage as the session token.

`GET /v1/objects/{transfer_id}/descriptor` and `.../chunks/{n}` and `POST .../complete` require **no token at all** - the design spec's "capability-based download in MVP" (section 11.4) is implemented literally: knowledge of the 128-bit `transfer_id` plus the recipient-bound sealed envelope inside the encrypted manifest is the download's only access control at the Relay layer. `/v1/info` confirms this as `"download_authorization": "transfer_capability"`.

There is no per-account/per-installation upload identity in 0.1.0 - "anonymous upload" is disabled (spec section 11.4's first bullet), but every uploader shares the *same* upload access token. Multi-tenant installation tokens are explicitly listed in `SECURITY.md`'s pre-public-launch checklist as not yet done ("решение о registration/account tokens вместо одного общего upload token").

### Rate limiting

Implemented, per-action, HMAC-pseudonymized by client IP - not a generic reverse-proxy limit. `mca_rate_limit()` computes `bucket = HMAC-SHA256(rate_limit_secret, action + "\n" + window + "\n" + REMOTE_ADDR)` with a fixed-size sliding window (`window = time() div windowSeconds`), stores per-bucket hit counts in MySQL, and returns `429` with a `Retry-After` header once the action's limit is exceeded within its window. `rate_limit_secret` is a 32-byte value generated once at setup and held only in the Relay's own config - the IP itself is never stored, only its per-day/per-action HMAC.

Per-action limits (requests per rolling hour, from `relay.php`):

| Action | Limit / hour |
|---|---:|
| `create-upload` (`POST /v1/uploads`) | 30 |
| `upload-status` (`GET /v1/uploads/{id}`) | 240 |
| `upload-part` (chunk `PUT` or manifest `PUT`) | 600 |
| `commit-upload` | 120 |
| `download-descriptor` | 240 |
| `download-chunk` | 1200 |
| `complete-object` | 120 |
| `revoke-object` | 60 |

`RelayClient`'s existing generic 429-retry-with-`Retry-After` logic (`meshsrv/attachments/relay_client.py`, built in Step 0.5 without knowledge of these specific numbers) needs no change to work correctly against this - it already reads `Retry-After` when present and falls back to exponential backoff otherwise.

### Service private key ownership

A single Ed25519 keypair (`sodium_crypto_sign_keypair()`), generated once during the one-time `/setup` flow and stored in the Relay's own PHP config file (outside `public_html`, `0600`, alongside the MySQL credentials and `rate_limit_secret`). The public half is served plaintext at `/v1/info` and used to compute `provider_id`; the secret half signs every object descriptor (`sodium_crypto_sign_detached`) over a canonical-JSON structure domain-separated as `"MCA-RELAY-DESCRIPTOR-V1"` (see "Descriptor signature domain" below). There is **no rotation mechanism implemented** for this key in 0.1.0 - `SECURITY.md`'s own pre-public-launch checklist only mentions rotating the *upload access token*, not the service key. This is a real, acknowledged gap (tracked in ADR-0004's threat-model checklist, row 13/20, "Key change" - the service key itself has no TOFU-pinning-refresh story yet, only client-side identity keys do), not an oversight of this review.

### Descriptor signature domain (informs ADR-0002)

The Relay signs `{domain: "MCA-RELAY-DESCRIPTOR-V1", protocol: "MCA/1", provider_id, transfer_id, total_size, ciphertext_sha256 (hex), manifest_size, manifest_sha256 (hex), chunks: [{index, size, sha256 (hex)}], committed_at, hard_expires_at, delete_after}` as canonical JSON (object keys sorted, no unescaped slashes, unescaped Unicode). This is the Relay-side signature ADR-0001 section 6 refers to as distinct from the sender's own OFFER/descriptor signature - crypto ADR-0002 (Step 0.2, not yet written) must adopt this exact domain string and field set rather than inventing an independent one, since it is what the deployed Relay actually signs.

### `provider_id` format - resolved

Execution Plan Step 1.1 flagged `provider_id = "61G003-kTq8"` as looking "human-readable, not hex" and asked whether it needs correcting. It does not: it is **Base64URL** (RFC 4648 section 5, no padding) of the first 8 bytes of `SHA-256(lowercased origin with trailing slash stripped + "\n" + raw 32-byte Ed25519 public key)` - `61G003-kTq8` is exactly 11 characters, matching Base64URL(8 bytes). The design spec's section 10.1 prose ("первые 8 байт SHA-256... Provider ID") never specified a text encoding; the deployed Relay's choice is Base64URL, consistent with every other identifier in its API (`transfer_id`, `upload_id`, tokens). **`meshsrv/attachments/provider_registry.py`'s `compute_provider_id`/`normalize_origin` are corrected in this same step to match this formula exactly** - the Step 0.6/0.7 implementation used hex encoding and omitted the `"\n"` separator between origin and public key, which would have produced a different `provider_id` than the real Relay for the same inputs. This is exactly the kind of mismatch Step 1.1 exists to catch.

### 5 MiB vs 6 MiB - resolved

Confirmed with the project owner and by reading `setup.php`'s installed defaults directly: `max_ciphertext_bytes = 6291456` (exactly 6 MiB) is a **deliberate rounded margin** over the design spec's 5 MiB plaintext limit, not a misconfigured or forgotten number. No further action needed; recorded here so it is not re-litigated later as an apparent bug. Other real defaults, for the same reason (documented once, not re-discovered by a future step):

| Config key | Value | Note |
|---|---:|---|
| `max_ciphertext_bytes` | 6 MiB (6291456) | Deliberate margin over the 5 MiB plaintext limit |
| `max_manifest_bytes` | 256 KiB (262144) | |
| `max_chunk_bytes` | 300000 (≈293 KiB) | Not the 256 KiB + AEAD-tag figure the Step 0.5 mock assumed |
| `max_chunks` | 64 | |
| `max_receipts` | 32 | Max recipients per transfer |
| `min_hard_ttl_seconds` / `default` / `max` | 1h / 72h / 72h | Admin cannot currently request longer than the 72h default |
| `default_grace_seconds` / `max` | 1h / 24h | |
| `upload_session_seconds` | 6h | Unfinished upload session auto-deleted after this |
| `tombstone_seconds` | 7 days (604800) | Matches ADR-0001 section 6's "≥7 days" exactly |
| `max_storage_bytes` | 10 GiB | Whole-Relay quota, not per-workspace |

## Consequences

- `meshsrv/attachments/relay_client.py` is rewritten to speak the real wire shapes: `transfer_id`/tokens as Base64URL text over the wire (decoded to/from raw bytes at the Python API boundary, consistent with `codec.py`'s `bytes(16)` convention); the three-tier token model (`upload_access_token` at construction time, per-session `upload_token`/`revoke_token` threaded through by the caller); `receipt_hashes` as a flat declared list at session-creation time rather than the Step 0.5 mock's per-recipient-envelope object; the new `GET /v1/uploads/{upload_id}` status/resume endpoint; and the signed descriptor envelope (`service_signature`).
- `meshsrv/attachments/relay/mock_server.py` is rewritten to mirror this real contract's request/response shapes and status codes (`422` for field-level validation errors, not `400`; `401` for bad tokens; the exact `mca_*` error codes) so that tests written against the mock are testing the real contract, not the Step 0.5 approximation of it.
- `meshsrv/attachments/provider_registry.py`'s `compute_provider_id`/`normalize_origin` are corrected to the real formula (Base64URL output, `origin + "\n" + raw pubkey bytes` as the hashed material).
- Multi-tenant/per-installation upload tokens, service-key rotation, and independent code audit are pre-public-launch items the Relay's own `SECURITY.md` already tracks as outstanding - MCAttach's Core-side code must not assume any of them exist yet.

## References

- MCAttach System Design and Implementation Spec, v1.2 - section 11.4.
- `MCAttach_Relay_0.1.0/src/relay.php`, `common.php`, `storage.php`, `setup.php`, `database.php`, `SECURITY.md`, `docs/API.md` (project owner-provided source, not fetched from the live site).
- ADR-0001 (`docs/architecture/ADR-0001-mca-protocol.md`) section 6 - tombstone retention, Relay descriptor signature.
- ADR-0004 (`docs/architecture/ADR-0004-threat-model.md`) - threat-model rows this ADR updates the status of.
