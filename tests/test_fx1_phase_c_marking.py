"""FX1 Phase C: passive MeshCenter marking of nodes (no broadcast; a real MCA frame marks the sender)."""
import time
import uuid

import pytest
from nacl.signing import SigningKey

from meshsrv import mc_marking
from meshsrv.attachments import codec


def _frame_text():
    key = SigningKey.generate()
    raw = codec.encode_offer(
        codec.OfferFields(provider_id=uuid.uuid4().bytes[:8], transfer_id=uuid.uuid4().bytes, sender_key_id=uuid.uuid4().bytes[:8],
                          kind=0, size_bucket=1, hard_expires_at=int(time.time()) + 3600, flags=0),
        key,
    )
    return codec.to_text(raw)


def test_a_real_frame_is_recognised_and_ordinary_text_is_not():
    assert mc_marking.is_mca_frame(_frame_text())
    for text in ("hello", "", "MCA1:", "MCA1:!!!notbase64", "MCA1:AAAA", "mca1:abcd", None, 5, "x" * 5000):
        assert mc_marking.is_mca_frame(text) is False, text
    assert mc_marking.is_mca_frame("MCA1:" + "A" * 4000) is False, "an oversized or meaningless body is not a frame"


def test_the_first_frame_marks_and_asks_for_a_write_then_the_last_seen_moves_quietly():
    node = {"node_id": "!11111111", "name": "Echo"}
    assert mc_marking.mc_fields(node) == {"mc_first_seen": None, "mc_last_seen": None}
    assert mc_marking.is_meshcenter_node(node) is False
    assert mc_marking.note_frame(node, 1000.0) is True, "a first sighting is written at once"
    assert (node["mc_first_seen"], node["mc_last_seen"]) == (1000.0, 1000.0)
    assert mc_marking.note_frame(node, 1010.0) is False, "a later frame only moves last_seen in memory"
    assert node["mc_first_seen"] == 1000.0 and node["mc_last_seen"] == 1010.0
    assert mc_marking.note_frame(node, 1400.0) is True, "and it is written at most every 5 minutes"
    assert mc_marking.is_meshcenter_node(node) is True
    assert mc_marking.mc_fields(node) == {"mc_first_seen": 1000.0, "mc_last_seen": 1400.0}


def test_damaged_fields_read_as_not_marked():
    for bad in ({"mc_first_seen": "x", "mc_last_seen": 5}, {"mc_first_seen": 0, "mc_last_seen": 0}, {"mc_first_seen": 5}, {}):
        assert mc_marking.is_meshcenter_node(bad) is False


@pytest.fixture
def clean_nodes(server_module):
    saved = dict(server_module.nodes)
    server_module.nodes.clear()
    yield server_module
    server_module.nodes.clear()
    server_module.nodes.update(saved)


def test_the_server_marks_a_node_from_a_frame_and_the_projections_carry_it(clean_nodes, monkeypatch):
    server = clean_nodes
    saves = []
    monkeypatch.setattr(server, "save_nodes", lambda: saves.append(1))
    server.nodes["!22222222"] = {"node_id": "!22222222", "name": "Echo", "last_seen": time.time()}
    server.nodes["!33333333"] = {"node_id": "!33333333", "name": "Plain", "last_seen": time.time()}

    server._note_meshcenter_frame("just a chat message", "!22222222")
    assert "mc_last_seen" not in server.nodes["!22222222"], "ordinary text never marks"
    server._note_meshcenter_frame("MCA1:garbage-that-is-not-a-frame", "!22222222")
    assert "mc_last_seen" not in server.nodes["!22222222"]

    server._note_meshcenter_frame(_frame_text(), "!22222222")
    assert server.nodes["!22222222"]["mc_first_seen"] > 0 and saves == [1], "marked and written once"
    server._note_meshcenter_frame(_frame_text(), "!22222222")
    assert saves == [1], "the second frame is not written again within 5 minutes"
    server._note_meshcenter_frame(_frame_text(), "!99999999")                 # a node the list does not know: nothing happens
    assert "!99999999" not in server.nodes

    listed = {n["node_id"]: n for n in server.get_nodes_list()}
    assert listed["!22222222"]["mc_first_seen"] > 0 and listed["!22222222"]["mc_last_seen"] >= listed["!22222222"]["mc_first_seen"]
    assert listed["!33333333"]["mc_first_seen"] is None and listed["!33333333"]["mc_last_seen"] is None
    managed = {n["node_id"]: n for n in server.app.test_client().get("/api/nodes_management").get_json()["nodes"]}
    assert managed["!22222222"]["mc_last_seen"] is not None and managed["!33333333"]["mc_last_seen"] is None


def test_a_nodeinfo_update_keeps_the_mark(clean_nodes):
    # the merge helpers copy the old node first (dict(old)), so a field they do not know survives every NodeInfo update
    import inspect

    assert "node = dict(old)" in inspect.getsource(clean_nodes._merge_nodeinfo_into_node)


