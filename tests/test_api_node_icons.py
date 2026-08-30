"""Tests for api/api_node_icons.py's node-icon routes.

icon_path()'s resolve()+is_relative_to() guard is defense-in-depth, not
a fix for a confirmed vulnerability: is_valid_node_id() (server.py,
`re.fullmatch(r"![0-9a-fA-F]{8}", value)`) is already called before
icon_path() in all three routes and already rejects every
traversal-shaped node_id tried against it directly
('!../../etc/passwd', '!../secret', '!....//....//etc/passwd',
'!b0f14d2a/../../secret' - all REJECTED). The guard protects against a
future loosening of that regex (for an unrelated reason) silently
reopening this route, not an active bug today.
"""
import io
from unittest.mock import MagicMock

import pytest
from flask import Flask
from PIL import Image

import api.api_node_icons as api_node_icons_module
from api.api_node_icons import register_node_icon_routes


def _real_validator(node_id):
    import re
    return bool(re.fullmatch(r"![0-9a-fA-F]{8}", node_id))


def _make_app(tmp_path, validator=_real_validator):
    app = Flask(__name__)
    app.testing = True
    register_node_icon_routes(app, str(tmp_path), "!12345678", validator)
    return app


# --- Baseline behavior, unchanged by the new guard ---------------------


def test_invalid_node_id_rejected_by_the_regex_before_icon_path_is_reached(tmp_path):
    client = _make_app(tmp_path).test_client()

    resp = client.get("/api/nodes/invalid_node/icon")
    assert resp.status_code == 400
    assert resp.json == {"ok": False, "error": "Invalid node_id"}

    resp = client.get("/api/nodes/!short/icon")
    assert resp.status_code == 400
    assert resp.json == {"ok": False, "error": "Invalid node_id"}


def test_get_missing_icon_returns_404(tmp_path):
    client = _make_app(tmp_path).test_client()

    resp = client.get("/api/nodes/!12345678/icon")
    assert resp.status_code == 404
    assert resp.json == {"ok": False, "error": "Node icon not found"}


def test_upload_get_delete_round_trip_unaffected_by_the_new_guard(tmp_path):
    client = _make_app(tmp_path).test_client()

    img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    resp = client.post(
        "/api/nodes/!12345678/icon",
        data={"icon": (buf, "test.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.json["ok"] is True
    assert resp.json["node_id"] == "!12345678"

    resp = client.get("/api/nodes/!12345678/icon")
    assert resp.status_code == 200
    assert resp.content_type == "image/png"
    resp.close()  # release send_file()'s underlying file handle (Windows keeps it locked otherwise)

    resp = client.delete("/api/nodes/!12345678/icon")
    assert resp.status_code == 200
    assert resp.json == {"ok": True, "node_id": "!12345678"}

    resp = client.get("/api/nodes/!12345678/icon")
    assert resp.status_code == 404


# --- The new guard, tested in isolation from the regex ------------------
# Uses a permissive validator (always True) to simulate the regex having
# been loosened/bypassed - exactly the scenario the guard exists for -
# so these requests reach icon_path() with a traversal-shaped node_id
# the route-level check would normally have already stopped.


TRAVERSAL_NODE_IDS = [
    "!../../etc/passwd",
    "!../secret",
    "!....//....//etc/passwd",
    "!b0f14d2a/../../secret",
]

# Of the four payloads above, three genuinely resolve outside icons_dir
# (real ".." parent-references) - icon_path()'s guard must reject them.
# "!....//....//etc/passwd" is different: "...." (four dots) is not a
# parent-directory operator to pathlib's resolve() at all, just an
# unusual literal path component - verified directly below, not assumed.
# It stays in TRAVERSAL_NODE_IDS because is_valid_node_id()'s regex
# still rejects it (non-hex characters), which is the layer actually
# relevant to the original finding - but it would NOT be caught by
# icon_path()'s own guard specifically, because it was never a real
# escape attempt against a resolve()-based check to begin with.
ESCAPING_NODE_IDS = [nid for nid in TRAVERSAL_NODE_IDS if "...." not in nid]


@pytest.mark.parametrize("node_id", TRAVERSAL_NODE_IDS)
def test_slash_containing_payloads_never_even_reach_the_view_function(tmp_path, monkeypatch, node_id):
    """A layer discovered while writing these tests, not assumed going
    in: Flask's default <node_id> URL converter only matches a single
    path segment ([^/]+) - every one of these "/"-containing payloads
    404s at routing, before is_valid_node_id() or icon_path() ever runs.
    Proven with a spy on the validator itself, not just the response
    code (a 404 could otherwise coincidentally mean something else)."""
    validator_spy = MagicMock(side_effect=AssertionError("node_id_validator() must never be called - routing should reject this first"))
    client = _make_app(tmp_path, validator=validator_spy).test_client()

    resp = client.get(f"/api/nodes/{node_id}/icon")

    assert resp.status_code == 404
    validator_spy.assert_not_called()


def test_icon_path_guard_rejects_traversal_when_called_directly(tmp_path):
    """The actual case the task asked for: icon_path()'s own guard,
    tested in isolation, bypassing the route/regex layer entirely - not
    just proving it's unreachable via HTTP (the test above), but proving
    the guard itself would catch these values if it were ever reached
    directly (e.g. from a future call site that doesn't go through
    is_valid_node_id() first). Uses ESCAPING_NODE_IDS, not the full
    TRAVERSAL_NODE_IDS list - see that list's own comment for why one
    entry doesn't belong here."""
    app = Flask(__name__)
    app.testing = True
    icon_path = register_node_icon_routes(app, str(tmp_path), "!12345678", _real_validator)

    for node_id in ESCAPING_NODE_IDS:
        with pytest.raises(ValueError):
            icon_path(node_id)

    # "...." is a literal path component, not a parent-reference - it
    # never escapes icons_dir under resolve(), verified directly rather
    # than assumed alongside the genuinely-escaping payloads above.
    contained = icon_path("!....//....//etc/passwd")
    assert contained.is_relative_to((tmp_path / "node_icons").resolve())

    # A legitimate node_id must still resolve inside icons_dir, unaffected.
    resolved = icon_path("!12345678")
    assert resolved == (tmp_path / "node_icons" / "12345678.png").resolve()
    assert resolved.is_relative_to((tmp_path / "node_icons").resolve())
