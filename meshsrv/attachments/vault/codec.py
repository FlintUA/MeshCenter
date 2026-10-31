"""The one place the running service reads and writes vault-protected data (ADR-0013, MCA-VAULT PR 4a).

Two kinds of protected data besides the attachment content files:

* **database fields** (``attachments.file_name`` and ``draft_comment``, the transfer keys and tokens): ``seal`` before a
  value is written, ``open`` after it is read. The encoding is the one the migration uses (``fields.py``, with a
  one-character tag that keeps text and bytes apart), so a value written by the migration and one written by the service
  are the same thing.
* **small secret files** (the identity seed and the provider upload tokens in ``keys/``): ``write_secret_file`` /
  ``read_secret_file``. With the vault enabled they live as ``<name>.mcv`` (the ``keys`` subkey, associated data
  ``keys/<name>``); with it disabled they are the plain file they always were.

``PlainCodec`` is the vault-disabled behaviour and the default for every connection and workspace manager that was not
given a vault, so code and tests that never heard of the vault run exactly as before. ``VaultCodec`` consults the vault
runtime on every call (enabled? unlocked?), so enable, disable, lock and unlock take effect without rewiring anything.

Locked: a secret cannot be read (``VaultError("vault_locked")``, nothing is written in plain form either); a display value
(a file name) is read as ``None``, which every caller already treats as "locked" or "no name".

Fail closed: with the vault enabled, a value that looks encrypted but does not authenticate for its table, column and row
(a flipped bit, a value copied from another row), and a plain secret file with no ``.mcv`` twin, are errors
(``VaultError("vault_corrupt")``), never "probably plain". After the migration no plain value can legitimately live in
these places; the migration and its resume read plain data through their own code, not through this codec. A display
value that is corrupt reads as ``UNREADABLE_NAME``, never the raw ``v1:`` string. The System Log gets the attachment id and
``table.column`` once per value, never the value.
"""
import os
import sqlite3
from pathlib import Path
from typing import Optional

from meshsrv.attachments.vault import fields, streamcrypt
from meshsrv.attachments.vault.errors import VaultError

MCV = ".mcv"
SECRET_FILE_MODE = 0o600
UNREADABLE_NAME = "(unreadable)"          # what a corrupt display value reads as; the UI of PR 5 gives it a translated look
_MAX_LOGGED = 256                         # corrupt values already reported (bounded; one line per value, not per read)


def to_text(value) -> Optional[str]:
    """A column value as the text that is encrypted: a one-character tag keeps text and bytes apart."""
    import base64
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "b" + base64.b64encode(bytes(value)).decode("ascii")
    return "s" + str(value)


def from_text(text: str):
    import base64
    if text.startswith("b"):
        return base64.b64decode(text[1:])
    return text[1:]


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    part = path.with_name(path.name + ".part")
    fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(part, mode)
    os.replace(str(part), str(path))


class PlainCodec:
    """No vault: values and files are stored as they always were."""

    active = False

    def seal(self, table: str, column: str, row_id, value):
        return value

    def open(self, table: str, column: str, row_id, value):
        return value

    def open_display(self, table: str, column: str, row_id, value):
        return value

    def write_secret_file(self, path: Path, data: bytes, *, mode: int = SECRET_FILE_MODE, exclusive: bool = False) -> None:
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0) | (os.O_EXCL if exclusive else os.O_TRUNC)
        fd = os.open(str(path), flags, mode)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.chmod(path, mode)

    def read_secret_file(self, path: Path) -> Optional[bytes]:
        return path.read_bytes() if path.exists() else None

    def secret_file_exists(self, path: Path) -> bool:
        return path.exists()

    def unlink_secret_file(self, path: Path) -> None:
        path.unlink(missing_ok=True)


PLAIN = PlainCodec()


