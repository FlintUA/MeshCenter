"""MCA-VAULT PR 2 (ADR-0013): the vault wired into the runtime, the updater, the schedule action, and the leak contract."""
import json
import logging
import os
import time
from functools import wraps

import pytest
from flask import Flask

from meshsrv import schedule_picture, update_service
from meshsrv.attachments import mca_runtime
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.vault import header, kdf, passphrase, runtime

PASS = "correct horse battery staple"
SECRET_NAME = "secret-report-2026.pdf"
SECRET_COMMENT = "private comment about the secret"


def make_vault_files(directory):
    created = header.create(passphrase.normalize(PASS), kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT))
    (directory / "vault.json").write_text(created.header.to_json(), encoding="utf-8")
    return created


class _Session:
    def request(self, *args, **kwargs):
        import requests
        raise requests.ConnectionError("down for this test")


def started_state(tmp_path, tag, *, before_start=None):
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_Session())
    data_dir = str(tmp_path / tag)
    state = mca_runtime._get_state(data_dir)  # noqa: SLF001
    if before_start:
        before_start(state)
    mca_runtime.start_attachments_service(data_dir, FakeRadioTransport(InMemoryEther(), "!aaaaaaaa"), control_channel_index=0)
    return state


# ---- runtime wiring -----------------------------------------------------------------------------------------------

def test_the_runtime_owns_a_vault_whose_unlock_is_serialized_with_the_worker_tick(tmp_path):
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_Session())
    try:
        state = mca_runtime._get_state(str(tmp_path / "w"))  # noqa: SLF001
        assert isinstance(state.vault, runtime.VaultRuntime) and state.vault._serialize_with is state.tick_lock
        assert mca_runtime.get_vault() is state.vault
        assert state.vault.state() == "disabled", "no vault.json: nothing changes for an existing node"
    finally:
        mca_runtime.reset_state_for_tests()


def test_get_vault_is_none_before_the_runtime_exists():
    mca_runtime.reset_state_for_tests()
    assert mca_runtime.get_vault() is None


def test_the_service_receives_the_vault_and_locked_state_follows_the_files(tmp_path):
    try:
        state = started_state(tmp_path, "s", before_start=lambda st: make_vault_files(st.vault._dir))
        assert state.service is not None and state.service._vault is state.vault
        assert state.vault.is_locked() is True and state.service._vault_locked() is True
        state.service.stop()
    finally:
        mca_runtime.reset_state_for_tests()


def test_a_vault_header_this_build_cannot_read_keeps_mcattach_down_and_says_why(tmp_path):
    lines = []
    mca_runtime.set_vault_log(lambda level, title, details: lines.append((level, title, details)))
    try:
        state = started_state(tmp_path, "bad", before_start=lambda st: (st.vault._dir / "vault.json").write_text('{"v": 99}'))
        assert state.service is None and mca_runtime.get_attachments_facade() is not None
        assert state.facade is not None
        assert lines == [("ERROR", "MCAttach not started", "The vault header cannot be read by this version (unsupported_header_version)")]
    finally:
        mca_runtime.set_vault_log(None)
        mca_runtime.reset_state_for_tests()


def test_vault_log_lines_reach_the_system_log_sink_and_carry_no_secret(tmp_path):
    lines = []
    mca_runtime.set_vault_log(lambda level, title, details: lines.append((level, title, details)))
    try:
        state = started_state(tmp_path, "log", before_start=lambda st: make_vault_files(st.vault._dir))
        state.service.stop()
        state.vault._free_ram = lambda: 4 * 1024 ** 3
        with pytest.raises(Exception):
            state.vault.unlock(passphrase="a wrong passphrase 1")
        state.vault._next_attempt_at = 0
        state.vault.unlock(passphrase=PASS)
        state.vault.lock()
        text = json.dumps(lines)
        assert "unlock failed" in text and "vault unlocked" in text and "vault locked" in text
        assert PASS not in text and "wrong passphrase" not in text
    finally:
        mca_runtime.set_vault_log(None)
        mca_runtime.reset_state_for_tests()


# ---- updater: Roll back refuses while a vault exists --------------------------------------------------------------

