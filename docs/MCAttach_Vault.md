# MCAttach vault (optional encryption of your file transfers at rest)

The vault encrypts what MCAttach keeps on the device: received and outgoing files, their names and comments, and the keys
MCAttach uses (the identity seed and the Relay upload tokens). It is **off by default**. When it is on, MCAttach is locked after
every start until you type the vault passphrase.

This is the user guide. The design, the reasons and the threat model are in
[ADR-0013](architecture/ADR-0013-mca-vault.md).

## What it protects, and what it does not

| Protected (inside the vault) | Not protected |
|---|---|
| Files you received (the cache, and the ones you saved with **Save to Files**) | The **Media gallery**: camera pictures, thumbnails and their sidecar files stay as they are |
| Files waiting to be sent (the spool) | Chats, nodes, telemetry, settings, the login hash, every other MeshCenter file |
| File names and transfer comments | Anything that existed in plaintext on an SD card or eMMC before you enabled the vault (see "Flash storage") |
| The MCAttach identity seed and the Relay upload tokens | A picture you sent from the gallery: only its MCAttach copy is inside the vault, the gallery original is not |

The vault protects a device that is **switched off or stolen**, or a copy of its card or volume. It does not protect a running,
unlocked device against someone who has root on it: while it is unlocked the key is in the process memory.

## Turn it on

1. Open **Settings**, find the **MCAttach vault** card, press **Enable the vault...**.
2. Read the notices. Choose a passphrase of at least 12 characters (a few words in a row are easier to remember than symbols).
   The strength hint under the field is only a hint; the node decides.
3. Keep **a plain backup of the current data** ticked unless you have your own backup. Tick **I understand the notice about flash
   storage**, press **Enable the vault**. Transfers that are still running must finish first.
4. MCAttach encrypts every file and rewrites the names and comments, verifying each file before the plain one is removed. A node
   with a few megabytes of transfers takes a few seconds.
5. **Write the recovery key on paper.** It is shown once, as 13 groups of 4 characters. The dialog asks you to type two of the
   groups back. Keep the paper away from the device.
6. **The unlock test:** the vault locks and you unlock it again with the passphrase you just chose. Only then are you sure you can
   get back in.
7. The dialog offers **Delete the plain backup now**. Do it, or keep it for a short time on purpose: the plain backup is a
   complete readable copy of everything the vault is meant to protect. While it exists a red warning stays in Files and on the
   vault card; it cannot be dismissed.

If MeshCenter restarts during step 4, MCAttach stays locked and the Files banner offers **Finish the job**. Every step can be
repeated safely.

## Daily use

- After every start (a reboot, a service restart, a container start) the vault is **locked**. The lock chip in the dock says so;
  press it, or **Unlock** in the Files banner, and type the passphrase. Wrong attempts wait 1 s, 2 s, 4 s and then 60 s after five
  failures. There is no permanent lockout.
- **Lock now** (the chip, or the card) locks it again. The key is wiped from memory.
- While locked, everything else in MeshCenter works. MCAttach itself:
  - shows transfers with a **Locked** placeholder instead of the name; the contact, time, size and type remain;
  - records incoming file offers, but accepts, downloads and decrypts nothing until you unlock;
  - cannot send, preview, download or save; the Send dialog offers **Unlock**;
  - postpones key requests and answers to the contact, and processes them after the unlock;
  - skips a scheduled "send camera picture" run with one System Log line and one notification (the schedule stays on).
- Names and comments never appear in the System Log, notifications or toasts.

## Passphrase, recovery key, changing them

- **Change passphrase...** re-wraps the key; the recovery key keeps working.
- **New recovery key...** makes a new one (shown once, two groups typed back); the old one stops working.
- **Forgetting the passphrase and losing the recovery key means the data is gone for good.** There is no other way in. That is
  the point.

## Turn it off

**Disable the vault...** decrypts everything back to plain files and removes the vault. Keep the backup of the encrypted data
tick on unless you have your own. That backup is encrypted (it still needs the passphrase), and it is listed with a calm notice
until you delete it.

## Flash storage (SD cards, eMMC)

Deleting a file on an SD card or eMMC is not a secure erase. Wear levelling can leave old copies of data that existed **before**
you enabled the vault, and of the plain backup after you delete it, recoverable from a cloned card until the blocks happen to be
reused. The vault fully protects data that was received or sent **after** it was enabled, because that data never existed in
plaintext on the card (outgoing files are encrypted when they are queued). For the strongest protection enable the vault on a
fresh installation. Overwriting before deleting is not offered, because it does not defeat wear levelling and shortens the life
of the card.

## Updates, Roll back and going back to an older version

Older versions cannot read the vault: they would treat the encrypted files as data and could write plain files next to them.
So **while a vault exists, Roll back is refused** ("Disable the vault first"), and an update never moves to an older version. If you want an older version, disable the vault first. The same applies to an older Docker image and to a manual
`git checkout` of an older tag: those cannot be refused by the code, so disable the vault before.

## Memory and swap

Unlocking derives the key with Argon2id. The memory it needs (the "memory limit") is chosen when the vault is enabled, from the
free memory of that moment: at least 32 MiB, never more than a quarter of what is free, aiming at about one second. On a Raspberry
Pi Zero 2 W (about 425 MB usable) the node chose **64 MiB**, and an unlock takes about one second.

