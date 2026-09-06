# ADR-0004: Threat model checklist for Stage 1

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2`, section 20.1; Execution Plan Step 0.7

## Context

Design spec section 20.1 lists 22 threat/mitigation pairs in prose-table form. That table is a good design review artifact but not directly actionable as a test checklist: it doesn't say which mitigation already has a test, which is out of scope for MVP, and which is still owed by a specific future step. This ADR is that reduction - one row per threat, with its current status and, where relevant, the exact test that proves the mitigation.

This document is expected to be edited at the end of every later step (1.2 through 1.9) - a step that closes one of the "owed" rows below updates its status and cites the new test, rather than leaving this file to rot as a Stage-0 snapshot.

## Checklist

| # | Threat (spec 20.1) | Status | Evidence / owed by |
|---:|---|---|---|
| 1 | Подмена файла Relay | Partial | Relay-side digest checks: `tests/test_relay_mock.py::test_upload_chunk_rejects_corrupted_chunk`. AEAD-per-chunk and sender-signature verification are crypto/state-machine concerns - owed by Step 1.4/1.5 (crypto suite itself confirmed by ADR-0002). |
| 2 | Просмотр Relay (ciphertext/sealed envelopes only) | Covered structurally | `MockRelayStore` never parses chunk/manifest/envelope content, only digests and lengths (`meshsrv/attachments/relay/mock_server.py`). No code path in the Relay contract touches plaintext. |
| 3 | Пересылка pointer (recipient-bound envelope) | Owed | Per-recipient sealed box (ADR-0002 confirms the crypto primitives; the actual sealed envelope is not implemented yet) + Step 1.4/1.5. |
| 4 | Replay OFFER (transfer ID/signature/dedup/tombstone) | Partial | Signature: `tests/test_mca_codec.py` (round-trip + tamper detection). Tombstone storage: `tests/test_mca_db_migrations.py::test_tombstone_*`. End-to-end replay-is-rejected behavior at the state-machine level is owed by Step 1.4/1.5. |
| 5 | Произвольный URL/SSRF | **Covered** | `tests/test_provider_registry.py::test_unknown_provider_id_never_triggers_network_call` and `test_provider_registry_module_does_not_import_networking_libraries`. This is this ADR's own Step 0.7 DoD item. |
| 6 | Path traversal | **Covered** | `tests/test_mca_workspace.py::test_resolve_saved_path_rejects_traversal` (parametrized) and `test_unique_file_name_avoids_overwriting_existing_file`. |
| 7 | Огромный файл | Partial | Relay chunk-size ceiling: `tests/test_relay_mock.py` (`_max_chunk_bytes`). Declared-size-vs-actual-size cross-check at the sender/receiver layer and a streaming hard limit on disk writes are owed by Step 1.4/1.5. |
| 8 | MIME spoofing | Partial | Allowlist itself: `tests/test_mime_allowlist.py`. Actual magic-byte sniffing (vs. trusting an extension or client claim) is owed by whichever step implements the receive-side file-write path (Step 1.5 or later) - `mime_allowlist.py`'s docstring flags this explicitly. |
| 9 | Executable/HTML attack | Owed | UI/download-disposition step, not yet built. `mime_allowlist.py` already excludes executables and HTML/SVG/JS from the MVP allowlist, which is most of this mitigation, but the "never auto-open, explicit disposition" UI behavior itself is a later step. |
| 10 | Zip bomb | N/A for MVP | No archive MIME type is in the MVP allowlist (`mime_allowlist.py`) and MCAttach performs no decompression - the threat has no code path to exploit yet. Re-open this row if/when archive support is ever added. |
| 11 | Disk exhaustion | **Covered** | `tests/test_mca_workspace.py::test_quota_check` and `test_low_disk_level` (OK/WARN/BLOCK thresholds from design spec section 16.5). |
| 12 | CPU/RAM exhaustion | Partial | Bounded parser: `tests/test_mca_codec.py::test_decode_offer_rejects_deeply_nested_cbor`, `test_fuzz_decode_offer_never_raises_uncaught`. Worker-count limits and streaming crypto are owed by Step 1.4 (job queue). |
| 13 | Key change (TOFU) | **Covered** | Step 1.2: `tests/test_key_exchange.py::test_key_change_after_tofu_confirmation_is_parked_not_applied` (a KEY_ANNOUNCE contradicting an already-confirmed binding is parked, never applied in place) and `test_accept_pending_key_change_promotes_and_requires_fresh_tofu` (accepting a key change requires a fresh, explicit TOFU confirmation - trust is never inherited from the replaced key). `mca_recipient_bindings.pending_public_identity`/`pending_key_epoch`/`pending_detected_at` (migration 4) is the persisted state backing this. |
| 14 | Потеря ключа (backup) | Owed, post-MVP | Explicitly deferred by the design spec itself. |
| 15 | Clock skew | **Covered** | Server-side `hard_expires_at` is authoritative in the mock Relay regardless of client-reported time: `tests/test_relay_mock.py` (object expiry checked against the store's own clock, never a client-supplied "now"). |
| 16 | Доступ к MeshCenter в LAN | N/A for MCAttach | Inherited from existing MeshCenter web auth/CSRF/permissions - out of this subsystem's scope. |
| 17 | Поддельный messenger webhook | N/A for MVP | No messenger adapter exists yet (Meshtastic-only through Stage 1). Re-open when a Telegram/WhatsApp adapter is built. |
| 18 | Кража bot/API token | N/A for MVP | Same as #17. `keys/` directory permissions (`0700`, `tests/test_mca_workspace.py::test_ensure_workspace_creates_directories_and_locks_keys_dir`) and, as of Step 1.2, the individual private-key file's own `0600` permissions (`tests/test_mca_identity.py::test_private_key_file_is_0600_inside_keys_dir_which_is_0700`) are both now covered - no remaining owed part on the storage side; the row stays N/A overall pending a messenger adapter with its own bot/API token to steal. |
| 19 | Подмена chat/phone binding | N/A for MVP | Meshtastic-only through Stage 1; re-open for the first messenger adapter. |
| 20 | Forward MCA-кода | Partial | Step 1.2 built the identity/binding plumbing this depends on - a `mca_recipient_bindings` row is always tied to one specific `(adapter_id, transport_address)` pair and one specific public identity (`tests/test_key_exchange.py::test_full_round_trip_over_fake_text_adapter`), so there is a concrete notion of "who this MCA code is bound to" to check against. The actual enforcement - a recipient-bound sealed envelope that a forwarded pointer/code cannot be decrypted or accepted by anyone else - is not implemented yet and is owed by Step 1.4/1.5. |
| 21 | Тихий downgrade маршрута | Partial | `DeliveryAdapter.encode()` already refuses a route type it wasn't asked for rather than silently substituting one: `tests/test_delivery_contract.py::test_encode_rejects_unsupported_route_type_for_text_adapter`. The UI-level "always show the actually selected adapter/route before sending" behavior (design spec section 5.4) is owed by the send-flow UI step. |
| 22 | Дубли при нескольких transports | Partial | Schema-level uniqueness: `attachment_deliveries` has `UNIQUE (attachment_id, idempotency_key)` (`meshsrv/attachments/db/migrations.py`). End-to-end idempotent-handling-across-adapters behavior is owed by Step 1.4/1.5. |

## Summary

Of 22 rows: **5 fully covered** by an existing automated test (5, 6, 11, 13, 15), **2 not applicable to MVP scope as built** (10 - no archive support exists to exploit; 16 - out of subsystem scope, inherited from MeshCenter's own web auth), **3 not yet applicable because no messenger adapter exists** (17, 18, 19 - #18's storage-permission half is now fully covered), and the remaining **12 rows are partial or fully owed** by Stage 1 steps 1.4 and 1.5 (the crypto suite itself is already confirmed by ADR-0002; what remains owed is the sealed-envelope/state-machine code that uses it).

**Definition of done for Stage 1** (Execution Plan): every row in this table that is not marked N/A must be "Covered" - with a cited test - before Stage 1 is considered complete. A row still "Owed" or "Partial" when Step 1.9 (hardware acceptance testing) begins is a gap that must be explicitly accepted by the project owner, not silently carried forward.

## References

- MCAttach System Design and Implementation Spec, v1.2 - section 20.1, 20.2.
- ADR-0001 (`docs/architecture/ADR-0001-mca-protocol.md`), ADR-0002 (`docs/architecture/ADR-0002-crypto-suite.md`), ADR-0003 (`docs/architecture/ADR-0003-attachments-sqlite-exception.md`).
- `tests/test_mca_codec.py`, `tests/test_delivery_contract.py`, `tests/test_relay_mock.py`, `tests/test_mca_workspace.py`, `tests/test_mca_db_migrations.py`, `tests/test_mime_allowlist.py`, `tests/test_provider_registry.py`, `tests/test_mca_identity.py`, `tests/test_key_exchange.py`.