def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


@pytest.fixture
def updater(tmp_path):
    from api.api_updates import register_updates_routes
    update_service.configure(str(tmp_path / "update_check.json"))
    update_service.configure_vault_dir(str(tmp_path))
    app = Flask(__name__)
    events = []
    register_updates_routes(app, lambda: "1.2.0", str(tmp_path), _handle_errors,
                            log_event=lambda title, level="INFO", details="", source="system": events.append((title, level, details)))
    yield app.test_client(), events, tmp_path
    update_service.configure_vault_dir("")


def test_vault_enabled_is_decided_by_file_presence_in_any_workspace(updater):
    _client, _events, tmp_path = updater
    assert update_service.vault_enabled() is False
    (tmp_path / "mca" / "p1").mkdir(parents=True)
    assert update_service.vault_enabled() is False, "a workspace without a vault"
    (tmp_path / "mca" / "p1" / "vault.json").write_text("{}")
    assert update_service.vault_enabled() is True
    (tmp_path / "mca" / "p1" / "vault.json").unlink()
    (tmp_path / "mca" / "p1" / "vault-migration.json").write_text("{}")
    assert update_service.vault_enabled() is True, "a half-finished migration counts"


def test_rollback_is_refused_with_disable_the_vault_first_even_when_it_would_otherwise_be_allowed(updater, monkeypatch):
    client, events, tmp_path = updater
    (tmp_path / "mca" / "p1").mkdir(parents=True)
    (tmp_path / "mca" / "p1" / "vault.json").write_text("{}")
    monkeypatch.setattr(update_service, "rollback", lambda *a, **k: pytest.fail("a rollback must not even start"))
    resp = client.post("/api/updates/rollback", json={})
    body = resp.get_json()
    assert resp.status_code == 409 and body["error"] == "Disable the vault first" and body["error_code"] == "vault_enabled"
    assert body["preflight"]["reason"] == "vault_enabled"
    assert any(e[0] == "Rollback refused" for e in events)


def test_the_status_says_why_rollback_is_off(updater):
    client, _events, tmp_path = updater
    assert client.get("/api/updates/status").get_json()["rollback_blocked_reason"] is None
    (tmp_path / "mca" / "p1").mkdir(parents=True)
    (tmp_path / "mca" / "p1" / "vault.json").write_text("{}")
    assert client.get("/api/updates/status").get_json()["rollback_blocked_reason"] == "vault_enabled"


# ---- the schedule action ------------------------------------------------------------------------------------------

class _World:
    def __init__(self, locked):
        self.locked, self.now = locked, 1_000_000.0
        self.taken, self.sent, self.logs, self.notes = [], [], [], []

    def deps(self):
        return schedule_picture.PictureDeps(
            clock=lambda: self.now, radio_available=lambda: True, source_exists=lambda s: True, contact_trusted=lambda c: True,
            take_picture=lambda s: self.taken.append(s) or {"filename": "f.jpg", "display_name": "f.jpg"},
            prepare_copy=lambda f, k: b"jpeg", create_transfer=lambda c, n, d: self.sent.append(c),
            log=lambda *a: self.logs.append(a), notify=lambda *a: self.notes.append(a),
            vault_locked=lambda: self.locked,
        )


def test_a_locked_vault_skips_the_scheduled_picture_with_one_log_one_notification_and_takes_nothing():
    world = _World(locked=True)
    action = {"type": "send_camera_picture", "params": {"source_id": "cam1", "contact_id": "!b0f14d2a"}}
    assert schedule_picture.run(action, {"id": "r"}, world.deps()) == "vault_locked"
    assert world.taken == [] and world.sent == []
    assert len(world.logs) == 1 and world.logs[0][0] == "WARNING" and "vault_locked" in world.logs[0][2]
    assert len(world.notes) == 1 and "vault_locked" in world.notes[0][2]
    world.now += 60
    world.locked = False
    assert schedule_picture.run(action, {"id": "r"}, world.deps()) == "too_soon", "the skipped run counts as an attempt: nothing piles up"
    world.now += 15 * 60
    assert schedule_picture.run(action, {"id": "r"}, world.deps()) == "sent"


