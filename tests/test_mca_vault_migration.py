"""MCA-VAULT PR 3 (ADR-0013 Decision 8): the enable/disable migration engine against a real workspace and database."""
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from meshsrv.attachments.db import migrations
from meshsrv.attachments.vault import fields, header, kdf, migration, passphrase, streamcrypt
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.keys import subkey
from meshsrv.attachments.workspace import MCAWorkspaceManager

PRINCIPAL = "ab" * 8
WORKSPACE_ID = "ws1"
PASS = passphrase.normalize("correct horse battery staple")
FLOOR = kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT)

SECRET_NAME = "secret-report-2026.pdf"
SECRET_NAME_2 = "holiday photo ü.jpg"
ORPHAN_NAME = "stray-orphan-file.txt"
SECRET_COMMENT = "private comment about the secret"
SEED_BYTES = b"S" * 32
TOKEN_BYTES = b"relay-upload-token-0123456789"
DATA_KEY = "datakey-hex-aaaaaaaaaaaaaaaaaaaaaaaa"
REVOKE_TOKEN_BYTES = b"\x00\x01\x02revoke-capability"

CONTENT = {
    "recv1": os.urandom(70000),            # received, still in the cache
    "saved1": os.urandom(5000),            # received and saved to files/ (display name SECRET_NAME)
    "saved2": b"",                          # an empty saved file
    "sent1": os.urandom(130000),           # outgoing spool
}


def build_workspace(tmp_path):
    data_dir = tmp_path / "data"
    wsm = MCAWorkspaceManager(str(data_dir))
    paths = wsm.ensure_workspace(PRINCIPAL)
    conn = sqlite3.connect(str(wsm.mca_dir / "attachments.db"), check_same_thread=False)
    migrations.migrate(conn)

    def row(rid, direction, state, name, saved_path, comment=None, size=0):
        conn.execute(
            "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, file_name, mime_type, plain_size, "
            "plain_sha256, saved_path, draft_comment, created_at, hard_expires_at, download_grace_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'application/pdf', ?, ?, ?, ?, 1, 2, 3600)",
            (rid, WORKSPACE_ID, rid[:8] + "00" * 12, direction, PRINCIPAL, state, name, size,
             hashlib.sha256(CONTENT.get(rid, b"")).hexdigest(), saved_path, comment))

    (paths.cache_incoming / "recv1").write_bytes(CONTENT["recv1"])
    row("recv1", "received", "AVAILABLE", SECRET_NAME_2, str(paths.cache_incoming / "recv1"), size=len(CONTENT["recv1"]))
    (paths.files / SECRET_NAME).write_bytes(CONTENT["saved1"])
    row("saved1", "received", "AVAILABLE", SECRET_NAME, str(paths.files / SECRET_NAME), size=len(CONTENT["saved1"]))
    (paths.files / "empty.bin").write_bytes(CONTENT["saved2"])
    row("saved2", "received", "AVAILABLE", "empty.bin", str(paths.files / "empty.bin"), size=0)
    (paths.files / ORPHAN_NAME).write_bytes(b"nobody owns this file")                      # no row: an orphan
    (paths.spool_outgoing / "sent1").write_bytes(CONTENT["sent1"])
    row("sent1", "sent", "READY_TO_SEND", "outgoing.pdf", str(paths.spool_outgoing / "sent1"), comment=SECRET_COMMENT, size=len(CONTENT["sent1"]))
    row("offer1", "received", "WAITING_CONSENT", "offered-but-not-accepted.pdf", None)       # no file yet, still has a name
    row("dang1", "received", "AVAILABLE", "dangling-name.pdf", str(paths.files / "dangling-name.pdf"))   # saved file is gone, the path stays
    row("moved1", "received", "AVAILABLE", "moved-away.pdf", "/old/data/dir/mca/ws/files/moved-away.pdf")   # a database that was moved
    (paths.quarantine / "deadbeef.bin").write_bytes(b"quarantined bytes")
    (paths.keys / "identity.seed").write_bytes(SEED_BYTES)
    (paths.keys / "provider-token").write_bytes(TOKEN_BYTES)
    conn.execute("INSERT INTO mca_receiver_state (attachment_id, data_key, nonce_prefix, receipt_secret, chunk_count, plain_size, created_at, updated_at) "
                 "VALUES ('recv1', ?, 'np-recv', 'receipt-secret-recv1', 1, 10, 1, 1)", (DATA_KEY,))
    conn.execute("INSERT INTO mca_sender_state (attachment_id, data_key, nonce_prefix, upload_token, revoke_token, created_at, updated_at) "
                 "VALUES ('sent1', ?, 'np-sent', 'upload-token-sent1', ?, 1, 1)", ("v1:looks-encrypted-but-is-a-plain-value", REVOKE_TOKEN_BYTES))
    conn.execute("INSERT INTO mca_sender_revoke_state (attachment_id, revoke_token, delete_after, created_at, updated_at) "
                 "VALUES ('sent1', 'revoke-token-text', 'x', 'x', 'x')")
    conn.commit()
    env = migration.Env(paths=paths, data_dir=data_dir, conn=conn, workspace_id=WORKSPACE_ID,
                        free_space=lambda path: 10 ** 12, swap_inspector=lambda: {"disk_swap": False, "known": True})
    return env


