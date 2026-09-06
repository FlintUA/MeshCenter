# Handoff for the session with `dev`/`prod` access ("code")

One task below — push and open a PR for Step 1.4. No hardware round trip
is needed for this step (everything is tested against the mock Relay and
in-process/subprocess fakes), so this is purely the same "push what the
Cowork session built locally" pattern as Steps 0.2 and 1.2's Task A.

## Push and open a PR for Step 1.4

- Branch already exists locally on the shared checkout:
  `mcattach-step-1.4-sender-state-machine`, two commits on top of
  `origin/main` (`5a76eff`, the already-merged PR #224):
  - `b819122` — object encryption (manifest blob, sealed envelopes,
    chunk/header AEAD): `docs/architecture/ADR-0006-object-encryption.md`,
    new `meshsrv/attachments/crypto.py`, new
    `meshsrv/attachments/manifest.py`, `identity.py` gains
    `derive_x25519_private()`, new `tests/test_crypto.py`,
    `tests/test_manifest.py`.
  - `ec47af8` — the sender state machine itself: new
    `meshsrv/attachments/sender.py`, migration 5
    (`mca_sender_state` table) in `meshsrv/attachments/db/migrations.py`,
    new `tests/test_sender.py`, new
    `tests/test_sender_crash_recovery.py` + `tests/_sender_crash_driver.py`
    (the scripted kill-9 tests — these spawn real OS subprocesses and send
    real `SIGKILL`, so they need an environment where `subprocess`/signals
    work normally; no special hardware, just a normal POSIX process
    model), and an update to
    `docs/architecture/ADR-0004-threat-model.md` (rows 3 and 20 move to
    Covered).
- Push it and open a PR the same way as #223/#224 (squash or merge, your
  call). Do **not** rewrite/rebase either commit; just push and open.
- 968/968 tests passed locally on `minipc` before this commit
  (`python -m pytest -q --ignore=tests/hardware`). Please re-run the same
  command on your end before opening, since I have no real Pi hardware to
  verify against for anything beyond pure-Python/mock-Relay logic — in
  particular the kill-9 tests
  (`tests/test_sender_crash_recovery.py`) spawn real subprocesses via
  `sys.executable` and a real `werkzeug` HTTP server on a random localhost
  port; if `dev`/`prod`'s Python environment handles subprocess/signals/
  sockets any differently than this sandbox, that's exactly the kind of
  thing worth re-confirming there.
- No new third-party dependencies were added in this step beyond what's
  already in `requirements.txt`/`requirements-dev.txt` from Steps 0.2/1.1
  (`cbor2`, `pynacl`) — `crypto.py`/`manifest.py` use only `nacl.bindings`/
  `nacl.public`/`cbor2`, all already-installed. `werkzeug` (used only by
  the new crash-recovery test, not by any runtime code) is already a
  transitive dependency of Flask, which the mock Relay already required
  in Step 1.1 — please confirm `import werkzeug` and
  `from werkzeug.serving import make_server` work out of the box on
  `dev`/`prod` too, since I can't verify that outside this sandbox's own
  installed environment.

Report back: PR URL + your own test run result, same format as previous
steps' reports.

## Nothing else to do for Step 1.4

This step's scope is the *sender* side only (design spec's own step
boundary) — no real hardware round trip belongs to Step 1.4, unlike Steps
0.2/1.3. The receive-path counterpart (opening a downloaded manifest,
verifying the ADR-0006 trust chain, decrypting into the download folder)
is Step 1.5, not part of this handoff.
