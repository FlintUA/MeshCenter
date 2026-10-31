# ADR-0013: MCAttach vault (optional passphrase-protected storage for MCAttach data)

**Status:** Proposed (PR 0 of the MCA-VAULT plan; no code until this ADR is approved)
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
- **Subkeys:** `crypto_kdf_derive_from_key(SK, subkey_id, context)` with three fixed contexts (`mcvfiles`, `mcvfield`, `mcvkeys`), so file encryption, DB field encryption and key-file encryption never share a key.
- **Passphrase wrap:** Argon2id (`crypto_pwhash`, ALG_ARGON2ID13) over the passphrase and a random 16-byte salt gives a wrap key; SK is sealed with XChaCha20-Poly1305 (`crypto_aead_xchacha20poly1305_ietf`). The KDF parameters and salt are bound into the AEAD associated data, so a header whose parameters were tampered with fails to open.
- **Recovery wrap:** a 32-byte random recovery key (shown once, encoded as 8 groups of 4 base32 characters) wraps the same SK the same way; high entropy, so no KDF is needed beyond one `crypto_generichash` domain separation.
- **Header file:** `data/mca/<principal-id>/vault.json`, plain, versioned:
  ```
  { "v": 1, "kdf": {"alg": "argon2id13", "opslimit": n, "memlimit": n, "salt": b64, "measured_ms": n},
    "wrap_pass": {"nonce": b64, "ct": b64}, "wrap_recovery": {"nonce": b64, "ct": b64}, "check": b64 }
  ```
  Written atomically (temp file, fsync, rename), with the previous version kept as `vault.json.bak` until the next successful unlock.
- **Floor:** `memlimit` below 32 MiB or `opslimit` below the libsodium interactive minimum is refused on read, so a downgraded header cannot weaken the vault.

### 3. KDF calibration

At enable (and on "Re-calibrate"), starting from the 32 MiB floor and the minimum passes, the memory and then the passes are raised until one derivation takes about 1 second on this host, within a ceiling of one quarter of the currently free RAM. The chosen parameters and the measured time go into the header. Re-calibrating derives again, re-wraps SK under the new parameters and rewrites the header; no data is touched. A backup restored on a slower machine still unlocks (the parameters travel with the header), only more slowly. scrypt is a documented fallback only for a platform build that lacks Argon2id.

### 4. Encrypted file format

`<attachment-id>.mcv`, always named by the attachment id, never by a display name.

- Header: magic `MCV1`, format byte, the 24-byte `crypto_secretstream_xchacha20poly1305` header.
- Body: 64 KiB plaintext chunks, one secretstream message each, associated data = `MCV1 || attachment-id`; the last chunk carries `TAG_FINAL`, so truncation is detected; a chunk moved to another file fails authentication because the associated data differs.
- Key: the `mcvfiles` subkey, one per file via the random stream header.
- Reads are streaming: preview decodes into memory, "Download to device" streams chunk by chunk, and no decrypted temporary file is ever written.

### 5. Encrypted database fields

`v1:<base64 nonce>:<base64 ciphertext>` (XChaCha20-Poly1305, `mcvfield` subkey, associated data = `table.column.row-id`), stored in the same TEXT columns, so the schema does not change. Reading a field that starts with `v1:` while locked yields "locked", never an error and never the ciphertext in an API response. The DB migration is data only (a `vault_state` row says enabled).

### 6. Runtime: where the key lives, and what "locked" means

- The key ring is one object in the single gunicorn worker (`workers = 1` is already a hard rule): a `bytearray` holding SK, a generation counter, an `RLock`. Lock now zeroes the bytearray, bumps the generation, and aborts in-flight streaming reads at the next chunk. Best-effort `mlock` where available; documented as best effort.
- The key is read by the MCAttach worker thread and by request threads that stream content (decrypting needs the key but no SQLite). Neither holds the passphrase; only the derived wrap key exists, and only during unlock.
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

- Unlock by passphrase or recovery key; a wrong attempt is rate limited (delays 1 s, 2 s, 4 s, then 60 s after 5 failures), logged without any value.
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
7. Show the recovery key once with the type-back confirmation. The plain backup is kept until the user deletes it after the first successful unlock test.

The journal (`vault-migration.json`) records the phase and the last completed item; a crash resumes from it and every step is idempotent. Disable runs the same steps in reverse (unlocked, decrypt and verify, rewrite plain, remove `vault.json` last) with the same backup option and the same journal. Failure at any point leaves a consistent mix that the journal completes; both directions are re-runnable.

### 9. Docker and unattended starts

Same code path, same in-memory key. The passphrase is never an environment variable, a compose value or an image label. After every container start the vault is locked until unlocked in the UI. An optional, off-by-default `MESHCENTER_MCA_VAULT_UNLOCK_FILE` (a Docker secret path) exists for unattended NAS restarts; the documentation states the trade-off (the key then sits next to the data) and the System Log notes when it is in use.

### 10. API and commands

- `GET /api/mca/vault`: `{state: disabled | locked | unlocked | migrating, kdf: {memlimit, opslimit, measured_ms}, locked_items}`; never a key, passphrase or name while locked.
- `POST /api/mca/vault/enable | unlock | lock | change-passphrase | recalibrate | new-recovery-key | disable`: authenticated, CSRF-protected, rate limited (unlock), danger dialogs for enable and disable (UI1).
- New worker command kinds (`vault_enable`, `vault_unlock`, `vault_lock`, `vault_change_passphrase`, `vault_recalibrate`, `vault_disable`), added to `COMMAND_KINDS` with the pinned-set tests updated in the same PR, because keys, files and the database are worker-owned.
- The attachments snapshot carries `vault: {state}` and per-record `locked: true` with `file_name` and `comment` absent while locked.

## Threat model

| Threat | Result |
|---|---|
| Stolen or cloned SD card, node off | no content, name, comment or key readable without the passphrase or recovery key; sizes, MIME types, contacts and timing remain visible |
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
- Roll back to a release without the vault is not possible once a vault is enabled (older code would read ciphertext as data); the release notes of the shipping version say so, and disable-before-downgrade is the documented path.

## Alternatives considered

- **SQLCipher for `attachments.db`:** protects the database but not the files, needs a different SQLite build on the Pi and in Docker, and ADR-0003 keeps the standard library `sqlite3`.
- **LUKS or fscrypt on the data directory:** stronger at the block level but needs root, partitioning or a kernel feature that a Docker volume and the Droidian node do not have; not portable across our targets.
- **Per-file keys wrapped individually:** more flexible for sharing, no benefit here; one storage key with per-file stream headers is simpler and enough.
- **A key from the OS keyring or TPM:** unavailable on the Pi Zero 2 W and in containers; the passphrase model works everywhere.
- **Auto-lock after idle:** deferred to a later version by decision.

## PR plan

PR 1 vault core library (header, KDF calibration, key wrapping, secretstream files, field encryption, tests, no wiring); PR 2 vault state and locked runtime (commands, API, locked behaviour); PR 3 enable, disable, conversion and migration; PR 4 file operations under the vault; PR 5 UI; PR 6 Docker and documentation. Details, tests and the live round of each are in section 13 of `docs/proposals/MCA-VAULT.md`.

## Open questions for review

1. Minimum passphrase length and strength rule at enable (proposal: at least 12 characters, with a visible strength hint, not a hard complexity rule).
2. The wrong-attempt delays of Decision 7 (proposal as written).
3. Whether `vault.json.bak` should be kept after a successful unlock (proposal: no, it is replaced).
