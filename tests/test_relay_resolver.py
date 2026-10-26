"""SIG4: meshsrv/relay_resolver.py - the last relay of a packet from the low byte of its node number."""
import meshsrv.relay_resolver as rr

NOW = 1_000_000.0
SENDER = "!11111122"          # low byte 0x22


def table(**entries):
    return entries


def test_low_byte_of_a_node_id():
    assert rr.low_byte("!aabbccdd") == 0xDD
    assert rr.low_byte("!AABBCCDD") == 0xDD
    for bad in (None, "", "aabbccdd", "!abc", "!zzzzzzzz", 5):
        assert rr.low_byte(bad) is None


def test_heard_directly_needs_equal_hops_and_a_known_start():
    assert rr.is_direct(3, 3) is True
    assert rr.is_direct(3, 2) is False
    assert rr.is_direct(0, 0) is False, "hop_start 0 is 'unknown' on older firmware, not 'no hops'"
    assert rr.is_direct(None, 3) is False and rr.is_direct("x", 3) is False


def test_note_direct_only_stamps_a_direct_packet():
    node = {}
    rr.note_direct(node, 3, 2, NOW)
    assert "last_direct" not in node
    rr.note_direct(node, 3, 3, NOW)
    assert node["last_direct"] == NOW


def test_no_relay_byte_means_nothing_to_say():
    for byte in (None, 0, 256, "x", -1):
        assert rr.resolve_relay(byte, sender_id=SENDER, hop_start=3, hop_limit=1, nodes={}, now=NOW) is None


def test_a_direct_packet_is_relayed_by_its_sender():
    nodes = table(**{SENDER: {"short_name": "SEND"}})
    result = rr.resolve_relay(0x22, sender_id=SENDER, hop_start=3, hop_limit=3, nodes=nodes, now=NOW)
    assert result == {"status": "direct", "node_id": SENDER, "short_name": "SEND", "basis": "direct"}


def test_one_directly_heard_candidate_is_resolved():
    nodes = table(**{
        "!aaaaaa84": {"short_name": "FMG", "last_direct": NOW - 3600, "last_seen": NOW - 3600},
        "!bbbbbb84": {"short_name": "OLD", "last_direct": NOW - 3 * 86400, "last_seen": NOW - 3 * 86400},  # too old for 24 h
        "!ccccccaa": {"short_name": "OTHER", "last_direct": NOW - 60},                                     # other byte
    })
    result = rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=0, nodes=nodes, now=NOW)
    assert result["status"] == "resolved" and result["node_id"] == "!aaaaaa84" and result["basis"] == "direct24h"
    assert result["short_name"] == "FMG"


def test_several_directly_heard_candidates_are_ambiguous_most_recent_first():
    nodes = table(**{
        "!aaaaaa84": {"short_name": "A", "last_direct": NOW - 7200},
        "!bbbbbb84": {"short_name": "B", "last_direct": NOW - 60},
        "!ccccc184": {"short_name": "C", "last_direct": NOW - 3600},
    })
    result = rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=nodes, now=NOW)
    assert result["status"] == "ambiguous" and result["count"] == 3
    assert [c["short_name"] for c in result["candidates"]] == ["B", "C", "A"]


def test_a_node_heard_only_through_relays_is_not_a_direct_candidate():
    nodes = table(**{"!aaaaaa84": {"short_name": "FAR", "last_seen": NOW - 60}})  # never heard directly
    # no direct candidate: the recent-window rule decides
    result = rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=nodes, now=NOW)
    assert result["status"] == "resolved" and result["basis"] == "recent2h"


def test_the_recent_window_is_only_a_fallback_and_two_hours_long():
    nodes = table(**{
        "!aaaaaa84": {"short_name": "A", "last_seen": NOW - 3600},
        "!bbbbbb84": {"short_name": "B", "last_seen": NOW - 3 * 3600},   # outside 2 h
    })
    assert rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=nodes, now=NOW)["node_id"] == "!aaaaaa84"
    both = table(**{"!aaaaaa84": {"last_seen": NOW - 60}, "!bbbbbb84": {"last_seen": NOW - 120}})
    assert rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=both, now=NOW)["status"] == "ambiguous"
    # a directly heard candidate beats any number of merely recent ones
    mixed = table(**{"!aaaaaa84": {"last_seen": NOW - 60}, "!bbbbbb84": {"last_seen": NOW - 120},
                     "!cccccc84": {"short_name": "NEAR", "last_direct": NOW - 5000, "last_seen": NOW - 5000}})
    result = rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=mixed, now=NOW)
    assert result["status"] == "resolved" and result["short_name"] == "NEAR" and result["basis"] == "direct24h"


def test_a_byte_nobody_matches_is_unknown():
    nodes = table(**{"!aaaaaa11": {"last_direct": NOW}})
    assert rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=nodes, now=NOW) == {"status": "unknown", "basis": "none"}


def test_neither_the_sender_nor_our_own_node_is_a_relay_candidate():
    me = "!99999984"
    nodes = table(**{SENDER: {"last_direct": NOW}, me: {"last_direct": NOW}, "!aaaaaa22": {"short_name": "REAL", "last_direct": NOW - 10}})
    result = rr.resolve_relay(0x22, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=nodes, local_node_id=me, now=NOW)
    assert result["status"] == "resolved" and result["node_id"] == "!aaaaaa22", "the sender cannot be the last relay of a relayed packet"
    nodes2 = table(**{me: {"last_direct": NOW}})
    assert rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=nodes2, local_node_id=me, now=NOW)["status"] == "unknown"


def test_a_long_candidate_list_is_capped():
    nodes = {f"!aaaa{i:02x}84": {"short_name": f"N{i}", "last_direct": NOW - i} for i in range(9)}
    result = rr.resolve_relay(0x84, sender_id=SENDER, hop_start=4, hop_limit=1, nodes=nodes, now=NOW)
    assert result["status"] == "ambiguous" and result["count"] == 9 and len(result["candidates"]) == rr.MAX_LISTED
