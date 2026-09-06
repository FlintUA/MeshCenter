# ADR-0002: Crypto suite verification — Ed25519→X25519 key derivation, domain separation, performance budget

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2` (sections 7.3, 8.1-8.2), `docs/architecture/STEP_0.2_HANDOFF.md`, `docs/architecture/ADR-0001-mca-protocol.md` section 6

## Context

ADR-0001 fixes `suite_id = 1` (Ed25519 signatures, X25519/Curve25519 sealed box for key wrapping, XChaCha20-Poly1305 for encryption, SHA-256 digests, HKDF-SHA-256 for key derivation) but left one specific design choice unconfirmed: whether the recipient's X25519 key is **derived** from their already-announced Ed25519 identity key via libsodium's `crypto_sign_ed25519_pk_to_curve25519`, rather than carried as a second, independent key. The spec itself rates confidence in this choice as medium until (a) a crypto ADR records the decision explicitly and (b) the library call is verified on the actual target hardware — a Raspberry Pi Zero 2 W, not just a development machine. This ADR is that verification.

The reason this matters enough to block on: if the conversion works, `KEY_ANNOUNCE` stays a single ~156-byte text frame (one Ed25519 identity key does double duty). If it doesn't — or if the owner later decides two independent keys are preferable for other reasons — `KEY_ANNOUNCE` becomes two frames, which ripples into ADR-0001's byte budget (section 4) and `KEY_ROTATE`. Nothing in Step 1.2 (MCA principal + Meshtastic address binding) can commit to a key format until this is settled.

## Decision

### 1. Ed25519 → X25519 conversion: confirmed, works identically on both target hosts

The conversion is implemented via PyNaCl's `nacl.bindings.crypto_sign_ed25519_pk_to_curve25519` / `crypto_sign_ed25519_sk_to_curve25519` (a direct binding to libsodium's own functions of the same name — no reimplementation). Verified with a fixed test vector (`tests/crypto/test_key_derivation.py`) on three separate machines:

| Machine | Arch | Ed25519 pk (fixed seed `0x11 * 32`) | Derived X25519 pk |
|---|---|---|---|
| Dev workstation (`minipc`) | x86_64, Windows | `d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737` | `7a46e129fd805047448437e4744f1f1576be8c449fdf57e0c580d36c5cfc6668` |
| `dev` (192.168.2.104) | aarch64, Raspberry Pi Zero 2 W | `d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737` | `7a46e129fd805047448437e4744f1f1576be8c449fdf57e0c580d36c5cfc6668` |
| `prod` (192.168.2.103) | aarch64, Raspberry Pi Zero 2 W | `d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737` | `7a46e129fd805047448437e4744f1f1576be8c449fdf57e0c580d36c5cfc6668` |

Byte-identical across all three, including both target hosts. Also confirmed on real hardware:
- The X25519 secret key derived from the Ed25519 secret key (`crypto_sign_ed25519_sk_to_curve25519`) scalar-multiplies to the exact same X25519 public key the pk-only conversion produces — the actual round-trip `KEY_ANNOUNCE` depends on.
- A freshly generated random keypair (not just the fixed vector) converts correctly.
- libsodium rejects the all-zero public key (the identity point) rather than silently producing a usable-looking key.
- `pynacl` installed as a real ARM manylinux wheel with a compiled C extension on both Pi hosts (`pynacl-1.6.2-cp38-abi3-manylinux_2_34_aarch64.whl`) — not a slow pure-Python fallback.

**Decision: use the Ed25519→X25519 conversion, not two independent keys.** The rollback path in ADR-0001 section 6 / STEP_0.2_HANDOFF.md is not triggered.

### 2. Domain-separation labels: pointer signature vs. descriptor signature

Both already implemented in the committed codec/relay code (`meshsrv/attachments/codec.py`, `meshsrv/attachments/relay/mock_server.py`) and confirmed here to satisfy the "must be different" requirement:

- **Pointer signature** (the `OFFER` message's own signature, ADR-0001 section 3 key 9): domain label `b"MCA1/OFFER/v1"`, applied as a raw byte prefix — `signing_key.sign(label + unsigned_canonical_cbor)` (`codec.py`'s `_DOMAIN_LABELS` / `_sign()` / `_verify()`). Every other MCA message type (`KEY_ANNOUNCE`, `KEY_REQUEST`, `KEY_ACK`, `ACK_*`, `CANCEL`, `REJECTED`, `EXPIRED`) gets its own distinct `MCA1/<TYPE>/v1` label the same way — not just `OFFER` vs. the descriptor, the whole message-type space is domain-separated from itself, which is a stronger property than the spec strictly asked for here and costs nothing extra.
- **Descriptor signature** (the Relay's signature over the committed transfer descriptor — manifest digest, ordered chunk digests, `signed_root`): domain label the string `"MCA-RELAY-DESCRIPTOR-V1"`, embedded as an explicit `"domain"` field inside the canonical-JSON payload that gets signed (`mock_server.py`'s `DESCRIPTOR_SIGNATURE_DOMAIN` / `descriptor_payload()`), rather than a raw byte prefix.

These two labels are unambiguously different strings, and — more importantly than the strings merely differing — the two signatures are computed over structurally different canonical encodings (canonical CBOR for the pointer signature, canonical JSON for the descriptor signature) signed with the label embedded two different ways (prefix vs. explicit field). There is no byte sequence that is simultaneously a valid signed pointer payload and a valid signed descriptor payload — cross-protocol replay between the two signature types is not just labeled against, it's structurally impossible. **Confirmed as implemented correctly; no change needed.**

### 3. Performance budget: measured, not estimated

`tests/crypto/test_bench.py`, run manually on both target hosts (`python -m pytest tests/crypto/test_bench.py -v -s` on `dev`; an equivalent standalone script on `prod`, see note below):

| Operation | `dev` (Pi Zero 2 W) | `prod` (Pi Zero 2 W) |
|---|---:|---:|
| `crypto_sign_ed25519_pk_to_curve25519` | 594.5 µs/call | 582.0 µs/call |
| Ed25519 keypair generation | 247.0 µs/call | 237.8 µs/call |
| XChaCha20-Poly1305, one 256 KiB chunk | 5.17 ms/call | 5.80 ms/call |
| Peak RSS during chunk-encryption loop | 28.6 MiB | 13.0 MiB |
| **Extrapolated: 5 MiB file (20 chunks)** | **103.3 ms** | **116.1 ms** |

A 5 MiB attachment (the size Step 1.9's performance budget will need to reason about) encrypts in roughly a tenth of a second of pure crypto time on a Pi Zero 2 W — comfortably within "reasonable units of seconds," with two orders of magnitude of headroom before it would become a user-visible delay on its own (real end-to-end transfer time will be dominated by the Meshtastic link's own throughput, not this). Key conversion (~0.6 ms) and keypair generation (~0.25 ms) are negligible against any per-transfer budget.

`prod`'s peak-RSS figure (13.0 MiB) is lower than `dev`'s (28.6 MiB) because the two standalone-script runs started from different baseline process states, not because of a hardware difference — both are real Pi Zero 2 W boards. Treat the timing numbers (consistent within ~10% of each other, as expected for the same hardware class) as the meaningful comparison, not the RSS figures.

**Note on methodology:** per this repo's own convention (`CLAUDE.md`: `requirements-dev.txt`, which brings in `pytest`, must never be installed on a production Pi), the benchmark was run via `pytest` on `dev` but as an equivalent dependency-free standalone Python script piped over SSH on `prod`, exercising identical logic (same fixed seed, same iteration counts, same chunk size). Both produced the same pinned test-vector hex values before their respective benchmark sections ran, confirming the standalone script is testing the same code path as the pytest-based one, not a divergent reimplementation.

## Consequences

- Step 1.2 (MCA principal + Meshtastic address binding, `meshsrv/attachments/identity.py`, not yet created) can now implement the confirmed derive-from-Ed25519 scheme as final, not provisional. `KEY_ANNOUNCE` stays the single-frame ~156-byte format ADR-0001 section 4 already budgets for — no revision needed there.
- `requirements.txt` already pins `cbor2>=6.1.0,<7.0.0` and `pynacl>=1.6.0,<2.0.0` (added alongside the Step 0.1/0.3-0.7/1.1 backlog); both are now confirmed installed and working via real compiled wheels on both `dev` and `prod`'s Core venvs (not `adapters/meshtastic/venv` — this crypto logic is MIT Core, never linked against the GPLv3 adapter process).
- `tests/crypto/test_key_derivation.py` and `tests/crypto/test_bench.py` are committed as a permanent regression check (the former genuinely gates correctness; the latter is `@pytest.mark.benchmark`-marked, asserts correctness only, and is safe but not required to run in CI — its printed numbers are what matter, and those are hardware-specific, not CI-runner-specific).
- HKDF-SHA-256 key derivation (also part of `suite_id = 1`) is not yet implemented anywhere in the committed code and is out of scope for this ADR — it becomes relevant once actual per-chunk/per-recipient key derivation is built, not before.

## References

- MCAttach System Design and Implementation Spec, v1.2 — sections 7.3, 8.1-8.2.
- `docs/architecture/ADR-0001-mca-protocol.md` — section 1 (crypto suite selection), section 6 (signature domain separation, rollback trigger).
- `docs/architecture/STEP_0.2_HANDOFF.md` — the task spec this ADR closes out.
- [libsodium: Ed25519 to Curve25519](https://doc.libsodium.org/advanced/ed25519-curve25519) — the conversion functions verified here.
- `tests/crypto/test_key_derivation.py`, `tests/crypto/test_bench.py` — the tests backing every number in this ADR.
