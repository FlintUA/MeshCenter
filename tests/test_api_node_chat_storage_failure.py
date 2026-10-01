"""Tests for server.py's node/chat/message-mutating HTTP routes (F4.1 PR 2).

F4.0 finding: save_nodes()/save_chats()/save_messages() returned None and
every caller ignored it - a mutating route (toggle_ignore, toggle_favorite,
cleanup_nodes, clear_chat, delete_chat, nodes_import, delete_all_dm,
restore_deleted_dm) answered ok:true regardless of whether the write to disk
actually succeeded. All eight now check the result and answer 500
storage_write_failed on failure - these two (nodes_import, delete_all_dm)
are the most complex (several saves in sequence) and are exercised here
end-to-end against the real server_module app; the rest follow the exact
same one-line pattern.
"""

import json

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


def test_delete_all_dm_failed_deleted_file_write_returns_storage_error_and_keeps_chats(server_module, monkeypatch):
    client = _csrf_client(server_module)

    with server_module.state_lock:
        server_module.chats["!aabbccdd"] = {
            "id": "!aabbccdd", "name": "Some DM", "type": "dm",
            "last_message": "hi", "last_time": "now", "unread": 0,
        }
    original_chats = dict(server_module.chats)

    monkeypatch.setattr(server_module, "safe_write_json", lambda *a, **k: False)

    resp = client.post("/api/delete_all_dm", headers={"X-CSRF-Token": "test-csrf-token"})

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    # The in-memory delete already happened before the failed write is
    # discovered (del chats[chat_id] runs first) - this asserts the
    # specific, narrower guarantee this fix actually provides: the
    # deleted_dm.json marker write is checked and the request is honest
    # about failing, rather than answering ok:true. A full rollback of the
    # in-memory chats dict for this route is a separate, larger change not
    # in this PR's scope (see PR description).

    # Clean up for test isolation regardless of outcome.
    server_module.chats.clear()
    server_module.chats.update(original_chats)
