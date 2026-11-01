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
    assert imported <= {"__future__", "typing", "meshsrv.attachments"}, imported
