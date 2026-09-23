"""Tests for adapters/meshtastic/_json_safe.py's json_safe() - live-caught
bug: the installed meshtastic library injects a raw, non-JSON-serializable
protobuf message object under a "raw" key inside a received POSITION_APP
packet's decoded dict (see meshtastic/__init__.py's
_handlePacketFromRadio/_onPositionReceive), which then flowed untouched
into NodeInfo.position and crashed the whole adapter subprocess when
adapters/meshtastic/ipc_server.py's serve_forever() tried to json.dumps()
the IPC response. json_safe() is the fix, applied where NodeInfo.position
is built in each of the three transports - see
tests/test_transport_position_sanitization.py for that integration-level
coverage; this file is just json_safe() itself, in isolation.

Lives inside adapters/meshtastic/ rather than utils/helpers.py -
scripts/build-release.sh packages the Meshtastic adapter as a standalone
archive against an explicit whitelist that does not include utils/, and
nothing outside this package needs json_safe() - see that module's own
docstring for the full reasoning (a real CI catch: the release-build
smoke test failed with ModuleNotFoundError: No module named 'utils' when
this lived there).
"""
import json

from adapters.meshtastic._json_safe import json_safe


class _NotJsonSerializable:
    """Stand-in for a protobuf Message object (or anything else
    json.dumps() can't handle) - doesn't need to actually be a protobuf
    type, just needs to not be one of the JSON-primitive/container types
    json_safe() recognizes."""

    def __repr__(self):
        return "<_NotJsonSerializable>"


def test_json_safe_passes_through_plain_scalars_and_none():
    assert json_safe(None) is None
    assert json_safe("hello") == "hello"
    assert json_safe(42) == 42
    assert json_safe(3.14) == 3.14
    assert json_safe(True) is True


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
    sanitized = json_safe(position)
    assert sanitized == position
    assert json.dumps(sanitized)  # must not raise


def test_json_safe_drops_a_raw_protobuf_object_nested_under_a_dict_key():
    position = {
        "latitudeI": 507654321,
        "longitudeI": 303456789,
        "raw": _NotJsonSerializable(),
    }
    sanitized = json_safe(position)
    assert sanitized == {"latitudeI": 507654321, "longitudeI": 303456789}
    assert "raw" not in sanitized
    assert json.dumps(sanitized)  # must not raise


def test_json_safe_drops_unsafe_items_inside_a_list_but_keeps_safe_ones():
    # An unsafe list item is omitted entirely (not replaced with None);
    # an unsafe value nested inside a dict item is likewise just omitted
    # from that dict, per json_safe()'s own documented "omitted, not
    # replaced" behavior for containers.
    values = [1, "ok", _NotJsonSerializable(), {"nested": _NotJsonSerializable()}]
    sanitized = json_safe(values)
    assert sanitized == [1, "ok", {}]
    assert json.dumps(sanitized)


def test_json_safe_of_an_unrepresentable_top_level_value_is_none():
    assert json_safe(_NotJsonSerializable()) is None


def test_json_safe_of_none_position_stays_none():
    # data.get("position") legitimately returns None for a node with no
    # position at all - must not turn that into {} or crash.
    assert json_safe(None) is None