def test_the_marking_is_wired_in_the_page_the_catalogs_and_the_picker():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'data-node-filter="mcOnly"' in html, "MeshCenter only in the Sort and filters popover"
    assert 'id="nodesPillMc"' in html and 'data-chat-action="nodes-header-mc"' in html, "the counter in the Nodes header"
    chat = (root / "static" / "chat.js").read_text(encoding="utf-8")
    assert "mcOnly: 'nodes.filter_mc_only'" in chat and "'nodes-header-mc': () => toggleNodeHeaderFilter('mcOnly')" in chat
    assert "renderMeshCenterBadge" in chat and "renderMeshCenterSeenRow" in chat, "badge, first/last seen"
    picker = (root / "static" / "recipient-picker.js").read_text(encoding="utf-8")
    assert "data-rp-mc" in picker and "c.mc === true" in picker
    for loc in ("en", "de", "ru", "uk"):
        d = json.loads((root / "static" / "i18n" / ("%s.json" % loc)).read_text(encoding="utf-8"))
        for key in ("filter_mc_only", "header_mc", "header_mc_aria", "mc_badge_title", "mc_seen_row"):
            assert d["nodes"][key], (loc, key)
        for key in ("mc_only", "mc_badge"):
            assert d["recipient"][key], (loc, key)
        assert "{first}" in d["nodes"]["mc_badge_title"] and "{last}" in d["nodes"]["mc_seen_row"]


def test_marking_never_sends_anything():
    """No broadcast, probe or beacon: the module imports nothing that can send (no transport, network or process) and only
    reads text it already received and writes two timestamps."""
    import ast
    import inspect

    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(mc_marking))):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0] + "." + (node.module or "").split(".")[1] if node.module and node.module.startswith("meshsrv") and len(node.module.split(".")) > 1 else (node.module or ""))
    assert imported <= {"__future__", "os", "pathlib", "sqlite3", "typing", "meshsrv.attachments"}, imported   # files and the database only


# ---- back-fill from what MCAttach already knows -----------------------------------------------------------------------------


def _db(tmp_path):
    from meshsrv.attachments.db import migrations

    path = tmp_path / "attachments.db"
    conn = migrations.open_attachments_db(path)
    return path, conn


def _bind(conn, address, bound_at, *, pending_at=None, confirmed=True):
    conn.execute(
        "INSERT INTO mca_recipient_bindings (id, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity, "
        "key_epoch, bound_at, tofu_confirmed_at, pending_detected_at) VALUES (?, 'local', 'meshtastic_text', ?, 'p', 'k', 'ab', 0, ?, ?, ?)",
        (uuid.uuid4().hex, address, bound_at, bound_at if confirmed else None, pending_at),
    )


def _received(conn, address, created_at):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, created_at, hard_expires_at, "
        "download_grace_seconds, reply_route_id) VALUES (?, 'local', ?, 'received', 'p', 'AVAILABLE', ?, ?, 3600, ?)",
        (uuid.uuid4().hex, uuid.uuid4().hex, created_at, created_at + 100, address),
    )


def _acked(conn, address, ack_at):
    attachment_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, created_at, hard_expires_at, "
        "download_grace_seconds) VALUES (?, 'local', ?, 'sent', 'p', 'DOWNLOADED', 1, 2, 3600)",
        (attachment_id, uuid.uuid4().hex),
    )
    conn.execute(
        "INSERT INTO attachment_deliveries (id, attachment_id, adapter_id, connector_profile_id, route_type, route_id, wire_format, "
        "idempotency_key, state, sent_at, ack_at) VALUES (?, ?, 'a', 'c', 'DIRECT', ?, 'text', ?, 'ACKED', 1, ?)",
        (uuid.uuid4().hex, attachment_id, address, uuid.uuid4().hex, ack_at),
    )


def test_a_node_with_a_key_binding_is_marked_from_the_database_in_any_trust_state(tmp_path):
    path, conn = _db(tmp_path)
    _bind(conn, "!aa000001", 1000)                                          # trusted
    _bind(conn, "!aa000002", 2000, confirmed=False)                         # announced, never confirmed
    _bind(conn, "!aa000003", 3000, pending_at=3500)                         # a changed key is waiting
    conn.commit()
    conn.close()
    nodes = {a: {"node_id": a} for a in ("!aa000001", "!aa000002", "!aa000003", "!aa000004")}
    assert mc_marking.backfill_from_database(nodes, str(path), 9999.0) == 3
    assert (nodes["!aa000001"]["mc_first_seen"], nodes["!aa000001"]["mc_last_seen"]) == (1000.0, 1000.0)
    assert (nodes["!aa000002"]["mc_first_seen"], nodes["!aa000002"]["mc_last_seen"]) == (2000.0, 2000.0), "an unconfirmed key counts too"
    assert (nodes["!aa000003"]["mc_first_seen"], nodes["!aa000003"]["mc_last_seen"]) == (3000.0, 3500.0), "the pending key announce is a later frame"
    assert "mc_last_seen" not in nodes["!aa000004"], "a node with no MCA trace stays unmarked"