# ---- the leak contract: no file name or comment in the System Log, notifications or logger output ----------------

def test_no_file_name_or_comment_reaches_the_system_log_notifications_or_logger_while_a_vault_is_in_use(tmp_path, caplog, monkeypatch):
    """ADR-0013 Decision 12. Drives the flows this PR owns (lock, unlock, wrong unlock, refused unlock, postponed work,
    the scheduled-send skip, the refused rollback, the locked list) with a transfer whose name and comment are
    recognisable, and asserts neither string appears in anything that is logged or notified."""
    from meshsrv import notification_service
    from meshsrv.attachments.snapshots import serialize_attachment_public
    from meshsrv.attachments.snapshots import AttachmentRecord

    system_log, notified = [], []
    mca_runtime.set_vault_log(lambda level, title, details: system_log.append((level, title, details)))
    monkeypatch.setattr(notification_service, "push_notification", lambda **kw: notified.append(kw))
    caplog.set_level(logging.DEBUG)
    try:
        state = started_state(tmp_path, "leak", before_start=lambda st: make_vault_files(st.vault._dir))
        state.service.stop()
        record = AttachmentRecord(
            id="c" * 32, direction="received", state="WAITING_CONSENT", file_name=SECRET_NAME, mime_type="image/jpeg",
            plain_size=10, cipher_size=11, created_at=1.0, hard_expires_at=2.0, download_grace_seconds=3600, provider_id=None,
            saved=False, primary_delivery_id=None, error_code=None, counterparty_contact_id="!756f9960", recipients=(),
            deliveries=(), descriptor=None, timeline=())
        vault = state.vault
        vault._free_ram = lambda: 4 * 1024 ** 3
        shown_locked = serialize_attachment_public(record, vault_locked=vault.is_locked())
        for attempt in ("a wrong passphrase 1", "another wrong passphrase"):
            with pytest.raises(Exception):
                vault.unlock(passphrase=attempt)
            vault._next_attempt_at = 0
        vault._free_ram = lambda: 1024
        with pytest.raises(Exception):
            vault.unlock(passphrase=PASS)
        vault._free_ram = lambda: 4 * 1024 ** 3
        vault.note_postponed("Key requests")
        vault.unlock(passphrase=PASS)
        vault.lock()
        world = _World(locked=True)
        world.deps().log("WARNING", "Schedule: x", "Picture not sent")
        schedule_picture.run({"type": "send_camera_picture", "params": {"source_id": "cam1", "contact_id": "!b0f14d2a"}}, {"id": "r", "label": "Garden"}, world.deps())
        everything = json.dumps([system_log, notified, world.logs, world.notes, [r.getMessage() for r in caplog.records], shown_locked], default=str)
        assert SECRET_NAME not in everything and SECRET_COMMENT not in everything
        assert shown_locked["file_name"] is None
        assert PASS not in everything and "wrong passphrase" not in everything
    finally:
        mca_runtime.set_vault_log(None)
        mca_runtime.reset_state_for_tests()


def test_the_files_ui_never_puts_a_file_name_into_a_toast_or_progress_text():
    """The same contract for the page: every toast and progress text in files.js is a catalog key or a contact name."""
    import re
    source = open(os.path.join(os.path.dirname(__file__), "..", "static", "files.js"), encoding="utf-8").read()
    offenders = []
    for number, line in enumerate(source.splitlines(), 1):
        if re.search(r"\b(toast|notifyProgress|notifyUpdate|showToast)\(", line) and re.search(r"file_name|display_name|\.name\b.*file", line):
            offenders.append((number, line.strip()))
    assert offenders == [], offenders


# ---- MCA-VAULT PR 3: the vault commands against the real service, runtime and workspace ---------------------------------

import uuid
from pathlib import Path

from meshsrv.attachments.commands import Command
from meshsrv.attachments.vault import migration, recovery as vault_recovery, streamcrypt
from meshsrv.attachments.vault.errors import VaultError

SECRET = "quarterly-secret-report.pdf"


