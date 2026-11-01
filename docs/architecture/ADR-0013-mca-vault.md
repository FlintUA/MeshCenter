# ADR-0013: MCAttach vault (optional passphrase-protected storage for MCAttach data)

**Status:** Proposed, revised after the owner's review of 2026-10-31 (PR 0 of the MCA-VAULT plan; no code until this ADR is approved)
**Date:** 2026-10-31
**Context documents:** `docs/proposals/MCA-VAULT.md` (the proposal and the owner's decisions this ADR makes binding), ADR-0002 (crypto suite, PyNaCl/libsodium), ADR-0003 (`attachments.db` is the one SQLite exception, one workspace per MCA principal), ADR-0004 (threat model), ADR-0006 (object encryption: the relay copy is already end-to-end encrypted), ADR-0008 (worker-owned state, published snapshots, command queue), ADR-0012 (shared target model), and the Docker runtime notes in `docs/Docker_Installation.md`.

## Context

MCAttach keeps four kinds of sensitive data on the node, all under `data/mca/<principal-id>/`:

- file contents: `cache/incoming/<attachment-id>` (received copies), `files/` (saved files, named by their display name), `spool/outgoing/<attachment-id>` (outgoing plaintext until sent), `quarantine/`;
- key material: `keys/` (the Ed25519 identity seed, mode 0600, and the provider upload-token files);
- per-transfer secrets in `attachments.db`: `data_key`, `nonce_prefix` and `receipt_secret` in the sender and receiver state tables;
- names: `attachments.file_name` (the sender-chosen display name), the optional transfer comment, and the display name that becomes the on-disk name under `files/`.

Today the protection is file permissions only. A copied SD card, a backup of `data/mca/`, a decommissioned device, or another local user of a shared machine or Docker volume reads everything, including file names, and can use the node's MCAttach identity.

This ADR adds an optional vault: an application-level encryption layer for exactly that data, protected by a passphrase, locked by default after every start. It is not full-disk encryption and it does not change the MCAttach wire protocol or the relay copy.

## Decisions already taken by the owner (binding)

1. **Save to Files stays encrypted inside the vault.** A saved file is moved into the vault's encrypted files area. Plaintext leaves the vault only through "Download to device", streamed through the decryptor to the browser. Preview is in memory only.
2. **No auto-lock in v1.** The vault is locked after every start of the service or container (so after every reboot). There is a "Lock now" button.
3. **Argon2id is calibrated at enable time** to about 1 second of unlock time on the actual host, with a minimum of 32 MiB; the parameters are stored in the vault header so they can be raised later.
4. **File names are encrypted, and so is the comment.** While locked, the transfer list shows the contact, the time, the size and the type icon with a "locked" placeholder instead of the name; row counts are visible.
5. **An existing plain `files/` library is converted in place when the vault is enabled** (same migration, same backup, with verification).
6. Recovery key shown once at enable; works natively and in Docker; enable and disable migrate with a backup.
7. Minimum passphrase length 12 with a visible strength hint (not a complexity rule).
8. Wrong-attempt delays 1 s, 2 s, 4 s, then 60 s after 5 failures, with no permanent lockout.
9. No `vault.json.bak` after an unlock: the previous header copy exists only from a header rewrite until the next
   successful unlock, which deletes it.
10. Review requirements of 2026-10-31: the plain backup lifecycle (Decision 8), the flash limitation stated honestly
    (Decision 8), swap and memory hygiene (Decision 6), a hard refusal of Roll back and downgrade while a vault exists
    (Decision 11), no names or comments in the System Log, notifications or toasts (Decision 12), and an explicit
    statement that the Media gallery is not covered (Decision 12).

## Decision

### 1. Scope: what is encrypted and what stays plain

| Item | In the vault |
|---|---|
| Received copies, saved files, outgoing spool, quarantine | encrypted files |
| Ed25519 identity seed, provider upload-token files (`keys/`) | encrypted files |
| `data_key`, `nonce_prefix`, `receipt_secret` | encrypted DB fields |
| `attachments.file_name`, transfer comment | encrypted DB fields |
| Attachment ids, states, sizes, MIME types, timestamps, contact ids, delivery rows, events | plain |
| Public keys, fingerprints, trust state, provider origins | plain |

Plain metadata is deliberate: the service must list transfers while locked, and none of it is a secret the protocol does not already expose to a relay operator or a radio listener. File names and comments are encrypted because they describe content. Sizes, MIME types and timing are visible to anyone with the disk; this is stated in the threat model, not hidden.

### 2. Key hierarchy

- **Storage key (SK):** 32 random bytes from libsodium, generated once at enable. SK is never derived from the passphrase, so changing the passphrase re-wraps SK and never re-encrypts data.
- **Subkeys:** keyed BLAKE2b with a fixed personalization per purpose (`files`, `fields`, `keys`, `check`), so file encryption, DB field encryption, key-file encryption and the header check never share a key. This is the construction libsodium's `crypto_kdf` uses; PyNaCl does not expose `crypto_kdf`, so `crypto_generichash_blake2b_salt_personal` is called directly (amended in PR 1).
- **Passphrase wrap:** Argon2id (`crypto_pwhash`, ALG_ARGON2ID13) over the passphrase and a random 16-byte salt gives a wrap key; SK is sealed with XChaCha20-Poly1305 (`crypto_aead_xchacha20poly1305_ietf`). The KDF parameters and salt are bound into the AEAD associated data, so a header whose parameters were tampered with fails to open.
- **Recovery wrap:** a 32-byte random recovery key (shown once, encoded as 13 groups of 4 base32 characters: 52 characters carry all 256 bits; the first draft said 8 groups, which would have been only 160 bits, corrected in PR 1) wraps the same SK the same way; high entropy, so no KDF is needed beyond one `crypto_generichash` domain separation.
- **Header file:** `data/mca/<principal-id>/vault.json`, plain, versioned:
  ```
  { "v": 1, "kdf": {"alg": "argon2id13", "opslimit": n, "memlimit": n, "salt": b64, "measured_ms": n},
    "wrap_pass": {"nonce": b64, "ct": b64}, "wrap_recovery": {"nonce": b64, "ct": b64}, "check": b64 }
  ```
  Written atomically (temp file, fsync, rename). When the header is rewritten (passphrase change, re-calibration) the
  previous version is kept as `vault.json.bak` only until the next successful unlock, which deletes it; an unlock that
  has not happened since the rewrite is the one case where the old header is still needed.
- **Floor:** `memlimit` below 32 MiB or `opslimit` below the libsodium interactive minimum is refused on read, so a downgraded header cannot weaken the vault.

### 3. KDF calibration

At enable (and on "Re-calibrate"), starting from the 32 MiB floor and the minimum passes, the memory and then the passes are raised until one derivation takes about 1 second on this host, within a ceiling of one quarter of the currently free RAM. The chosen parameters and the measured time go into the header. Re-calibrating derives again, re-wraps SK under the new parameters and rewrites the header; no data is touched. A backup restored on a slower machine still unlocks (the parameters travel with the header), only more slowly. Each step is measured as the best of two runs, so a cold first run cannot make the result too weak, and a host that is already slower than the target at the floor stays at the floor. scrypt is a documented fallback only for a platform build that lacks Argon2id.

**Measured calibration (PR 1 live round, 2026-10-31, `kdf.calibrate()` run twice per node from outside the checkout):**

| Node | Hardware | Free RAM at the time | Ceiling (a quarter) | Chosen | Unlock time | Peak RSS of the run |
|---|---|---|---|---|---|---|
| dev (.104) | Raspberry Pi Zero 2 W, 512 MB class | about 120 MB | 32 MiB (the floor wins) | 5 passes, 32 MiB | 0.92 s (floor alone: 0.38 s) | 46 MiB |
| camtest (.107) | Raspberry Pi 4, 2 GB | about 1.4 GB | 343 to 351 MiB | 3 to 4 passes, 128 MiB | 0.89 to 1.15 s (floor alone: 0.15 s) | 142 MiB |
| droidian (.111) | Pixel-class phone SoC, aarch64, 4 GB | about 1.2 GB | 295 MiB | 2 passes, 32 MiB (already slower than the target at the floor) | 0.97 to 1.15 s | 43 MiB |

The production node (.103) was not measured: the session was not permitted to read it, and the numbers above already
cover both ends (a Zero 2 W and a Pi 4). On the Zero 2 W the 32 MiB floor is a real cost: an unlock transiently adds
about 32 MiB to a node that has roughly 120 MB free, so the unlock must not run while the camera stream and a heavy
update are active; PR 2 serializes it with the other MCAttach work and refuses it when free RAM is below the floor plus
a margin (with a clear message, never an out-of-memory kill).

### 4. Encrypted file format

`<attachment-id>.mcv`, always named by the attachment id, never by a display name.

- Header: magic `MCV1`, format byte, the 24-byte `crypto_secretstream_xchacha20poly1305` header.
- Body: 64 KiB plaintext chunks, one secretstream message each, associated data = `MCV1 || attachment-id`; the last chunk carries `TAG_FINAL`, so truncation is detected; a chunk moved to another file fails authentication because the associated data differs.
- Key: the `files` subkey of the storage key; each file has its own random stream header.
- Reads are streaming: preview decodes into memory, "Download to device" streams chunk by chunk, and no decrypted temporary file is ever written.

### 5. Encrypted database fields

`v1:<base64 nonce>:<base64 ciphertext>` (XChaCha20-Poly1305, `mcvfield` subkey, associated data = `table.column.row-id`), stored in the same TEXT columns, so the schema does not change. Reading a field that starts with `v1:` while locked yields "locked", never an error and never the ciphertext in an API response. The DB migration is data only (a `vault_state` row says enabled).

### 6. Runtime: where the key lives, and what "locked" means

- The key ring is one object in the single gunicorn worker (`workers = 1` is already a hard rule): a `bytearray` holding SK, a generation counter, an `RLock`. Lock now zeroes the bytearray, bumps the generation, and aborts in-flight streaming reads at the next chunk. Best-effort `mlock` where available; documented as best effort.
- The key is read by the MCAttach worker thread and by request threads that stream content (decrypting needs the key but no SQLite). Neither holds the passphrase; only the derived wrap key exists, and only during unlock.
- **Memory and swap hygiene** (best effort, stated as such):
  - SK lives in a page locked with `mlock` (libsodium `sodium_mlock` through `ctypes`, because PyNaCl does not expose
    it), so it is not written to swap. The limit that allows it (`RLIMIT_MEMLOCK`) is 8192 KiB on our nodes, far more
    than a 32-byte key and a few small buffers need. If `mlock` fails (a container with a low limit) the vault still
    works, logs one System Log warning that the key may be swappable, and the Settings card shows it.
  - Decrypted data has the shortest possible lifetime. Downloads stream 64 KiB chunks through a reused buffer that is
    zeroed after each write; nothing is accumulated. A preview of an image (at most 5 MiB, the MCAttach limit) is
    decrypted into one buffer, served and dropped; that buffer is too large to lock and could in principle be
    swapped, which is why the swap setup below matters.
  - The passphrase arrives as a Python `str`, which cannot be zeroed and may linger until garbage collection; the
    request handler converts it to `bytes` at once, derives, and drops its references. The derived wrap key and the
    unwrapped SK copy used during unlock are `bytearray`s that are zeroed. This reduces, but does not remove, the
    window in which the passphrase exists in memory; it is not defended against root on a running node.
  - **Pi OS swap setup (checked on the dev and camtest nodes, 2026-10-31):** Raspberry Pi OS Trixie uses zram
    (compressed swap in RAM, `/dev/zram0`; `dphys-swapfile` is inactive), so swapped pages do not reach the SD card.
    Older Raspberry Pi OS releases and some images use `dphys-swapfile`, a swap file on the SD card, where a swapped
    page can persist. At enable time the vault reads `/proc/swaps`; if a swap device that is not zram is active it shows
    a warning with the way out (switch to zram or disable swap) before continuing. In Docker the host decides (a NAS
    may swap to disk); the documentation says so and the check runs inside the container where visible.
- **Locked behaviour** (everything else in MeshCenter runs normally):
  - inbound OFFER frames are still recorded (verification needs only the sender's public key); they appear as "locked" offers; nothing is accepted, downloaded, decrypted or stored until unlock;
  - anything that needs the Ed25519 seed (signing acknowledgements, KEY_ANNOUNCE, KEY_REQUEST, accepting) is queued in the existing bounded queues and processed on unlock;
  - the KEY_REQUEST auto-retry series postpones attempts without using one (the same rule as "radio busy") and logs one System Log line;
  - the G3 schedule action skips with one System Log warning and one notification (`vault_locked`), takes no picture, and counts as a refused attempt for its 15 minute rule;
  - sending from the UI is refused with `vault_locked`; the Send dialog offers Unlock;
  - cleanup that needs no key stays available: "Delete older received copies" only unlinks files;
  - the transfer list shows contact, time, size, type icon and row counts with a "locked" placeholder for name and comment.
- `/healthz` is unchanged while locked; a locked vault is not an unhealthy node.

### 7. Unlock, lock, recovery

- Unlock by passphrase or recovery key; wrong attempts are rate limited: 1 s, 2 s, 4 s, then 60 s after 5 failures,
  never a permanent lockout (an owner who mistypes cannot be locked out by an attacker's failed attempts for good, and
  the KDF cost already limits guessing). Logged without any value.
- At enable the passphrase must have at least 12 characters; a strength hint is shown while typing (length and a
  common-password check), with no further composition rules.
- Lock now and "locked after start" are the only ways to lock in v1.
- Changing the passphrase re-wraps SK; the recovery key stays valid. A new recovery key can be issued from an unlocked vault (re-wrap, show once, the old one stops working).
- Forgetting both the passphrase and the recovery key is permanent data loss by design; the enable dialog says so and requires typing back two groups of the recovery key.

### 8. Enable, disable and conversion of an existing library

Enable is an explicit job (danger dialog, progress, resumable), run by the MCAttach worker with the service otherwise paused for MCAttach only:

1. Preconditions: free space for the largest category plus 20 percent; no transfer in an intermediate state that needs a plain read (the job waits for or refuses with a list).
2. Backup of the plain workspace to `data/mca-vault-backup-<timestamp>/` (mode 0700) unless the user states they have their own; the dialog says where it is and that it is plain.
3. Generate SK, wrap it twice, write `vault.json`.
4. For every file in `cache/incoming`, `spool/outgoing`, `quarantine`, `keys/` and **every file in the existing plain `files/` library**: encrypt to `<attachment-id>.mcv.part`, **decrypt it again in memory and compare the SHA-256 with the original**, fsync, rename, and only then delete the plain file. A file in `files/` is matched to its attachment row to get the id; a file with no row (should not exist, since only the save command creates them) is reported before the job starts and, if the user continues, is encrypted under a generated id with its name kept in an encrypted index inside the vault.
5. Rewrite the secret columns, `file_name` and comment in one SQLite transaction per batch, journaled.
6. A final verification pass walks the workspace: no plaintext name, content or key remains (the same scan runs in the tests).
7. Show the recovery key once with the type-back confirmation, then the **unlock test**: the wizard locks the vault
   and asks the user to unlock it again with the passphrase just chosen. The first successful unlock completes the
   enable job.

**The plain backup does not stay in `data/` silently.** It is a full plaintext copy of everything the vault is meant to
protect (content, names, the identity seed), so it is treated as a liability, not a convenience:

- Right after the first successful unlock test the dialog offers **"Delete the plain backup now"** (a danger dialog
  naming the folder and its size) as the default action; "Keep it for now" is the explicit alternative.
- While `data/mca-vault-backup-*` exists, a **persistent warning** is shown in Files (a banner above the transfer list)
  and in System (a notice on the vault card and in the System Log summary): "A plain backup of your MCAttach data
  still exists", with its location, size, age and a Delete button. The warning is derived from the folder's presence
  on every status read, so it cannot be lost by a restart or a missed click; it also appears if the user copied the
  folder back by hand.
- The backup folder is created with mode 0700 and the vault status API reports its path and size, never its content.
- Choosing "I have my own backup" at enable skips creating it; the dialog says the migration then has no safety net
  except the verification step.

**Flash storage limitation (stated to the user in the enable dialog and in the documentation):** the migration deletes
the plain files, but on an SD card or eMMC deletion is not secure erase. Wear levelling and the flash translation layer
can leave old copies of the plaintext, and of the plain backup after it is deleted, recoverable by forensic tools from a
cloned card until the blocks happen to be reused. The vault fully protects **data that was received or sent after it was
enabled**, because that data never existed in plaintext on the card (outgoing files are encrypted when spooled). For
data that already existed in plaintext, the protection is as good as the card's own behaviour. **Recommendation: enable
the vault on a fresh installation (or a freshly flashed card) for the strongest protection**; on an existing node the
dialog says what is and is not achieved, and suggests re-flashing if the old plaintext matters. Overwriting before
deleting is not offered as a fix because it does not defeat wear levelling and would shorten the life of the card.

The journal (`vault-migration.json`) records the phase and the last completed item; a crash resumes from it and every step is idempotent. Disable runs the same steps in reverse (unlocked, decrypt and verify, rewrite plain, remove `vault.json` last) with the same backup option and the same journal. Failure at any point leaves a consistent mix that the journal completes; both directions are re-runnable.

### 9. Docker and unattended starts

Same code path, same in-memory key. The passphrase is never an environment variable, a compose value or an image label. After every container start the vault is locked until unlocked in the UI. An optional, off-by-default `MESHCENTER_MCA_VAULT_UNLOCK_FILE` (a Docker secret path) exists for unattended NAS restarts; the documentation states the trade-off (the key then sits next to the data) and the System Log notes when it is in use.

### 10. API and commands

- `GET /api/mca/vault`: `{state: disabled | locked | unlocked | migrating, kdf: {memlimit, opslimit, measured_ms}, locked_items}`; never a key, passphrase or name while locked.
- `POST /api/mca/vault/enable | unlock | lock | change-passphrase | recalibrate | new-recovery-key | disable`: authenticated, CSRF-protected, rate limited (unlock), danger dialogs for enable and disable (UI1).
- **Amended in PR 2:** unlock and lock are not worker commands. A command payload sits in the command queue and the
  command registry, and a passphrase must never be stored there. Unlock and lock run on the request thread inside the
  vault runtime, and unlock takes the service's tick lock for the duration of the derivation, so it is serialized
  with all MCAttach worker activity exactly as a command would be. `GET /api/mca/vault`, `POST /api/mca/vault/unlock`
  and `POST /api/mca/vault/lock` are the PR 2 surface. The state-changing, file-touching operations of PR 3 (enable,
  disable, change passphrase, re-calibrate) will still be worker commands, because they touch files and the
  database, and they receive the passphrase through a one-shot in-memory hand-off that is never queued or logged.
- Locked behaviour at the API: the requests that need the identity seed or the storage key answer HTTP 423
  `vault_locked` (create, retry, download, reject, cancel, save, revoke, request-key, confirm, accept or reject a key
  change, set an upload token); the worker refuses the same commands with `vault_locked` if one was queued before the
  lock. A wrong passphrase or recovery key is 403 `unlock_failed`, a delay window is 429 `unlock_rate_limited` with
  `Retry-After`, too little free memory is 503 `insufficient_memory` with the numbers.
- The attachments snapshot carries `vault: {state}` and per-record `locked: true` with `file_name` and `comment` absent while locked.

### 11. Roll back and downgrade are refused while a vault exists

Older code cannot read ciphertext (it would treat `v1:` strings and `.mcv` files as data and could write plain files
next to them), and a schema version bump does not stop it (the migrator applies pending migrations only; a database
with a higher `user_version` opens normally in older code). So the protection is in code, in every build that has the
vault:

- `POST /api/updates/rollback` and the updater's rollback preflight refuse with HTTP 409 `vault_enabled` and the text
  "Disable the vault first" whenever `data/mca/*/vault.json` or `vault-migration.json` exists (file presence only, so
  the updater never imports MCAttach and a half-finished migration counts). The Roll back button in the Updates card is
  disabled with the same text.
- Apply never downgrades already (`ahead` is refused); the vault adds no new path, but its tests pin that an apply to
  an older tag stays refused.
- Fail-closed start: a build with vault support refuses to start MCAttach if `vault.json` has a format version it does
  not know, and logs why; a header written by a newer build is never "repaired".
- A manual `git checkout` of an older tag, or an older Docker image, is outside what code can prevent in builds that
  predate the vault. The enable dialog, the release notes and `docs/Docker_Installation.md` say: disable the vault
  before any downgrade; the disable job is the supported way back.

### 12. What is not covered, and what must never leak

- **The Media gallery is not covered by the vault.** Only MCAttach data is. Camera pictures, thumbnails and sidecars
  under `data/screenshots/`, the camera sources, chats, nodes, telemetry and every other MeshCenter file stay as they
  are. A picture sent from the gallery stays in the gallery in plaintext; only its MCAttach copy (the resized send
  copy and the transfer) is inside the vault. The UI states this on the vault card.
- **No names or comments outside the vault while it is enabled.** File names and transfer comments must not appear
  in the System Log, in notifications or in toasts. Audit of the current code (2026-10-31): MCAttach does not write to
  the System Log at all (`log_system_event` is not used under `meshsrv/attachments/` or in `api/api_attachments.py`);
  the Python logger lines there carry ids, states and error codes, not names; `attachment_events.detail_json` holds
  only kinds and size buckets; the Files toasts and progress texts are generic ("Download started", "Saved to Files",
  "Received copies deleted") or use the contact's node name, never a file name; the G3 scheduler logs the contact id,
  the size class and the byte count; chat bubbles show no name. The names exist only in `attachments.file_name`, the
  comment column and the Files list and detail, which read them from the snapshot (absent while locked). This is a
  contract, enforced by tests in PRs 2 to 4: while a vault is enabled, a capture of the System Log, the notification
  queue, the journal logger output and every toast text across the main flows (receive, accept, save, send, schedule,
  purge, lock, unlock) contains no file name and no comment.
- **Lines written before the vault was enabled** cannot be changed by the vault. The audit found none that contain
  names in this version; a log line from an older version, if one existed, stays in the System Log
  (`system_events.jsonl`) or in the journal until the user clears it. The enable dialog says so and links to the System
  Log clear action.
- **Outside the node:** the browser's own download history keeps the file name of a "Download to device"; a downloaded
  file is plain on the user's device by definition; the relay copy is end-to-end encrypted and unchanged; the radio
  sees only the MCAttach control frames, as today.

## Threat model

| Threat | Result |
|---|---|
| Stolen or cloned SD card, node off | no content, name, comment or key of data received or sent after enabling is readable without the passphrase or recovery key; sizes, MIME types, contacts and timing remain visible |
| Plaintext that existed before enabling (and the deleted plain backup) on flash | may remain recoverable from the card (wear levelling, no secure erase); only a fresh installation avoids it |
| The plain backup folder while it exists | fully readable plaintext; a persistent warning and a delete offer right after the first unlock (Decision 8) |
| Swap (disk swap file, or zram) | the key is `mlock`ed; decrypted buffers are short-lived; a large preview buffer could be swapped; zram (Pi OS Trixie) never reaches the SD card, a swap file on SD could keep pages, and the vault warns about it at enable |
| Downgrade or Roll back with a vault enabled | refused by the updater with "Disable the vault first"; a manual checkout of an older build is documented, not preventable |
| File names or comments leaking through the System Log, notifications or toasts | audited clean in this version; enforced by tests while a vault is enabled; lines written earlier remain (documented) |
| The Media gallery | not covered, by design; only MCAttach data is |
| Backup of `data/mca/` copied elsewhere | ciphertext plus `vault.json`; needs the passphrase or recovery key |
| Decommissioned device | wipe is destroying `vault.json` (or the wrapped keys) |
| Another local user, shared Docker volume | ciphertext while locked; as today while unlocked |
| Web login compromised | nothing served while locked; as today while unlocked |
| Root on the running, unlocked node | not addressed (the key is in memory) |
| Offline passphrase guessing | slowed by calibrated Argon2id; the passphrase strength is the limit; a minimum length and a strength hint at enable |
| Header tampering (weaker KDF) | refused by the floor and by the AEAD binding of the parameters |
| File swapped between transfers | authentication fails (associated data = attachment id) |
| Truncated or reordered ciphertext | authentication fails (secretstream final tag, ordering) |
| Lost passphrase and recovery key | permanent loss, by design |

## Consequences

- One more state for every MCAttach feature to handle (locked); the list in Decision 6 is the contract, and each of the PRs below adds its tests for it.
- Existing nodes are unaffected until the vault is enabled; with it disabled nothing changes on disk.
- A vault migration is the riskiest step of the plan; it is covered by crash injection tests, the backup, verification before deleting any plain file, and a live round on a copy of the dev workspace first.
- PyNaCl is already a dependency (ADR-0002); no new package is needed. `crypto_pwhash` and `crypto_secretstream` are in the libsodium build it ships.
- Roll back to a release without the vault is not possible once a vault is enabled (older code would read ciphertext as
  data). This is enforced in code (Decision 11): the updater refuses Roll back with "Disable the vault first", the
  button is disabled, and the release notes and Docker documentation say disable-before-downgrade.
- The plain backup is a deliberate, visible liability with a lifecycle (offer to delete, persistent warning), and the
  flash limitation is part of the user-facing text, not a footnote.

## Alternatives considered

- **SQLCipher for `attachments.db`:** protects the database but not the files, needs a different SQLite build on the Pi and in Docker, and ADR-0003 keeps the standard library `sqlite3`.
- **LUKS or fscrypt on the data directory:** stronger at the block level but needs root, partitioning or a kernel feature that a Docker volume and the Droidian node do not have; not portable across our targets.
- **Per-file keys wrapped individually:** more flexible for sharing, no benefit here; one storage key with per-file stream headers is simpler and enough.
- **A key from the OS keyring or TPM:** unavailable on the Pi Zero 2 W and in containers; the passphrase model works everywhere.
- **Auto-lock after idle:** deferred to a later version by decision.

## PR plan

PR 1 vault core library (header, KDF calibration, key wrapping, secretstream files, field encryption, `mlock` helper,
tests, no wiring); PR 2 vault state and locked runtime (commands, API, locked behaviour, the updater's Roll back refusal
and the fail-closed start, the log/notification/toast leak test, the swap check); PR 3 enable, disable, conversion and
migration (backup lifecycle, unlock test, flash notice, the leak test across the migration); PR 4 file operations under
the vault (leak test across receive, save, send, schedule, purge); PR 5 UI (persistent plain-backup warning in Files and
System, strength hint, disabled Roll back button, the "gallery not covered" note); PR 6 Docker and documentation
(swap notes, downgrade notes) - all six are done. Details, tests and the live round of each are in section 13 of
`docs/proposals/MCA-VAULT.md`.

## Amendments made in PR 3 (enable, disable, migration)

- **The experimental flag.** The consumers of the encrypted layout (receive into `.mcv`, send from an encrypted spool,
  preview, download, Save to Files as a move inside the vault, the identity seed read through the vault) are PR 4. A
  vault enabled before that would leave a node whose service cannot read its own workspace. So the enable endpoint
  answers 409 `vault_not_ready` unless the process environment has `MESHCENTER_MCA_VAULT_EXPERIMENTAL=1`. The flag exists
  for the live round on a copy and for the tests, and PR 4 removes it. Disable, resume and the header operations do not
  need it (they only matter once a vault exists). (PR 4c removed the flag.)
- **Enable and disable are worker commands** (`vault_enable`, `vault_disable`, `vault_resume`). The passphrase of an
  enable reaches the worker through the runtime's one-shot in-memory hand-off (120 s), never in the payload; the payload
  holds a random single-use token. The recovery key is not in the command result either: it is read once from
  `GET /api/mca/vault/recovery-key` (410 afterwards).
- **Change passphrase, re-calibrate and new recovery key** run on the request thread under the tick lock, like unlock,
  and need the current passphrase again (re-authentication); the old header is kept as `vault.json.bak` until the next
  successful unlock.
- **Verification is stronger than the prefix.** A database value counts as encrypted only if it authenticates for its
  table, column and row (a plain value that happens to start with `v1:` is still encrypted), and the final scan decrypts
  every sensitive value. After the database phase the job runs `PRAGMA secure_delete` and `VACUUM`, because SQLite keeps
  the old bytes of an updated value in free pages (a test searches the raw database file for the plain names, comments
  and secrets).
- **The plain backup** is a folder `mca-vault-backup-<timestamp>` next to the MCA tree (workspace copy plus a consistent
  SQLite snapshot, mode 0700). `GET /api/mca/vault` lists it (`plain_backups`, `plain_backup_warning`) from the folder
  itself, and `POST /api/mca/vault/backup/delete` removes one by exact folder name.
- **Resume after a crash** needs an unlock first (the key is not on disk). A resumed enable never showed its recovery
  key, so the status reports `recovery_key_needs_reissue` until a new key is issued.
- **Memory pre-check for enable, disable and resume (PR 3 review).** Before anything starts, the API and then the worker check MemAvailable against an estimate: 16 MiB working buffers + 16 MiB margin, plus the 32 MiB KDF floor for an enable (64 MiB / 32 MiB). Below it: HTTP 503 `insufficient_memory`, nothing queued, no backup started, no passphrase left waiting. A migration is never started on a node that could run out of memory in the middle of it.

## Amendments made in PR 4a (data layer)

PR 4 is split in three reviewable parts: 4a the data layer (this one), 4b receive and send under the vault, 4c preview,
download, Save to Files, delete, cleanup, and the removal of the experimental flag.

- **One codec** (`vault/codec.py`). `seal()` before a protected value is written, `open()` after it is read, for
  `attachments.file_name` and `draft_comment` and the sender's `data_key`, `nonce_prefix`, `upload_token`, `revoke_token`
  (and the retained `mca_sender_revoke_state.revoke_token`). The encoding is the migration's own (`to_text`/`from_text`
  with the text/bytes tag), so a value written by the migration and one written by the service are the same thing.
  `mca_receiver_state` is not written by any code path today; its columns stay in the migration's list.
- **Where it hangs.** The MCA database connection is a `VaultConnection` (a `sqlite3.Connection` subclass that carries a
  `codec`), and `MCAWorkspaceManager.codec` is the same object. A plain connection or workspace manager (every existing
  test) gets `PLAIN`, a pass-through, so nothing changes where the vault is not involved. `VaultCodec` asks the vault
  runtime on every call, so enable, disable, lock and unlock need no rewiring.
- **Locked.** A secret cannot be opened (`vault_locked`) and nothing is written in plain form instead; a display value (the
  file name in the transfer list) is read as `None`, the "locked" placeholder, never the ciphertext. A plain value that
  merely starts with `v1:` is NOT plain once the vault is enabled (see the next item).
- **Fail closed (PR 4a review).** With the vault enabled, an encrypted-looking value that does not authenticate for its
  table, column and row (a flipped bit, a value copied from another row) raises `vault_corrupt`; a display value reads as
  `(unreadable)`, never the raw `v1:` string; a plain secret file in `keys/` without its `.mcv` twin raises `vault_corrupt`
  instead of being served. The System Log gets `table.column of <attachment id>` once per value, never the value. After
  the migration no plain value can legitimately live in these places, and the migration and its resume read plain data
  through their own code, not through this codec.
- **Secret files.** The identity seed and the provider upload tokens in `keys/` are read and written through the codec
  (`<name>.mcv`, the `keys` subkey, associated data `keys/<name>`, atomic temp file and rename, no plain twin left). A
  plain file is still read when no encrypted one exists (an unmigrated or half-migrated workspace).
- **Guard.** `test_only_the_known_modules_touch_the_encrypted_columns_and_each_uses_the_codec` fails when a new module
  starts touching these tables, so "is this sealed?" is a review question. The leak test drives a real send through the
  sender and searches the raw database file for the name, the comment, the keys and the tokens (mutation-checked).
- **An interrupted migration with too little memory** stays locked with `migration_pending {op, memory_needed_mib,
  memory_free_mib, memory_ok}` in the status; unlock and resume are refused with 503 `insufficient_memory`, nothing is
  retried (only the user's resume request submits `vault_resume`), nothing is touched.
- Not yet covered (4b and 4c): the content files themselves on receive and send, preview, download, Save to Files,
  delete and cleanup of `.mcv` files. The experimental flag stays until 4c.

## Amendments made in PR 4b (receive and send under the vault)

- **Content API on the codec** (`write_content`, `open_content`, `content_exists`, `content_plain_size`,
  `content_size_and_sha256`, `unlink_content`). With the vault on, content for `base` is `base.mcv`; it is encrypted to
  `base.mcv.part`, fsynced and renamed, and a plain file of the same name is removed. Names and associated data are the
  migration's: cache and spool `<id>.mcv` (AD = attachment id), quarantine `<transfer>.bin.mcv` (AD
  `quarantine/<transfer>.bin`), so a disable can restore everything the service wrote.
- **Receive.** The verified plaintext (held in memory only) is encrypted straight into `cache/incoming/<id>.mcv` and
  `saved_path` points at it; quarantine data is encrypted the same way. No plain file or temp file exists at any moment
  (the test fails the moment any file write carries the plaintext).
- **Send.** The outgoing source is read through the codec: the size and SHA-256 are those of the plaintext, computed by
  decrypting in memory; the chunks are cut from one in-memory decrypt of at most the 5 MiB attachment limit (secretstream
  is sequential, so there is no seeking in the file).
- **Upload staging.** With the vault on, the create endpoint keeps the request's file parts in memory (Werkzeug otherwise
  spools a part over 500 KB to the system temp directory, in plain), runs the same checks, and writes ONLY ciphertext, to
  its exclusive dot-prefixed `.tmp` name, renamed to `<id>.mcv`. The orphan sweep knows `<id>.mcv` and `<id>.mcv.part`.
