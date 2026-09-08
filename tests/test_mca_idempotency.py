"""Tests for meshsrv/attachments/idempotency.py (internal-rest-api.md
§3.5/§3.6; Execution Plan Step 1.6A.1).

Covers the canonical-hash formula byte-for-byte against the spec, the
deterministic JSON serialization (sorted keys, no whitespace, exactly the
11 semantic fields, literal UTF-8), the two input validators, and the
`PendingReservations` race-closing map - including the §3.6 "real
concurrent threads/barriers" case where N threads attempt to reserve the
same id simultaneously and exactly one wins.

Everything here is pure (no Flask, no SQLite, no filesystem, no network),
so the tests import the module directly and run anywhere, including CI.
"""

import hashlib
import json
import threading

import pytest

from meshsrv.attachments.idempotency import (
    PendingReservation,
    PendingReservations,
    ReservationOutcome,
    build_canonical_json,
    compute_canonical_hash,
    validate_canonical_hash,
    validate_client_request_id,
)


# --- build_canonical_json ---------------------------------------------------

_EXPECTED_FIELDS = frozenset({
    "recipient", "adapter_id", "connector_profile_id", "route_type", "route_id",
    "provider_id", "comment", "hard_ttl_seconds", "download_grace_seconds",
    "source_name", "mime_type",
})


def _sample_kwargs():
    return dict(
        source_address="!067a40fa",
        adapter_id="meshtastic",
        connector_profile_id="meshtastic",
        route_type="DIRECT",
        route_id="!067a40fa",
        provider_id="AbCdEfGhIjK",
        comment="hello world",
        hard_ttl_seconds=259200,
        download_grace_seconds=3600,
        source_name="photo.jpg",
        mime_type="image/jpeg",
    )


def test_build_canonical_json_has_exactly_the_eleven_semantic_fields():
    payload = json.loads(build_canonical_json(**_sample_kwargs()))
    assert set(payload.keys()) == _EXPECTED_FIELDS
    assert payload["recipient"] == {"source_address": "!067a40fa"}


