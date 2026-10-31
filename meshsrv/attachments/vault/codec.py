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
"""
import os
import sqlite3
from pathlib import Path
from typing import Optional

from meshsrv.attachments.vault import fields, streamcrypt
from meshsrv.attachments.vault.errors import VaultError

MCV = ".mcv"
SECRET_FILE_MODE = 0o600


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
        """A secret: raises ``vault_locked`` while locked. A value that is not ours (plain, or a plain value that merely
        starts with ``v1:``) is returned as it is."""
        if value is None or not fields.is_encrypted(value) or not self.active:
            return value
        key = self._vault.subkey("fields")
        if key is None:
            raise VaultError("vault_locked")
        try:
            return from_text(fields.decrypt_field(key, table, column, str(row_id), value))
        except VaultError:
            return value

    def open_display(self, table: str, column: str, row_id, value):
        """A name for display: ``None`` while locked."""
        try:
            return self.open(table, column, row_id, value)
        except VaultError as error:
            if error.code == "vault_locked":
                return None
            raise

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
        target = self._enc(path)
        if self.active and target.exists():
            key = self._vault.subkey("keys")
            if key is None:
                raise VaultError("vault_locked")
            return streamcrypt.decrypt_bytes(target.read_bytes(), key, "keys/" + path.name)
        return path.read_bytes() if path.exists() else None

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
