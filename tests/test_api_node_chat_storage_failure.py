"""Tests for server.py's node/chat/message-mutating HTTP routes (F4.1 PR 2).

F4.0 finding: save_nodes()/save_chats()/save_messages() returned None and
every caller ignored it - a mutating route (toggle_ignore, toggle_favorite,
cleanup_nodes, clear_chat, delete_chat, nodes_import, delete_all_dm,
restore_deleted_dm) answered ok:true regardless of whether the write to disk
actually succeeded. All eight now check the result and answer 500
storage_write_failed on failure.

Review fix (same PR): checking the result wasn't enough on its own - these
routes mutate shared in-memory state (nodes/chats/messages) BEFORE saving,
so a failed write used to leave memory permanently diverged from disk even
though the client got a 500. Every one of them now snapshots the state it's
about to touch and restores it (in place - the F2 identity invariant) if
the save fails, with a best-effort resync of disk too in multi-save routes
where one save could succeed before another fails. delete_all_dm's own
deleted_dm.json marker is the one deliberate exception, documented at its
own call site: the marker is written LAST, after chats/messages are
already persisted, so a marker-only failure leaves the deletion applied
(not rolled back) - the narrower, self-healing gap is a missing marker,
never a chats/messages memory-vs-disk mismatch. Since the deletion itself
already succeeded in that case, the route answers ok:true with a warning
field (H1-C4(a)) rather than a 500.
"""

import json
import os

import pytest


def _csrf_client(server_module):
    client = server_module.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf_token"] = "test-csrf-token"
    return client


