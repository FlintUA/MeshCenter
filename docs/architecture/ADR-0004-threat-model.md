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
| 1 | Подмена файла Relay | Partial | Relay-side digest checks: `tests/test_relay_mock.py::test_upload_chunk_rejects_corrupted_chunk`. AEAD-per-chunk and sender-signature verification are crypto/state-machine concerns - owed by ADR-0002 + Step 1.4/1.5. |
| 2 | Просмотр Relay (ciphertext/sealed envelopes only) | Covered structurally | `MockRelayStore` never parses chunk/manifest/envelope content, only digests and lengths (`meshsrv/attachments/relay/mock_server.py`). No code path in the Relay contract touches plaintext. |
| 3 | Пересылка pointer (recipient-bound envelope) | Owed | ADR-0002 (per-recipient sealed box) + Step 1.4/1.5. |
| 4 | Replay OFFER (transfer ID/signature/dedup/tombstone) | Partial | Signature: `tests/test_mca_codec.py` (round-trip + tamper detection). Tombstone storage: `tests/test_mca_db_migrations.py::test_tombstone_*`. End-to-end replay-is-rejected behavior at the state-machine level is owed by Step 1.4/1.5. |
| 5 | Произвольный URL/SSRF | **Covered** | `tests/test_provider_registry.py::test_unknown_provider_id_never_triggers_network_call` and `test_provider_registry_module_does_not_import_networking_libraries`. This is this ADR's own Step 0.7 DoD item. |
| 6 | Path traversal | **Covered** | `tests/test_mca_workspace.py::test_resolve_saved_path_rejects_traversal` (parametrized) and `test_unique_file_name_avoids_overwriting_existing_file`. |
| 7 | Огромный файл | Partial | Relay chunk-size ceiling: `tests/test_relay_mock.py` (`_max_chunk_bytes`). Declared-size-vs-actual-size cross-check at the sender/receiver layer and a streaming hard limit on disk writes are owed by Step 1.4/1.5. |
| 8 | MIME spoofing | Partial | Allowlist itself: `tests/test_mime_allowlist.py`. Actual magic-byte sniffing (vs. trusting an extension or client claim) is owed by whichever step implements the receive-side file-write path (Step 1.5 or later) - `mime_allowlist.py`'s docstring flags this explicitly. |
| 9 | Executable/HTML attack | Owed | UI/download-disposition step, not yet built. `mime_allowlist.py` already excludes executables and HTML/SVG/JS from the MVP allowlist, which is most of this mitigation, but the "never auto-open, explicit disposition" UI behavior itself is a later step. |
| 10 | Zip bomb | N/A for MVP | No archive MIME type is in the MVP allowlist (`mime_allowlist.py`) and MCAttach performs no decompression - the threat has no code path to exploit yet. Re-open this row if/when archive support is ever added. |
| 11 | Disk exhaustion | **Covered** | `tests/test_mca_workspace.py::test_quota_check` and `test_low_disk_level` (OK/WARN/BLOCK thresholds from design spec section 16.5). |
| 12 | CPU/RAM exhaustion | Partial | Bounded parser: `tests/test_mca_codec.py::test_decode_offer_rejects_deeply_nested_cbor`, `test_fuzz_decode_offer_never_raises_uncaught`. Worker-count limits and streaming crypto are owed by Step 1.4 (job queue) and the crypto ADR-0002 implementation. |
| 13 | Key change (TOFU) | Owed | Step 1.2 (`mca_recipient_bindings`, TOFU confirmation flow). |
| 14 | Потеря ключа (backup) | Owed, post-MVP | Explicitly deferred by the design spec itself. |
| 15 | Clock skew | **Covered** | Server-side `hard_expires_at` is authoritative in the mock Relay regardless of client-reported time: `tests/test_relay_mock.py` (object expiry checked against the store's own clock, never a client-supplied "now"). |
| 16 | Доступ к MeshCenter в LAN | N/A for MCAttach | Inherited from existing MeshCenter web auth/CSRF/permissions - out of this subsystem's scope. |
| 17 | Поддельный messenger webhook | N/A for MVP | No messenger adapter exists yet (Meshtastic-only through Stage 1). Re-open when a Telegram/WhatsApp adapter is built. |
| 18 | Кража bot/API token | N/A for MVP | Same as #17. `keys/` directory permissions (`0700`) are already enforced (`tests/test_mca_workspace.py::test_ensure_workspace_creates_directories_and_locks_keys_dir`) as the closest already-built analog; individual key-file `0600` permissions are owed by Step 1.2. |
| 19 | Подмена chat/phone binding | N/A for MVP | Meshtastic-only through Stage 1; re-open for the first messenger adapter. |
| 20 | Forward MCA-кода | Owed | ADR-0002 (recipient-bound sealed envelope) + Step 1.2. |
| 21 | Тихий downgrade маршрута | Partial | `DeliveryAdapter.encode()` already refuses a route type it wasn't asked for rather than silently substituting one: `tests/test_delivery_contract.py::test_encode_rejects_unsupported_route_type_for_text_adapter`. The UI-level "always show the actually selected adapter/route before sending" behavior (design spec section 5.4) is owed by the send-flow UI step. |
| 22 | Дубли при нескольких transports | Partial | Schema-level uniqueness: `attachment_deliveries` has `UNIQUE (attachment_id, idempotency_key)` (`meshsrv/attachments/db/migrations.py`). End-to-end idempotent-handling-across-adapters behavior is owed by Step 1.4/1.5. |

## Summary

Of 22 rows: **4 fully covered** by an existing automated test (5, 6, 11, 15), **2 not applicable to MVP scope as built** (10 - no archive support exists to exploit; 16 - out of subsystem scope, inherited from MeshCenter's own web auth), **3 not yet applicable because no messenger adapter exists** (17, 18, 19 - though #18's directory-permission half is already covered), and the remaining **13 rows are partial or fully owed** by ADR-0002 (crypto) and Stage 1 steps 1.2, 1.4, and 1.5.

**Definition of done for Stage 1** (Execution Plan): every row in this table that is not marked N/A must be "Covered" - with a cited test - before Stage 1 is considered complete. A row still "Owed" or "Partial" when Step 1.9 (hardware acceptance testing) begins is a gap that must be explicitly accepted by the project owner, not silently carried forward.

## References

- MCAttach System Design and Implementation Spec, v1.2 - section 20.1, 20.2.
- ADR-0001 (`docs/architecture/ADR-0001-mca-protocol.md`), ADR-0003 (`docs/architecture/ADR-0003-attachments-sqlite-exception.md`).
- `tests/test_mca_codec.py`, `tests/test_delivery_contract.py`, `tests/test_relay_mock.py`, `tests/test_mca_workspace.py`, `tests/test_mca_db_migrations.py`, `tests/test_mime_allowlist.py`, `tests/test_provider_registry.py`.