def plain_state(tmp_path, tag):
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_Session())
    data_dir = str(tmp_path / tag)
    state = mca_runtime._get_state(data_dir)  # noqa: SLF001
    mca_runtime.start_attachments_service(data_dir, FakeRadioTransport(InMemoryEther(), "!aaaaaaaa"), control_channel_index=0)
    assert state.service.stop()
    paths = state.workspace_manager.paths(state.principal.principal_id)
    (paths.files / SECRET).write_bytes(b"report body " * 500)
    (paths.cache_incoming / ("a1" * 16)).write_bytes(b"cached bytes")
    state.conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, file_name, mime_type, plain_size, plain_sha256, "
        "saved_path, draft_comment, created_at, hard_expires_at, download_grace_seconds) VALUES (?, ?, ?, 'received', ?, 'AVAILABLE', ?, 'application/pdf', 6000, NULL, ?, 'a private comment', 1, 2, 3600)",
        ("b2" * 16, state.principal.workspace_id, "00" * 16, state.principal.principal_id, SECRET, str(paths.files / SECRET)))
    state.conn.commit()
    return state, paths


def run_command(state, kind, payload):
    command = Command(command_id=uuid.uuid4().hex, kind=kind, payload=payload, created_at=time.time())
    state.service._command_registry.register(command)
    state.service._execute_command(command)
    return state.service._command_registry.get(command.command_id)


def test_vault_enable_then_disable_through_the_worker_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHCENTER_MCA_VAULT_EXPERIMENTAL", "1")
    lines = []
    mca_runtime.set_vault_log(lambda level, title, details: lines.append((level, title, details)))
    try:
        state, paths = plain_state(tmp_path, "e2e")
        vault = state.vault
        vault._free_ram = lambda: 4 * 1024 ** 3
        vault.put_handoff("tok1", passphrase.normalize(PASS))
        result = run_command(state, "vault_enable", {"handoff": "tok1", "backup": True})
        assert result.status == "succeeded", result
        assert result.result["files"] == 3 and result.result["backup"].startswith("mca-vault-backup-")
        assert PASS not in repr(result) and "recovery" not in repr(result).lower(), "the recovery key is not in the command result"
        assert vault.state() == "unlocked" and vault.status()["recovery_key_pending"] is True
        recovery_text = vault.take_recovery_key()
        assert len(vault_recovery.groups(recovery_text)) == 13 and vault.take_recovery_key() is None
        names = sorted(p.name for p in paths.files.iterdir())
        assert names == ["b2" * 16 + ".mcv"] and not (paths.files / SECRET).exists()
        assert any(p.name.endswith(".mcv") for p in paths.keys.iterdir()), "the real identity seed was encrypted too"
        assert (paths.cache_incoming / (("a1" * 16) + ".mcv")).exists() and not (paths.cache_incoming / ("a1" * 16)).exists()
        assert list(Path(state.workspace_manager.data_dir).glob("mca-vault-backup-*")), "the plain backup exists and is reported"
        stored = state.conn.execute("SELECT file_name, draft_comment FROM attachments WHERE id = ?", ("b2" * 16,)).fetchone()
        assert SECRET not in stored[0] and "private" not in stored[1] and stored[0].startswith("v1:")
        # locked behaviour of the real service while the vault exists
        vault.lock()
        assert state.service._vault_locked() is True and vault.state() == "locked"
        # unlock, then disable: back to plain, vault gone
        vault.unlock(passphrase=PASS)
        result = run_command(state, "vault_disable", {"backup": False})
        assert result.status == "succeeded" and vault.state() == "disabled" and not vault.is_enabled()
        assert (paths.files / SECRET).read_bytes() == b"report body " * 500
        assert state.conn.execute("SELECT file_name FROM attachments WHERE id = ?", ("b2" * 16,)).fetchone()[0] == SECRET
        text = repr(lines)
        assert SECRET not in text and "private" not in text and PASS not in text, "no name, comment or secret in the System Log lines"
        assert [l[1] for l in lines if "enabled" in l[1] or "disabled" in l[1]] == ["MCAttach vault enabled", "MCAttach vault disabled"]
    finally:
        mca_runtime.set_vault_log(None)
        mca_runtime.reset_state_for_tests()


