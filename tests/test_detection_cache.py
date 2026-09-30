"""Tests for meshsrv/detection_cache.py - the server-side cache that makes
Discovery -> Accept one probe instead of two (TCP lifecycle P0, PR-B).
Trust properties under test: single-use, TTL-bounded, node_id must match,
size-bounded, never populated by anything but put() (server-side)."""
from meshsrv.detection_cache import DetectionCache


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _cache(**kw):
    clock = _Clock()
    return DetectionCache(clock=clock, **kw), clock


DETECTED = {"node_id": "!1fa065f0", "long_name": "T-Beam"}


def test_hit_returns_the_detection_and_is_single_use():
    cache, _ = _cache()
    cache.put("192.168.2.34", 4403, DETECTED, "2026-09-25T10:00:00+00:00")

    first = cache.pop_matching("192.168.2.34", 4403, "!1fa065f0")
    second = cache.pop_matching("192.168.2.34", 4403, "!1fa065f0")

    assert first == {"detected": DETECTED, "checked_at": "2026-09-25T10:00:00+00:00"}
    assert second is None, "a second Accept must never replay the same detection"


def test_expired_entry_is_a_miss():
    cache, clock = _cache(ttl_s=60)
    cache.put("h", 4403, DETECTED, "t")
    clock.now += 61

    assert cache.pop_matching("h", 4403, "") is None


def test_entry_is_still_valid_just_inside_the_ttl():
    cache, clock = _cache(ttl_s=60)
    cache.put("h", 4403, DETECTED, "t")
    clock.now += 59

    assert cache.pop_matching("h", 4403, "") is not None


def test_mismatching_node_id_is_a_miss_and_drops_the_entry():
    """The client says it's confirming a different radio than the one we
    detected: the cached detection no longer describes what's being
    confirmed, so it must not be used - and must not linger either."""
    cache, _ = _cache()
    cache.put("h", 4403, DETECTED, "t")

    assert cache.pop_matching("h", 4403, "!deadbeef") is None
    assert cache.pop_matching("h", 4403, "!1fa065f0") is None


def test_empty_requested_node_id_matches_any_cached_entry():
    cache, _ = _cache()
    cache.put("h", 4403, DETECTED, "t")

    assert cache.pop_matching("h", 4403, "") is not None


def test_node_id_comparison_ignores_case_and_whitespace():
    cache, _ = _cache()
    cache.put("h", 4403, DETECTED, "t")

    assert cache.pop_matching("h", 4403, "  !1FA065F0 ") is not None


def test_a_client_cannot_create_a_hit_by_claiming_a_node_id():
    """Nothing but put() (called server-side after a real probe) can create
    an entry - a claimed node_id for an endpoint never probed is a miss."""
    cache, _ = _cache()

    assert cache.pop_matching("never-probed", 4403, "!1fa065f0") is None


def test_keys_are_per_endpoint_and_host_case_insensitive():
    cache, _ = _cache()
    cache.put("Radio.Local", 4403, DETECTED, "t")

    assert cache.pop_matching("192.168.2.99", 4403, "") is None
    assert cache.pop_matching("radio.local", 4404, "") is None
    assert cache.pop_matching("radio.local", 4403, "") is not None


def test_entry_without_a_node_id_is_never_stored():
    cache, _ = _cache()
    cache.put("h", 4403, {"node_id": "", "long_name": "x"}, "t")

    assert cache.pop_matching("h", 4403, "") is None


def test_size_is_bounded_and_oldest_entries_are_evicted():
    cache, clock = _cache(max_entries=3)
    for i in range(5):
        clock.now += 1
        cache.put(f"h{i}", 4403, {"node_id": f"!0000000{i}"}, "t")

    assert cache.pop_matching("h0", 4403, "") is None
    assert cache.pop_matching("h1", 4403, "") is None
    assert cache.pop_matching("h4", 4403, "") is not None
