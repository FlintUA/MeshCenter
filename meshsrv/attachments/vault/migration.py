"""Enable and disable the vault: the migration engine (ADR-0013 Decision 8).

Enable turns a plain workspace into a vault workspace; disable turns it back. Both are resumable jobs whose state is
the disk itself (a plain file with its ``.mcv`` next to it, a column that does or does not start with ``v1:``) plus a
small journal, so a crash at ANY step resumes to the same end state and every step is idempotent.

Safety rules, each pinned by a test:
* before the first file is touched a full plaintext backup is made (unless the caller states it has its own);
* a plain file is deleted only after its ``.mcv`` was written, fsynced, renamed AND decrypted again in memory with
  the SHA-256 and size matching the original (``streamcrypt.verify_file``);
* the journal never holds a file name or a comment (items are named by attachment id), so it is not a leak;
* ``vault.json`` is written first on enable and removed LAST on disable; ``vault-migration.json`` goes last of all;
* a final scan proves that no plaintext file remains in the vault categories and that every sensitive column is
  encrypted (enable), or that none is (disable).

What is covered: ``cache/incoming``, ``spool/outgoing``, ``quarantine``, ``keys/``, and the saved-files library
``files/`` (its names become ``<attachment-id>.mcv``; a file without a database row is kept under an
``orphan-...`` id and its name goes into an encrypted index), plus the secret columns, the file name and the
comment in ``attachments.db``. The consumers of the encrypted layout (receive, send, preview, download) are PR 4.
"""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from meshsrv.attachments.vault import fields, header as header_mod, kdf, streamcrypt, swapcheck
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.keys import subkey

JOURNAL_FILE = "vault-migration.json"
HEADER_FILE = "vault.json"
MCV = ".mcv"
PART = ".part"
ORPHAN_INDEX = ".orphans" + MCV                      # inside files/, JSON {orphan id: display name}
ORPHAN_INDEX_AD = "orphans-index"
SPACE_MARGIN = 1.2                                   # the largest category plus 20 percent (ADR)

# (table, column, primary key column): what the vault encrypts in attachments.db. Names and comments are covered
# by decision 4; the others are secrets (data keys, nonce prefixes, receipt/upload/revoke tokens).
SENSITIVE_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    ("attachments", "file_name", "id"),
    ("attachments", "draft_comment", "id"),
    ("mca_receiver_state", "data_key", "attachment_id"),
    ("mca_receiver_state", "nonce_prefix", "attachment_id"),
    ("mca_receiver_state", "receipt_secret", "attachment_id"),
    ("mca_sender_state", "data_key", "attachment_id"),
    ("mca_sender_state", "nonce_prefix", "attachment_id"),
    ("mca_sender_state", "upload_token", "attachment_id"),
    ("mca_sender_state", "revoke_token", "attachment_id"),
    ("mca_sender_revoke_state", "revoke_token", "attachment_id"),
)
# transfers in these states read or write plaintext right now: the job refuses to start until they settle
BLOCKING_STATES = ("VALIDATING", "ENCRYPTING", "QUEUED_UPLOAD", "UPLOADING", "DOWNLOADING", "VERIFYING")
DB_BATCH = 100


class MigrationError(VaultError):
    """A refusal or failure of the job; ``code`` is stable, ``detail`` carries numbers (never a name or a secret)."""

    def __init__(self, code: str, message: str = "", **detail):
        super().__init__(code, message)
        self.detail = detail


@dataclass
class Env:
    """Everything the engine touches, injected: no globals, so a test can drive it against a temp workspace."""
    paths: object                                    # WorkspacePaths of the workspace
    data_dir: Path                                   # the instance data directory (the backup goes next to the MCA tree)
    conn: sqlite3.Connection
    workspace_id: str
    progress: Callable[[str, int, int], None] = lambda phase, done, total: None
    clock: Callable[[], float] = time.time
    free_space: Callable[[Path], int] = lambda path: shutil.disk_usage(str(path)).free
    swap_inspector: Callable[[], dict] = swapcheck.inspect_swaps
    after_step: Callable[[str], None] = lambda step: None      # test hook: raise to simulate a crash after a step