def test_enable_command_failures_clean_up_and_report_a_stable_code(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHCENTER_MCA_VAULT_EXPERIMENTAL", "1")
    try:
        state, paths = plain_state(tmp_path, "fail")
        state.vault._free_ram = lambda: 4 * 1024 ** 3
        assert run_command(state, "vault_enable", {"handoff": "missing"}).error_code == "vault_handoff_expired"
        state.conn.execute("UPDATE attachments SET state = 'DOWNLOADING' WHERE id = ?", ("b2" * 16,))
        state.conn.commit()
        state.vault.put_handoff("tok2", passphrase.normalize(PASS))
        result = run_command(state, "vault_enable", {"handoff": "tok2", "backup": False})
        assert result.status == "failed" and result.error_code == "transfers_in_progress"
        assert state.vault.state() == "disabled" and (paths.files / SECRET).exists(), "nothing was touched"
        assert state.vault.take_handoff("tok2") is None, "the secret was consumed"
        assert run_command(state, "vault_disable", {}).error_code == "vault_locked"
        assert run_command(state, "vault_resume", {}).error_code == "vault_locked"
    finally:
        mca_runtime.reset_state_for_tests()


def test_an_interrupted_enable_is_migrating_and_locked_until_resumed(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHCENTER_MCA_VAULT_EXPERIMENTAL", "1")
    try:
        state, paths = plain_state(tmp_path, "resume")
        vault = state.vault
        vault._free_ram = lambda: 4 * 1024 ** 3
        env = state.service._vault_env()
        counter = {"n": 0}

        def boom(step):
            counter["n"] += 1
            if counter["n"] == 3:
                raise RuntimeError("power cut")
        env.after_step = boom
        with pytest.raises(RuntimeError):
            migration.enable(env, passphrase.normalize(PASS), params=kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT), make_backup_copy=False)
        assert vault.state() == "migrating" and vault.is_locked() and state.service._vault_locked() is True
        assert vault.status()["state"] == "migrating"
        assert update_service_vault_enabled(tmp_path / "resume"), "Roll back is refused while a migration is unfinished"
        vault.unlock(passphrase=PASS)                                   # the key from the passphrase, as after a restart
        result = run_command(state, "vault_resume", {})
        assert result.status == "succeeded" and result.result == {"resumed": "enable"}
        assert vault.state() == "unlocked" and not (Path(paths.root) / "vault-migration.json").exists()
        status = vault.status()
        assert status["recovery_key_needs_reissue"] is True and status["recovery_key_pending"] is False, "its key was never shown: issue a new one"
        vault.new_recovery_key(PASS)
        assert vault.status()["recovery_key_needs_reissue"] is False
        assert migration.verify_enabled(env, vault.storage_key()) == {"plain_files": 0, "plain_columns": 0}
    finally:
        mca_runtime.reset_state_for_tests()


def update_service_vault_enabled(data_dir):
    from meshsrv import update_service
    update_service.configure_vault_dir(str(data_dir))
    try:
        return update_service.vault_enabled()
    finally:
        update_service.configure_vault_dir("")


def test_the_worker_refuses_a_migration_for_memory_before_touching_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHCENTER_MCA_VAULT_EXPERIMENTAL", "1")
    try:
        state, paths = plain_state(tmp_path, "lowmem")
        state.vault._free_ram = lambda: 30 * 1024 * 1024
        before = sorted(p.name for p in paths.files.iterdir())
        state.vault.put_handoff("tokm", passphrase.normalize(PASS))
        result = run_command(state, "vault_enable", {"handoff": "tokm", "backup": True})
        assert result.status == "failed" and result.error_code == "insufficient_memory"
        assert state.vault.state() == "disabled" and sorted(p.name for p in paths.files.iterdir()) == before
        assert not list(Path(state.workspace_manager.data_dir).glob("mca-vault-backup-*")), "no backup was started either"
        assert not (Path(paths.root) / "vault-migration.json").exists() and not (Path(paths.root) / "vault.json").exists()
        assert state.vault.take_handoff("tokm") is None, "the secret was consumed, not left waiting"
    finally:
        mca_runtime.reset_state_for_tests()