def tree(root: Path):
    out = {}
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            full = Path(base) / name
            out[str(full.relative_to(root)).replace("\\", "/")] = full.read_bytes()
    return out


def sensitive_snapshot(conn):
    snapshot = {}
    conn.row_factory = sqlite3.Row
    for table, column, pk in migration.SENSITIVE_COLUMNS:
        snapshot[(table, column)] = {r["pk"]: r["v"] for r in conn.execute("SELECT %s AS pk, %s AS v FROM %s" % (pk, column, table))}
    snapshot["saved_path"] = {r["id"]: r["saved_path"] for r in conn.execute("SELECT id, saved_path FROM attachments")}
    return snapshot


@pytest.fixture
def env(tmp_path):
    e = build_workspace(tmp_path)
    yield e
    e.conn.close()


def run_enable(env, **kw):
    return migration.enable(env, PASS, params=FLOOR, **kw)


# ---- enable: the end state ------------------------------------------------------------------------------------------

def test_preflight_counts_what_enable_will_touch(env):
    report = migration.preflight(env, backup=True)
    assert report.files == 8 and report.orphans == 1 and report.rows == 7 and report.blocking == 0
    assert report.bytes == sum(len(v) for v in CONTENT.values()) + len(b"nobody owns this file") + len(b"quarantined bytes") + 32 + len(TOKEN_BYTES)
    assert report.backup_bytes > report.bytes and report.needed_bytes > report.bytes + report.backup_bytes


def test_enable_encrypts_every_file_and_every_sensitive_value_and_ends_with_a_vault(env):
    result = run_enable(env)
    root = Path(env.paths.root)
    assert not (root / "vault-migration.json").exists() and (root / "vault.json").exists(), "the journal goes, the vault stays"
    assert result.summary["plain_files"] == 0 and result.summary["plain_columns"] == 0
    names = sorted(tree(root))
    assert all(n.endswith(".mcv") or n in ("vault.json",) for n in names), names
    sk = bytes(result.storage_key)
    files_key, field_key, keys_key = subkey(sk, "files"), subkey(sk, "fields"), subkey(sk, "keys")
    assert streamcrypt.decrypt_bytes((root / "cache/incoming/recv1.mcv").read_bytes(), files_key, "recv1") == CONTENT["recv1"]
    assert streamcrypt.decrypt_bytes((root / "files/saved1.mcv").read_bytes(), files_key, "saved1") == CONTENT["saved1"]
    assert streamcrypt.decrypt_bytes((root / "files/saved2.mcv").read_bytes(), files_key, "saved2") == b""
    assert streamcrypt.decrypt_bytes((root / "spool/outgoing/sent1.mcv").read_bytes(), files_key, "sent1") == CONTENT["sent1"]
    assert streamcrypt.decrypt_bytes((root / "keys/identity.seed.mcv").read_bytes(), keys_key, "keys/identity.seed") == SEED_BYTES
    assert streamcrypt.decrypt_bytes((root / "quarantine/deadbeef.bin.mcv").read_bytes(), files_key, "quarantine/deadbeef.bin") == b"quarantined bytes"
    value = env.conn.execute("SELECT file_name FROM attachments WHERE id = 'saved1'").fetchone()[0]
    assert fields.decrypt_field(field_key, "attachments", "file_name", "saved1", value) == "s" + SECRET_NAME
    assert env.conn.execute("SELECT saved_path FROM attachments WHERE id = 'saved1'").fetchone()[0] == str(root / "files/saved1.mcv")
    assert env.conn.execute("SELECT saved_path FROM attachments WHERE id = 'offer1'").fetchone()[0] is None