@dataclass
class Item:
    category: str          # cache | spool | files | quarantine | keys
    src: Path              # the plain file (enable) or the .mcv (disable)
    dest: Path
    ad_id: str             # associated data id of the stream
    row_id: Optional[str]  # the attachments row this file belongs to, when there is one


@dataclass
class Preflight:
    files: int = 0
    bytes: int = 0
    blocking: int = 0
    orphans: int = 0
    rows: int = 0
    backup_bytes: int = 0
    free_bytes: int = 0
    needed_bytes: int = 0
    swap: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# ---- small utilities --------------------------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    part = path.with_name(path.name + PART)
    with open(part, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, path)
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:                                   # Windows has no directory descriptors
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> Tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def journal_path(env: Env) -> Path:
    return Path(env.paths.root) / JOURNAL_FILE


def header_path(env: Env) -> Path:
    return Path(env.paths.root) / HEADER_FILE


def read_journal(env: Env) -> Optional[dict]:
    try:
        return json.loads(journal_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_journal(env: Env, doc: dict) -> None:
    _atomic_write(journal_path(env), json.dumps(doc, indent=2, sort_keys=True))


def _to_text(value) -> Optional[str]:
    """A column value as the text that is encrypted: a one-character tag keeps text and bytes apart."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "b" + base64.b64encode(bytes(value)).decode("ascii")
    return "s" + str(value)


def _from_text(text: str):
    if text.startswith("b"):
        return base64.b64decode(text[1:])
    return text[1:]


def _step(env: Env, name: str) -> None:
    env.after_step(name)


# ---- scanning the workspace ---------------------------------------------------------------------------------------

def _rows(env: Env) -> List[sqlite3.Row]:
    env.conn.row_factory = sqlite3.Row
    return env.conn.execute(
        "SELECT id, saved_path, plain_sha256, plain_size FROM attachments WHERE workspace_id = ?", (env.workspace_id,)
    ).fetchall()


def _listing(directory: Path, *, mcv: bool) -> List[Path]:
    """Files directly inside ``directory``: the plain ones (enable) or the encrypted ones (disable)."""
    if not directory.is_dir():
        return []
    out = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.name.endswith(PART):
            continue
        if entry.name == ORPHAN_INDEX:
            continue
        if entry.name.endswith(MCV) == mcv:
            out.append(entry)
    return out


def _row_by_saved_path(rows) -> Dict[str, str]:
    return {os.path.normcase(os.path.abspath(r["saved_path"])): r["id"] for r in rows if r["saved_path"]}


def plan_enable(env: Env) -> List[Item]:
    p = env.paths
    rows = _rows(env)
    row_ids = {r["id"] for r in rows}
    by_path = _row_by_saved_path(rows)
    items: List[Item] = []
    for src in _listing(p.cache_incoming, mcv=False):
        items.append(Item("cache", src, src.with_name(src.name + MCV), src.name, src.name if src.name in row_ids else None))
    for src in _listing(p.spool_outgoing, mcv=False):
        items.append(Item("spool", src, src.with_name(src.name + MCV), src.name, src.name if src.name in row_ids else None))
    for src in _listing(p.files, mcv=False):
        row_id = by_path.get(os.path.normcase(os.path.abspath(str(src))))
        if row_id is None:
            size = src.stat().st_size
            row_id = None
            ad_id = "orphan-" + hashlib.sha256(("%s|%d" % (src.name, size)).encode("utf-8")).hexdigest()[:12]
        else:
            ad_id = row_id
        items.append(Item("files", src, p.files / (ad_id + MCV), ad_id, row_id))
    for src in _listing(p.quarantine, mcv=False):
        items.append(Item("quarantine", src, src.with_name(src.name + MCV), "quarantine/" + src.name, None))
    for src in _listing(p.keys, mcv=False):
        items.append(Item("keys", src, src.with_name(src.name + MCV), "keys/" + src.name, None))
    return items


def preflight(env: Env, *, backup: bool) -> Preflight:
    """What an enable would do, and whether it may start. Raises MigrationError for a blocker."""
    items = plan_enable(env)
    report = Preflight(files=len(items), bytes=sum(i.src.stat().st_size for i in items))
    report.orphans = sum(1 for i in items if i.category == "files" and i.row_id is None)
    report.rows = len(_rows(env))
    placeholders = ",".join("?" for _ in BLOCKING_STATES)
    report.blocking = env.conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE workspace_id = ? AND state IN (%s)" % placeholders,
        (env.workspace_id,) + BLOCKING_STATES,
    ).fetchone()[0]
    if backup:
        report.backup_bytes = _tree_bytes(Path(env.paths.root)) + _db_bytes(env)
    report.needed_bytes = int((report.bytes + report.backup_bytes) * SPACE_MARGIN)
    report.free_bytes = env.free_space(Path(env.data_dir))
    report.swap = env.swap_inspector()
    if report.blocking:
        raise MigrationError("transfers_in_progress", "Transfers are still in progress.", count=report.blocking)
    if report.free_bytes < report.needed_bytes:
        raise MigrationError("insufficient_space", "Not enough free disk space.",
                             needed_bytes=report.needed_bytes, free_bytes=report.free_bytes)
    return report


def _tree_bytes(root: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def _db_bytes(env: Env) -> int:
    row = env.conn.execute("PRAGMA database_list").fetchall()
    for _seq, name, path in row:
        if name == "main" and path:
            try:
                return os.path.getsize(path)
            except OSError:
                return 0
    return 0


# ---- the plain backup ---------------------------------------------------------------------------------------------

BACKUP_PREFIX = "mca-vault-backup-"


def make_backup(env: Env) -> Path:
    """A full plaintext copy of the workspace tree and a consistent snapshot of attachments.db, in a 0700 folder next
    to the MCA tree. It is a liability (it holds everything the vault protects): the caller offers its deletion after
    the first successful unlock and the status shows a persistent warning while it exists."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(env.clock()))
    target = Path(env.data_dir) / (BACKUP_PREFIX + stamp)
    suffix = 1
    while target.exists():
        suffix += 1
        target = Path(env.data_dir) / ("%s%s-%d" % (BACKUP_PREFIX, stamp, suffix))
    target.mkdir(mode=0o700)
    copy = target / "workspace"
    shutil.copytree(str(env.paths.root), str(copy), ignore=shutil.ignore_patterns(JOURNAL_FILE, "*" + PART))
    snapshot = sqlite3.connect(str(target / "attachments.db"))
    try:
        env.conn.backup(snapshot)
    finally:
        snapshot.close()
    src_files = sum(1 for _b, _d, f in os.walk(str(env.paths.root)) for n in f if n != JOURNAL_FILE and not n.endswith(PART))
    dst_files = sum(len(f) for _b, _d, f in os.walk(str(copy)))
    if dst_files != src_files:
        raise MigrationError("backup_failed", "The backup is incomplete.")
    try:
        os.chmod(str(target), 0o700)
    except OSError:
        pass
    return target


def list_backups(data_dir) -> List[dict]:
    """The plain backups that exist (folder name, bytes, age): derived from the folder, so the warning cannot be lost
    by a restart or a missed click."""
    out = []
    try:
        entries = sorted(Path(data_dir).iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.is_dir() and entry.name.startswith(BACKUP_PREFIX):
            out.append({"name": entry.name, "bytes": _tree_bytes(entry), "created_at": entry.stat().st_mtime})
    return out


def delete_backup(data_dir, name: str) -> bool:
    """Delete one plain backup by folder name. Only a direct child of the data directory with the backup prefix."""
    if not name.startswith(BACKUP_PREFIX) or os.sep in name or "/" in name or name in (".", ".."):
        raise MigrationError("invalid_backup_name")
    target = Path(data_dir) / name
    if not target.is_dir():
        return False
    shutil.rmtree(str(target))
    return True


# ---- enable -----------------------------------------------------------------------------------------------------

@dataclass
class EnableResult:
    header: header_mod.VaultHeader
    storage_key: bytearray
    recovery_key: bytes
    backup: Optional[Path]
    summary: dict


def _encrypt_item(env: Env, item: Item, files_key: bytes, keys_key: bytes) -> None:
    key = keys_key if item.category == "keys" else files_key
    if item.dest.exists():
        # a crash after the rename and before the delete: if the encrypted copy verifies, only the plain file is left
        digest, size = _sha256_file(item.src)
        if streamcrypt.verify_file(str(item.dest), key, item.ad_id, digest, size):
            os.remove(str(item.src))
            return
        os.remove(str(item.dest))
    digest, size = _sha256_file(item.src)
    streamcrypt.encrypt_file(str(item.src), str(item.dest), key, item.ad_id)
    _step(env, "encrypted")
    if not streamcrypt.verify_file(str(item.dest), key, item.ad_id, digest, size):
        os.remove(str(item.dest))
        raise MigrationError("verify_failed", "A file could not be verified after encryption; nothing was deleted.")
    _fsync_dir(item.dest.parent)
    os.remove(str(item.src))
    _step(env, "plain_removed")


def _write_orphan_index(env: Env, files_key: bytes, names: Dict[str, str]) -> None:
    path = Path(env.paths.files) / ORPHAN_INDEX
    blob = json.dumps(names, sort_keys=True).encode("utf-8")
    part = str(path) + PART
    with open(part, "wb") as handle:
        handle.write(streamcrypt.encrypt_bytes(blob, files_key, ORPHAN_INDEX_AD))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, str(path))


def _read_orphan_index(env: Env, files_key: bytes) -> Dict[str, str]:
    path = Path(env.paths.files) / ORPHAN_INDEX
    if not path.exists():
        return {}
    return json.loads(streamcrypt.decrypt_bytes(path.read_bytes(), files_key, ORPHAN_INDEX_AD).decode("utf-8"))


def _clean_residue(env: Env) -> None:
    for directory in (env.paths.cache_incoming, env.paths.spool_outgoing, env.paths.files, env.paths.quarantine, env.paths.keys):
        if Path(directory).is_dir():
            for entry in Path(directory).iterdir():
                if entry.is_file() and entry.name.endswith(PART):
                    entry.unlink()


def _run_files_enable(env: Env, sk: bytes) -> int:
    files_key, keys_key = subkey(sk, "files"), subkey(sk, "keys")
    _clean_residue(env)
    items = plan_enable(env)
    orphans = _read_orphan_index(env, files_key)
    # the orphan names are recorded in the encrypted index BEFORE the file is touched, so a crash cannot lose one
    names = {i.ad_id: i.src.name for i in items if i.category == "files" and i.row_id is None}
    if names:
        orphans.update(names)
        _write_orphan_index(env, files_key, orphans)
    total = len(items)
    for number, item in enumerate(items, 1):
        _encrypt_item(env, item, files_key, keys_key)
        env.progress("files", number, total)
    return total


def _is_ours(field_key: bytes, table: str, column: str, pk, value) -> bool:
    """True when the stored value is one of OUR encrypted fields: the prefix alone is not proof (a plain value may start
    with ``v1:``), so it must also authenticate for this table, column and row."""
    if not fields.is_encrypted(value):
        return False
    try:
        fields.decrypt_field(field_key, table, column, str(pk), value)
        return True
    except VaultError:
        return False


def _scrub_db(env: Env) -> None:
    """SQLite keeps the old bytes of an updated value in free pages until they are reused: overwrite them (secure_delete
    zeroes freed content) and rewrite the file (VACUUM), so a name or a secret that was plain does not survive inside
    attachments.db. A flash card may still keep old blocks (ADR-0013, the flash limitation)."""
    env.conn.commit()
    env.conn.execute("VACUUM")
    try:
        env.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass


def _encrypt_db(env: Env, sk: bytes) -> int:
    field_key = subkey(sk, "fields")
    changed = 0
    env.conn.execute("PRAGMA secure_delete = ON")
    for table, column, pk in SENSITIVE_COLUMNS:
        env.conn.row_factory = sqlite3.Row
        rows = env.conn.execute("SELECT %s AS pk, %s AS v FROM %s WHERE %s IS NOT NULL" % (pk, column, table, column)).fetchall()
        todo = [r for r in rows if not _is_ours(field_key, table, column, r["pk"], r["v"])]
        for start in range(0, len(todo), DB_BATCH):
            for row in todo[start:start + DB_BATCH]:
                token = fields.encrypt_field(field_key, table, column, str(row["pk"]), _to_text(row["v"]))
                env.conn.execute("UPDATE %s SET %s = ? WHERE %s = ?" % (table, column, pk), (token, row["pk"]))
            env.conn.commit()
            changed += len(todo[start:start + DB_BATCH])
            _step(env, "db_batch")
    # the rows whose file moved: point saved_path at the encrypted file
    changed += _repoint_saved_paths(env, to_encrypted=True)
    _scrub_db(env)
    return changed


def _dest_for_row(env: Env, row_id: str, saved_path: str, *, encrypted: bool) -> Optional[Path]:
    """Where the file of this row lives in the other form, derived from where it lives now."""
    p = Path(saved_path)
    parent = os.path.normcase(os.path.abspath(str(p.parent)))
    for directory in (env.paths.cache_incoming, env.paths.spool_outgoing, env.paths.files):
        if os.path.normcase(os.path.abspath(str(directory))) == parent:
            return Path(directory) / (row_id + (MCV if encrypted else ""))
    return None


def _repoint_saved_paths(env: Env, *, to_encrypted: bool) -> int:
    """Enable: a row whose file now exists as <id>.mcv points at it. (Disable has its own, see _repoint_disabled.)"""
    count = 0
    if not to_encrypted:
        return count
    for row in _rows(env):
        saved = row["saved_path"]
        if not saved or saved.endswith(MCV):
            continue
        dest = _dest_for_row(env, row["id"], saved, encrypted=True)
        if dest is not None and dest.exists():
            env.conn.execute("UPDATE attachments SET saved_path = ? WHERE id = ?", (str(dest), row["id"]))
            count += 1
    return count


def verify_enabled(env: Env, sk: bytes) -> dict:
    """The final proof: no plaintext file in any vault category, and every sensitive value authenticates as ours."""
    leftovers = []
    for directory in (env.paths.cache_incoming, env.paths.spool_outgoing, env.paths.files, env.paths.quarantine, env.paths.keys):
        leftovers += [e.name for e in _listing(Path(directory), mcv=False)]
    if leftovers:
        raise MigrationError("plaintext_remains", "Plain files remain.", count=len(leftovers))
    field_key = subkey(sk, "fields")
    plain_columns = 0
    for table, column, pk in SENSITIVE_COLUMNS:
        env.conn.row_factory = sqlite3.Row
        for row in env.conn.execute("SELECT %s AS pk, %s AS v FROM %s WHERE %s IS NOT NULL" % (pk, column, table, column)):
            if not _is_ours(field_key, table, column, row["pk"], row["v"]):
                plain_columns += 1
    if plain_columns:
        raise MigrationError("plaintext_remains", "Plain database values remain.", count=plain_columns)
    return {"plain_files": 0, "plain_columns": 0}


def enable(env: Env, passphrase: bytes, *, params: Optional[kdf.KdfParams] = None, make_backup_copy: bool = True) -> EnableResult:
    if header_path(env).exists() or journal_path(env).exists():
        raise MigrationError("vault_already_enabled")
    report = preflight(env, backup=make_backup_copy)
    params = params or kdf.calibrate()
    env.progress("backup", 0, 1)
    backup = make_backup(env) if make_backup_copy else None
    _step(env, "backup_done")
    created = header_mod.create(passphrase, params)
    _write_journal(env, {"v": 1, "op": "enable", "phase": "files", "started_at": env.clock(),
                         "backup": backup.name if backup else None})
    _atomic_write(header_path(env), created.header.to_json())      # first: from here on the workspace is "migrating"
    _step(env, "header_written")
    sk = bytes(created.storage_key)
    try:
        _run_files_enable(env, sk)
        _write_journal(env, {"v": 1, "op": "enable", "phase": "db", "backup": backup.name if backup else None})
        _step(env, "files_done")
        changed = _encrypt_db(env, sk)
        _write_journal(env, {"v": 1, "op": "enable", "phase": "verify", "backup": backup.name if backup else None})
        proof = verify_enabled(env, sk)
        _step(env, "verified")
        journal_path(env).unlink()                                  # last of all: the workspace is now a vault
        _fsync_dir(Path(env.paths.root))
    finally:
        sk = b""
    summary = dict(report.as_dict(), db_values=changed, **proof)
    return EnableResult(header=created.header, storage_key=created.storage_key, recovery_key=created.recovery_key,
                        backup=backup, summary=summary)


def resume_enable(env: Env, storage_key: bytes) -> dict:
    """Finish an enable that was interrupted (the user unlocked with the passphrase first). Idempotent."""
    doc = read_journal(env)
    if not doc or doc.get("op") != "enable":
        raise MigrationError("nothing_to_resume")
    _run_files_enable(env, storage_key)
    changed = _encrypt_db(env, storage_key)
    proof = verify_enabled(env, storage_key)
    journal_path(env).unlink()
    _fsync_dir(Path(env.paths.root))
    return dict(db_values=changed, **proof)


# ---- disable ------------------------------------------------------------------------------------------------------

def _decrypt_item(env: Env, src: Path, dest: Path, key: bytes, ad_id: str, expect_sha: Optional[str], expect_size: Optional[int]) -> None:
    if dest.exists():
        os.remove(str(dest))                          # residue of a crash before the .mcv was removed: redo from the .mcv
    part = str(dest) + PART
    digest, size = hashlib.sha256(), 0
    try:
        with open(str(src), "rb") as handle, open(part, "wb") as out:
            for chunk in streamcrypt.decrypt_iter(handle, key, ad_id):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    if (expect_sha and digest.hexdigest() != str(expect_sha).lower()) or (expect_size is not None and size != expect_size):
        os.remove(part)
        raise MigrationError("verify_failed", "A decrypted file does not match its recorded checksum; nothing was deleted.")
    os.replace(part, str(dest))
    _fsync_dir(dest.parent)
    _step(env, "decrypted")
    os.remove(str(src))
    _step(env, "mcv_removed")


def _plan_disable(env: Env, field_key: bytes, files_key: bytes) -> List[Tuple[Path, Path, bytes, str, Optional[str], Optional[int]]]:
    from meshsrv.attachments.mime_allowlist import sanitize_display_name
    p = env.paths
    rows = {r["id"]: r for r in _rows(env)}
    orphans = _read_orphan_index(env, files_key)
    plan = []
    keys_key = None
    for src in _listing(Path(p.cache_incoming), mcv=True):
        rid = src.name[:-len(MCV)]
        r = rows.get(rid)
        plan.append((src, Path(p.cache_incoming) / rid, files_key, rid, r["plain_sha256"] if r else None, r["plain_size"] if r else None))
    for src in _listing(Path(p.spool_outgoing), mcv=True):
        rid = src.name[:-len(MCV)]
        plan.append((src, Path(p.spool_outgoing) / rid, files_key, rid, None, None))
    used = set()
    for src in _listing(Path(p.files), mcv=True):
        rid = src.name[:-len(MCV)]
        r = rows.get(rid)
        if r is not None:
            name = _display_name(env, field_key, rid) or rid
        else:
            name = orphans.get(rid, rid)
        name = sanitize_display_name(name)
        candidate, counter = name, 2
        while candidate in used or ((Path(p.files) / candidate).exists() and not _is_residue(Path(p.files) / candidate, src, files_key, rid)):
            stem, dot, suffix = name.rpartition(".")
            candidate = ("%s (%d).%s" % (stem, counter, suffix)) if dot and stem else "%s (%d)" % (name, counter)
            counter += 1
        used.add(candidate)
        plan.append((src, Path(p.files) / candidate, files_key, rid, r["plain_sha256"] if r else None, r["plain_size"] if r else None))
    for src in _listing(Path(p.quarantine), mcv=True):
        name = src.name[:-len(MCV)]
        plan.append((src, Path(p.quarantine) / name, files_key, "quarantine/" + name, None, None))
    return plan, keys_key


def _is_residue(candidate: Path, src: Path, key: bytes, ad_id: str) -> bool:
    """True when ``candidate`` is the plain file of an earlier, interrupted disable of this very item (same size and
    same SHA-256 as the decrypted .mcv): then it is reused, not given a counter name. A different file of the same name
    is a collision and is never overwritten."""
    try:
        if candidate.stat().st_size != streamcrypt.plain_size(src.stat().st_size):
            return False
        wanted, _ = _sha256_file(candidate)
        digest = hashlib.sha256()
        with open(str(src), "rb") as handle:
            for chunk in streamcrypt.decrypt_iter(handle, key, ad_id):
                digest.update(chunk)
        return digest.hexdigest() == wanted
    except (OSError, VaultError):
        return False


def _stored_name(env: Env, rid: str) -> Optional[str]:
    row = env.conn.execute("SELECT file_name FROM attachments WHERE id = ?", (rid,)).fetchone()
    return row[0] if row else None


def _display_name(env: Env, field_key: bytes, rid: str) -> Optional[str]:
    """The row's real file name: decrypted and untagged (a value that is not one of ours is a plain name)."""
    stored = _stored_name(env, rid)
    if stored is not None and _is_ours(field_key, "attachments", "file_name", rid, stored):
        return _from_text(fields.decrypt_field(field_key, "attachments", "file_name", rid, stored))
    return stored


def disable(env: Env, storage_key: bytes, *, make_backup_copy: bool = True) -> dict:
    """Turn the vault back into a plain workspace. Needs the key (an unlocked vault)."""
    if not header_path(env).exists():
        raise MigrationError("vault_not_enabled")
    if journal_path(env).exists() and (read_journal(env) or {}).get("op") == "enable":
        raise MigrationError("migration_pending")
    files_key, keys_key, field_key = subkey(storage_key, "files"), subkey(storage_key, "keys"), subkey(storage_key, "fields")
    backup = make_backup(env) if make_backup_copy else None
    _write_journal(env, {"v": 1, "op": "disable", "phase": "files", "backup": backup.name if backup else None, "started_at": env.clock()})
    summary = _run_disable(env, files_key, keys_key, field_key)
    header_path(env).unlink()                         # the vault goes away LAST ...
    _fsync_dir(Path(env.paths.root))
    _step(env, "header_removed")
    journal_path(env).unlink()                        # ... and then its journal
    _fsync_dir(Path(env.paths.root))
    summary["backup"] = backup.name if backup else None
    return summary


def resume_disable(env: Env, storage_key: bytes) -> dict:
    doc = read_journal(env)
    if not doc or doc.get("op") != "disable":
        raise MigrationError("nothing_to_resume")
    summary = _run_disable(env, subkey(storage_key, "files"), subkey(storage_key, "keys"), subkey(storage_key, "fields"))
    if header_path(env).exists():
        header_path(env).unlink()
    journal_path(env).unlink()
    return summary


def _run_disable(env: Env, files_key: bytes, keys_key: bytes, field_key: bytes) -> dict:
    _clean_residue(env)
    plan, _ = _plan_disable(env, field_key, files_key)
    total = len(plan)
    moved: Dict[str, Path] = {}
    for number, (src, dest, key, ad_id, sha, size) in enumerate(plan, 1):
        _decrypt_item(env, src, dest, key, ad_id, sha, size)
        if "/" not in ad_id:
            moved[ad_id] = dest
        env.progress("files", number, total)
    for src in _listing(Path(env.paths.keys), mcv=True):
        name = src.name[:-len(MCV)]
        _decrypt_item(env, src, Path(env.paths.keys) / name, keys_key, "keys/" + name, None, None)
    orphan_index = Path(env.paths.files) / ORPHAN_INDEX
    if orphan_index.exists():
        orphan_index.unlink()
    changed = _decrypt_db(env, field_key, moved)
    left = [e.name for d in (env.paths.cache_incoming, env.paths.spool_outgoing, env.paths.files, env.paths.quarantine, env.paths.keys)
            for e in _listing(Path(d), mcv=True)]
    if left:
        raise MigrationError("encrypted_files_remain", "Encrypted files remain.", count=len(left))
    return {"files": total, "db_values": changed}


def _repoint_disabled(env: Env, field_key: bytes, moved: Dict[str, Path]) -> int:
    """Disable: a row that still points at an <id>.mcv that is gone points at the plain file now. The files library
    uses display names, so the path comes from this run's moves, or (after a crash and resume) from the name field."""
    from meshsrv.attachments.mime_allowlist import sanitize_display_name
    count = 0
    for row in _rows(env):
        saved = row["saved_path"]
        if not saved or not saved.endswith(MCV) or Path(saved).exists():
            continue
        target = moved.get(row["id"])
        if target is None:
            parent = Path(saved).parent
            if os.path.normcase(os.path.abspath(str(parent))) == os.path.normcase(os.path.abspath(str(env.paths.files))):
                name = _display_name(env, field_key, row["id"])
                target = parent / sanitize_display_name(name or row["id"])
            else:
                target = Path(saved[:-len(MCV)])
        if target.exists():
            env.conn.execute("UPDATE attachments SET saved_path = ? WHERE id = ?", (str(target), row["id"]))
            count += 1
    return count


def _decrypt_db(env: Env, field_key: bytes, moved: Dict[str, Path]) -> int:
    env.conn.execute("PRAGMA secure_delete = ON")
    changed = _repoint_disabled(env, field_key, moved)          # first: it needs the encrypted name to find the file
    env.conn.commit()
    for table, column, pk in SENSITIVE_COLUMNS:
        env.conn.row_factory = sqlite3.Row
        rows = env.conn.execute("SELECT %s AS pk, %s AS v FROM %s WHERE %s IS NOT NULL" % (pk, column, table, column)).fetchall()
        ours = [r for r in rows if _is_ours(field_key, table, column, r["pk"], r["v"])]
        for start in range(0, len(ours), DB_BATCH):
            for row in ours[start:start + DB_BATCH]:
                plain = fields.decrypt_field(field_key, table, column, str(row["pk"]), row["v"])
                env.conn.execute("UPDATE %s SET %s = ? WHERE %s = ?" % (table, column, pk), (_from_text(plain), row["pk"]))
            env.conn.commit()
            changed += len(ours[start:start + DB_BATCH])
            _step(env, "db_batch")
    _scrub_db(env)
    return changed
