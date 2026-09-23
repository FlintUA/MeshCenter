"""Tests for utils/helpers.py's get_device_model() - added so the system
log's reboot/shutdown labels no longer hardcode "Raspberry Pi" (that string
was wrong on non-Pi hosts, e.g. Droidian phones - see api/api_system.py's
execute_system_action()/api_system_info(), both of which now call this
function instead of reading /proc/device-tree/model themselves).
"""

import builtins
import json

import pytest

import utils.helpers as helpers


@pytest.fixture(autouse=True)
def _reset_cache():
    # get_device_model() caches in a module-level global - reset it around
    # every test so tests don't leak state into each other.
    helpers._device_model_cache = None
    yield
    helpers._device_model_cache = None


def test_reads_and_strips_null_terminator_from_device_tree_model(monkeypatch):
    # /proc/device-tree/model is conventionally null-terminated - this is a
    # regression guard for a real bug found while writing this function:
    # the code this replaced (api/api_system.py's old inline read) searched
    # for the *literal 4-character string* "\x00" (double-escaped in that
    # source) instead of the actual null byte, so a real trailing null was
    # never actually stripped there.
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/device-tree/model":
            class FakeFile:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return "Raspberry Pi 4 Model B Rev 1.4\x00"
            return FakeFile()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert helpers.get_device_model() == "Raspberry Pi 4 Model B Rev 1.4"


def test_falls_back_to_platform_node_when_device_tree_missing(monkeypatch):
    def fake_open(path, *args, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr("platform.node", lambda: "some-hostname")
    assert helpers.get_device_model() == "some-hostname"


def test_returns_empty_string_when_nothing_is_available(monkeypatch):
    def fake_open(path, *args, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr("platform.node", lambda: "")
    assert helpers.get_device_model() == ""


def test_caches_result_across_calls(monkeypatch):
    call_count = 0
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        nonlocal call_count
        if path == "/proc/device-tree/model":
            call_count += 1
            class FakeFile:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return "Some Board\x00"
            return FakeFile()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    first = helpers.get_device_model()
    second = helpers.get_device_model()
    assert first == second == "Some Board"
    assert call_count == 1


# ---------------------------------------------------------------------------
# json_safe() - live-caught bug: the installed meshtastic library injects a
# raw, non-JSON-serializable protobuf message object under a "raw" key
# inside a received POSITION_APP packet's decoded dict (see
# meshtastic/__init__.py's _handlePacketFromRadio/_onPositionReceive), which
# then flowed untouched into NodeInfo.position and crashed the whole
# adapter subprocess when adapters/meshtastic/ipc_server.py's serve_forever()
# tried to json.dumps() the IPC response. json_safe() is the fix, applied
# where NodeInfo.position is built in each of the three transports.
# ---------------------------------------------------------------------------

class _NotJsonSerializable:
    """Stand-in for a protobuf Message object (or anything else
    json.dumps() can't handle) - doesn't need to actually be a protobuf
    type, just needs to not be one of the JSON-primitive/container types
    json_safe() recognizes."""

    def __repr__(self):
        return "<_NotJsonSerializable>"


def test_json_safe_passes_through_plain_scalars_and_none():
    assert helpers.json_safe(None) is None
    assert helpers.json_safe("hello") == "hello"
    assert helpers.json_safe(42) == 42
    assert helpers.json_safe(3.14) == 3.14
    assert helpers.json_safe(True) is True


def test_json_safe_leaves_a_normal_position_dict_completely_unchanged():
    # The common case - real MessageToDict() output has none of the "raw"
    # contamination, must survive sanitization byte-for-byte.
    position = {
        "latitudeI": 507654321,
        "longitudeI": 303456789,
        "altitude": 123,
        "time": 1758000000,
        "precisionBits": 32,
        "latitude": 50.7654321,
        "longitude": 30.3456789,
    }
    sanitized = helpers.json_safe(position)
    assert sanitized == position
    assert json.dumps(sanitized)  # must not raise


def test_json_safe_drops_a_raw_protobuf_object_nested_under_a_dict_key():
    position = {
        "latitudeI": 507654321,
        "longitudeI": 303456789,
        "raw": _NotJsonSerializable(),
    }
    sanitized = helpers.json_safe(position)
    assert sanitized == {"latitudeI": 507654321, "longitudeI": 303456789}
    assert "raw" not in sanitized
    assert json.dumps(sanitized)  # must not raise


def test_json_safe_drops_unsafe_items_inside_a_list_but_keeps_safe_ones():
    # An unsafe list item is omitted entirely (not replaced with None);
    # an unsafe value nested inside a dict item is likewise just omitted
    # from that dict, per json_safe()'s own documented "omitted, not
    # replaced" behavior for containers.
    values = [1, "ok", _NotJsonSerializable(), {"nested": _NotJsonSerializable()}]
    sanitized = helpers.json_safe(values)
    assert sanitized == [1, "ok", {}]
    assert json.dumps(sanitized)


def test_json_safe_of_an_unrepresentable_top_level_value_is_none():
    assert helpers.json_safe(_NotJsonSerializable()) is None


def test_json_safe_of_none_position_stays_none():
    # data.get("position") legitimately returns None for a node with no
    # position at all - must not turn that into {} or crash.
    assert helpers.json_safe(None) is None
