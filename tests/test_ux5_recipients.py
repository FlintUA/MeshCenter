"""UX5: the settings the recipient picker remembers, and the short name the node list now carries for its search."""
from api.api_settings import _deep_merge, normalize_settings


def test_send_recipients_defaults_and_normalization():
    default = {"favorites_only_desktop": None, "favorites_only_mobile": None, "recent": []}
    assert normalize_settings({})["send_recipients"] == default
    assert normalize_settings({"send_recipients": "x"})["send_recipients"] == default
    got = normalize_settings({"send_recipients": {
        "favorites_only_desktop": True, "favorites_only_mobile": "yes",
        "recent": ["!067A40FA", "!067a40fa", "!756f9960", "not-an-id", 7, None, "!b0f14d2a"],
    }})["send_recipients"]
    assert got["favorites_only_desktop"] is True
    assert got["favorites_only_mobile"] is None, "only a real boolean is kept; anything else means 'not chosen'"
    assert got["recent"] == ["!067a40fa", "!756f9960", "!b0f14d2a"], "ids lower-cased, deduplicated, junk dropped"


def test_recent_recipients_are_capped_at_eight():
    ids = ["!%08x" % (0x10000000 + i) for i in range(20)]
    assert normalize_settings({"send_recipients": {"recent": ids}})["send_recipients"]["recent"] == ids[:8]


def test_a_device_choice_does_not_touch_the_other_device_or_the_recents():
    stored = normalize_settings({"send_recipients": {"favorites_only_desktop": True, "favorites_only_mobile": False, "recent": ["!067a40fa"]}})
    after_phone = normalize_settings(_deep_merge(stored, {"send_recipients": {"favorites_only_mobile": True}}))["send_recipients"]
    assert after_phone == {"favorites_only_desktop": True, "favorites_only_mobile": True, "recent": ["!067a40fa"]}
    after_use = normalize_settings(_deep_merge(stored, {"send_recipients": {"recent": ["!756f9960", "!067a40fa"]}}))["send_recipients"]
    assert after_use["recent"] == ["!756f9960", "!067a40fa"] and after_use["favorites_only_mobile"] is False
    reset = normalize_settings(_deep_merge(stored, {"send_recipients": {"favorites_only_desktop": None}}))["send_recipients"]
    assert reset["favorites_only_desktop"] is None, "null puts a device back to 'the picker decides'"


def test_the_node_list_of_the_target_store_carries_the_short_name(server_module):
    server = server_module
    with server.state_lock:
        server.nodes["!deadbeef"] = {"name": "Long Name Node", "short_name": "LNN", "favorite": True, "last_seen": 1}
    try:
        body = server.app.test_client().get("/api/nodes_management").get_json()
    finally:
        with server.state_lock:
            server.nodes.pop("!deadbeef", None)
    row = next(n for n in body["nodes"] if n["node_id"] == "!deadbeef")
    assert row["short_name"] == "LNN" and row["name"] == "Long Name Node" and row["favorite"] is True