class VaultCodec(PlainCodec):
    def __init__(self, vault) -> None:
        self._vault = vault
        self._reported = set()

    @property
    def active(self) -> bool:                       # type: ignore[override]
        return self._vault is not None and self._vault.is_enabled()

    def _fields_key(self) -> bytes:
        key = self._vault.subkey("fields")
        if key is None:
            raise VaultError("vault_locked")
        return key

    def seal(self, table: str, column: str, row_id, value):
        if value is None or not self.active:
            return value
        return fields.encrypt_field(self._fields_key(), table, column, str(row_id), to_text(value))

    def open(self, table: str, column: str, row_id, value):
        """A secret: raises ``vault_locked`` while locked and ``vault_corrupt`` for an encrypted-looking value that does not
        authenticate for this table, column and row. Without the vault a value is returned as it is; a value that does
        not look encrypted is plain (a database that was never migrated)."""
        if value is None or not self.active or not fields.is_encrypted(value):
            return value
        key = self._vault.subkey("fields")
        if key is None:
            raise VaultError("vault_locked")
        try:
            return from_text(fields.decrypt_field(key, table, column, str(row_id), value))
        except VaultError:
            self._report("%s.%s of %s" % (table, column, row_id))
            raise VaultError("vault_corrupt", "A stored value could not be verified.")

    def open_display(self, table: str, column: str, row_id, value):
        """A name for display: ``None`` while locked, ``UNREADABLE_NAME`` when it is corrupt."""
        try:
            return self.open(table, column, row_id, value)
        except VaultError as error:
            if error.code == "vault_locked":
                return None
            if error.code == "vault_corrupt":
                return UNREADABLE_NAME
            raise

    def _report(self, where: str) -> None:
        """One System Log line per corrupt value: what and where, never the value."""
        if where in self._reported or len(self._reported) >= _MAX_LOGGED:
            return
        self._reported.add(where)
        log = getattr(self._vault, "_log", None)
        if log is not None:
            log("ERROR", "MCAttach stored value unreadable", where)

    # ---- small secret files ---------------------------------------------------------------------------------------

    @staticmethod
    def _enc(path: Path) -> Path:
        return path.with_name(path.name + MCV)

    def write_secret_file(self, path: Path, data: bytes, *, mode: int = SECRET_FILE_MODE, exclusive: bool = False) -> None:
        if not self.active:
            return PlainCodec.write_secret_file(self, path, data, mode=mode, exclusive=exclusive)
        key = self._vault.subkey("keys")
        if key is None:
            raise VaultError("vault_locked")
        target = self._enc(path)
        if exclusive and (target.exists() or path.exists()):
            raise FileExistsError(str(target))
        _write_atomic(target, streamcrypt.encrypt_bytes(data, key, "keys/" + path.name), mode)
        path.unlink(missing_ok=True)                 # never leave a plain twin next to the encrypted file

    def read_secret_file(self, path: Path) -> Optional[bytes]:
        if not self.active:
            return PlainCodec.read_secret_file(self, path)
        target = self._enc(path)
        if target.exists():
            key = self._vault.subkey("keys")
            if key is None:
                raise VaultError("vault_locked")
            try:
                return streamcrypt.decrypt_bytes(target.read_bytes(), key, "keys/" + path.name)
            except VaultError:
                self._report("keys/%s" % path.name)
                raise VaultError("vault_corrupt", "A stored secret could not be verified.")
        if path.exists():                  # a plain secret next to an enabled vault: never a silent fallback
            self._report("keys/%s (plain file found)" % path.name)
            raise VaultError("vault_corrupt", "A plain secret file was found while the vault is enabled.")
        return None

    def secret_file_exists(self, path: Path) -> bool:
        return path.exists() or (self.active and self._enc(path).exists())

    def unlink_secret_file(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self._enc(path).unlink(missing_ok=True)


class VaultConnection(sqlite3.Connection):
    """The MCA database connection, carrying the codec the module-level SQL helpers use (``codec_of``). A plain
    ``sqlite3.Connection`` (a test fixture) has none and gets ``PLAIN``."""

    codec = PLAIN


def codec_of(conn) -> PlainCodec:
    return getattr(conn, "codec", PLAIN)
