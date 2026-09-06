# ADR-0006: MCA object encryption — manifest blob, sealed envelopes, and the `signed_root` reconciliation

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2`, sections 8.2-8.4, 13, 14, 14.1, 15.1; Execution Plan Step 1.4

## Context

Design spec section 8.2 describes, in prose, a 12-step file-encryption recipe (random data-encryption key, chunked XChaCha20-Poly1305, an "encrypted manifest", one "sealed envelope" per recipient, and a `signed_root` that a receiver verifies before trusting anything it downloads). None of that has a concrete wire schema yet. Three things already built constrain the design and must not be re-invented:

- `meshsrv/attachments/codec.py::OfferFields` (ADR-0001) — `OFFER`'s own Ed25519 signature (key 9) covers keys 0-8 only: `transfer_id`, `sender_key_id`, and metadata. It authenticates *who sent this transfer_id*, nothing about the file's content.
- `meshsrv/attachments/relay_client.py::ObjectDescriptor` / the real Relay (ADR-0005) — `commit()` returns a descriptor the Relay itself signs (`service_signature`, domain `MCA-RELAY-DESCRIPTOR-V1`) over `{transfer_id, total_size, ciphertext_sha256, manifest_size, manifest_sha256, chunks: [{index, size, sha256}], committed_at, hard_expires_at, delete_after}`. This authenticates *that the Relay has not altered what was committed under this transfer_id since commit*, nothing about who authored it.
- `upload_manifest(upload_id, upload_token, manifest: bytes)` — the Relay API accepts exactly **one opaque blob** per upload. There is no separate endpoint for a "header" and per-recipient "envelopes" — anything spec 8.2 calls a separate object must be packed into this one blob if it is to be covered by `manifest_sha256` at all.

ADR-0001 section 6 already anticipates this and names the requirement precisely: "The separate Relay *descriptor* signature ... covers the full `signed_root` — manifest digest, ordered chunk digests, ordered recipient-envelope digests — and uses a different domain-separation label. Never treat one signature as redundant with the other." That sentence was written before this ADR existed and describes an aspiration, not a shipped mechanism — the real Relay's actual signed fields (confirmed in ADR-0005) are `manifest_sha256` and `chunks[].sha256` only. There is no dedicated `recipient_envelope_digests` field anywhere in the real Relay's wire format. This ADR resolves that gap by construction rather than by asking the Relay for a field it doesn't have and never will in Stage 1 (protocol.md is closed, per ADR-0001's "Never silently add fields" rule).

## Decision

**No new signature scheme is introduced.** `signed_root` (spec 8.2's term) is satisfied entirely by composing three already-existing, already-implemented signatures/hashes, chained through `transfer_id`:

1. **Authorship** — the `OFFER`'s Ed25519 signature (ADR-0001 key 9), already implemented, already tested (`tests/test_mca_codec.py`). Proves *this specific `transfer_id` was offered by this specific `sender_key_id`*.
2. **Content integrity since commit** — the Relay's `service_signature` over the descriptor (ADR-0005), already implemented in `relay_client.py`. Proves *what is stored at this `transfer_id` today is byte-identical to what was committed*, specifically including `manifest_sha256` and every `chunks[].sha256`.
3. **Manifest-blob integrity, including recipient envelopes** — by construction, described below: the "encrypted manifest" and all per-recipient "sealed envelopes" from spec 8.2 are packed into **one CBOR blob**, uploaded via the single `upload_manifest()` call. Its SHA-256 is exactly the `manifest_sha256` that (2) already covers. Because the envelope list is serialized in a fixed order inside that one blob, a single-bit change to any recipient's envelope changes `manifest_sha256`, which the Relay's own signature already protects — satisfying ADR-0001's "ordered recipient-envelope digests" intent without a dedicated per-envelope digest array. No per-envelope digest is needed on top of this because the manifest blob is always fetched whole (there is no partial/streamed fetch of individual envelopes the way there is for chunks); a digest granularity finer than "the whole blob" buys nothing a receiver can act on differently.

A receiver's trust chain is therefore, in order: verify `OFFER` signature (have I truly been offered this `transfer_id` by this `sender_key_id`?) → `get_descriptor(transfer_id)` → verify `service_signature` against the known Relay `service_public_key` (has the Relay's own record of this object been tampered with since commit?) → hash the downloaded manifest blob and compare to `descriptor.manifest_sha256` (do I have the exact blob the Relay committed?) → locate and open *my own* sealed envelope inside that verified blob → hash each downloaded chunk and compare to `descriptor.chunks[index].sha256` before decrypting it. Every step is a comparison against an already-signed value; nothing is trusted on the receiver's own say-so.

### Manifest blob schema (new: `meshsrv/attachments/manifest.py`)

Canonical CBOR, integer-keyed map, mirroring `codec.py`'s style:

```
{
  0: version                 # uint, = 1
  1: transfer_id              # bytes(16) — must equal the OFFER's transfer_id
  2: encrypted_header: {
        0: nonce               # bytes(24) = nonce_prefix || 0xFFFFFFFFFFFFFFFF
        1: ciphertext           # XChaCha20-Poly1305 ciphertext+tag of the plaintext header
     }
  3: recipients: [                       # ordered list, order is part of what manifest_sha256 covers
        {
          0: recipient_key_id    # bytes(8)
          1: sealed_envelope      # bytes — nacl.public.SealedBox ciphertext, see below
        },
        ...
     ]
}
```

Plaintext header (the "encrypted manifest" of spec 8.2), encrypted under key 2 above:

```
{
  0: file_name        # text, receiver-facing only, never trusted for path construction
                       #   (workspace.py's existing traversal-safe resolver still owns
                       #   the on-disk name — this field is display metadata)
  1: mime_type         # text, still checked against mime_allowlist.py on receive
  2: plain_size         # uint
  3: plain_sha256        # bytes(32), of the whole plaintext file
  4: chunk_count          # uint
  5: comment               # text, optional
}
```

Sealed envelope plaintext (spec 8.2's per-recipient secret), sealed via `nacl.public.SealedBox(recipient_x25519_pubkey)` — recipient's X25519 public key is `identity.derive_x25519_public(recipient_public_identity)`, i.e. the same derivation ADR-0002 already confirmed byte-identical across all three test machines:

```
{
  0: data_key         # bytes(32) — the file's DEK, see chunk encryption below
  1: nonce_prefix       # bytes(16)
  2: receipt_secret      # bytes(32) — passed to RelayClient.complete(); receiver-generated
                          #   half is out of scope here (spec 11.x); this is the
                          #   sender-chosen secret the receiver must echo back
  3: chunk_count           # uint — duplicated from the plaintext header so a receiver can
                            #   validate progress without touching the encrypted header first
}
```

### Chunk encryption

Unchanged from the design spec's own numbers, now fixed in code rather than prose:

- `CHUNK_SIZE_BYTES = 256 * 1024`.
- Per-chunk nonce = `nonce_prefix (16 bytes) || struct.pack(">Q", index)` (8 bytes) = 24 bytes, matching XChaCha20-Poly1305's nonce size. `index` ranges over `[0, chunk_count)`; the encrypted-header nonce's suffix (`0xFFFFFFFFFFFFFFFF`) can never collide with a real chunk index because `chunk_count` is bounded far below 2**64 by the Relay's own `max_chunks` limit (`RelayLimits.max_chunks`, already enforced by the mock and real Relay).
- AAD per chunk: canonical CBOR of `{version, transfer_id, index, chunk_count, plain_size}` — binds every chunk to its transfer, its position, the total chunk count (so truncation/reordering fails AEAD verification, not just hash comparison), and the overall plaintext length.
- `ciphertext_sha256` (passed to `create_upload`) is the SHA-256 of the concatenation of all chunk ciphertexts in order — already exactly what `relay_client.create_upload`'s `ciphertext_sha256` parameter expects.

## Consequences

- `meshsrv/attachments/manifest.py` (new, Step 1.4) owns encode/decode of the manifest blob and the sealed-envelope plaintext; it depends on `identity.py` (Step 1.2, for X25519 derivation) and `nacl.public.SealedBox`/`nacl.bindings` (already a dependency per ADR-0002), and produces/consumes exactly the bytes `relay_client.upload_manifest()`/`get_descriptor()`+manifest-fetch already move around unmodified.
- `meshsrv/attachments/crypto.py` (new, Step 1.4) owns chunk AEAD encrypt/decrypt and the plaintext-header AEAD encrypt/decrypt, both parameterized only by `(data_key, nonce_prefix, transfer_id, chunk_count, plain_size)` — no Relay or manifest-blob knowledge, so it is unit-testable with zero I/O.
- The sender must persist `data_key` and `nonce_prefix` in plaintext, sender-side only, for the lifetime of an in-flight upload (state machine `Encrypting` through `Sent`) so a crash mid-upload can resume without re-deriving keys or aborting — this is new sender-only state, not present in `attachment_recipients.receipt_secret_hash` (which is a receiver-verification hash, not a resumable secret) or anywhere else in the current schema. Migration 5 (Step 1.4) adds it; see the follow-up sender-state-machine work for the exact column set.
- No change to `ObjectDescriptor`, `relay_client.py`, `codec.py`, or the real Relay is required by this ADR — everything here is new code that produces bytes those already-shipped, already-tested interfaces already know how to move.
- ADR-0004 threat-model rows 1 ("AEAD-per-chunk and sender-signature verification"), 3 (per-recipient sealed box), and 20 (forward of an MCA code) move from Owed/Partial toward Covered once `manifest.py`/`crypto.py` land with tests — tracked as part of Step 1.4's own closing update to ADR-0004, not duplicated here.

## References

- MCAttach System Design and Implementation Spec, v1.2 — sections 8.2-8.4, 11.4, 13.
- ADR-0001 (`docs/architecture/ADR-0001-mca-protocol.md`) section 6 — the `signed_root` requirement this ADR satisfies.
- ADR-0002 (`docs/architecture/ADR-0002-crypto-suite.md`) — Ed25519→X25519 derivation, confirmed byte-identical across dev/prod/workstation.
- ADR-0003 (`docs/architecture/ADR-0003-attachments-sqlite-exception.md`) — where sender-side transient key material will be persisted (`attachments.db`).
- ADR-0005 (`docs/architecture/ADR-0005-relay-hosting.md`) — the real Relay's actual signed descriptor fields and domain-separation label, which this ADR's chain relies on verbatim.
- `meshsrv/attachments/relay_client.py`, `meshsrv/attachments/codec.py`, `meshsrv/attachments/identity.py`.
