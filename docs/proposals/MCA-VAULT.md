# MCA-VAULT: optional encrypted MCAttach vault (proposal, no code)

Status: proposal with the owner's decisions recorded (section 0); the design is ADR-0013 (proposed). Not implemented. The project backlog
(`claude/MeshCenter_Backlog_Small_Items.md`, section MCA-VAULT) is not on the development machine; the owner pastes the
relevant spec into messages, so this file follows those messages. Anything that differs from the backlog text should be
corrected there or here before work starts.

## 0. Decisions (owner, 2026-10-31)

1. **"Save to Files" stays encrypted inside the vault.** A saved file is moved into the vault's encrypted files area
   (`files/<attachment-id>.mcv`). A plaintext copy leaves the vault only through "Download to device" (streamed
   through the decryptor to the browser; nothing is written decrypted to the node's disk). Preview stays in memory.
2. **No auto-lock in v1.** The vault is locked after every start of the service or container (and therefore after a
   reboot), and there is a **"Lock now"** button. Auto-lock after idle time is a later version.
3. **Argon2id is calibrated at enable time** to about 1 second of unlock time on the actual host, with a **minimum
   of 32 MiB** of memory. The parameters (and the measured time) are stored in the vault header, so they can be raised
   later (re-calibrate = re-wrap the storage key, files untouched).
4. **File names are encrypted too.** While locked, the transfer list shows the contact, the time and the size with a
   "locked" placeholder instead of the name. The same rule applies to the optional comment that travels with a
   transfer (it can say as much as a name does); the MIME type and size stay plain so the locked list can show a
   generic icon and the size.

## 1. Goal and non-goals

Goal: an optional passphrase-protected vault for everything MCAttach keeps on a node, so that a stolen SD card, a
copied backup, a decommissioned Pi or a curious second user of the machine sees no MCAttach file content and cannot use
the node's MCAttach identity.

In scope: received files, sent files (the outgoing copy), the MCAttach private key material, the per-transfer secrets.

Not in scope (stated so nobody assumes otherwise):
- Chats, nodes, telemetry, camera pictures, the gallery and every other MeshCenter file stay as they are. This is not
  full-disk encryption.
- A running node that is unlocked is as exposed as today: root on a live, unlocked node can read the key from memory.
- Protection of the relay copy: that is already end-to-end encrypted (ADR-0006) and unchanged.

## 2. Threat model

| Threat | Today | With the vault |
|---|---|---|
| Stolen or cloned SD card / disk image, node powered off | files, file names, DB secrets and the Ed25519 seed readable | nothing readable (content, names, comments, keys) without the passphrase or recovery key |
| Backup copied elsewhere (`data/mca/`) | readable | readable only as ciphertext |
| Decommissioned or sold device | must be wiped by hand | wipe is "destroy the wrapped key" |
| Another local user on a shared machine / Docker volume | readable (0600/0700 only) | ciphertext only while locked; same as today while unlocked |
| Remote attacker with the web login | can read files via the API | unchanged while unlocked; while locked the API serves nothing |
| Root on the running, unlocked node | full access | full access (not addressed) |
| Offline passphrase guessing | n/a | slowed by Argon2id; strength of the passphrase is the limit |
| Forgotten passphrase | n/a | recovery key (see 5); without both, data is gone by design |

The vault does not hide that MCAttach exists, the file sizes, the MIME types, the contacts or the timing of transfers
(metadata in `attachments.db` stays plain except for the items listed in 3).

## 3. What is protected

Verified against the current layout (`data/mca/<principal-id>/`):
- `cache/incoming/<id>` received copies; `files/` saved files; `spool/outgoing/<id>` outgoing plaintext; `quarantine/`.
- `keys/`: the Ed25519 identity seed (0600) and the provider upload-token files.
- `attachments.db`: the columns that hold secrets (`data_key`, `nonce_prefix`, `receipt_secret` in the sender and
  receiver state tables) and, by decision 4, the **file name** (`attachments.file_name`) and the **comment** of every
  transfer. The rest of the database (states, sizes, MIME types, times, contact ids) stays plain so the service can
  list transfers while locked ("3 locked offers"; each row shows contact, time, size and a "locked" placeholder).
- Anywhere else a name could surface: the sidecar and event rows that quote `file_name` (to be found by a grep audit
  in PR 1 and covered by the test that scans the workspace for the plaintext name after enable), the chat note and the
  notification text for a transfer (they show no name today and keep it that way while locked).

## 4. Data format

One vault header file `data/mca/<principal-id>/vault.json` (plain, versioned):

```
{ "v": 1, "kdf": "argon2id", "kdf_params": {"opslimit": ..., "memlimit": ..., "salt": b64},
  "wrapped_key": {"nonce": b64, "ct": b64},          # storage key wrapped by the passphrase key
  "wrapped_key_recovery": {"nonce": b64, "ct": b64}, # the same storage key wrapped by the recovery key
  "check": b64 }                                      # an AEAD tag over a constant, to tell a wrong passphrase quickly
```

- **Storage key (SK):** 32 random bytes, generated once. SK is never derived from the passphrase, so a passphrase change
  re-wraps SK only and never re-encrypts the files.
- **Passphrase key:** Argon2id (libsodium `crypto_pwhash`, already a dependency through PyNaCl). At enable time
  the host is **calibrated**: starting from the 32 MiB floor, raise the memory (then the passes) until one derivation
  takes about 1 second on this machine, within a ceiling that leaves the node's other work its memory (never more than
  a quarter of free RAM). The chosen `memlimit`, `opslimit` and the measured time are written into the header; the
  floor of 32 MiB is enforced on every read, so a tampered header cannot weaken it. "Re-calibrate" (Settings) derives
  again, re-wraps the storage key under the new parameters and rewrites the header; files are not touched. A backup
  restored on a slower machine still unlocks (the parameters travel with it), only slower. scrypt is a documented
  fallback only if a platform build lacks Argon2id.
