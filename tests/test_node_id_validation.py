"""Tests for node ID format validation - server.py's own
is_valid_node_id()/is_valid_chat_id()/normalize_node_id(), the ones actually
wired into every route.

utils/helpers.py used to have a second, textually similar but looser
is_valid_node_id() that nothing imported - it was removed (see that file's
git history) once confirmed unused; only utils.helpers.now() is actually
used elsewhere, by telemetry.py. The test that documented the divergence
between the two copies (test_is_valid_node_id_utils_helpers_is_looser) went
with it, since there's no longer a second copy to diverge from.
"""


def test_is_valid_node_id_accepts_well_formed_ids(server_module):
    assert server_module.is_valid_node_id("!aabbccdd") is True
    assert server_module.is_valid_node_id("!00000000") is True
    assert server_module.is_valid_node_id("!ABCDEF12") is True


def test_is_valid_node_id_rejects_malformed_ids(server_module):
    assert server_module.is_valid_node_id("aabbccdd") is False  # missing "!"
    assert server_module.is_valid_node_id("!aabbcc") is False  # too short
    assert server_module.is_valid_node_id("!aabbccddee") is False  # too long
    assert server_module.is_valid_node_id("!aabbccdg") is False  # non-hex char
    assert server_module.is_valid_node_id("!aabb ccdd") is False  # embedded space
    assert server_module.is_valid_node_id("") is False
    assert server_module.is_valid_node_id(None) is False


def test_is_valid_node_id_rejects_injection_attempts(server_module):
    # The strict full-match regex is what stands between free-form input
    # reaching a node-id-shaped code path (file paths, CLI args, etc.) - a
    # few adversarial shapes worth pinning down explicitly.
    assert server_module.is_valid_node_id("!aabbccdd; rm -rf /") is False
    assert server_module.is_valid_node_id("!aabbccdd/../../etc/passwd") is False
    assert server_module.is_valid_node_id("!aabbccdd\x00") is False


def test_is_valid_chat_id_accepts_channel_and_node_shapes(server_module):
    assert server_module.is_valid_chat_id("channel") is True  # CHANNEL_CHAT_ID
    assert server_module.is_valid_chat_id("channel:0") is True
    assert server_module.is_valid_chat_id("channel:7") is True
    assert server_module.is_valid_chat_id("!aabbccdd") is True


def test_is_valid_chat_id_rejects_out_of_range_channel_index(server_module):
    assert server_module.is_valid_chat_id("channel:8") is False
    assert server_module.is_valid_chat_id("channel:-1") is False
    assert server_module.is_valid_chat_id("channel:") is False


def test_normalize_node_id_accepts_bare_hex(server_module):
    assert server_module.normalize_node_id("aabbccdd") == "!aabbccdd"


def test_normalize_node_id_passes_through_well_formed_id(server_module):
    assert server_module.normalize_node_id("!aabbccdd") == "!aabbccdd"


def test_normalize_node_id_returns_none_for_empty_input(server_module):
    assert server_module.normalize_node_id("") is None
    assert server_module.normalize_node_id(None) is None
