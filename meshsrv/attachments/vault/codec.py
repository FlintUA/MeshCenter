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
import hashlib
import io
import os
import sqlite3
from pathlib import Path
from typing import BinaryIO, Optional, Tuple

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

    # ---- content files (received copies, the outgoing spool, quarantine) ------------------------------------------

    def content_path(self, base: Path) -> Path:
        """Where the content for ``base`` is stored: ``base`` itself, or ``base.mcv`` when the vault is on."""
        return base

    def write_content(self, base: Path, data: bytes, ad_id: str) -> Path:
        """Store ``data`` for ``base`` and return the path that now holds it."""
        base.write_bytes(data)
        return base

    def open_content(self, path: Path, ad_id: str) -> BinaryIO:
        """A readable, seekable file object over the content at ``path`` (plain bytes; an ``.mcv`` is decrypted into one
        buffer of at most the 5 MiB attachment limit)."""
        if str(path).endswith(MCV):
            raise VaultError("vault_not_available", "Encrypted content cannot be read without the vault.")
        return open(path, "rb")

    def content_exists(self, path: Path) -> bool:
        return os.path.isfile(path)

    def content_plain_size(self, path: Path) -> int:
        """The plaintext size of the content at ``path`` without reading it (``OSError`` when it is missing)."""
        size = os.stat(path).st_size
        if str(path).endswith(MCV):
            plain = streamcrypt.plain_size(size)
            if plain is None:
                raise VaultError("vault_corrupt", "Stored content has an impossible size.")
            return plain
        return size

    def content_size_and_sha256(self, path: Path, ad_id: str) -> Tuple[int, str]:
        digest, size = hashlib.sha256(), 0
        with self.open_content(path, ad_id) as handle:
            while True:
                block = handle.read(64 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
        return size, digest.hexdigest()

    def unlink_content(self, base: Path) -> None:
        """Remove the content for ``base`` in either form."""
        base.unlink(missing_ok=True)
        base.with_name(base.name + MCV).unlink(missing_ok=True)


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


    # ---- content files ---------------------------------------------------------------------------------------------

    def _files_key(self) -> bytes:
        key = self._vault.subkey("files")
        if key is None:
            raise VaultError("vault_locked")
        return key

    def content_path(self, base: Path) -> Path:
        return self._enc(base) if self.active else base

    def write_content(self, base: Path, data: bytes, ad_id: str) -> Path:
        """With the vault on: encrypt straight to ``<base>.mcv.part`` (fsync, rename), so no plaintext reaches the disk, and
        remove any plain file of the same name. A locked vault raises ``vault_locked`` and writes nothing."""
        if not self.active:
            return PlainCodec.write_content(self, base, data, ad_id)
        target = self._enc(base)
        part = target.with_name(target.name + ".part")
        try:
            self.write_encrypted_file(part, data, ad_id)
            os.replace(str(part), str(target))
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        base.unlink(missing_ok=True)
        return target

    def write_encrypted_file(self, path: Path, data: bytes, ad_id: str) -> None:
        """Encrypt ``data`` to exactly ``path`` (opened, written, fsynced, closed; mode 0600). The caller owns the name:
        a temp name it renames later (the upload staging), or the ``.part`` of ``write_content``."""
        key = self._files_key()
        with open(path, "wb") as handle:
            streamcrypt.encrypt_stream(io.BytesIO(data), handle, key, ad_id)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, SECRET_FILE_MODE)

    def open_content(self, path: Path, ad_id: str) -> BinaryIO:
        if str(path).endswith(MCV):
            if not self.active:
                raise VaultError("vault_not_available", "Encrypted content cannot be read without the vault.")
            key = self._files_key()
            try:
                return io.BytesIO(streamcrypt.decrypt_bytes(Path(path).read_bytes(), key, ad_id))
            except VaultError:
                self._report("content of %s" % ad_id)
                raise VaultError("vault_corrupt", "Stored content could not be verified.")
        if self.active:                                    # plain content next to an enabled vault: never served silently
            self._report("content of %s (plain file found)" % ad_id)
            raise VaultError("vault_corrupt", "A plain content file was found while the vault is enabled.")
        return open(path, "rb")

    def content_exists(self, path: Path) -> bool:
        return os.path.isfile(path)

    def unlink_content(self, base: Path) -> None:
        base.unlink(missing_ok=True)
        self._enc(base).unlink(missing_ok=True)


class VaultConnection(sqlite3.Connection):
    """The MCA database connection, carrying the codec the module-level SQL helpers use (``codec_of``). A plain
    ``sqlite3.Connection`` (a test fixture) has none and gets ``PLAIN``."""

    codec = PLAIN


def codec_of(conn) -> PlainCodec:
    return getattr(conn, "codec", PLAIN)
