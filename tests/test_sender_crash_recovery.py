"""tests/test_sender_crash_recovery.py

Scripted `kill -9` restart-recovery tests for the sender state machine
(Execution Plan Step 1.4 DoD, verbatim): killing the real OS process at
any intermediate state must not lose the job or create a duplicate send
on restart. Special required coverage: the "commit succeeded, then
crashed before radio send" case, which must resume at READY_TO_SEND and
only retry the pointer send, never re-upload.

This spawns `tests/_sender_crash_driver.py` as a real subprocess and
sends it a genuine `SIGKILL` (not a same-process function call standing
in for one) once it reports having reached the target state, then starts
a *fresh* subprocess against the same on-disk `attachments.db` to prove
recovery survives an actual process death rather than merely "calling
run_step() again in the same interpreter that never really crashed".

The mock Relay runs as a real HTTP server (werkzeug, in a background
thread of this test process) so that both subprocess invocations - the
one that gets killed and the one that resumes - talk to the *same*
server-side upload session across the process boundary, exactly like two
real MeshCenter process lifetimes would talk to the same real Relay.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
from werkzeug.serving import make_server

from meshsrv.attachments import identity, sender
from meshsrv.attachments.db import migrations
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app

_DRIVER_PATH = os.path.join(os.path.dirname(__file__), "_sender_crash_driver.py")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def relay_server():
    port = _free_port()
    store = MockRelayStore(base_url=f"http://127.0.0.1:{port}")
    app = create_mock_relay_app(store)
    server = make_server("127.0.0.1", port, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", store
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def workspace(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "attachments.db"
    conn = __import__("sqlite3").connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.migrate(conn)
    from meshsrv.attachments.workspace import MCAWorkspaceManager

    wsm = MCAWorkspaceManager(str(data_dir))
    identity.ensure_principal(conn, wsm, "local")
    conn.close()  # the driver subprocess opens its own connection
    return str(db_path), str(data_dir)


def _run_driver(*, db_path, data_dir, relay_base_url, upload_access_token, source_path,
                 recipient_pub_hex, attachment_id, stop_after_state, sent_log_path, timeout=30):
    args = [
        sys.executable, _DRIVER_PATH, db_path, data_dir, relay_base_url, upload_access_token,
        source_path, recipient_pub_hex, attachment_id, stop_after_state or "NONE", sent_log_path,
    ]
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _sent_lines(sent_log_path: str):
    if not os.path.exists(sent_log_path):
        return []
    with open(sent_log_path, "r", encoding="ascii") as fh:
        return [line for line in fh.read().splitlines() if line]


STATES_TO_KILL_AT = [
    sender.DRAFT,
    sender.VALIDATING,
    sender.ENCRYPTING,
    sender.QUEUED_UPLOAD,
    sender.UPLOADING,  # -> READY_TO_SEND: the DoD's specific "committed, crashed before send" case
    sender.READY_TO_SEND,  # -> SENT
]


@pytest.mark.parametrize("kill_state", STATES_TO_KILL_AT)
def test_kill_9_at_each_state_resumes_without_duplicate_send(relay_server, workspace, tmp_path, kill_state):
    relay_base_url, store = relay_server
    db_path, data_dir = workspace
    sent_log_path = str(tmp_path / f"sent_{kill_state}.log")

    source_path = tmp_path / "payload.txt"
    source_path.write_bytes(os.urandom(50_000))

    from nacl.signing import SigningKey

    recipient_sk = SigningKey.generate()
    recipient_pub_hex = bytes(recipient_sk.verify_key).hex()

    # First run: drive to `kill_state` and self-SIGKILL there.
    first = _run_driver(
        db_path=db_path, data_dir=data_dir, relay_base_url=relay_base_url,
        upload_access_token=store.upload_access_token, source_path=str(source_path),
        recipient_pub_hex=recipient_pub_hex, attachment_id="NEW", stop_after_state=kill_state,
        sent_log_path=sent_log_path,
    )
    # A process that dies to SIGKILL reports a negative returncode equal
    # to -signal.SIGKILL on POSIX (subprocess's documented convention) -
    # asserting this, not just "it stopped", is what makes this a real
    # kill-9 test rather than a disguised normal-exit test.
    assert first.returncode == -signal.SIGKILL, (
        f"expected the driver to be killed by SIGKILL, got returncode={first.returncode}, "
        f"stdout={first.stdout!r}, stderr={first.stderr!r}"
    )

    attachment_id = None
    for line in first.stdout.splitlines():
        if line.startswith("ATTACHMENT_ID="):
            attachment_id = line.split("=", 1)[1]
    assert attachment_id, f"driver never printed an attachment id; stdout={first.stdout!r}"

    # Second run: fresh process, same on-disk DB, same Relay server - must
    # resume from exactly where the first one left off and reach SENT.
    second = _run_driver(
        db_path=db_path, data_dir=data_dir, relay_base_url=relay_base_url,
        upload_access_token=store.upload_access_token, source_path=str(source_path),
        recipient_pub_hex=recipient_pub_hex, attachment_id=attachment_id, stop_after_state=None,
        sent_log_path=sent_log_path,
    )
    assert second.returncode == 0, f"resume did not exit cleanly: stdout={second.stdout!r} stderr={second.stderr!r}"

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    final_state = conn.execute("SELECT state FROM attachments WHERE id = ?", (attachment_id,)).fetchone()["state"]
    conn.close()
    assert final_state == sender.SENT, f"expected SENT after resume, got {final_state}"

    sent = _sent_lines(sent_log_path)
    assert len(sent) == 1, f"expected exactly one sent OFFER, got {len(sent)}: {sent}"
    assert sent[0].startswith("MCA1:")


def test_kill_9_after_ready_to_send_never_recreates_upload_session(relay_server, workspace, tmp_path):
    """The DoD's specifically-called-out case, checked directly against
    server-side state rather than only the end-to-end outcome above:
    after `commit()` has succeeded and READY_TO_SEND is durably persisted,
    a killed-and-resumed process must never call `create_upload` again for
    the same transfer_id - resuming at READY_TO_SEND dispatches straight
    to the send handler, which never touches the Relay at all."""

    relay_base_url, store = relay_server
    db_path, data_dir = workspace
    sent_log_path = str(tmp_path / "sent_ready.log")

    source_path = tmp_path / "payload.txt"
    source_path.write_bytes(os.urandom(20_000))

    from nacl.signing import SigningKey

    recipient_sk = SigningKey.generate()
    recipient_pub_hex = bytes(recipient_sk.verify_key).hex()

    first = _run_driver(
        db_path=db_path, data_dir=data_dir, relay_base_url=relay_base_url,
        upload_access_token=store.upload_access_token, source_path=str(source_path),
        recipient_pub_hex=recipient_pub_hex, attachment_id="NEW", stop_after_state=sender.READY_TO_SEND,
        sent_log_path=sent_log_path,
    )
    assert first.returncode == -signal.SIGKILL

    attachment_id = next(
        line.split("=", 1)[1] for line in first.stdout.splitlines() if line.startswith("ATTACHMENT_ID=")
    )

    transfers_before = len(store._transfers_by_upload_id)  # noqa: SLF001 - white-box check of server-side session count

    second = _run_driver(
        db_path=db_path, data_dir=data_dir, relay_base_url=relay_base_url,
        upload_access_token=store.upload_access_token, source_path=str(source_path),
        recipient_pub_hex=recipient_pub_hex, attachment_id=attachment_id, stop_after_state=None,
        sent_log_path=sent_log_path,
    )
    assert second.returncode == 0

    transfers_after = len(store._transfers_by_upload_id)  # noqa: SLF001
    assert transfers_after == transfers_before, (
        "resuming from READY_TO_SEND must not create a new Relay upload session "
        f"(had {transfers_before}, now {transfers_after})"
    )
    assert len(_sent_lines(sent_log_path)) == 1
