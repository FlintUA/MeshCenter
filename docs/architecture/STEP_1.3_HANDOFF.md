# Handoff for the session with `dev`/`prod` access ("code")

Two tasks below. Do them in order — the first is a two-minute admin step,
the second is the real work (Step 1.3, first real hardware test).

## Task A — push and open a PR for Step 1.2

The Cowork session (this repo's shared working directory, via device
bridge on `minipc`) implemented Step 1.2 and committed it locally, but
that bridge has no network access to GitHub — same situation as Step 0.2.

- Branch already exists locally on the shared checkout:
  `mcattach-step-1.2-principal-key-exchange`, one commit on top of
  `origin/main` (`59d4fa8`, the already-merged PR #222).
- Push it and open a PR the same way as #222 (squash or merge, your call
  — #222 was squashed). Do **not** rewrite/rebase the commit; just push
  and open.
- Diffstat to sanity-check before opening: 6 files changed — new
  `meshsrv/attachments/identity.py`, `meshsrv/attachments/key_exchange.py`,
  `tests/test_mca_identity.py`, `tests/test_key_exchange.py`; modified
  `meshsrv/attachments/db/migrations.py` (adds migration 4) and
  `docs/architecture/ADR-0004-threat-model.md` (closes threat-model row
  13, part of row 18, moves row 20 from Owed to Partial).
- 378/378 MCAttach tests passed locally on `minipc` before this commit
  (run `python -m pytest tests/test_mca_identity.py tests/test_key_exchange.py
  tests/test_delivery_contract.py tests/test_mca_codec.py
  tests/test_mca_db_migrations.py tests/test_mca_workspace.py
  tests/test_mime_allowlist.py tests/test_provider_registry.py
  tests/test_relay_mock.py tests/crypto/` again on your end before
  opening, since you have the real `dev`/`prod` hardware this was never
  tested against for anything beyond the pure-Python logic).

Report back: PR URL + your own test run result, same format as your
Step 0.2 report.

## Task B — Step 1.3: real `MeshtasticTextAdapter`, first real hardware test

This is the next step in `MCAttach_Execution_Plan.md`. Do this **after**
Task A's PR is merged (`git pull`/checkout `main` fresh, don't build on
top of the unmerged branch).

### What already exists for you to build on

- `meshsrv/attachments/delivery/base.py` — the `DeliveryAdapter` abstract
  contract (Step 0.4): `capabilities()`, `resolve_route()`, `encode()`,
  `send()`, `ingest()`. Two fake implementations
  (`meshsrv/attachments/delivery/fakes.py`) already pass the full
  contract test suite (`tests/test_delivery_contract.py`) — read that
  file first; `MeshtasticTextAdapter` must pass the *same* contract
  tests, parametrized onto the real adapter instead of the fake one.
- `meshsrv/attachments/key_exchange.py` (Step 1.2, just landed) —
  `KeyExchangeCoordinator.handle_incoming(envelope: DeliveryEnvelope)`
  already contains all the KEY_REQUEST/KEY_ANNOUNCE/KEY_ACK protocol
  logic and is adapter-agnostic. It does not need to change for this
  step — it's already designed to be driven by whatever `ingest()`
  returns.
- `meshsrv/adapter_ipc_client.py` (`AdapterIPCTransport`) and
  `meshsrv/transport_router.py` (`TransportRouter`) — the existing
  Core-side IPC client to the adapter subprocess. `RadioTransport`
  (`meshsrv/radio_transport.py`) is the interface it implements:
  `send_text()`, `get_nodes()`, etc. **Do not modify anything under
  `adapters/meshtastic/`** — Step 1.2/1.3's whole point is building on
  top of this existing boundary, not inside it.

### What to build

`meshsrv/attachments/delivery/meshtastic.py` (MIT Core, matching spec
section 19, `MeshtasticTextAdapter(DeliveryAdapter)`):

- `capabilities()`: `wire_formats={MCA1_TEXT}`, `max_payload_bytes=180`
  (ADR-0001's own text-transport ceiling — confirm this is still right
  against whatever Meshtastic's real per-message text limit is on the
  firmware your `dev`/`prod` radios run; ADR-0001 section 4 has the byte
  budget this was computed from), `supports_direct=True`,
  `supports_channel=False` (Stage 1 scope only), `supports_incoming=True`,
  `ack_semantics`: `CONFIRMED` over USB serial, `BEST_EFFORT` over BLE
  (spec 19.3 — see below), `connector_state` reflecting whatever
  `AdapterIPCTransport.get_connection_info()` currently reports.