- **Wait, not fail.** `vault_locked` raised by a transfer step (the lock arrived between the tick's check and the step)
  rolls the step back, leaves the transfer's state exactly as it was, stops the tick's row loop (every row needs the same
  key) and logs one line per lock period. `vault_corrupt` is an error of that transfer only and does not stop the others.
- **Fail closed for content too.** Plain content next to an enabled vault, a tampered `.mcv`, or one moved to another
  attachment raises `vault_corrupt`.
- **Residual, stated.** The idempotency `canonical_hash` of a create request is a SHA-256 over the file hash, comment,
  source name and routing fields; it is a one-way value, not a name, but a guessable name and comment could be confirmed
  against it by someone with the disk. Not changed here.
- **Still plain until 4c:** the content route (preview, download) does not decrypt yet, Save to Files still moves a plain
  file, and delete and cleanup of `.mcv` files are 4c. The experimental flag stays until 4c.

## Amendments made in PR 4c (preview, download, Save to Files, delete, cleanup; the experimental flag is gone)

- **Preview** (an inline image, at most the 5 MiB attachment limit): decrypted into ONE buffer and served only after the WHOLE
  file has verified (every chunk, the final tag, nothing after it). A damaged file answers 500 `vault_corrupt` with a JSON
  body; not one byte of the picture is sent first.
- **Download to device** (the owner's PR 1 note): streamed chunk by chunk with the exact `Content-Length`. The FIRST chunk is
  authenticated before the headers go out. Any failure while streaming (a chunk that does not authenticate, a truncated file,
  data after the final chunk, a size that does not add up, Lock now or a lock-unlock cycle while it runs) RAISES out of the
  response body: the connection is aborted and the browser marks the download failed; the body is never ended cleanly, so a
  truncated file cannot be presented as complete. Even if a server swallowed the exception, the promised length would not be
  met and `http.client` raises `IncompleteRead` (tested on a real connection). No decrypted temp file is ever written; no
  `Range` (`Accept-Ranges: none`).
- **The content route is gated** (423 `vault_locked` while locked); an `.mcv` without an active vault answers 409
  `vault_not_available`, a plain file next to an enabled vault 500 `vault_corrupt`: neither is ever served.
- **Save to Files** under the vault is a MOVE inside the vault: `cache/incoming/<id>.mcv` to `files/<id>.mcv`, the same bytes
  (same key, same associated data), nothing decrypted, nothing written in plain, no display name on the file system. A plain
  cache file next to an enabled vault is refused (`vault_corrupt`) and left where it is. A disable restores the saved file under
  its display name (tested: enable, receive, save, disable gives `files/<name>` with the original bytes and `saved_path` back).
- **Delete a saved copy and purge old received copies** only unlink files, so they stay available while locked (the command
  reads the display name with `open_display`, which is `None` while locked, instead of failing on a name it never uses).
- **The experimental flag is removed** (`MESHCENTER_MCA_VAULT_EXPERIMENTAL`, `enable_allowed`, `vault_not_ready`): with 4a, 4b
  and this part, every consumer of the encrypted layout exists. The status no longer carries `enable_allowed`.
- Still to come: Docker and documentation (PR 6), and the live round on the real workspace (with the owner's go and
  the full backup and exact restore rule).

## Amendments made in PR 5 (the UI)

- **One module, `static/vault-ui.js` (`window.MCVault`).** The lock chip in the dock (hidden while there is no vault; locked,
  unlocked or "vault job"), the banner above the Files transfer list (`data-vault-banner`), the unlock dialog (a centred dialog on a
  desktop, a bottom sheet at 768 px and below), the vault card in System (enable wizard, Lock now, change passphrase, new recovery
  key, disable) and the "locked" notice inside the Send dialog (`data-vault-notice`). The status comes from `GET /api/mca/vault`
  (polled every 20 s, every 1.5 s while a job runs; a 423 `vault_locked` from any Files request re-reads it at once).
- **Secrets stay in the field and the one request.** A passphrase or recovery key is never kept in a variable that outlives the
  handler, never written to browser storage, a URL or a toast, and the input is cleared as soon as the request is sent. The
  dialogs are not the UI1 `MCDialog` (it has no password field); the destructive confirmations that need no secret (Lock now,
  delete the plain backup) are `appConfirm` dialogs.
- **Enable wizard:** notices (flash storage, gallery not covered, downgrade refused, forgetting is permanent, disk swap when the
  status says so) with the strength hint and the "I understand the flash notice" box, then the job with its progress, then the
  recovery key shown once with two random groups typed back, then the unlock test (the wizard locks the vault and asks for the
  passphrase again), then the offer to delete the plain backup now. A recovery key that was not confirmed (the page was closed)
  stays available from the card ("Show the recovery key") because the status carries `recovery_key_pending`.
- **The plain backup warning cannot be dismissed.** It is drawn from `plain_backups` on every status read, in Files and on the
  vault card, with its folder, size and age and a Delete button (a danger `appConfirm`).
- **While locked** the Files list shows "Locked" instead of a name and the detail shows the facts that are not secret (direction,
  size) with an Unlock button. Roll back in the Updates card is shown disabled with "Disable the vault first" while
  `rollback_blocked_reason` is `vault_enabled`.
- **A leak found and closed (G3):** the in-process create the schedule engine uses (`_scheduled_create`) built its multipart body
  with the Werkzeug test builder, which spools a body over 500 KB to a temporary file. With the vault on, a scheduled picture over
  that size (the "original" resize) therefore reached the disk in plaintext. The body is now encoded in memory
  (`stream_encode_multipart(use_tempfile=False)`). `tests/test_mca_api_attachments.py` pins it (a 750 KB picture, the plaintext-write spy) and the locked case
  (one log line, one notification, nothing taken or queued, the rule stays enabled and the run counts for the 15 minute rule).

- **Findings of the real round on .104 (2026-11-01), fixed in PR 5:**
  - after a restart and an unlock the transfer list showed "Encrypted file" for every row: the snapshot built while locked has no
    names and the incremental refresh only rebuilds rows that changed. The service now asks the publisher for one full rebuild
    after an unlock (`request_rebuild()`).
  - enable and disable wrote the converted files with the umask's 0644: spool files and the identity seed and tokens went from 0600
    to 0644 (the `keys/` and `files/` directories did not change, so this was a loosening, not a leak). `encrypt_file` now takes a
    mode (0600 by default) and the migration keeps each file's own mode in both directions.
  - the backup a disable makes holds the encrypted workspace and needs the passphrase; it is listed with `encrypted: true` and
    warned about calmly. Only a plain backup gets the red "anyone can read it" warning.
  - the worker logged `AttachmentsService tick failed ... cannot schedule new futures after interpreter shutdown` on a stop and the
    stop took about 10 s. Not vault related: nothing told the MCAttach worker to stop when gunicorn's worker was asked to exit, so a
    tick that was mid-flight started a relay probe on a thread pool the exiting interpreter refuses, and the in-flight probe held
    the exit. `gunicorn.conf.py`'s `worker_exit` now calls `mca_runtime.stop_for_shutdown()` (stop flag, bounded wait), the tick
    starts nothing once the flag is set, and the interpreter-shutdown error ends the worker without an ERROR line.

## Amendments made in PR 6 (Docker, documentation, memory)

- **The unlock file is implemented** (`meshsrv/attachments/vault/unlock_file.py`, decision 9): `MESHCENTER_MCA_VAULT_UNLOCK_FILE`, off by default, honoured only in container mode (a native install logs that it is ignored), read once per start from `start_attachments_service()`, UTF-8 text of at most 1 KiB with only the final line break removed, symlinks followed, one attempt (a wrong secret counts against the rate limit like a typed one, there is no retry loop), a missing or wrong file leaves the vault locked with one System Log line carrying a code and never a value. A successful use logs a WARNING "MCAttach unlocked from the unlock file" that names the trade-off. `deploy/docker/compose.yaml` carries the commented secret block.
- **Memory on the Zero 2 W was measured, nothing is retained.** After the real round on .104 the free memory read 142.9 MB before and 103.4 MB after an enable, which looked like a leak. In an isolated process on the same node (`scripts/vault_memory_probe.py`, and an enable, three unlocks and a disable on a copy of the real workspace) resident memory goes back to the baseline after every Argon2id derivation: start 23.7 MB, after enable 24.4 MB (peak 87.7 MB, the transient 64 MiB), after unlock 18.4 to 18.5 MB on three runs, after disable 20.7 MB; unlocking from new threads (as request threads do) also returns to the baseline; `malloc_trim` returns about 1 MB. `MemAvailable` on a 425 MB node moves by 15 MB or more by itself (page cache), so the drop was the process's own few megabytes plus the page cache of the two backup copies and ordinary variation, not retained key-derivation memory. As cheap insurance `release_unused_memory()` (gc + `malloc_trim(0)`, best effort, glibc only) runs after an unlock, a lock, an enable and a disable. Running the derivation in a short-lived subprocess was considered and not done: it would not lower the peak (the memory is needed for that second either way) and there is nothing to give back.
- **Documentation:** `docs/MCAttach_Vault.md` (user guide: what is and is not protected, enable, daily use, recovery, flash storage, downgrade, memory and swap with the measured numbers, Docker and the unlock file, troubleshooting), README, User Guide, Docker guide, CLAUDE.md.

## Notes for later PRs (from the owner's PR 1 review)

- **PR 2 (done here):** unlock is serialized with MCAttach work and refused with a clear message when free memory is
  too low: the unlock needs the header's `memlimit` plus a 16 MiB margin (48 MiB for the 32 MiB floor); on a Zero 2 W
  with about 120 MB free that is allowed, below it the answer is 503 `insufficient_memory` with the numbers, and it is
  not counted as a wrong attempt.
- **PR 4:** "Download to device" streams before the final tag is verified. On any stream error the HTTP response is
  aborted (no clean end of the body), so the browser marks the download failed instead of saving a truncated file.
  A preview is served only after the whole file has been verified.

## Open questions

None. The three questions of the first draft were answered by the owner on 2026-10-31 (minimum passphrase 12 with a
strength hint; delays 1 s, 2 s, 4 s, then 60 s after 5 failures, no permanent lockout; no `vault.json.bak` after an
unlock) and are recorded in decisions 7 to 9.