def test_nodes_import_failed_write_returns_storage_error(server_module, monkeypatch):
    client = _csrf_client(server_module)
    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)

    resp = client.post(
        "/api/nodes_import",
        json={"nodes": [{"node_id": "!11223344", "name": "Test Import Node"}]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"


def test_nodes_import_success_still_works(server_module):
    client = _csrf_client(server_module)

    resp = client.post(
        "/api/nodes_import",
        json={"nodes": [{"node_id": "!11223344", "name": "Test Import Node"}]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["imported_count"] == 1
    assert "!11223344" in server_module.nodes


def test_delete_all_dm_failed_chats_write_returns_storage_error_and_restores_chats(server_module, monkeypatch):
    """Review fix: save_chats()/save_messages() failing must restore the
    in-memory chats dict (and messages list) to exactly what it held
    before the request - not leave the DM deleted in memory while the
    write failed."""
    client = _csrf_client(server_module)

    with server_module.state_lock:
        server_module.chats["!aabbccdd"] = {
            "id": "!aabbccdd", "name": "Some DM", "type": "dm",
            "last_message": "hi", "last_time": "now", "unread": 0,
        }
    original_chats = dict(server_module.chats)
    original_messages = list(server_module.messages)

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)

    resp = client.post("/api/delete_all_dm", headers={"X-CSRF-Token": "test-csrf-token"})

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.chats == original_chats
    assert server_module.messages == original_messages


def test_delete_all_dm_failed_marker_write_still_deletes_the_chats(server_module, monkeypatch):
    """Review fix, documented tradeoff: the deleted_dm.json marker is
    written LAST, only after chats.json/messages.json are actually
    persisted with the deletion applied. If only the marker write fails,
    the deletion is NOT rolled back (chats/messages are already genuinely
    saved) - the accepted, narrower gap is a missing marker, not a
    memory/disk mismatch for chats/messages themselves.

    H1-C4(a): since the deletion genuinely succeeded in this case, the
    route now answers ok:true with a warning field instead of a 500 that
    would make the caller believe nothing happened."""
    client = _csrf_client(server_module)

    with server_module.state_lock:
        server_module.chats["!aabbccdd"] = {
            "id": "!aabbccdd", "name": "Some DM", "type": "dm",
            "last_message": "hi", "last_time": "now", "unread": 0,
        }

    real_safe_write_json = server_module.safe_write_json

    def _fail_only_marker(path, data):
        if path == server_module.DELETED_DM_FILE:
            return False
        return real_safe_write_json(path, data)

    monkeypatch.setattr(server_module, "safe_write_json", _fail_only_marker)

    resp = client.post("/api/delete_all_dm", headers={"X-CSRF-Token": "test-csrf-token"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["warning"] == "deleted_dm_marker_write_failed"
    assert "!aabbccdd" not in server_module.chats


def test_delete_all_dm_success_removes_dm_chats_and_writes_marker(server_module):
    client = _csrf_client(server_module)

    with server_module.state_lock:
        server_module.chats["!aabbccdd"] = {
            "id": "!aabbccdd", "name": "Some DM", "type": "dm",
            "last_message": "hi", "last_time": "now", "unread": 0,
        }

    resp = client.post("/api/delete_all_dm", headers={"X-CSRF-Token": "test-csrf-token"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "!aabbccdd" not in server_module.chats
    assert os.path.exists(server_module.DELETED_DM_FILE)


def test_toggle_favorite_failed_write_restores_previous_value(server_module, monkeypatch):
    node_id = "!11223344"
    with server_module.state_lock:
        server_module.nodes[node_id] = {"node_id": node_id, "name": "N", "favorite": False}

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)
    client = _csrf_client(server_module)

    resp = client.post(
        "/api/toggle_favorite",
        json={"node_id": node_id},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.nodes[node_id]["favorite"] is False


def test_toggle_ignore_failed_write_restores_previous_value(server_module, monkeypatch):
    node_id = "!11223344"
    with server_module.state_lock:
        server_module.nodes[node_id] = {"node_id": node_id, "name": "N", "ignored": False}

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)
    client = _csrf_client(server_module)

    resp = client.post(
        "/api/toggle_ignore",
        json={"node_id": node_id},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.nodes[node_id]["ignored"] is False


def test_cleanup_nodes_failed_write_restores_chats(server_module, monkeypatch):
    node_id = "!11223344"
    with server_module.state_lock:
        server_module.nodes[node_id] = {"node_id": node_id, "name": "New Node"}
        server_module.chats.pop(node_id, None)
    original_chats = dict(server_module.chats)

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)
    client = _csrf_client(server_module)

    resp = client.post("/api/cleanup_nodes", headers={"X-CSRF-Token": "test-csrf-token"})

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.chats == original_chats


def test_clear_chat_failed_messages_write_restores_messages_and_chat_entry(server_module, monkeypatch):
    chat_id = "!11223344"
    with server_module.state_lock:
        server_module.chats[chat_id] = {
            "id": chat_id, "name": "N", "type": "dm",
            "last_message": "hello", "last_time": "now", "unread": 3,
        }
        server_module.messages.append({"chat_id": chat_id, "id": "m1", "text": "hello"})
    original_messages = list(server_module.messages)
    original_chat_entry = dict(server_module.chats[chat_id])

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)
    client = _csrf_client(server_module)

    resp = client.post(
        "/api/clear_chat",
        json={"chat_id": chat_id},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.messages == original_messages
    assert server_module.chats[chat_id] == original_chat_entry


def test_delete_chat_failed_write_restores_chats_and_messages(server_module, monkeypatch):
    chat_id = "!11223344"
    with server_module.state_lock:
        server_module.chats[chat_id] = {
            "id": chat_id, "name": "N", "type": "dm",
            "last_message": "hello", "last_time": "now", "unread": 0,
        }
        server_module.messages.append({"chat_id": chat_id, "id": "m1", "text": "hello"})
    original_chats = dict(server_module.chats)
    original_messages = list(server_module.messages)

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)
    client = _csrf_client(server_module)

    resp = client.post(
        "/api/delete_chat",
        json={"chat_id": chat_id},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.chats == original_chats
    assert server_module.messages == original_messages


def test_nodes_import_failed_write_restores_nodes_and_chats(server_module, monkeypatch):
    original_nodes = dict(server_module.nodes)
    original_chats = dict(server_module.chats)

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)
    client = _csrf_client(server_module)

    resp = client.post(
        "/api/nodes_import",
        json={"nodes": [{"node_id": "!99887766", "name": "Imported Node"}]},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.nodes == original_nodes
    assert server_module.chats == original_chats


def test_restore_deleted_dm_failed_write_restores_chats_and_keeps_marker(server_module, monkeypatch):
    with server_module.state_lock:
        server_module.safe_write_json(server_module.DELETED_DM_FILE, {"deleted": ["!11223344"]})
    original_chats = dict(server_module.chats)

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)
    client = _csrf_client(server_module)

    resp = client.post("/api/restore_deleted_dm", headers={"X-CSRF-Token": "test-csrf-token"})

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert server_module.chats == original_chats
    # The marker must survive a failed restore - removing it first (the
    # pre-fix behaviour) would make the deletion unrecoverable.
    assert os.path.exists(server_module.DELETED_DM_FILE)


def test_restore_deleted_dm_success_removes_marker(server_module):
    with server_module.state_lock:
        server_module.safe_write_json(server_module.DELETED_DM_FILE, {"deleted": []})
    client = _csrf_client(server_module)

    resp = client.post("/api/restore_deleted_dm", headers={"X-CSRF-Token": "test-csrf-token"})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert not os.path.exists(server_module.DELETED_DM_FILE)


def test_ensure_chat_tolerates_legacy_list_shaped_deleted_dm_file(server_module):
    """H2-B regression: storage/profile_manager.py's create_clean_profile()
    used to initialize deleted_dm.json as a bare `[]` instead of the
    `{"deleted": [...]}` shape every writer (api_delete_all_dm) and reader
    (ensure_chat) actually use. Any profile created while that bug was live
    has a `[]` on disk forever (nothing rewrites this file except
    delete_all_dm/restore_deleted_dm), so ensure_chat() must tolerate the
    legacy shape, not just rely on the writer being fixed. This is what
    crashed live on pixel-111's TCP position/text inbound paths (both call
    ensure_chat(..., force=False)) with
    AttributeError: 'list' object has no attribute 'get'.
    """
    with server_module.state_lock:
        server_module.safe_write_json(server_module.DELETED_DM_FILE, [])

    with server_module.state_lock:
        server_module.ensure_chat("!aabbccdd", "Test Node", force=False)

    assert "!aabbccdd" in server_module.chats
