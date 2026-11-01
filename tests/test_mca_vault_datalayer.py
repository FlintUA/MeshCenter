"""MCA-VAULT PR 4a: the data layer. Every read and write of an encrypted column, of the identity seed and of the provider
upload tokens goes through the codec; with the vault on nothing sensitive reaches the database file or `keys/` in plain."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from meshsrv.attachments import identity, sender
from meshsrv.attachments.db import migrations
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.attachments.relay_client import RelayClient
from meshsrv.attachments.vault import codec as codec_mod, fields, header, kdf, migration, passphrase
from meshsrv.attachments.vault.codec import PLAIN, VaultCodec, VaultConnection, codec_of
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.runtime import VaultRuntime
from meshsrv.attachments.workspace import MCAWorkspaceManager

PASS = "correct horse battery staple 42"
FLOOR = kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT)
NAME = "quarterly-secret-report.txt"
COMMENT = "a private comment nobody else may read"
BASE_URL = "https://mock-relay.test"


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class _Shim:
    def __init__(self, r):
        self.status_code, self.headers, self.content = r.status_code, r.headers, r.data
        self.text = r.get_data(as_text=True)
        self._r = r

    def json(self):
        return self._r.get_json()


class _Session:
    def __init__(self, client):
        self._client = client

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        return _Shim(self._client.open(url[len(BASE_URL):], method=method, json=json, data=data, headers=headers or {}))


@pytest.fixture
def world(tmp_path):
    """A workspace with a principal, a VaultConnection and a vault header; the codec is wired but the vault is unlocked
    only when a test says so."""
    conn = sqlite3.connect(str(tmp_path / "attachments.db"), factory=VaultConnection, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.migrate(conn)
    wsm = MCAWorkspaceManager(str(tmp_path / "data"))
    principal = identity.ensure_principal(conn, wsm, "local")
    root = wsm.paths(principal.principal_id).root
    created = header.create(passphrase.normalize(PASS), FLOOR)
    (root / "vault.json").write_text(created.header.to_json(), encoding="utf-8")
    logs = []
    runtime = VaultRuntime(root, log=lambda *a: logs.append(a), clock=_Clock(), free_ram=lambda: 4 * 1024 ** 3)
    conn.codec = wsm.codec = VaultCodec(runtime)

    class W:
        pass
    w = W()
    w.conn, w.wsm, w.principal, w.root, w.vault, w.tmp, w.logs = conn, wsm, principal, root, runtime, tmp_path, logs
    yield w
    conn.close()


def unlock(w):
    w.vault.unlock(passphrase=PASS)


def raw_db(w) -> bytes:
    w.conn.commit()
    return (w.tmp / "attachments.db").read_bytes() + b"".join(p.read_bytes() for p in w.tmp.glob("attachments.db-*"))


# ---- the codec itself -----------------------------------------------------------------------------------------------

def test_plain_codec_is_a_pass_through_and_the_default():
    assert PLAIN.seal("t", "c", "1", "x") == "x" and PLAIN.open("t", "c", "1", "v1:whatever") == "v1:whatever"
    assert codec_of(sqlite3.connect(":memory:")) is PLAIN, "a connection that was never given a vault"


def test_with_no_vault_header_the_vault_codec_is_plain_too(tmp_path):
    runtime = VaultRuntime(tmp_path, log=lambda *a: None)
    c = VaultCodec(runtime)
    assert c.active is False and c.seal("attachments", "file_name", "1", NAME) == NAME and c.open("attachments", "file_name", "1", NAME) == NAME


def test_seal_and_open_round_trip_text_and_bytes(world):
    unlock(world)
    c = world.conn.codec
    sealed = c.seal("attachments", "file_name", "r1", NAME)
    assert sealed.startswith("v1:") and NAME not in sealed
    assert c.open("attachments", "file_name", "r1", sealed) == NAME
    blob = c.seal("mca_sender_state", "data_key", "r1", b"\x00\x01\xff" * 8)
    assert c.open("mca_sender_state", "data_key", "r1", blob) == b"\x00\x01\xff" * 8
    assert c.seal("attachments", "draft_comment", "r1", None) is None and c.open("attachments", "draft_comment", "r1", None) is None


def test_a_sealed_value_is_bound_to_its_table_column_and_row(world):
    unlock(world)
    c = world.conn.codec
    sealed = c.seal("attachments", "file_name", "r1", NAME)
    for table, column, row in (("attachments", "file_name", "r2"), ("attachments", "draft_comment", "r1"), ("mca_sender_state", "file_name", "r1")):
        with pytest.raises(VaultError) as info:
            c.open(table, column, row, sealed)
        assert info.value.code == "vault_corrupt", "copied to another place it must fail, not pass through"


def test_locked_a_secret_raises_a_display_value_is_none_and_nothing_is_written(world):
    unlock(world)
    sealed = world.conn.codec.seal("attachments", "file_name", "r1", NAME)
    world.vault.lock()
    c = world.conn.codec
    assert c.open_display("attachments", "file_name", "r1", sealed) is None
    with pytest.raises(VaultError) as info:
        c.open("mca_sender_state", "revoke_token", "r1", sealed)
    assert info.value.code == "vault_locked"
    with pytest.raises(VaultError):
        c.seal("attachments", "file_name", "r1", NAME)
    assert c.open("attachments", "file_name", "r1", "a plain value") == "a plain value"


def test_a_tampered_field_raises_instead_of_passing_the_ciphertext_through(world):
    unlock(world)
    c = world.conn.codec
    sealed = c.seal("attachments", "file_name", "r1", NAME)
    head, nonce, ct = sealed.split(":")
    raw = bytearray(fields._unb64(ct))
    raw[0] ^= 0x01                                                        # one flipped bit
    tampered = "v1:%s:%s" % (nonce, fields._b64(bytes(raw)))
    for bad in (tampered, "v1:not-really-ours", "v1:", sealed[:-4]):
        with pytest.raises(VaultError) as info:
            c.open("attachments", "file_name", "r1", bad)
        assert info.value.code == "vault_corrupt" and "v1:" not in info.value.message
    assert c.open("attachments", "file_name", "r1", sealed) == NAME, "the untouched value still opens"


def test_a_corrupt_display_value_reads_as_unreadable_never_as_the_raw_string(world):
    unlock(world)
    c = world.conn.codec
    sealed = c.seal("attachments", "file_name", "r1", NAME)
    assert c.open_display("attachments", "file_name", "r2", sealed) == codec_mod.UNREADABLE_NAME
    assert c.open_display("attachments", "file_name", "r1", "v1:garbage") == codec_mod.UNREADABLE_NAME
    assert c.open_display("attachments", "file_name", "r1", sealed) == NAME


def test_a_corrupt_value_is_logged_once_with_where_and_never_the_value(world):
    unlock(world)
    c = world.conn.codec
    sealed = c.seal("attachments", "file_name", "r1", NAME)
    for _ in range(5):
        c.open_display("attachments", "file_name", "r2", sealed)
    lines = [l for l in world.logs if l[1] == "MCAttach stored value unreadable"]
    assert len(lines) == 1, "one line per value, not one per read"
    level, title, details = lines[0]
    assert level == "ERROR" and details == "attachments.file_name of r2"
    assert sealed not in repr(world.logs) and NAME not in repr(world.logs)


def test_the_snapshot_shows_a_corrupt_name_as_unreadable(world, tmp_path):
    unlock(world)
    world.conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, file_name, mime_type, plain_size, "
        "plain_sha256, saved_path, draft_comment, created_at, hard_expires_at, download_grace_seconds) "
        "VALUES (?, 'local', ?, 'received', ?, 'AVAILABLE', 'v1:tampered-by-someone', 'text/plain', 1, NULL, NULL, NULL, 1, 2, 3600)",
        ("c3" * 16, "00" * 16, world.principal.principal_id))
    world.conn.commit()
    from meshsrv.attachments import snapshots
    snap = snapshots.build_attachments_snapshot(world.conn, workspace_id="local", workspace_manager=world.wsm,
                                                principal_id=world.principal.principal_id, now=2000.0)
    assert [r.file_name for r in snap.records] == [codec_mod.UNREADABLE_NAME]
    assert "tampered-by-someone" not in repr(snap.records)


def test_the_service_and_the_migration_share_one_encoding(world):
    unlock(world)
    key = world.vault.subkey("fields")
    sealed = world.conn.codec.seal("mca_sender_state", "upload_token", "r1", "tok-123")
    assert migration._is_ours(key, "mca_sender_state", "upload_token", "r1", sealed)
    assert migration._from_text(fields.decrypt_field(key, "mca_sender_state", "upload_token", "r1", sealed)) == "tok-123"
    assert migration._to_text is codec_mod.to_text and migration._from_text is codec_mod.from_text


# ---- secret files: the identity seed and the provider tokens ----------------------------------------------------------

def test_secret_files_are_stored_encrypted_and_never_leave_a_plain_twin(world):
    unlock(world)
    path = world.wsm.paths(world.principal.principal_id).keys / "some.secret"
    world.wsm.codec.write_secret_file(path, b"super secret token")
    assert (path.with_name("some.secret.mcv")).exists() and not path.exists()
    assert b"super secret token" not in path.with_name("some.secret.mcv").read_bytes()
    assert world.wsm.codec.read_secret_file(path) == b"super secret token" and world.wsm.codec.secret_file_exists(path)
    with pytest.raises(FileExistsError):
        world.wsm.codec.write_secret_file(path, b"again", exclusive=True)
    world.wsm.codec.unlink_secret_file(path)
    assert not world.wsm.codec.secret_file_exists(path)


def test_a_secret_file_cannot_be_read_while_locked_and_a_plain_legacy_file_is_still_read(world):
    unlock(world)
    keys = world.wsm.paths(world.principal.principal_id).keys
    world.wsm.codec.write_secret_file(keys / "a.secret", b"aaa")
    world.vault.lock()
    with pytest.raises(VaultError) as info:
        world.wsm.codec.read_secret_file(keys / "a.secret")
    assert info.value.code == "vault_locked"
    with pytest.raises(VaultError):
        world.wsm.codec.write_secret_file(keys / "b.secret", b"bbb")
    assert not (keys / "b.secret").exists() and not (keys / "b.secret.mcv").exists(), "nothing is written in plain form either"


def test_a_plain_secret_file_without_its_encrypted_twin_is_an_error_when_the_vault_is_enabled(world):
    keys = world.wsm.paths(world.principal.principal_id).keys
    (keys / "legacy.secret").write_bytes(b"legacy")
    unlock(world)                                                        # the vault is enabled (header present)
    with pytest.raises(VaultError) as info:
        world.wsm.codec.read_secret_file(keys / "legacy.secret")
    assert info.value.code == "vault_corrupt" and "legacy" not in info.value.message
    assert [l for l in world.logs if l[1] == "MCAttach stored value unreadable"][0][2] == "keys/legacy.secret (plain file found)"
    world.vault.lock()
    with pytest.raises(VaultError):                                       # locked or not, it is never silently served
        world.wsm.codec.read_secret_file(keys / "legacy.secret")


def test_a_tampered_encrypted_secret_file_is_an_error(world):
    unlock(world)
    keys = world.wsm.paths(world.principal.principal_id).keys
    world.wsm.codec.write_secret_file(keys / "a.secret", b"aaa")
    target = keys / "a.secret.mcv"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0x01
    target.write_bytes(bytes(data))
    with pytest.raises(VaultError) as info:
        world.wsm.codec.read_secret_file(keys / "a.secret")
    assert info.value.code == "vault_corrupt"


def test_without_a_vault_a_plain_secret_file_is_the_normal_case(tmp_path):
    c = VaultCodec(VaultRuntime(tmp_path, log=lambda *a: None))          # no vault.json: disabled
    (tmp_path / "x.secret").write_bytes(b"plain")
    assert c.read_secret_file(tmp_path / "x.secret") == b"plain"


def test_the_identity_seed_is_read_through_the_vault(world):
    seed_path = world.wsm.paths(world.principal.principal_id).keys / world.principal.private_key_file
    seed = seed_path.read_bytes()
    unlock(world)
    world.wsm.codec.write_secret_file(seed_path, seed)                    # what the migration did: <name>.mcv, plain gone
    assert not seed_path.exists()
    assert bytes(identity.load_signing_key(world.wsm, world.principal)) == seed
    assert seed not in seed_path.with_name(seed_path.name + ".mcv").read_bytes()
    world.vault.lock()
    with pytest.raises(VaultError):
        identity.load_signing_key(world.wsm, world.principal)


def test_a_new_identity_is_written_encrypted_when_the_vault_is_already_on(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "a.db"), factory=VaultConnection)
    migrations.migrate(conn)
    wsm = MCAWorkspaceManager(str(tmp_path / "data"))

    class _Always:                                    # a vault that is enabled and unlocked from the start
        def is_enabled(self):
            return True

        def subkey(self, purpose):
            return bytes(range(32))
    wsm.codec = conn.codec = VaultCodec(_Always())
    principal = identity.ensure_principal(conn, wsm, "local")
    keys = wsm.paths(principal.principal_id).keys
    assert not (keys / principal.private_key_file).exists() and (keys / (principal.private_key_file + ".mcv")).exists()
    assert identity.load_signing_key(wsm, principal) is not None


def test_provider_upload_tokens_live_encrypted(world):
    registry = ProviderRegistry(world.conn, "local")
    profile = registry.register(display_name="Relay", base_url="https://real.example.net", service_public_key=b"\xab" * 32,
                                max_ciphertext_bytes=6 * 1024 * 1024)
    unlock(world)
    registry.set_upload_token(profile.provider_id, world.wsm, world.principal.principal_id, "the-upload-token-value")
    keys = world.wsm.paths(world.principal.principal_id).keys
    assert [p.name for p in keys.glob("relay_upload_token_*")] == [("relay_upload_token_%s.secret.mcv" % profile.provider_id)]
    assert b"the-upload-token-value" not in b"".join(p.read_bytes() for p in keys.iterdir())
    assert registry.get_upload_token(profile.provider_id, world.wsm, world.principal.principal_id) == "the-upload-token-value"
    world.vault.lock()
    with pytest.raises(VaultError):
        registry.get_upload_token(profile.provider_id, world.wsm, world.principal.principal_id)
    unlock(world)
    registry.clear_upload_token(profile.provider_id, world.wsm, world.principal.principal_id)
    assert list(keys.glob("relay_upload_token_*")) == [] and registry.get_upload_token(profile.provider_id, world.wsm, world.principal.principal_id) is None


# ---- the sender: names, comments, keys and tokens never reach the database file in plain --------------------------------

def test_a_sent_attachment_never_puts_a_name_comment_key_or_token_into_the_database_in_plain(world, tmp_path):
    unlock(world)
    store = MockRelayStore(base_url=BASE_URL)
    relay = RelayClient(BASE_URL, upload_access_token=store.upload_access_token,
                        session=_Session(create_mock_relay_app(store).test_client()), sleep=lambda _s: None)
    recipient_key = SigningKey.generate()
    pub = bytes(recipient_key.verify_key)
    key_id = identity.compute_key_id(pub)
    staged_id = "ab" * 16                                   # the service stages the source under the attachment id, never the name
    spool = world.wsm.paths(world.principal.principal_id).spool_outgoing
    source = world.wsm.codec.write_content(spool / staged_id, b"hello " * 500, staged_id)    # encrypted: this is a vault workspace
    assert source.name == staged_id + ".mcv"
    attachment_id = sender.create_draft(
        world.conn, world.wsm, world.principal, workspace_id="local", attachment_id=staged_id, source_path=str(source), file_name=NAME, mime_type="text/plain",
        recipients=[sender.RecipientTarget(public_identity=pub, key_id=key_id)], adapter_id="fake-text", connector_profile_id="default",
        route_type="DIRECT", route_id="receiver", provider_id=b"\x01" * 8, comment=COMMENT)
    stored = world.conn.execute("SELECT file_name, draft_comment FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert stored[0].startswith("v1:") and stored[1].startswith("v1:")
    assert world.conn.codec.open("attachments", "file_name", attachment_id, stored[0]) == NAME

    identities = {key_id: pub}
    seen = set()
    for _ in range(12):
        state = sender.get_state(world.conn, attachment_id)
        seen.add(state)
        if state in (sender.READY_TO_SEND, sender.SENT, sender.FAILED_VALIDATION):
            break
        sender.run_step(world.conn, workspace_manager=world.wsm, principal=world.principal, recipient_identities=identities,
                        relay_client=relay, delivery_adapter=_NoDelivery(), attachment_id=attachment_id)
    assert sender.get_state(world.conn, attachment_id) in (sender.READY_TO_SEND, sender.SENT), seen
    state_row = sender._get_sender_state(world.conn, attachment_id)
    secrets = [state_row["data_key"], state_row["nonce_prefix"]]
    revoke = world.conn.execute("SELECT revoke_token FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)).fetchone()
    assert revoke is not None and revoke[0].startswith("v1:")
    secrets.append(world.conn.codec.open("mca_sender_revoke_state", "revoke_token", attachment_id, revoke[0]))
    if state_row["upload_token"]:
        secrets.append(state_row["upload_token"])
    assert all(isinstance(s, str) and len(s) >= 8 for s in secrets)
    data = raw_db(world)
    for needle in [NAME, COMMENT] + secrets:
        assert needle.encode("utf-8") not in data, "a plain value reached the database file: " + _where(world, needle)
    raw_state = world.conn.execute("SELECT data_key, nonce_prefix, upload_token, revoke_token FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone()
    assert all(v is None or v.startswith("v1:") for v in raw_state)
    from meshsrv.attachments import snapshots
    snap = snapshots.build_attachments_snapshot(world.conn, workspace_id="local", workspace_manager=world.wsm,
                                                principal_id=world.principal.principal_id, now=2000.0)
    assert [r.file_name for r in snap.records] == [NAME]
    world.vault.lock()
    locked = snapshots.build_attachments_snapshot(world.conn, workspace_id="local", workspace_manager=world.wsm,
                                                  principal_id=world.principal.principal_id, now=2001.0)
    assert [r.file_name for r in locked.records] == [None], "locked: a placeholder, never the ciphertext"


class _NoDelivery:
    """The control-message delivery is not part of this test (it needs no name or key)."""

    def __getattr__(self, name):
        def _call(*a, **k):
            raise AssertionError("delivery should not be needed up to READY_TO_SEND: " + name)
        return _call


def _where(world, needle) -> str:
    """Which table and column holds a plain value (for a readable failure)."""
    found = []
    for (table,) in world.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for column in [r[1] for r in world.conn.execute("PRAGMA table_info(%s)" % table)]:
            for (value,) in world.conn.execute("SELECT %s FROM %s" % (column, table)):
                if (isinstance(value, str) and needle in value) or (isinstance(value, bytes) and needle.encode() in value):
                    found.append("%s.%s" % (table, column))
    return ", ".join(sorted(set(found))) or "(only in free pages or the journal)"


def test_only_the_known_modules_touch_the_encrypted_columns_and_each_uses_the_codec():
    """A new module that reads or writes the key and token tables, or the file name of an attachment, fails here, and so
    does a known one that stops going through the codec: "is this value sealed?" is asked in review, not found in a leak."""
    root = Path(__file__).resolve().parent.parent / "meshsrv" / "attachments"
    skipped = ("vault/", "db/migrations.py", "relay/mock_server.py")
    tables = ("mca_sender_state", "mca_receiver_state", "mca_sender_revoke_state")
    touching, using_codec = [], []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(skipped):
            continue
        text = path.read_text(encoding="utf-8")
        if any(t in text for t in tables) or 'row["file_name"]' in text or '"file_name": record.file_name' in text:
            touching.append(relative)
        if "codec_of" in text:
            using_codec.append(relative)
    # storage_merge.py (FX1 Phase B) reads `file_name` raw only on its plain-file branch (no vault: the value is plain); with
    # the vault on it moves `<id>.mcv` by the attachment id and never looks at a name
    assert set(touching) <= {"receiver.py", "sender.py", "service.py", "snapshots.py", "storage_merge.py"}, touching
    assert {"receiver.py", "sender.py", "service.py", "snapshots.py"} <= set(using_codec)


# ---- MCA-VAULT PR 5 (the real round on .104): the list shows the names again after an unlock ---------------------------------

def _insert_named_attachment(world, attachment_id="d4" * 16):
    sealed = world.conn.codec.seal("attachments", "file_name", attachment_id, NAME)
    world.conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, file_name, mime_type, plain_size, "
        "plain_sha256, saved_path, draft_comment, created_at, hard_expires_at, download_grace_seconds) "
        "VALUES (?, 'local', ?, 'received', ?, 'AVAILABLE', ?, 'text/plain', 1, NULL, NULL, NULL, 1, 2, 3600)",
        (attachment_id, "00" * 16, world.principal.principal_id, sealed))
    world.conn.commit()
    return attachment_id


def _publish(world, publisher):
    snap = publisher.refresh(world.conn, workspace_id="local", workspace_manager=world.wsm, principal_id=world.principal.principal_id)
    return [r.file_name for r in snap.records]


def test_a_snapshot_built_while_locked_gets_its_names_back_only_through_a_rebuild(world):
    """The incremental refresh only touches rows that changed, so after a restart (snapshot built locked) and an unlock the
    list kept the placeholders until each row changed. request_rebuild() is what the service asks for after the unlock."""
    from meshsrv.attachments.snapshots import AttachmentsSnapshotPublisher
    unlock(world)
    _insert_named_attachment(world)
    world.vault.lock()
    publisher = AttachmentsSnapshotPublisher()
    assert _publish(world, publisher) == [None]
    unlock(world)
    assert _publish(world, publisher) == [None], "nothing is dirty: the incremental refresh keeps the withheld name"
    publisher.request_rebuild()
    assert _publish(world, publisher) == [NAME]


def test_the_service_rebuilds_the_snapshot_once_after_an_unlock():
    from meshsrv.attachments import service as service_mod

    class _Vault:
        locked = True

        def is_locked(self):
            return self.locked

    class _Publisher:
        def __init__(self):
            self.calls = []

        def request_rebuild(self):
            self.calls.append("rebuild")

        def refresh(self, *a, **k):
            self.calls.append("refresh")

    svc = object.__new__(service_mod.AttachmentsService)
    svc._vault, svc._snapshot_publisher = _Vault(), _Publisher()
    svc._conn = svc._workspace_manager = None
    svc._principal = type("P", (), {"workspace_id": "w", "principal_id": "p"})()
    svc._refresh_snapshot()
    svc._refresh_snapshot()
    assert svc._snapshot_publisher.calls == ["refresh", "refresh"], "locked: nothing to rebuild yet"
    svc._vault.locked = False
    svc._refresh_snapshot()
    svc._refresh_snapshot()
    assert svc._snapshot_publisher.calls == ["refresh", "refresh", "rebuild", "refresh", "refresh"], "one rebuild, then back to the incremental refresh"
    svc._vault.locked = True
    svc._refresh_snapshot()
    svc._vault.locked = False
    svc._refresh_snapshot()
    assert svc._snapshot_publisher.calls[-3:] == ["refresh", "rebuild", "refresh"], "and again after the next lock and unlock"