def test_first_is_the_earliest_known_and_last_the_latest_across_bindings_offers_and_acks(tmp_path):
    path, conn = _db(tmp_path)
    _bind(conn, "!bb000001", 5000)
    _received(conn, "!bb000001", 4000)           # an offer from before the binding
    _received(conn, "!bb000001", 8000)
    _acked(conn, "!bb000001", 9000)              # the recipient acknowledged a file of ours
    _received(conn, "!bb000002", 7000)           # an offer from a node with no binding (the key is still unknown) is a frame too
    conn.commit()
    conn.close()
    nodes = {"!bb000001": {"node_id": "!bb000001"}, "!bb000002": {"node_id": "!bb000002"}}
    mc_marking.backfill_from_database(nodes, str(path), 99999.0)
    assert (nodes["!bb000001"]["mc_first_seen"], nodes["!bb000001"]["mc_last_seen"]) == (4000.0, 9000.0)
    assert (nodes["!bb000002"]["mc_first_seen"], nodes["!bb000002"]["mc_last_seen"]) == (7000.0, 7000.0)


def test_the_backfill_is_idempotent_and_never_moves_a_live_mark_backwards(tmp_path):
    path, conn = _db(tmp_path)
    _bind(conn, "!cc000001", 1000)
    _received(conn, "!cc000001", 2000)
    conn.commit()
    conn.close()
    nodes = {"!cc000001": {"node_id": "!cc000001", "mc_first_seen": 1500.0, "mc_last_seen": 7000.0}}      # a live frame after the DB
    assert mc_marking.backfill_from_database(nodes, str(path), 1.0) == 1
    assert (nodes["!cc000001"]["mc_first_seen"], nodes["!cc000001"]["mc_last_seen"]) == (1000.0, 7000.0), "earliest first, latest last"
    assert mc_marking.backfill_from_database(nodes, str(path), 2.0) == 0, "a second run changes nothing"
    assert mc_marking.backfill_from_database(nodes, str(path), 3.0) == 0


def test_the_backfill_only_reads_never_writes_and_survives_a_missing_or_broken_database(tmp_path):
    path, conn = _db(tmp_path)
    _bind(conn, "!dd000001", 1000)
    conn.commit()
    conn.close()
    before = path.read_bytes()
    mc_marking.backfill_from_database({"!dd000001": {}}, str(path), 1.0)
    assert path.read_bytes() == before, "the database file is untouched"
    assert mc_marking.backfill_from_database({"!dd000001": {}}, str(tmp_path / "nothing.db"), 1.0) == 0
    assert mc_marking.backfill_from_database({"!dd000001": {}}, "", 1.0) == 0
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all")
    assert mc_marking.backfill_from_database({"!dd000001": {}}, str(junk), 1.0) == 0


def test_an_unknown_time_becomes_now_and_unknown_nodes_are_skipped():
    nodes = {"!ee000001": {}}
    assert mc_marking.backfill_from_times(nodes, {"!ee000001": (0, 0), "!ffffffff": (5.0, 6.0)}, 42.0) == 1, "the unknown node is skipped"
    assert (nodes["!ee000001"]["mc_first_seen"], nodes["!ee000001"]["mc_last_seen"]) == (42.0, 42.0)


def test_the_server_backfills_at_startup_and_saves_once(clean_nodes, monkeypatch, tmp_path):
    server = clean_nodes
    data_dir = tmp_path / "data"
    (data_dir / "mca").mkdir(parents=True)
    from meshsrv.attachments.db import migrations

    conn = migrations.open_attachments_db(data_dir / "mca" / "attachments.db")
    _bind(conn, "!ab000001", 1234)
    conn.commit()
    conn.close()
    server.nodes["!ab000001"] = {"node_id": "!ab000001", "name": "Echo"}
    server.nodes["!ab000002"] = {"node_id": "!ab000002", "name": "Plain"}
    saves = []
    monkeypatch.setattr(server, "save_nodes", lambda: saves.append(1))
    monkeypatch.setattr(server.mca_runtime, "get_data_dir", lambda: str(data_dir))
    server.backfill_meshcenter_marks()
    assert server.nodes["!ab000001"]["mc_first_seen"] == 1234.0 and "mc_last_seen" not in server.nodes["!ab000002"]
    assert saves == [1]
    server.backfill_meshcenter_marks()
    assert saves == [1], "a second start changes nothing and writes nothing"
    monkeypatch.setattr(server.mca_runtime, "get_data_dir", lambda: None)
    server.backfill_meshcenter_marks()                                     # no runtime yet: nothing happens, nothing raises