def test_the_header_unlocks_with_the_passphrase_and_with_the_recovery_key_after_enable(env):
    result = run_enable(env)
    parsed = header.VaultHeader.from_json((Path(env.paths.root) / "vault.json").read_text(encoding="utf-8"))
    assert bytes(header.unlock(parsed, PASS)) == bytes(result.storage_key)
    assert bytes(header.unlock_with_recovery(parsed, result.recovery_key)) == bytes(result.storage_key)


def test_a_plain_value_that_looks_like_an_encrypted_one_is_still_encrypted(env):
    result = run_enable(env)
    field_key = subkey(bytes(result.storage_key), "fields")
    stored = env.conn.execute("SELECT data_key FROM mca_sender_state WHERE attachment_id = 'sent1'").fetchone()[0]
    assert fields.decrypt_field(field_key, "mca_sender_state", "data_key", "sent1", stored) == "s" + "v1:looks-encrypted-but-is-a-plain-value"


def test_a_blob_column_round_trips_as_bytes(env):
    result = run_enable(env)
    key = bytes(result.storage_key)
    migration.disable(env, key, make_backup_copy=False)
    assert env.conn.execute("SELECT revoke_token FROM mca_sender_state WHERE attachment_id = 'sent1'").fetchone()[0] == REVOKE_TOKEN_BYTES


# ---- no plaintext left (the leak scan) ----------------------------------------------------------------------------

def test_no_plain_name_content_comment_seed_or_token_remains_in_the_workspace_or_the_database_file(env):
    before_db_markers = [SECRET_NAME, SECRET_NAME_2, SECRET_COMMENT, DATA_KEY, "receipt-secret-recv1", "upload-token-sent1",
                         "revoke-token-text", "offered-but-not-accepted", "outgoing.pdf", "dangling-name.pdf", "moved-away.pdf"]
    assert any(m.encode("utf-8") in Path(env.conn.execute("PRAGMA database_list").fetchone()[2]).read_bytes() for m in before_db_markers), \
        "the markers really are in the database file before enabling (the scan is meaningful)"
    run_enable(env)
    root = Path(env.paths.root)
    blobs = list(tree(root).items()) + [("attachments.db", Path(env.conn.execute("PRAGMA database_list").fetchone()[2]).read_bytes())]
    needles = [SECRET_NAME, SECRET_NAME_2, ORPHAN_NAME, SECRET_COMMENT, DATA_KEY, "receipt-secret-recv1", "upload-token-sent1",
               "revoke-token-text", "offered-but-not-accepted", "outgoing.pdf", "empty.bin", "dangling-name.pdf", "moved-away.pdf"]
    raw = [SEED_BYTES, TOKEN_BYTES, REVOKE_TOKEN_BYTES, CONTENT["recv1"][:64], CONTENT["saved1"][:64], CONTENT["sent1"][:64], b"nobody owns this file"]
    for name, data in blobs:
        for needle in needles:
            assert needle.encode("utf-8") not in data, (needle, name)
        for chunk in raw:
            assert chunk not in data, (chunk[:8], name)
    for relative in tree(root):                                   # and no name on disk
        for needle in (SECRET_NAME, SECRET_NAME_2, ORPHAN_NAME, "outgoing.pdf", "empty.bin"):      # (the key files keep their fixed internal names)
            assert needle not in relative, relative


def test_a_saved_path_that_cannot_be_converted_does_not_keep_its_name_and_comes_back_on_disable(env):
    original = env.conn.execute("SELECT saved_path FROM attachments WHERE id = 'dang1'").fetchone()[0]
    assert "dangling-name.pdf" in original
    result = run_enable(env)
    after = env.conn.execute("SELECT saved_path FROM attachments WHERE id = 'dang1'").fetchone()[0]
    assert "dangling-name" not in after and after.endswith("dang1.mcv"), after
    moved = "/old/data/dir/mca/ws/files/moved-away.pdf"
    assert migration._read_index(env, subkey(bytes(result.storage_key), "files"))["dangling"] == {"dang1": original, "moved1": moved}
    assert "moved-away" not in env.conn.execute("SELECT saved_path FROM attachments WHERE id = 'moved1'").fetchone()[0]
    migration.disable(env, bytes(result.storage_key), make_backup_copy=False)
    assert env.conn.execute("SELECT saved_path FROM attachments WHERE id = 'dang1'").fetchone()[0] == original
    assert env.conn.execute("SELECT saved_path FROM attachments WHERE id = 'moved1'").fetchone()[0] == moved


