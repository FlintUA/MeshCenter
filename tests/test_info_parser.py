"""Tests for meshsrv/info_parser.py (F3) - the shared, string-aware
`meshtastic --info` JSON extraction that replaces server.py's brace-
counting extract_json_block() and the class of bugs it caused (local
metrics read from a neighbour's block; a node name containing '{'/'}'
breaking the whole NodeDB import).
"""

from meshsrv.info_parser import find_json_object, local_node_entry, parse_info_nodes


# ---------------------------------------------------------------------------
# find_json_object() / parse_info_nodes() - string-awareness
# ---------------------------------------------------------------------------

def test_node_name_containing_closing_brace_does_not_truncate_the_block():
    output = (
        'Nodes in mesh: {\n'
        '  "!aaaaaaaa": {"user": {"longName": "Evil }", "shortName": "EVL"}},\n'
        '  "!bbbbbbbb": {"user": {"longName": "Normal Node", "shortName": "NRM"}}\n'
        '}\n'
        'Preferences: {"foo": 1}\n'
    )
    nodes = parse_info_nodes(output)
    assert set(nodes.keys()) == {"!aaaaaaaa", "!bbbbbbbb"}
    assert nodes["!aaaaaaaa"]["user"]["longName"] == "Evil }"
    assert nodes["!bbbbbbbb"]["user"]["longName"] == "Normal Node"


def test_node_name_containing_opening_brace_does_not_desync_depth():
    output = (
        'Nodes in mesh: {\n'
        '  "!aaaaaaaa": {"user": {"longName": "{Bracketed}", "shortName": "BRK"}},\n'
        '  "!bbbbbbbb": {"user": {"longName": "After", "shortName": "AFT"}}\n'
        '}\n'
    )
    nodes = parse_info_nodes(output)
    assert nodes["!aaaaaaaa"]["user"]["longName"] == "{Bracketed}"
    assert nodes["!bbbbbbbb"]["user"]["longName"] == "After"


def test_node_name_containing_quote_and_backslash():
    output = (
        'Nodes in mesh: {\n'
        '  "!aaaaaaaa": {"user": {"longName": "Quo\\"te\\\\Back", "shortName": "QB"}}\n'
        '}\n'
    )
    nodes = parse_info_nodes(output)
    assert nodes["!aaaaaaaa"]["user"]["longName"] == 'Quo"te\\Back'


def test_node_name_with_emoji_and_non_ascii():
    output = (
        'Nodes in mesh: {\n'
        '  "!aaaaaaaa": {"user": {"longName": "Basisstation F\\u00fcrth\\u2618\\ufe0f", "shortName": "BF"}}\n'
        '}\n'
    )
    nodes = parse_info_nodes(output)
    assert nodes["!aaaaaaaa"]["user"]["longName"] == "Basisstation Fürth☘️"


def test_nodes_block_followed_by_preferences_section():
    output = (
        'Nodes in mesh: {"!aaaaaaaa": {"user": {"longName": "A"}}}\n'
        'Preferences: {"positionBroadcastSecs": 900}\n'
        'Channels: {"index": 0}\n'
    )
    nodes = parse_info_nodes(output)
    assert nodes == {"!aaaaaaaa": {"user": {"longName": "A"}}}


def test_nodes_block_followed_by_channels_section_no_preferences():
    output = (
        'Nodes in mesh: {"!aaaaaaaa": {"user": {"longName": "A"}}}\n'
        'Channels: {"index": 0}\n'
    )
    nodes = parse_info_nodes(output)
    assert nodes == {"!aaaaaaaa": {"user": {"longName": "A"}}}


def test_marker_absent_returns_empty_dict():
    assert parse_info_nodes("no nodes-in-mesh marker here at all") == {}


def test_truncated_json_returns_empty_dict_not_an_exception():
    output = 'Nodes in mesh: {"!aaaaaaaa": {"user": {"longName": "Cut off'
    assert parse_info_nodes(output) == {}


def test_ordinary_nested_braces_still_balance_correctly():
    # Moved from tests/test_cli_parsing.py's old
    # test_extract_json_block_balances_nested_braces (server.py's
    # extract_json_block() is removed by F3 - see that module's history).
    text = 'Nodes in mesh: {"a": {"b": 1}, "c": 2} trailing text'
    value = find_json_object(text, "Nodes in mesh:")
    assert value == {"a": {"b": 1}, "c": 2}


def test_empty_string_input():
    assert parse_info_nodes("") == {}
    assert find_json_object("", "marker") is None


def test_marker_present_but_no_opening_brace_after_it():
    assert find_json_object("Nodes in mesh: no brace here", "Nodes in mesh:") is None


def test_json_at_marker_is_not_an_object():
    # A malformed/unexpected shape (a bare array, say) - never raises,
    # and returns None since it isn't a dict.
    assert find_json_object('Nodes in mesh: [1, 2, 3]', "Nodes in mesh:") is None


# ---------------------------------------------------------------------------
# local_node_entry() - node-id case/format handling
# ---------------------------------------------------------------------------

_TWO_NODE_OUTPUT = (
    'Nodes in mesh: {\n'
    '  "!067a40fa": {"user": {"longName": "Local"}, "deviceMetrics": {"voltage": 4.1}},\n'
    '  "!756f9960": {"user": {"longName": "Neighbour"}, "environmentMetrics": {"temperature": 99}}\n'
    '}\n'
)


def test_local_node_entry_exact_case_match():
    entry = local_node_entry(_TWO_NODE_OUTPUT, "!067a40fa")
    assert entry["user"]["longName"] == "Local"


def test_local_node_entry_uppercase_hex_still_matches():
    entry = local_node_entry(_TWO_NODE_OUTPUT, "!067A40FA")
    assert entry["user"]["longName"] == "Local"


def test_local_node_entry_bare_decimal_number_matches():
    # 0x067a40fa == 108675322
    entry = local_node_entry(_TWO_NODE_OUTPUT, "108675322")
    assert entry["user"]["longName"] == "Local"


def test_local_node_entry_never_returns_a_different_node():
    entry = local_node_entry(_TWO_NODE_OUTPUT, "!067a40fa")
    assert "environmentMetrics" not in entry


def test_local_node_entry_missing_node_returns_none():
    assert local_node_entry(_TWO_NODE_OUTPUT, "!ffffffff") is None


def test_local_node_entry_empty_node_id_returns_none():
    assert local_node_entry(_TWO_NODE_OUTPUT, "") is None


def test_local_node_entry_no_marker_returns_none():
    assert local_node_entry("no marker here", "!067a40fa") is None