def test_build_canonical_json_is_sorted_compact_and_utf8():
    kwargs = _sample_kwargs()
    kwargs["source_name"] = "café.jpg"  # non-ASCII, must stay literal
    kwargs["comment"] = "nospace"  # a value with a space would legitimately
    # contain one; insignificant *whitespace* (between tokens) is what must
    # be absent, so keep the value itself space-free for this check.
    raw = build_canonical_json(**kwargs)
    text = raw.decode("utf-8")

    # No insignificant whitespace between tokens (a string value may still
    # contain a space; that is not insignificant).
    assert " " not in text
    assert "\n" not in text
    assert "\t" not in text
    # Literal UTF-8, not an escaped surrogate (ensure_ascii=False).
    assert "café.jpg" in text
    assert "\\u00e9" not in text

    # Round-trips to the exact expected object, and matches a hand-built
    # sorted/compact serialization of that same object.
    expected = {
        "recipient": {"source_address": kwargs["source_address"]},
        "adapter_id": kwargs["adapter_id"],
        "connector_profile_id": kwargs["connector_profile_id"],
        "route_type": kwargs["route_type"],
        "route_id": kwargs["route_id"],
        "provider_id": kwargs["provider_id"],
        "comment": kwargs["comment"],
        "hard_ttl_seconds": kwargs["hard_ttl_seconds"],
        "download_grace_seconds": kwargs["download_grace_seconds"],
        "source_name": kwargs["source_name"],
        "mime_type": kwargs["mime_type"],
    }
    assert raw == json.dumps(
        expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def test_build_canonical_json_is_order_independent():
    # Keyword-argument order cannot vary here, but the *caller* may pass
    # the same logical values as the same kwargs in a different order; the
    # serialization must not depend on any insertion order. Since Python
    # dict order is insertion order, assert that the output is sorted by
    # key by checking the first key is lexicographically smallest.
    text = build_canonical_json(**_sample_kwargs()).decode("utf-8")
    assert text.startswith('{"adapter_id":')  # 'a' < all other keys


def test_build_canonical_json_null_comment_is_preserved():
    kwargs = _sample_kwargs()
    kwargs["comment"] = None
    payload = json.loads(build_canonical_json(**kwargs))
    assert payload["comment"] is None
    assert '"comment":null' in build_canonical_json(**kwargs).decode("utf-8")


# --- compute_canonical_hash -------------------------------------------------

def test_compute_canonical_hash_matches_the_spec_formula_byte_for_byte():
    file_sha256 = "a" * 64  # a plausible 64-hex digest
    canonical = build_canonical_json(**_sample_kwargs())
    expected = hashlib.sha256(
        b"MCA-IDEMPOTENCY-v1\0" + file_sha256.encode("ascii") + canonical
    ).hexdigest()
    assert compute_canonical_hash(file_sha256, canonical) == expected


def test_compute_canonical_hash_is_deterministic_and_sensitive_to_every_input():
    kwargs = _sample_kwargs()
    canonical = build_canonical_json(**kwargs)
    digest = compute_canonical_hash("0" * 64, canonical)

    # Same inputs -> same digest.
    assert compute_canonical_hash("0" * 64, canonical) == digest
    # Different file bytes -> different digest.
    assert compute_canonical_hash("f" * 64, canonical) != digest
    # Different canonical content (any one field) -> different digest.
    other = dict(kwargs, comment="different")
    assert compute_canonical_hash("0" * 64, build_canonical_json(**other)) != digest


def test_compute_canonical_hash_is_64_lowercase_hex():
    digest = compute_canonical_hash("0" * 64, build_canonical_json(**_sample_kwargs()))
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


# --- validators -------------------------------------------------------------

@pytest.mark.parametrize("valid", [
    "a", "A", "0", "_", "-", "abc123_-XYZ", "x" * 64, "0" * 1,
])
def test_validate_client_request_id_accepts_legal_values(valid):
    validate_client_request_id(valid)  # must not raise


@pytest.mark.parametrize("invalid", [
    "", " ", "has space", "slash/", "dot.", "trailing\n", "x" * 65, "café", None, 123,
])
def test_validate_client_request_id_rejects_illegal_values(invalid):
    with pytest.raises(ValueError):
        validate_client_request_id(invalid)


def test_validate_canonical_hash_accepts_64_lowercase_hex():
    validate_canonical_hash("a" * 64)  # must not raise
    validate_canonical_hash("0123456789abcdef" * 4)


@pytest.mark.parametrize("invalid", [
    "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "0" * 64 + "\n", None, 123,
])
def test_validate_canonical_hash_rejects_illegal_values(invalid):
    with pytest.raises(ValueError):
        validate_canonical_hash(invalid)


# --- PendingReservations ----------------------------------------------------

def _res(client_request_id="req-1", canonical="a" * 64, attachment="att-1", command="cmd-1"):
    return PendingReservation(
        canonical_hash=canonical, attachment_id=attachment, command_id=command,
    )


def test_reserve_fresh_then_replay_pending_then_replay_committed():
    r = PendingReservations()
    outcome = r.reserve("req-1", _res(), committed_hashes={})
    assert outcome.kind == "fresh"
    assert outcome.reservation.attachment_id == "att-1"

    # Same id + same hash while still pending -> replay_pending, same ids.
    replay = r.reserve("req-1", _res(), committed_hashes={})
    assert replay.kind == "replay_pending"
    assert replay.reservation.attachment_id == "att-1"

    # After the worker commits (reservation removed, id now in the
    # committed index), same id + same hash -> replay_committed.
    r.remove("req-1")
    committed = r.reserve("req-1", _res(), committed_hashes={"req-1": "a" * 64})
    assert committed.kind == "replay_committed"
    assert committed.existing_hash == "a" * 64


def test_reserve_conflict_when_same_id_but_different_hash():
    r = PendingReservations()
    r.reserve("req-1", _res(canonical="a" * 64), committed_hashes={})

    # Different canonical content, same id, still pending -> conflict.
    outcome = r.reserve("req-1", _res(canonical="b" * 64), committed_hashes={})
    assert outcome.kind == "conflict"
    assert outcome.existing_hash == "a" * 64

    # Same id, different hash against a *committed* entry -> conflict too.
    r.remove("req-1")
    outcome = r.reserve("req-1", _res(canonical="b" * 64), committed_hashes={"req-1": "a" * 64})
    assert outcome.kind == "conflict"


def test_remove_drops_only_the_named_reservation():
    r = PendingReservations()
    r.reserve("req-1", _res(), committed_hashes={})
    r.reserve("req-2", _res(), committed_hashes={})
    r.remove("req-1")
    assert r.get("req-1") is None
    assert r.get("req-2") is not None
    assert len(r) == 1


def test_snapshot_ids_is_a_copy():
    r = PendingReservations()
    r.reserve("req-1", _res(), committed_hashes={})
    snap = r.snapshot_ids()
    snap.clear()
    assert r.get("req-1") is not None  # original unaffected


def test_concurrent_same_id_same_hash_exactly_one_fresh():
    # §3.6: real concurrent threads with a barrier - N threads race to
    # reserve the same id with the same hash; exactly one observes "fresh"
    # and the rest observe "replay_pending" with the winner's ids.
    r = PendingReservations()
    n = 32
    barrier = threading.Barrier(n)
    outcomes = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        out = r.reserve("req-1", _res(), committed_hashes={})
        with lock:
            outcomes.append(out)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    fresh = [o for o in outcomes if o.kind == "fresh"]
    replays = [o for o in outcomes if o.kind == "replay_pending"]
    assert len(fresh) == 1
    assert len(replays) == n - 1
    # Every replay hands back the winner's exact reservation.
    assert all(o.reservation.attachment_id == "att-1" for o in replays)
    assert len(r) == 1


def test_concurrent_same_id_different_hash_one_fresh_rest_not_fresh():
    # N threads, some with one hash and some with another, racing on the
    # same id: exactly one reserves fresh, and every other thread is
    # classified by whether its hash matches the winner's - same hash
    # -> replay_pending, different hash -> conflict. There is never a
    # second fresh reservation and never a silent overwrite. (Which thread
    # wins is non-deterministic, so the assertion is over the *counts* and
    # the invariant, not the winner's identity.)
    r = PendingReservations()
    n = 16
    half = n // 2
    barrier = threading.Barrier(n)
    outcomes = []
    lock = threading.Lock()

    def worker(idx):
        barrier.wait()
        canonical = "a" * 64 if idx < half else "b" * 64
        out = r.reserve("req-1", _res(canonical=canonical), committed_hashes={})
        with lock:
            outcomes.append(out)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    fresh = [o for o in outcomes if o.kind == "fresh"]
    replays = [o for o in outcomes if o.kind == "replay_pending"]
    conflicts = [o for o in outcomes if o.kind == "conflict"]

    assert len(fresh) == 1
    assert len(replays) + len(conflicts) == n - 1
    # The winner's hash fully determines the loser split: the half that
    # shares the winner's hash replays, the other half conflicts.
    winner_hash = fresh[0].reservation.canonical_hash
    assert len(replays) == (half - 1) if winner_hash == "a" * 64 else (half)
    assert all(o.existing_hash == winner_hash for o in conflicts)
    assert len(r) == 1