- `resolve_route()`: turns a `!node_id` selection into a DIRECT `Route`.
- `encode()`: `codec.to_text(logical_message)` (already implemented,
  Step 0.3) — raise `PayloadTooLargeError` if over 180 bytes, checked
  against the actual encoded output like the fakes already do.
- `send()`: calls `AdapterIPCTransport.send_text()` (via
  `TransportRouter`, however Core currently obtains one — check
  `server.py`/`api/api_meshtastic.py` for the existing pattern, don't
  invent a new one) with the destination node ID. Returns a
  `DeliveryReceipt` — `sent=True`/`external_message_id` if
  `send_text()` reports success.
- `ingest()`: spec 19.1's "дешёвая проверка префикса" — this is the
  **input** hook, not something this adapter polls for itself. Find
  wherever Core currently saves an incoming direct text message (message
  store update in `server.py`/`meshsrv/`, triggered by the radio
  listener) and add the cheap `text.startswith("MCA1:")` check *there*,
  after the normal message is already saved (spec 19.1: never block the
  radio listener on parsing/crypto/Relay — the check itself must be O(1)
  string comparison only). When it matches, hand the raw text off to
  `MeshtasticTextAdapter.ingest(transport_event)`, which does
  `codec.from_text()` and returns the `DeliveryEnvelope` (`route_type=
  DIRECT`, `source_address=<sender's !node_id>`, `adapter_id=
  "meshtastic"`). Everything downstream of `ingest()` returning a
  `DeliveryEnvelope` (routing it into `KeyExchangeCoordinator.
  handle_incoming()`, sending back whatever it returns) is regular Core
  code, not part of this adapter.
- BLE limitation (spec 19.3, already flagged by the plan as **required**
  for this step, not optional): if the adapter's `capabilities()`
  reports `connector_state`/whatever field indicates the active
  transport is BLE, `ack_semantics` must be `BEST_EFFORT`, and the
  send-flow UI (wherever the actual send button lives — this may be
  later than Step 1.3's own UI work, so at minimum the adapter itself
  must expose this fact correctly) must show "Отправка без
  подтверждения" rather than implying delivery confirmation it cannot
  provide. The receiving side of Step 1.3's own acceptance test **must**
  be USB serial, not BLE, on both `dev` and `prod` — BLE cannot reliably
  receive on current MeshCenter (spec 19.3, this is a hard requirement
  from the existing codebase's own known limitation, not a new one this
  step introduces).

### Tests

- `tests/test_delivery_contract.py`'s existing parametrized suite,
  extended to also run against `MeshtasticTextAdapter` — but only the
  parts that don't need real hardware (encode/decode round-trip,
  oversized-payload rejection, capabilities reporting). Anything that
  needs a live radio goes in a separate hardware-only test, not CI.
- A new `tests/hardware/test_meshtastic_delivery_adapter_live.py` (or
  wherever this repo's existing hardware-only tests live — check for a
  precedent before inventing a new location/marker convention) that
  actually sends an `MCA1-TEXT` `KEY_REQUEST` from `dev` to `prod` over
  USB serial and asserts `prod` receives it, decodes it via `ingest()`,
  and (using Step 1.2's already-built `KeyExchangeCoordinator`) responds
  with a real `KEY_ANNOUNCE` that `dev` receives back. This is the
  literal DoD: "отправка MCA1-TEXT реально уходит через dev's
  serial-адаптер и реально приходит на prod (первый настоящий
  аппаратный тест, не мок)."

### DoD (from the plan, verbatim)

- `MCA1-TEXT` sent from `dev`'s serial adapter really arrives at `prod`
  — first real hardware test, not a mock.
- BLE limitation is explicitly encoded, not just a comment: если
  активный transport — BLE, `ack_semantics`/whatever the UI reads must
  say `Отправка без подтверждения`; receiving side of MVP is tested only
  via USB serial.

### Report back format (same as Step 0.2/Task A)

- What you built (files, one line each).
- Real command/log evidence of the dev→prod round trip (not "it worked" —
  the actual sent bytes / received bytes / KEY_ANNOUNCE content, like
  ADR-0002's measured-numbers table).
- Test counts (existing suite + new hardware test), confirmed on both
  `dev` and `prod` where relevant.
- Anything from the plan you had to deviate from, and why (same
  discipline as Step 1.1's Relay-contract corrections) — I will read the
  diff and cross-check against `docs/architecture/ADR-0001-mca-protocol.md`
  section 4 (byte budget) and spec section 19 before treating this step
  as closed.
