# ADR-0001: MCA/1 transport-neutral logical protocol format

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2` (внутренняя спецификация MCAttach, разделы 8-9)

## Context

MCAttach needs a short, signed, transport-neutral logical message (`OFFER`, `KEY_ANNOUNCE`, `ACK_*`, `CANCEL`, ...) that can be carried over very different channels: Meshtastic text messages (~200 byte application budget), MeshCore binary channel datagrams (163 byte payload), messenger bot APIs, or manual copy/paste. The numbers below are scattered across prose in the design spec; this ADR is the single source of truth the codec (`meshsrv/attachments/codec.py`), its tests, and every future `DeliveryAdapter` must implement against. If any of these numbers need to change later, this file changes first, and the codec/tests/adapters follow — not the other way around.

## Decision

### 1. Crypto suite

`suite_id = 1`:

- Ed25519 for all signatures.
- X25519/Curve25519 sealed box for per-recipient key wrapping.
- XChaCha20-Poly1305 (AEAD) for manifest and file chunk encryption.
- SHA-256 for all digests.
- HKDF-SHA-256 for key derivation where needed.
- OS CSPRNG for all keys, nonces, and the `transfer_id`.

Implementation MUST use a vetted libsodium binding (PyNaCl or equivalent) — no custom primitive implementations.

### 2. Wire encodings

Two encodings of the exact same canonical CBOR bytes:

- **`MCA1-TEXT`** (text-capable transports: Meshtastic, Telegram, WhatsApp, clipboard/manual):
  `MCA1:<base64url without padding of canonical CBOR>` — see RFC 8949 (CBOR) and RFC 4648 (Base64URL).
- **`MCA1-CBOR`** (binary transports: MeshCore channel data datagrams): the raw canonical CBOR bytes, no `MCA1:` prefix, no Base64. The transport's own application/`data_type` discriminator identifies the payload as MCA; the CBOR `version` field inside is still checked and is never skipped just because the outer transport already claims "this is MCA".

Signing happens once, over the canonical CBOR bytes, before either encoding is applied. A receiver always: (1) strips the transport envelope, (2) recovers the canonical CBOR bytes (decoding Base64URL for `MCA1-TEXT`, or taking the bytes as-is for `MCA1-CBOR`), (3) runs one shared parser + signature check regardless of which transport it arrived on. This is what allows the same `OFFER` (same `transfer_id`, same signature) to be re-delivered over a second transport later without being treated as a different message.

### 3. `OFFER` canonical CBOR schema

Integer-keyed CBOR map:

| Key | Field | Size/type | Purpose |
|---:|---|---|---|
| 0 | `version` | uint | `1` |
| 1 | `message_type` | uint | `OFFER` |
| 2 | `provider_id` | bytes(8) | Locally-resolved Relay provider fingerprint |
| 3 | `transfer_id` | bytes(16) | Relay object locator **and** dedup ID (client-generated, 128 bits of entropy, immutable after commit) |
| 4 | `sender_key_id` | bytes(8) | Lookup key for sender's public identity |
| 5 | `kind` | uint | generic / image / video / audio / document |
| 6 | `size_bucket` | uint | Approximate size for UI, not exact bytes |
| 7 | `hard_expires_at` | uint32 | Unix time UTC (valid until year 2106) |
| 8 | `flags` | uint | Capability/policy bits |
| 9 | `signature` | bytes(64) | Ed25519 over domain-separated canonical fields (keys 0-8) |

There is no separate `object_id` field: `transfer_id` alone is the Relay object locator, generated client-side before upload. Do not reintroduce a second locator field — it was removed deliberately to save 18 raw CBOR bytes without losing entropy or dedup quality.

**New fields must never be silently added to `OFFER` v1.** A new field requires either a new message version or moving the data into the encrypted Relay descriptor (which has no tight size budget). Any PR that adds a field to this table without bumping `version` or updating this ADR is a bug.

### 4. Exact byte budget (golden numbers — must match a codec unit test byte-for-byte)

| Element | CBOR bytes |
|---|---:|
| Map header (10 pairs) | 1 |
| Integer keys 0-9 | 10 |
| `version` + `message_type` | 2 |
| `provider_id` bytes(8) | 9 |
| `transfer_id` bytes(16) | 17 |
| `sender_key_id` bytes(8) | 9 |
| `kind` + `size_bucket` | 2 |
| `hard_expires_at` uint32 | 5 |
| `flags` | 1 |
| `signature` bytes(64) | 66 |
| **Total canonical CBOR** | **122** |
| Base64URL, no padding | 163 |
| `MCA1:` + Base64URL (`MCA1-TEXT`) | **168 ASCII bytes** |

Hard limits by transport:

- **Meshtastic (`MCA1-TEXT`):** target ceiling **180 ASCII bytes** for the whole text message. 168 leaves 12 bytes of internal margin and ~32 bytes short of the ~200-byte Meshtastic application payload budget (239-byte encrypted payload minus protocol overhead — see Meshtastic Overview docs). This 180-byte ceiling must be verified against the real firmware/adapter path in hardware testing (Step 1.9 of the execution plan) — the 122/168 numbers above are the calculated upper bound, not yet a hardware-confirmed one.
- **MeshCore (`MCA1-CBOR`, future adapter):** documented text limit is 133 characters — an `OFFER` in `MCA1-TEXT` form does **not** fit and must never be sent that way over MeshCore. The 122-byte canonical CBOR **does** fit MeshCore's 163-byte binary channel data datagram, with 41 bytes to spare. Do not build automatic text-chunking/reassembly for `OFFER` over MeshCore — use `MCA1-CBOR` binary delivery instead.
- **`KEY_ANNOUNCE`:** one Ed25519 public identity + `uint32 epoch` + full Ed25519 signature, calculated upper bound ≈ **156 ASCII bytes** in `MCA1-TEXT`. This assumes the X25519 recipient key is *derived* from the announced Ed25519 key (see ADR-0002), not sent as a second independent key — two independent public keys plus one signature would very likely exceed the safe ceiling and require splitting into two signed frames instead.

A codec-level test MUST serialize real (non-mocked) field values and assert length **after** `adapter.encode()`, not against a hand-computed constant duplicated in test code — the table above is the constant; tests read it from one place (this ADR, mirrored as constants in `meshsrv/attachments/codec.py`, e.g. `OFFER_MAX_ASCII_BYTES = 180`).

### 5. Message types and numeric codes (MVP scope)

The design spec names these message types but never assigns them wire-level integer values for the `message_type` field (key 1). This ADR is where that gets decided, once, so `codec.py` has a single source of truth instead of inventing numbering ad hoc:

| Code | Type | Purpose | In MVP (Stage 1)? |
|---:|---|---|---|
| 1 | `OFFER` | File offer | Yes |
| 2 | `ACK_RECEIVED` | Pointer received & stored | Yes, rate-limited |
| 3 | `ACK_DOWNLOADED` | File verified & decrypted | Yes |
| 4 | `ACK_PROVIDER_UNKNOWN` | Receiver doesn't know this provider | Yes |
| 5 | `CANCEL` | Sender revokes a still-available object | Yes |
| 6 | `KEY_REQUEST` | Request sender's public MCA identity | Yes |
| 7 | `KEY_ANNOUNCE` | Announce MCA identity | Yes |
| 8 | `KEY_ACK` | Acknowledge key receipt | Optional |
| 9 | `REJECTED` | User declined | Optional |
| 10 | `EXPIRED` | Receiver observed expiry | Optional |
| 11 | `KEY_ROTATE` | Old key signs new epoch's digest | Post-MVP (with backup/rotation UI) |

Codes are assigned once and never reused or reordered — a future message type gets the next unused code, never a gap-fill, so a `version=1` parser can always tell "unknown type" from "type whose meaning changed."

### 5.1. Field sets for non-`OFFER` message types

Only `OFFER` (section 3) and `KEY_ANNOUNCE` (below) have a spec-mandated exact byte budget. The remaining MVP types are simple enough that their field set is fixed here directly, following the same integer-key CBOR map style:

- **`KEY_ANNOUNCE`** — `{0: version, 1: message_type, 2: public_identity (bytes(32), Ed25519), 3: epoch (uint32), 4: signature (bytes(64), self-signed)}`. Calculated upper bound ≈156 ASCII bytes in `MCA1-TEXT` (section 4 above).
- **`KEY_REQUEST`** — `{0: version, 1: message_type, 2: sender_key_id (bytes(8)), 3: signature (bytes(64))}`. Signed by the requester's own identity so the recipient can record a candidate binding even before a full `KEY_ANNOUNCE` round-trip.
- **`KEY_ACK`** — `{0: version, 1: message_type, 2: sender_key_id (bytes(8)), 3: epoch (uint32), 4: signature (bytes(64))}`.
- **`ACK_RECEIVED`, `ACK_DOWNLOADED`, `ACK_PROVIDER_UNKNOWN`, `CANCEL`, `REJECTED`, `EXPIRED`** — all share one minimal shape: `{0: version, 1: message_type, 2: transfer_id (bytes(16)), 3: signature (bytes(64))}`. These are the "simple acks" — one shared codec function handles encode/decode for all six, keyed only by which numeric `message_type` is passed in.
- **`KEY_ROTATE`** — deferred to the stage that implements key rotation (post-MVP); not implemented in the Stage 0 codec.

ACKs always go back to the original sender via a direct/contact/chat route, never re-broadcast on the same channel the `OFFER` arrived on — this prevents ACK storms on open channels.

### 6. Deduplication and replay

- `transfer_id` is unique per logical send; a repeated `OFFER` with the same ID updates delivery metadata, never creates a second attachment or Relay object.
- Repeated ACKs are idempotent.
- Revoked/expired IDs stay in a short tombstone cache for at least 7 days.
- The `OFFER` signature (key 9) covers keys 0-8. The separate Relay *descriptor* signature (see crypto ADR-0002) covers the full `signed_root` — manifest digest, ordered chunk digests, ordered recipient-envelope digests — and uses a different domain-separation label. Never treat one signature as redundant with the other.
- The parser enforces a strict maximum input length and CBOR nesting depth before attempting to interpret any field — malformed or oversized input must fail closed, not be attempted-and-caught.

## Consequences

- The codec module (`meshsrv/attachments/codec.py`, Execution Plan Step 0.3) can be written and fully unit-tested — including the golden 122/168-byte test — without any dependency on Relay, Meshtastic, or the database.
- Any future transport (MeshCore, Telegram, WhatsApp, manual/clipboard) only needs a `DeliveryAdapter` that knows how to carry `MCA1-TEXT` or `MCA1-CBOR` bytes and enforce its own size ceiling — it never re-derives the logical format.
- If real hardware testing (Meshtastic firmware/adapter path) shows the practical limit is below 168 bytes, this ADR must be revised *before* the OFFER schema is touched — do not shrink individual fields ad hoc without updating this document and its golden test.
- If the crypto ADR (ADR-0002) rejects Ed25519→X25519 key derivation in favor of two independent keys, the `KEY_ANNOUNCE` estimate in section 4 is invalid and must be recalculated for a two-frame format.

## References

- MCAttach System Design and Implementation Spec, v1.2 — sections 8, 9, 9.2.1.
- [Meshtastic Overview](https://meshtastic.org/docs/overview/) — 239-byte encrypted payload, ~200-byte application budget.
- [MeshCore Companion Protocol](https://docs.meshcore.io/companion_protocol/) — 133-char text limit, 163-byte channel data datagram.
- [RFC 8949 — CBOR](https://www.rfc-editor.org/rfc/rfc8949)
- [RFC 4648 — Base64 / Base64URL](https://www.rfc-editor.org/rfc/rfc4648)
- [RFC 8032 — Ed25519](https://www.rfc-editor.org/rfc/rfc8032)