- The memory is needed **for about a second and is then freed**. The vault card shows the numbers (**Key derivation: 64 MiB**).
  If less than the limit plus a 16 MiB margin is free at unlock time, the unlock is refused with "Not enough free memory"
  (it is not counted as a wrong attempt); close something and try again.
- Measured on the Zero 2 W in an isolated process (the numbers come from `scripts/vault_memory_probe.py`; see below):

  | Step | Resident memory | Peak |
  |---|---|---|
  | Start (imports, database open) | 23.7 MB | |
  | After an enable job on a copy of the real workspace (3.8 MB, plain backup on) | 24.4 MB | 87.7 MB (+64 MiB, transient) |
  | After the 1st, 2nd, 3rd unlock | 18.4, 18.5, 18.5 MB | no growth |
  | After a disable job | 20.7 MB | |

  Nothing is retained after the derivation. After an unlock, a lock and a finished job MeshCenter still runs a garbage collection
  and asks glibc to return free pages (`malloc_trim`); that gives back about 1 MB at most. The free memory shown by `free` moves by
  15 MB or more on its own on a node this small (the page cache comes and goes), so compare resident memory (`VmRSS` of the
  gunicorn worker), not `MemAvailable`, before and after.
- **Run it on your node:** from a checkout, `python scripts/vault_memory_probe.py 64 3 5 trim` (memory limit in MiB, passes,
  rounds, `trim`). It uses a throwaway workspace and a made-up passphrase and touches nothing of yours.
- **Swap.** The key lives in a page locked with `mlock` so it is not written to swap; if `mlock` fails the vault card says so.
  Decrypted data in ordinary memory can be swapped. Raspberry Pi OS Trixie uses **zram** (compressed swap in RAM, nothing reaches
  the card). Older images and some setups use `dphys-swapfile`, a swap file **on the card**. Check with `swapon --show`: a line
  with a path under `/` (not `/dev/zram0`) is disk swap. The enable dialog and the vault card warn when they see it. Switch to
  zram or turn swap off.

## Docker and the NAS

- The vault works the same in the container (one worker, the key in the process memory). After **every container start the
  vault is locked** until you unlock it in the UI. The passphrase is never an environment variable, a compose value or an image
  label.
- In Docker the **host** decides about swap: a NAS may swap to disk. The check runs inside the container where it can see
  `/proc/swaps`.
- `data/` stays as secret as before. With the vault on, `data/mca/` holds only ciphertext, `vault.json` (the wrapped key) and the
  database with encrypted columns. Back up by stopping the container and copying `data/`; keep the recovery key somewhere else.
- **Unattended unlock (optional, off by default).** A NAS that restarts by itself leaves MCAttach locked until someone opens the UI.
  If that is not acceptable, set `MESHCENTER_MCA_VAULT_UNLOCK_FILE` to the path of a file that holds the passphrase, normally a
  Docker secret:

  ```yaml
  services:
    meshcenter:
      environment:
        MESHCENTER_MCA_VAULT_UNLOCK_FILE: /run/secrets/mca_vault_passphrase
      secrets:
        - mca_vault_passphrase
  secrets:
    mca_vault_passphrase:
      file: ./secrets/mca_vault_passphrase     # chmod 400, owned by the container user (PUID)
  ```

  **The trade-off is real:** the key can then be derived by anyone who can read both the volume and the secret file, so the vault
  protects much less (a stolen disk with the secret next to it is open). It still protects data that leaves the NAS without that
  file, for example a backup of `data/` alone. The file is read once per start, as UTF-8 text of at most 1 KiB with only the final
  line break removed; one attempt, no retry loop; a wrong or unreadable file leaves the vault locked and one System Log line says
  why. Every use is noted in the System Log (**MCAttach unlocked from the unlock file**). It is ignored outside container mode.

## If something goes wrong

| What you see | What it means |
|---|---|
| "The passphrase or recovery key is wrong" | Typo, or the other keyboard layout. Use the recovery key instead (**Use the recovery key** in the dialog). |
| "Too many attempts. Try again in N s" | The delay after failed attempts; wait. |
| "Not enough free memory" | The unlock needs the memory limit plus 16 MiB. Close something and try again. |
| "A vault job was interrupted" / **Finish the job** | The enable or disable stopped half way (power loss). The journal resumes it; press **Finish the job**. If memory is too low MCAttach stays locked and says so; nothing is retried in a loop. |
| "A stored value could not be verified" | A file or a database value does not authenticate. The data may be damaged; do not disable the vault, copy `data/` first. |
| A red warning about a plain backup | The folder `data/mca-vault-backup-...` is a readable copy. Delete it from the warning when you no longer need it. |
| MCAttach says "not running" after an update | A vault header written by a newer version cannot be read. The System Log says so; MCAttach is never "repaired" into a worse state. |

## For developers

`meshsrv/attachments/vault/` is the library (header, key derivation, `.mcv` stream files, encrypted fields, the runtime state
machine, the migration, `unlock_file.py`, `trim.py`); `static/vault-ui.js` is the UI; the API is `/api/mca/vault*` in
`api/api_attachments.py`. The tests are `tests/test_mca_vault_*.py` and `tests/frontend/test_vault_ui.mjs`. See ADR-0013.
