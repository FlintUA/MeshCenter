# ADR-0003: `attachments.db` as the second SQLite exception

**Status:** Accepted
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2`, section 16.3; Execution Plan Step 0.6

## Context

MeshCenter's storage convention (`CLAUDE.md`, "Storage conventions") is: everything is an atomically-written JSON file via `storage/json_store.py` (`safe_read_json`/`safe_write_json`), with exactly one documented exception - `storage/waypoint_store.py`'s `waypoints.db` (SQLite), used because waypoints need query/update patterns JSON files don't fit well. That exception is real, in the shipped code, and predates MCAttach.

MCAttach's attachment lifecycle needs: a queue of outgoing/incoming jobs (`mca_jobs`), uniqueness constraints on `transfer_id` within a workspace, several related tables with foreign-key-shaped relationships (`attachments` / `attachment_recipients` / `attachment_deliveries` / `attachment_events`), and state transitions that must be atomic across more than one row at a time (e.g. marking a delivery `acknowledged` and the parent attachment `state` in the same transaction). None of that is a good fit for independently-written JSON files without re-inventing transactions and referential integrity by hand.

## Decision

`meshsrv/attachments/db/` introduces `data/mca/<principal-id>/attachments.db` (SQLite) as the **second** documented exception to "everything is JSON", by direct analogy with `waypoints.db` - not as an independent decision made from scratch.

Two things this ADR fixes so the exception doesn't quietly become the norm:

1. **The physical root is transport-neutral, not a radio-profile path.** `attachments.db` lives under `data/mca/<principal-id>/`, computed exclusively by `MCAWorkspaceManager` (`meshsrv/attachments/workspace.py`) - never under `data/profiles/<node-id>/`. An MCA principal is not the same thing as a Meshtastic radio profile (design spec section 16.2): switching a workspace's active connector from Meshtastic to MeshCore must not create a new archive, and `ProfileManager`'s existing per-profile stores (`messages.json`, `waypoints.db`, etc.) must never be extended to also carry MCA data.
2. **JSON stays the default for everything else in MCAttach that isn't relational.** Provider Registry *configuration* (human-edited, single-object-per-provider) still goes through `safe_read_json`/`safe_write_json`, per the design spec's explicit instruction (section 16.3) - only the transactional, multi-table, queryable state (attachments, deliveries, jobs, bindings, tombstones) goes into `attachments.db`.

No path under `data/mca/` is ever computed by string concatenation outside `MCAWorkspaceManager` - enforced by a repo-wide grep test (`tests/test_mca_workspace.py::test_no_stray_data_mca_path_construction`).

## Consequences

- `MCAWorkspaceManager` follows the same shape as `storage/profile_manager.py::ProfileManager` (constructor takes a `data_dir`, validates the scoping ID with a regex, resolves and verifies the child path did not escape its parent) precisely so a future reviewer already familiar with `ProfileManager` recognizes the pattern instead of learning a second one.
- `attachments.db`'s schema (ten tables: `attachments`, `attachment_recipients`, `attachment_deliveries`, `attachment_events`, `mca_contacts`, `mca_recipient_bindings`, `mca_connector_profiles`, `mca_provider_profiles`, `mca_jobs`, `mca_tombstones` - design spec section 16.4) is versioned through an explicit migration list (`meshsrv/attachments/db/migrations.py`) with both up and down SQL, tested against a clean in-memory database on every CI run.
- `keys/` (workspace private keys) is created with directory mode `0700`, not the literal `0600` the design spec's prose names for "workspace private keys" - `0600` on a directory removes the execute bit needed to even list or open files inside it, which would make the directory unusable. Individual key files written into `keys/` by later steps (crypto ADR-0002, Step 1.2) are what must be `0600`; this ADR records the deviation so a future reviewer doesn't "fix" the directory back to a broken `0600` while implementing key storage.
- `mca_provider_profiles` (Provider Registry rows) lives in `attachments.db` even though the *default* provider's bootstrap config may also be shipped as JSON (design spec section 10.1) - the registry's queryable/joinable form is the SQLite table; a JSON file, if one exists, is only a seed/import source for it, never a second source of truth read at request time.

## References

- MCAttach System Design and Implementation Spec, v1.2 - sections 16.1-16.5, 10.1.
- `storage/waypoint_store.py`, `storage/profile_manager.py` - the existing precedent this ADR follows.
- `CLAUDE.md` - "Storage conventions" section.
- ADR-0001 (`docs/architecture/ADR-0001-mca-protocol.md`) section 6 - tombstone retention (≥7 days), which `mca_tombstones` implements.