def test_the_orphan_name_is_kept_only_in_the_encrypted_index_and_comes_back_on_disable(env):
    result = run_enable(env)
    key = bytes(result.storage_key)
    index = Path(env.paths.files) / migration.ORPHAN_INDEX
    assert index.exists() and ORPHAN_NAME.encode() not in index.read_bytes()
    names = migration._read_orphan_index(env, subkey(key, "files"))
    assert list(names.values()) == [ORPHAN_NAME]
    migration.disable(env, key, make_backup_copy=False)
    assert (Path(env.paths.files) / ORPHAN_NAME).read_bytes() == b"nobody owns this file" and not index.exists()


def test_the_journal_never_holds_a_name_or_a_comment(env):
    seen = []

    def spy(step):
        journal = migration.read_journal(env)
        if journal:
            seen.append(json.dumps(journal))
    env.after_step = spy
    run_enable(env)
    assert seen and all(SECRET_NAME not in text and SECRET_COMMENT not in text and "outgoing" not in text for text in seen)


# ---- disable: back to exactly the original ---------------------------------------------------------------------------

def test_enable_then_disable_gives_back_exactly_the_original_workspace_and_database(env):
    root = Path(env.paths.root)
    original_tree, original_db = tree(root), sensitive_snapshot(env.conn)
    result = run_enable(env)
    summary = migration.disable(env, bytes(result.storage_key), make_backup_copy=False)
    assert tree(root) == original_tree, "every file, byte for byte and name for name"
    assert sensitive_snapshot(env.conn) == original_db, "every sensitive value and saved_path, as it was"
    assert not (root / "vault.json").exists() and not (root / "vault-migration.json").exists()
    assert summary["files"] == 6, "the six items of the file categories (the two key files are handled separately)"


def test_disable_names_colliding_files_with_a_counter_and_never_overwrites(env):
    result = run_enable(env)
    key = bytes(result.storage_key)
    (Path(env.paths.files) / "empty.bin").write_bytes(b"a file that appeared meanwhile")      # same name as a restored one
    migration.disable(env, key, make_backup_copy=False)
    assert (Path(env.paths.files) / "empty.bin").read_bytes() == b"a file that appeared meanwhile"
    assert (Path(env.paths.files) / "empty (2).bin").read_bytes() == b""


def test_disable_refuses_without_a_vault_and_while_an_enable_is_unfinished(env):
    with pytest.raises(migration.MigrationError) as info:
        migration.disable(env, os.urandom(32), make_backup_copy=False)
    assert info.value.code == "vault_not_enabled"
    result = run_enable(env)
    migration._write_journal(env, {"v": 1, "op": "enable", "phase": "files"})
    with pytest.raises(migration.MigrationError) as info:
        migration.disable(env, bytes(result.storage_key), make_backup_copy=False)
    assert info.value.code == "migration_pending"


def test_enable_refuses_an_existing_vault(env):
    run_enable(env)
    with pytest.raises(migration.MigrationError) as info:
        run_enable(env)
    assert info.value.code == "vault_already_enabled"


# ---- crash injection: every step, both directions -------------------------------------------------------------------

class Boom(Exception):
    pass


def crash_at(env, number):
    counter = {"n": 0}

    def hook(step):
        counter["n"] += 1
        if counter["n"] == number:
            raise Boom(step)
    env.after_step = hook
    return counter


def count_steps(tmp_path, op):
    env = build_workspace(tmp_path)
    steps = []
    env.after_step = steps.append
    result = run_enable(env)
    if op == "disable":
        steps.clear()
        migration.disable(env, bytes(result.storage_key), make_backup_copy=False)
    env.conn.close()
    return len(steps)