- **Files:** libsodium `crypto_secretstream_xchacha20poly1305` in 64 KiB chunks, one random stream header per file,
  associated data = attachment id + format version (so a file cannot be swapped for another transfer's). File names on
  disk stay the attachment ids; extension `.mcv` marks an encrypted file.
- **Secrets, names and comments in the database:** these columns are stored as `v1:<nonce>:<ct>` strings
  (XChaCha20-Poly1305, AD = row id + column name) instead of the plain text; a `vault_state` row says the vault is
  enabled. Encrypted names mean the on-disk file name is always `<attachment-id>.mcv`, never a display name; the
  display name is shown only after unlock, and "Download to device" restores it in the browser's download name.
- **Key files:** `keys/*` get the same stream format.
- **Recovery key:** 32 random bytes shown once on enable as 13 groups of 4 base32 characters (all 256 bits), never stored; its wrapped copy
  of SK is in the header. Losing both passphrase and recovery key means the data is unrecoverable by design.

## 5. Enable, disable, migrate, backup

Enable (Settings > MCAttach > Vault, danger-style dialog, runs as an explicit job with progress):
1. Pause the MCAttach worker (a short "vault migration" state; offers and sends queue as locked/paused).
2. Verify free space for a copy of the largest category plus 20 percent; refuse otherwise.
3. Take a backup: `data/mca-vault-backup-<timestamp>/` (tar of the plain workspace, mode 0700) unless the user ticks
   "I have my own backup"; the dialog says where it is and that it is plain.
4. Generate SK, wrap with the passphrase key and with the recovery key, write `vault.json` (fsync, atomic replace).
5. Encrypt file by file into `<name>.mcv.part`, fsync, rename, then remove the plain file; the database secrets are
   re-written in one transaction. Progress is journaled so a crash resumes (idempotent per file).
6. Show the recovery key once; the user must type back two of its groups to finish.
7. Offer to delete the plain backup after the first successful unlock test.

Disable: unlock first, reverse the steps (decrypt to `.part`, rename, rewrite DB secrets plain, delete `vault.json`
last), with the same backup option. A failed or interrupted run leaves a consistent mix that the journal completes;
both directions are re-runnable.

Backup/restore: a vault node's `data/mca/` backup is ciphertext plus `vault.json`; restoring it elsewhere needs the
passphrase or the recovery key.

## 6. Runtime: locked and unlocked

- SK lives only in memory of the single gunicorn worker (`workers = 1` is already a hard rule), in a `bytearray` that is
  zeroed on lock; no swap hygiene beyond `mlock` where available (best effort, documented).
- **Unlock:** passphrase or recovery key. Wrong attempts are rate limited (exponential delay, 5 tries then 60 s) and
  logged in the System Log without the value. **Lock now** zeroes the in-memory key at once and returns to the locked
  behaviour below. There is **no auto-lock in v1** (decision 2); the vault is locked after every start of the service
  or container.
- **After a reboot everything else runs; only MCAttach waits for unlock:**
  - incoming OFFER frames are still recorded (plain metadata) and shown as "locked offer", the chat bubble works, the
    notification says "1 file waiting, unlock to accept";
  - nothing is downloaded, decrypted, verified or written until unlock (accepting needs SK to store the file);
  - the key-exchange logic that needs the Ed25519 seed (sign KEY_ANNOUNCE / ACKs, accept offers) pauses: the service
    keeps a bounded queue of frames to process (existing command queue limits apply) and processes them on unlock;
  - **auto key-request retry** (the 10 min / 30 min / 2 h series): the timer state is plain, but sending needs the seed,
    so attempts are postponed without consuming an attempt (the same rule that already exists for "radio busy") and one
    System Log line says "MCAttach locked: key request postponed"; they resume on unlock, respecting the per-address gate;
  - **scheduled picture sends (G3):** the action skips the run while locked, writes one warning to the System Log and
    one notification (`vault_locked`), takes no picture and counts as a refused attempt for the 15 minute rule so
    schedules do not pile up; nothing is queued for later;
  - sending from the UI is refused with a clear "unlock MCAttach" message; the Send dialog offers the Unlock button;
  - the transfer list stays browsable: contact, time, size and a "locked" placeholder instead of the name and comment.
- The Core never holds the passphrase; only the KDF output wraps/unwraps SK.

## 7. How files are used while unlocked

- Preview (images): decrypted into memory only, served with the existing no-store headers; never written to disk.
- Download to device: streamed through the secretstream decryptor chunk by chunk (no temporary plaintext file).
- Save to Files: moves the (already encrypted) received copy from `cache/incoming/` into the vault's `files/` area
  as `<attachment-id>.mcv`; no plaintext is ever written (decision 1). The way out of the vault is **Download to
  device**, which streams decrypted chunks to the browser.
- Sending a picture (G2/G3): the send copy is produced from the gallery as today; the outgoing spool file is written
  encrypted; the plaintext exists only in memory during encryption.

## 8. Native and Docker

- Same code path; the vault key lives in memory in both. Docker: the passphrase is never an environment variable or a
  compose value (it would sit in the image metadata and shell history). Unlock is through the UI after every container
  start; an optional `MESHCENTER_MCA_VAULT_UNLOCK_FILE` (a Docker secret path) is a documented, off-by-default
  convenience for unattended NAS restarts, with the trade-off stated (the key then sits next to the data).
- `/healthz` stays `{"status":"ok"}` while locked; a locked vault is not an unhealthy node.

## 9. Unlock UX

Desktop: a lock chip in the dock next to the radio chip ("MCAttach locked"); clicking it, the Files tab banner or a
locked-offer bubble opens the unlock dialog (passphrase field, "Use recovery key" link, show/hide, attempts left).
Phone: the same banner at the top of Files and the chat bubble; the dialog is a bottom sheet with a 44 px target
passphrase field and a password-manager friendly `autocomplete="current-password"` field name. The Files tab dot
(MCA-UX1) counts locked offers. Everything stays usable behind the banner: browsing the transfer list, contacts and
the rest of MeshCenter.

## 10. Settings and API surface

- `GET /api/mca/vault` (state: disabled / unlocked / locked, KDF params summary, counts of locked items)
- `POST /api/mca/vault/enable`, `/unlock`, `/lock`, `/change-passphrase`, `/disable` (CSRF, auth, rate limited,
  danger dialogs for enable/disable per UI1), job progress through the existing command/notification path.
- A new MCAttach worker command family (`vault_*`) because the keys and files are worker-owned (the same reason the
  purge command is one).
- Documented in `docs/BACKEND_API.md` and the ADR set: a new `ADR-0013-mca-vault.md` before any code.

## 11. Tests (planned)

- Format: round-trip of header, secretstream chunks (empty, 1 byte, exact chunk multiple, 5 MiB), AD mismatch rejected,
  truncated/reordered chunks rejected, wrong passphrase and wrong recovery key rejected, re-wrap on passphrase change
  keeps files readable, parameter floors enforced.
- Migration: enable and disable on a seeded workspace; crash injection after each file and after the DB transaction;
  resume yields the same end state; backup created and plain files gone; disk-full refused up front.
- Locked behaviour: offers recorded as locked, nothing decrypted, key requests postponed without using attempts, G3 skip
  with log and notification, Send refused, unlock drains the queue, bounded queue overflow behaviour.
- API: auth/CSRF, rate-limited unlock, no passphrase or key in any log line, response or exception text.
- Frontend: lock chip, banner, unlock dialog (desktop and phone), locked-offer bubble, Files dot count.
- Docker: unlock-file option off by default, healthz while locked, no secret in env or image labels.
- Security review checklist: nothing writes plaintext of a vault item outside memory except the explicit Save to Files.

## 12. Risks and open questions

1. Forgotten passphrase plus lost recovery key is permanent loss: the enable dialog must make that unmissable.
2. Pi Zero 2 W memory: Argon2id memory cost versus the 512 MB budget while the camera and the radio listener run;
   parameters need a measured default, with a lower floor documented.
3. Large files are streamed, but the current receiver assembles the plaintext in memory (up to 5 MiB cap): acceptable,
   no change.
4. Settled by the owner (section 0): Save to Files stays encrypted; no auto-lock in v1 with a "Lock now" button;
   Argon2id calibrated at enable time (about 1 s, 32 MiB minimum, parameters in the header); names and comments
   encrypted.
5. Settled by the owner (2026-10-31): the comment is encrypted too; the locked list may show row counts, type icons
   and sizes; an existing plain `files/` library is converted in place on enable (same migration, same backup, with
   verification before any plain file is deleted).
6. The binding design is `docs/architecture/ADR-0013-mca-vault.md` (this file stays the proposal and the plan).
7. Owner review of 2026-10-31 (recorded in the ADR, which wins over this file where they differ): the plain backup
   lifecycle, the flash limitation, swap and memory hygiene, Roll back refused while a vault exists, no names or
   comments in logs, notifications or toasts, the Media gallery not covered, minimum passphrase 12, the wrong-attempt
   delays, no `vault.json.bak` after an unlock.

Rough size: one ADR, then three PRs (crypto + format + migration with tests; worker/locked-state behaviour and API;
UI and Docker notes), each with live rounds on the dev node and a NAS Docker round.

## 13. Implementation plan (PR split, no code yet)

Each PR is small enough to review alone, leaves the node working with the vault disabled, and ends with live rounds
named below. Order matters: 1 before 2 before 3; 4 can start after 2.

**PR 0: ADR-0013-mca-vault.md** (docs only). The format of section 4, the locked-state rules of section 6, the
decisions of section 0, the threat model of section 2. Reviewed before any code.

**PR 1: vault core, no behaviour change** (`meshsrv/attachments/vault/`: `header.py`, `kdf.py`, `streamcrypt.py`,
`fields.py`). Pure library, stdlib plus PyNaCl: header read/write with the 32 MiB floor, Argon2id calibration
(injected timer, so tests are deterministic), wrap/unwrap of the storage key with passphrase and recovery key,
secretstream file encrypt/decrypt (chunked, AD bound to attachment id and version), field encryption for DB columns,
recovery key encoding. Tests: all of section 11 "Format", fuzzed truncation/reordering, parameter floor, passphrase
change and re-calibration keep files readable. No wiring; nothing in the running service uses it yet.
Live round: calibration timing on dev (Pi 4), camtest and the Pi Zero 2 W class node, numbers into the ADR.

**PR 2: vault state and locked/unlocked runtime** (`VaultState` in the MCAttach service, worker commands
`vault_unlock`, `vault_lock`, `vault_status`; API `GET /api/mca/vault`, `POST .../unlock`, `.../lock`;
rate-limited unlock; System Log events without secrets). Locked behaviour of section 6: offers recorded as locked,
key-request retry postponed without using an attempt, scheduled picture sends skipped with log and notification
(`vault_locked`), Send refused with a clear code, bounded inbound queue. A vault is created empty and disabled until
PR 3, so the state machine is testable end to end with a fixture workspace. Tests: locked/unlocked transitions,
queue drain on unlock, no secret in any log, API auth/CSRF/rate limit, G3 skip, retry postponement.
Live round: lock/unlock on dev, reboot of dev while locked (everything else runs), a G3 rule while locked.

**PR 3: enable, disable and migration** (job runner with journal, backup, space check, idempotent per file and per DB
transaction; `POST /api/mca/vault/enable|disable|change-passphrase|recalibrate`; recovery key shown once with the
type-back-two-groups confirmation). Encrypts `cache/incoming`, `files`, `spool/outgoing`, `quarantine`, `keys/`, the
secret columns, file names and comments; renames files to `<attachment-id>.mcv`. Tests: crash injection after every
file and after the DB transaction, resume to the same end state, disk-full refusal, backup created, a scan of the
whole workspace finds no plaintext name, content or key after enable, and full restore after disable.
Live round (with the owner's go, on a copy of dev's workspace first): enable, reboot, unlock, receive, send, disable.

**PR 4: file operations under the vault** (receive into `cache/incoming/*.mcv`, send from an encrypted spool, preview
in memory, Download to device streaming, Save to Files as a move inside the vault, Delete local copy, the received-
copies storage line and purge from MCA-UX1 working on `.mcv` files, G2 send copy encrypted in the spool).
Tests: byte-exact round trips for 0 bytes, 1 byte, chunk multiples and 5 MiB, no plaintext temp file (a test that
watches the workspace and the system temp dir), range/conditional requests, the 5 MB limit unchanged.
Live round: a picture from the gallery to Echo and back with the vault on both nodes, Download to device compared by sha256.

**PR 5: UI** (lock chip in the dock, Files banner, unlock dialog desktop and phone bottom sheet, "Lock now",
Settings > MCAttach > Vault card with enable/disable/change passphrase/recalibrate/recovery, locked placeholders in the
list and the detail, chat bubble "locked" state, the Files tab dot counting locked offers; i18n in four languages;
danger dialogs per UI1; cache bump). Tests: node tests for each state, dialog call-site pin, phone layout check at 320
and 412 px in light and dark.
Live round: owner check on the phone and desktop.

**PR 6: Docker and docs**: unlock after every container start through the UI, the optional off-by-default
`MESHCENTER_MCA_VAULT_UNLOCK_FILE` Docker-secret path with its trade-off written down, `/healthz` unchanged while
locked, `docs/Docker_Installation.md` and the user guide, `THIRD_PARTY_NOTICES` check (PyNaCl is already shipped), the
release notes. Live round: NAS Docker (the owner deploys), restart while locked, unlock, receive.

Cross-cutting: every PR adds its tests in the same PR, keeps `pytest` and CI green, never logs or returns a passphrase,
key or name, and follows the repository rules (no em or en dashes, i18n x4 where text changes, cache bump with frontend
changes, explicit `git add` paths). A release (v1.14.0) goes out after PR 6, with the vault documented as optional and
disabled by default.
