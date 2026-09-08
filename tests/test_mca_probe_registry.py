"""Tests for meshsrv/attachments/probe_registry.py (internal-rest-api.md
§3.2/§3.4/§7.11; Execution Plan Step 1.6A.1).

Covers the single-use, in-memory provider-onboarding probe store: the
frozen `ProbeRecord` (rejects a bad status, never prints the raw
`service_public_key`), `mint_probe_id()`'s unpredictability/shape, the
safe §7.9 `provider_probe` result serializer (never leaks the raw key or
`status`), the `add`/`get`/`consume` lifecycle, single-use consume
semantics, non-consuming `get`, TTL expiry and bounded FIFO eviction
(with an injected clock), and restart-amnesia. Pure stdlib - no
Flask/SQLite/network, safe in CI.
"""

import threading

import pytest

from meshsrv.attachments.probe_registry import (
    PROBE_MAX_ENTRIES,
    PROBE_TTL_SECONDS,
    PROBE_STATUS_FAILED,
    PROBE_STATUS_PROBED,
    ProbeRecord,
    ProbeRegistry,
    mint_probe_id,
    serialize_probe_record,
)


class Clock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _record(probe_id="probe-1", *, expires_at=1000.0, status=PROBE_STATUS_PROBED, **overrides):
    base = dict(
        probe_id=probe_id,
        origin="https://relay.example.net",
        provider_id="AbCdEfGhIjK",
        service_public_key=b"0" * 32,
        service_key_fingerprint="a" * 64,
        protocol_version="1",
        max_ciphertext_bytes=5 * 1024 * 1024,
        min_ttl_seconds=60,
        max_ttl_seconds=86400,
        expires_at=expires_at,
        status=status,
    )
    base.update(overrides)
    return ProbeRecord(**base)


def _registry(max_entries=PROBE_MAX_ENTRIES, ttl_seconds=PROBE_TTL_SECONDS):
    clock = Clock()
    return ProbeRegistry(max_entries=max_entries, ttl_seconds=ttl_seconds, now_fn=clock), clock


# --- mint_probe_id ----------------------------------------------------------

def test_mint_probe_id_is_32_hex_and_unique():
    seen = {mint_probe_id() for _ in range(1000)}
    assert len(seen) == 1000
    for pid in seen:
        assert len(pid) == 32
        assert all(ch in "0123456789abcdef" for ch in pid)


# --- ProbeRecord ------------------------------------------------------------

def test_probe_record_is_frozen():
    record = _record()
    with pytest.raises(AttributeError):
        record.origin = "https://evil.example.net"


def test_probe_record_repr_omits_service_public_key():
    record = _record(service_public_key=b"SECRET_KEY_BYTES_1234567890_1234567890")
    text = repr(record)
    assert "SECRET_KEY_BYTES" not in text
    # The fingerprint *is* user-facing (it is what the browser confirms).
    assert record.provider_id in text


def test_probe_record_rejects_bad_status():
    with pytest.raises(ValueError):
        _record(status="frobnicated")


def test_probe_record_rejects_empty_probe_id():
    with pytest.raises(ValueError):
        _record(probe_id="")


# --- serialize_probe_record -------------------------------------------------

def test_serialize_probe_record_omits_raw_key_and_status():
    record = _record()
    out = serialize_probe_record(record)
    assert "service_public_key" not in out
    assert "status" not in out
    assert out["probe_id"] == "probe-1"
    assert out["provider_id"] == "AbCdEfGhIjK"
    assert out["service_key_fingerprint"] == "a" * 64


# --- add / get / consume ----------------------------------------------------

def test_get_returns_none_for_unknown_probe():
    reg, _ = _registry()
    assert reg.get("never-added") is None


def test_add_then_get_returns_the_record():
    reg, _ = _registry()
    record = _record()
    reg.add(record)
    assert reg.get("probe-1") is record


def test_add_duplicate_raises():
    reg, _ = _registry()
    reg.add(_record())
    with pytest.raises(ValueError):
        reg.add(_record())


def test_get_does_not_consume():
    reg, _ = _registry()
    record = _record()
    reg.add(record)
    # get() is non-destructive: it can be read repeatedly for phase-2
    # validation, and a later consume() still succeeds.
    assert reg.get("probe-1") is record
    assert reg.get("probe-1") is record
    assert reg.consume("probe-1") is record


def test_consume_is_single_use():
    reg, _ = _registry()
    reg.add(_record())
    assert reg.consume("probe-1") is not None
    assert reg.consume("probe-1") is None  # already consumed
    assert reg.get("probe-1") is None


def test_consume_unknown_returns_none():
    reg, _ = _registry()
    assert reg.consume("never-added") is None


# --- expiry -----------------------------------------------------------------

def test_expired_record_is_invisible_and_consumable_as_none():
    reg, clock = _registry()
    reg.add(_record(expires_at=100.0))
    clock.advance(101.0)
    assert reg.get("probe-1") is None
    assert reg.consume("probe-1") is None
    assert len(reg) == 0


def test_unexpired_record_is_still_visible():
    reg, clock = _registry()
    reg.add(_record(expires_at=100.0))
    clock.advance(99.0)
    assert reg.get("probe-1") is not None


# --- bounded FIFO eviction --------------------------------------------------

def test_add_beyond_capacity_evicts_oldest():
    reg, _ = _registry(max_entries=3)
    for i in range(3):
        reg.add(_record(probe_id=f"probe-{i}", expires_at=10000.0))
    assert len(reg) == 3
    # A fourth add exceeds capacity -> oldest-inserted (probe-0) is evicted.
    reg.add(_record(probe_id="probe-3", expires_at=10000.0))
    assert reg.get("probe-0") is None
    assert reg.get("probe-1") is not None
    assert reg.get("probe-2") is not None
    assert reg.get("probe-3") is not None
    assert len(reg) == 3


def test_failed_probe_record_is_stored_like_any_other():
    reg, _ = _registry()
    record = _record(status=PROBE_STATUS_FAILED)
    reg.add(record)
    assert reg.get("probe-1").status == PROBE_STATUS_FAILED


# --- restart amnesia --------------------------------------------------------

def test_fresh_registry_is_empty():
    reg, _ = _registry()
    assert len(reg) == 0
    assert reg.get("anything") is None
    assert reg.consume("anything") is None


# --- thread safety ----------------------------------------------------------

def test_concurrent_add_and_consume_is_consistent():
    reg, _ = _registry()
    n = 50
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
        barrier.wait()
        try:
            record = _record(probe_id=f"probe-{i}", expires_at=10000.0)
            reg.add(record)
            got = reg.get(f"probe-{i}")
            assert got is record
            consumed = reg.consume(f"probe-{i}")
            assert consumed is record
            assert reg.consume(f"probe-{i}") is None  # single-use under concurrency
        except Exception as exc:  # pragma: no cover - failure signal
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(reg) == 0  # every record was consumed
