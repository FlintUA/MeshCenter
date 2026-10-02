"""H1-C1 (F-finding): cleanup_seen_ids() used to do
`seen_ids = set(list(seen_ids)[-500:])` to trim a plain set() down once it
grew past 1000 entries. Sets have NO guaranteed iteration order in
CPython - it follows hash bucket layout, not insertion order - so
`list(a_set)[-500:]` does not reliably return the 500 MOST RECENTLY ADDED
elements; it returns an arbitrary 500, which could evict a packet id seen
moments ago while keeping one from hours earlier, silently reopening the
dedup window for an id that should still be suppressed.

server._SeenPacketIds (a dict of pid -> first-seen timestamp, insertion-
ordered in Python 3.7+) replaces the plain set and provides a correct
trim() - tested directly here, independent of the full inbound-ingest
pipeline.
"""

import threading
import time

import pytest


@pytest.fixture
def tracker(server_module):
    return server_module._SeenPacketIds()


def test_add_and_contains(tracker):
    tracker.add(42)
    assert 42 in tracker
    assert 43 not in tracker


def test_len_and_clear(tracker):
    for pid in range(5):
        tracker.add(pid)
    assert len(tracker) == 5
    tracker.clear()
    assert len(tracker) == 0


def test_iteration_yields_pids_in_insertion_order(tracker):
    for pid in (10, 20, 30):
        tracker.add(pid)
    assert list(tracker) == [10, 20, 30]


def test_1200_ids_trimmed_to_500_keeps_the_most_recent_500(tracker):
    """The actual F-finding scenario: unlike the old
    `set(list(seen_ids)[-500:])`, trim() must deterministically keep the
    500 MOST RECENTLY ADDED ids, not an arbitrary 500."""
    for pid in range(1200):
        tracker.add(pid)

    tracker.trim(max_count=500, max_age_seconds=1_000_000)

    assert len(tracker) == 500
    # The 500 most recently added are ids 700..1199 - every one of them
    # must have survived, and nothing older than that.
    for pid in range(700, 1200):
        assert pid in tracker, f"recently-added pid {pid} was incorrectly evicted"
    for pid in range(0, 700):
        assert pid not in tracker, f"stale pid {pid} should have been evicted"


def test_trim_evicts_by_age_even_under_the_count_cap(tracker):
    now = time.time()
    # Monkeypatch-free: directly poke the internal timestamp dict to
    # simulate ids added long ago, since add() always stamps "now".
    tracker.add(1)
    tracker._timestamps[1] = now - 7200  # 2 hours old
    tracker.add(2)  # added "now"

    tracker.trim(max_count=500, max_age_seconds=1800)  # 30 min

    assert 1 not in tracker, "an id older than max_age_seconds must be evicted regardless of count"
    assert 2 in tracker


def test_trim_is_a_noop_when_under_both_limits(tracker):
    tracker.add(1)
    tracker.add(2)
    tracker.trim(max_count=500, max_age_seconds=1800)
    assert len(tracker) == 2
    assert 1 in tracker and 2 in tracker


def test_seen_ids_identity_is_preserved_across_cleanup(server_module, monkeypatch):
    """H1-C1: seen_ids must now be MUTATED, never rebound - this is the
    actual bug class the old `seen_ids = set(list(seen_ids)[-500:])`
    assignment belonged to (same shape as the F2 identity invariant for
    nodes/chats/messages), now enforced for seen_ids too via
    tests/conftest.py's _IDENTITY_INVARIANT_NAMES."""
    original = server_module.seen_ids
    for pid in range(1200):
        original.add(pid)

    with server_module.state_lock:
        server_module.seen_ids.trim(max_count=500, max_age_seconds=1_000_000)

    assert server_module.seen_ids is original
    assert len(server_module.seen_ids) == 500
