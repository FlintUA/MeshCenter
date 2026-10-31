"""meshsrv/attachments/vault/ - the MCAttach vault core library (ADR-0013, PR 1 of the plan).

Pure library: no file layout, no database, no threads, no wiring into the running service. Everything here takes
bytes or file objects and returns bytes or file objects, so it is testable with zero I/O and reviewable on its own.
The service, the migration, the API and the UI come in later PRs and only ever call into this package.

Modules:
  errors      VaultError with stable machine-readable codes (never a message that echoes a secret)
  kdf         Argon2id parameters, the floor, and calibration to about one second on the actual host
  header      vault.json: the storage key wrapped by the passphrase and by the recovery key
  streamcrypt .mcv files: libsodium secretstream in 64 KiB chunks, bound to the attachment id
  fields      encrypted database fields (``v1:<nonce>:<ciphertext>``), bound to table, column and row
  passphrase  passphrase normalization and the strength rule (minimum 12, a hint, no composition rules)
  recovery    the recovery key text form (13 groups of 4 base32 characters)
  memory      a zeroable, optionally mlock'ed buffer for key material

Only PyNaCl (already a dependency, ADR-0002) is used; this package never imports ``meshtastic``.
"""