def originals_are_safe(env, key):
    """The invariant that must hold after ANY crash: for every original file, either the plain file is still there, or an
    encrypted copy exists that decrypts to the original (nothing was deleted without a verified copy)."""
    files_key, keys_key = subkey(key, "files"), subkey(key, "keys")
    p = env.paths
    for rid, directory in (("recv1", p.cache_incoming), ("sent1", p.spool_outgoing)):
        plain, enc = directory / rid, directory / (rid + ".mcv")
        assert plain.exists() or (enc.exists() and streamcrypt.decrypt_bytes(enc.read_bytes(), files_key, rid) == CONTENT[rid])
    for rid in ("saved1", "saved2"):
        enc = p.files / (rid + ".mcv")
        name = SECRET_NAME if rid == "saved1" else "empty.bin"
        assert (p.files / name).exists() or (enc.exists() and streamcrypt.decrypt_bytes(enc.read_bytes(), files_key, rid) == CONTENT[rid])
    assert (p.keys / "identity.seed").exists() or streamcrypt.decrypt_bytes((p.keys / "identity.seed.mcv").read_bytes(), keys_key, "keys/identity.seed") == SEED_BYTES


def test_a_crash_after_any_step_of_enable_resumes_to_the_same_end_state(tmp_path):
    total = count_steps(tmp_path / "count", "enable")
    assert total >= 20
    for number in range(1, total + 1):
        env = build_workspace(tmp_path / ("e%d" % number))
        original_db = sensitive_snapshot(env.conn)
        crash_at(env, number)
        try:
            result = run_enable(env)
            sk = bytes(result.storage_key)                  # the crash point was past the last step: finished normally
        except Boom:
            parsed = header.VaultHeader.from_json((Path(env.paths.root) / "vault.json").read_text(encoding="utf-8")) \
                if (Path(env.paths.root) / "vault.json").exists() else None
            if parsed is None:                               # crashed before the header: nothing was touched, just start again
                assert tree(Path(env.paths.cache_incoming)).keys() == {"recv1"}
                env.after_step = lambda step: None
                run_enable(env)
                env.conn.close()
                continue
            sk = bytes(header.unlock(parsed, PASS))
            originals_are_safe(env, sk)
            env.after_step = lambda step: None
            migration.resume_enable(env, sk)
        proof = migration.verify_enabled(env, sk)
        assert proof == {"plain_files": 0, "plain_columns": 0}
        assert not (Path(env.paths.root) / "vault-migration.json").exists()
        field_key = subkey(sk, "fields")
        assert fields.decrypt_field(field_key, "attachments", "file_name", "saved1", env.conn.execute(
            "SELECT file_name FROM attachments WHERE id='saved1'").fetchone()[0]) == "s" + SECRET_NAME
        env.conn.close()


def test_a_crash_after_any_step_of_disable_resumes_to_the_original(tmp_path):
    total = count_steps(tmp_path / "count", "disable")
    assert total >= 15
    for number in range(1, total + 1):
        env = build_workspace(tmp_path / ("d%d" % number))
        original_tree, original_db = tree(Path(env.paths.root)), sensitive_snapshot(env.conn)
        result = run_enable(env)
        sk = bytes(result.storage_key)
        crash_at(env, number)
        try:
            migration.disable(env, sk, make_backup_copy=False)
        except Boom:
            env.after_step = lambda step: None
            if (Path(env.paths.root) / "vault.json").exists() or migration.read_journal(env):
                migration.resume_disable(env, sk)
        assert tree(Path(env.paths.root)) == original_tree, number
        assert sensitive_snapshot(env.conn) == original_db, number
        env.conn.close()


def test_leftover_part_files_from_a_crash_are_cleaned_and_never_mistaken_for_content(env):
    (Path(env.paths.cache_incoming) / "stale.part").write_bytes(b"half written")
    run_enable(env)
    assert not (Path(env.paths.cache_incoming) / "stale.part").exists()


# ---- refusals -------------------------------------------------------------------------------------------------------

def test_enable_refuses_while_transfers_are_in_flight_and_touches_nothing(env):
    env.conn.execute("UPDATE attachments SET state = 'UPLOADING' WHERE id = 'sent1'")
    env.conn.commit()
    before = tree(Path(env.paths.root))
    with pytest.raises(migration.MigrationError) as info:
        run_enable(env)
    assert info.value.code == "transfers_in_progress" and info.value.detail["count"] == 1
    assert tree(Path(env.paths.root)) == before and not list(Path(env.data_dir).glob("mca-vault-backup-*"))


def test_enable_refuses_without_free_space_and_touches_nothing(env):
    env.free_space = lambda path: 1000
    before = tree(Path(env.paths.root))
    with pytest.raises(migration.MigrationError) as info:
        run_enable(env)
    assert info.value.code == "insufficient_space" and info.value.detail["free_bytes"] == 1000 and info.value.detail["needed_bytes"] > 1000
    assert tree(Path(env.paths.root)) == before


def test_a_file_that_fails_verification_is_never_deleted(env, monkeypatch):
    monkeypatch.setattr(streamcrypt, "verify_file", lambda *a, **k: False)
    with pytest.raises(migration.MigrationError) as info:
        run_enable(env)
    assert info.value.code == "verify_failed"
    p = env.paths
    for plain, content in ((p.cache_incoming / "recv1", CONTENT["recv1"]), (p.spool_outgoing / "sent1", CONTENT["sent1"]),
                           (p.files / SECRET_NAME, CONTENT["saved1"]), (p.keys / "identity.seed", SEED_BYTES)):
        assert plain.read_bytes() == content, "every plain file is intact"
    assert [f.name for f in Path(p.root).rglob("*.mcv")] == [migration.ORPHAN_INDEX], "no unverified copy is left (the encrypted name index is written first, on purpose)"


def test_a_decrypted_file_that_does_not_match_its_recorded_checksum_is_not_swapped_in(env):
    result = run_enable(env)
    env.conn.execute("UPDATE attachments SET plain_sha256 = ? WHERE id = 'recv1'", ("0" * 64,))
    # (plain_sha256 is not a vault column: it stays plain, so the check on disable has a recorded value to compare)
    env.conn.commit()
    with pytest.raises(migration.MigrationError) as info:
        migration.disable(env, bytes(result.storage_key), make_backup_copy=False)
    assert info.value.code == "verify_failed"
    assert (Path(env.paths.cache_incoming) / "recv1.mcv").exists(), "the encrypted copy is kept"
    assert not (Path(env.paths.cache_incoming) / "recv1").exists() and not list(Path(env.paths.cache_incoming).glob("*.part"))


# ---- the plain backup -----------------------------------------------------------------------------------------------

def test_the_backup_is_a_full_plain_copy_with_a_consistent_database_snapshot(env):
    result = run_enable(env, make_backup_copy=True)
    backup = result.backup
    assert backup and backup.parent == Path(env.data_dir) and backup.name.startswith("mca-vault-backup-")
    if os.name == "posix":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    copied = tree(backup / "workspace")
    assert copied["files/" + SECRET_NAME] == CONTENT["saved1"] and copied["keys/identity.seed"] == SEED_BYTES
    assert "vault.json" not in copied and "vault-migration.json" not in copied
    snapshot = sqlite3.connect(str(backup / "attachments.db"))
    assert snapshot.execute("SELECT file_name FROM attachments WHERE id = 'saved1'").fetchone()[0] == SECRET_NAME, "the plain database"
    snapshot.close()
    assert [b["name"] for b in migration.list_backups(env.data_dir)] == [backup.name]


def test_no_backup_when_the_caller_has_its_own(env):
    result = run_enable(env, make_backup_copy=False)
    assert result.backup is None and migration.list_backups(env.data_dir) == []


def test_deleting_a_backup_only_accepts_a_real_backup_folder_name(env):
    result = run_enable(env, make_backup_copy=True)
    for bad in ("../data", "mca", "mca-vault-backup-../x", "", ".", "..", "mca-vault-backup-x/../../y"):
        with pytest.raises(migration.MigrationError):
            migration.delete_backup(env.data_dir, bad)
    assert Path(env.paths.root).exists()
    assert migration.delete_backup(env.data_dir, result.backup.name) is True
    assert migration.list_backups(env.data_dir) == [] and migration.delete_backup(env.data_dir, result.backup.name) is False


def test_swap_information_is_part_of_the_preflight(env):
    env.swap_inspector = lambda: {"disk_swap": True, "known": True}
    assert migration.preflight(env, backup=False).swap == {"disk_swap": True, "known": True}
