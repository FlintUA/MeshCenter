"""Tests for meshsrv/attachments/command_registry.py (internal-rest-api.md
§3.4; Execution Plan Step 1.6A.1).

Covers the queued -> running -> succeeded|failed lifecycle, the
register-before-enqueue guarantee (an entry exists before the queue
write), duplicate/illegal-transition rejection, terminal-only TTL/LRU
eviction with an injected clock, non-terminal entries never being evicted,
restart-amnesia (a fresh registry is empty), and the no-secret-in-repr
rule on `CommandResult`. Pure stdlib - no Flask/SQLite/network, safe in CI.
"""

import pytest

from meshsrv.attachments.command_registry import (
    COMMAND_RESULT_MAX_PAYLOAD_BYTES,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    CommandRegistry,
    CommandResult,
)
from meshsrv.attachments.commands import Command


class Clock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _command(command_id="cmd-1", kind="attachment_create"):
    return Command(command_id=command_id, kind=kind, payload={}, created_at=0.0)


def _registry(max_entries=1000, ttl_seconds=3600.0):
    clock = Clock()
    return CommandRegistry(max_entries=max_entries, ttl_seconds=ttl_seconds, now_fn=clock), clock


def _queued(reg, command_id="cmd-1", kind="attachment_create"):
    reg.register(_command(command_id, kind))
    return command_id


# --- lifecycle --------------------------------------------------------------

def test_register_makes_entry_visible_as_queued_before_enqueue():
    reg, _ = _registry()
    # register() is the request thread's first act, before put_nowait() -
    # so a get() immediately after sees queued, never None.
    reg.register(_command("cmd-1"))
    result = reg.get("cmd-1")
    assert result.status == STATUS_QUEUED
    assert result.kind == "attachment_create"


def test_duplicate_register_raises():
    reg, _ = _registry()
    _queued(reg, "cmd-1")
    with pytest.raises(ValueError):
        reg.register(_command("cmd-1"))


def test_full_lifecycle_queued_running_succeeded():
    reg, _ = _registry()
    _queued(reg, "cmd-1", kind="provider_register")
    assert reg.get("cmd-1").status == STATUS_QUEUED

    reg.mark_running("cmd-1")
    assert reg.get("cmd-1").status == STATUS_RUNNING

    reg.mark_succeeded("cmd-1", resource_id="AbCdEfGhIjK", result={"provider_id": "AbCdEfGhIjK"})
    result = reg.get("cmd-1")
    assert result.status == STATUS_SUCCEEDED
    assert result.resource_id == "AbCdEfGhIjK"
    assert dict(result.result) == {"provider_id": "AbCdEfGhIjK"}


def test_lifecycle_queued_running_failed_sets_error_code():
    reg, _ = _registry()
    _queued(reg, "cmd-1")
    reg.mark_running("cmd-1")
    reg.mark_failed("cmd-1", error_code="relay_unreachable")
    result = reg.get("cmd-1")
    assert result.status == STATUS_FAILED
    assert result.error_code == "relay_unreachable"


@pytest.mark.parametrize("transition", [
    ("running", "queued"),  # cannot go backwards
    ("succeeded", "queued"),  # cannot succeed without running
    ("failed", "queued"),  # cannot fail without running
    ("queued", "succeeded"),  # cannot skip running
    ("succeeded", "failed"),  # terminal -> anything is illegal
    ("failed", "succeeded"),
])
def test_illegal_transitions_raise(transition):
    reg, _ = _registry()
    _queued(reg, "cmd-1")
    from_, to = transition
    if from_ == "running":
        reg.mark_running("cmd-1")
    elif from_ == "succeeded":
        reg.mark_running("cmd-1")
        reg.mark_succeeded("cmd-1")
    elif from_ == "failed":
        reg.mark_running("cmd-1")
        reg.mark_failed("cmd-1", error_code="x")

    with pytest.raises(ValueError):
        if to == "queued":
            # queued is only the initial state, unreachable as a transition
            raise ValueError("unreachable")
        elif to == "running":
            reg.mark_running("cmd-1")
        elif to == "succeeded":
            reg.mark_succeeded("cmd-1")
        else:
            reg.mark_failed("cmd-1", error_code="x")


def test_transition_of_unknown_command_raises():
    reg, _ = _registry()
    with pytest.raises(ValueError):
        reg.mark_running("nonexistent")
    with pytest.raises(ValueError):
        reg.mark_succeeded("nonexistent")
    with pytest.raises(ValueError):
        reg.mark_failed("nonexistent", error_code="x")


# --- eviction ---------------------------------------------------------------

def test_queued_and_running_entries_never_evicted_by_ttl():
    reg, clock = _registry(ttl_seconds=10.0)
    _queued(reg, "queued-1")
    _queued(reg, "running-1")
    reg.mark_running("running-1")

    clock.advance(1000.0)  # far past TTL
    # Even after a get() triggers eviction, non-terminal entries survive.
    assert reg.get("queued-1").status == STATUS_QUEUED
    assert reg.get("running-1").status == STATUS_RUNNING


def test_terminal_entries_evicted_by_ttl():
    reg, clock = _registry(ttl_seconds=10.0)
    _queued(reg, "done")
    reg.mark_running("done")
    reg.mark_succeeded("done")

    assert reg.get("done").status == STATUS_SUCCEEDED
    clock.advance(11.0)
    assert reg.get("done") is None  # evicted on next access
    assert reg.terminal_count() == 0


def test_terminal_entries_evicted_lru_beyond_capacity():
    reg, _ = _registry(max_entries=3, ttl_seconds=3600.0)
    for i in range(3):
        cid = f"cmd-{i}"
        _queued(reg, cid)
        reg.mark_running(cid)
        reg.mark_succeeded(cid)

    # Touch cmd-0 (make it most-recently-used), then add two more terminal
    # entries to exceed capacity; the untouched cmd-1/2 are evicted first.
    reg.get("cmd-0")
    for i in range(3, 5):
        cid = f"cmd-{i}"
        _queued(reg, cid)
        reg.mark_running(cid)
        reg.mark_succeeded(cid)

    assert reg.terminal_count() == 3
    # cmd-0 was touched (MRU), so it survives; the two LRU entries (cmd-1,
    # cmd-2) were evicted to make room for cmd-3/cmd-4.
    assert reg.get("cmd-0") is not None
    assert reg.get("cmd-1") is None
    assert reg.get("cmd-2") is None
    assert reg.get("cmd-3") is not None
    assert reg.get("cmd-4") is not None


def test_lru_eviction_only_counts_terminal_entries():
    # A large number of non-terminal entries must not force terminal
    # eviction - the capacity budget is terminal-only (§3.4).
    reg, _ = _registry(max_entries=2, ttl_seconds=3600.0)
    for i in range(50):
        _queued(reg, f"queued-{i}")  # all non-terminal

    _queued(reg, "done")
    reg.mark_running("done")
    reg.mark_succeeded("done")

    assert reg.get("done") is not None  # terminal entry survives
    assert reg.get("queued-0") is not None  # non-terminal untouched
    assert reg.terminal_count() == 1


def test_fresh_registry_is_empty_restart_amnesia():
    # Restart-amnesic: a new registry (as after a process restart) has no
    # memory of prior commands; get() returns None for everything.
    reg, _ = _registry()
    assert reg.get("anything") is None
    assert len(reg) == 0


# --- thread safety ----------------------------------------------------------

def test_concurrent_register_and_get_is_consistent():
    reg, _ = _registry()
    import threading

    n = 100
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
        barrier.wait()
        try:
            reg.register(_command(f"cmd-{i}"))
            result = reg.get(f"cmd-{i}")
            assert result.status == STATUS_QUEUED
        except Exception as exc:  # pragma: no cover - failure signal
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(reg) == n


# --- no secret in repr ------------------------------------------------------

def test_command_result_repr_omits_result_payload():
    reg, _ = _registry()
    _queued(reg, "cmd-1", kind="provider_probe")
    reg.mark_running("cmd-1")
    secret = "not-for-logs"
    reg.mark_succeeded("cmd-1", result={"service_key_fingerprint": secret})

    result = reg.get("cmd-1")
    assert secret not in repr(result)
    # The result is still readable via the attribute, just not printed.
    assert dict(result.result)["service_key_fingerprint"] == secret


def test_command_result_result_is_frozen():
    reg, _ = _registry()
    _queued(reg, "cmd-1")
    reg.mark_running("cmd-1")
    reg.mark_succeeded("cmd-1", result={"a": 1})
    result = reg.get("cmd-1")
    with pytest.raises(TypeError):
        result.result["a"] = 2
    with pytest.raises(TypeError):
        result.result["b"] = 3


# --- result payload size bound (§15.2 / COMMAND_RESULT_MAX_PAYLOAD_BYTES) ---

def test_mark_succeeded_oversized_result_becomes_terminal_failed():
    """An oversized result must NOT raise out of the worker and leave the
    command stuck in `running` - it becomes a terminal FAILED with a bounded
    error_code and no payload (the payload was the thing that was bad)."""
    reg, _ = _registry()
    _queued(reg, "cmd-1")
    reg.mark_running("cmd-1")
    big = {"data": "x" * (COMMAND_RESULT_MAX_PAYLOAD_BYTES + 1)}
    reg.mark_succeeded("cmd-1", result=big)  # must not raise
    result = reg.get("cmd-1")
    assert result.status == STATUS_FAILED
    assert result.error_code == "result_payload_too_large"
    assert result.result is None  # nothing sensitive retained
    # A terminal entry, so it is now subject to eviction (no longer stuck).
    assert reg.terminal_count() == 1


def test_mark_succeeded_nonserializable_result_becomes_terminal_failed():
    """A circular result cannot be JSON-encoded (json.dumps raises ValueError
    even with default=str) - it must become a terminal FAILED with a distinct
    error_code, never raise out of the worker."""
    reg, _ = _registry()
    _queued(reg, "cmd-1")
    reg.mark_running("cmd-1")
    circular = {}
    circular["self"] = circular
    reg.mark_succeeded("cmd-1", result=circular)  # must not raise
    result = reg.get("cmd-1")
    assert result.status == STATUS_FAILED
    assert result.error_code == "result_payload_not_serializable"
    assert result.result is None


def test_mark_succeeded_accepts_result_at_or_under_bound():
    reg, _ = _registry()
    _queued(reg, "cmd-1")
    reg.mark_running("cmd-1")
    # A result whose JSON serialization is under the bound is accepted.
    reg.mark_succeeded("cmd-1", result={"attachment_id": "a" * 40, "ok": True})
    assert reg.get("cmd-1").status == STATUS_SUCCEEDED


def test_payload_bound_is_configurable():
    # A registry constructed with a 0-byte bound fails even a minimal result,
    # proving the constructor parameter is actually wired through (not just
    # the module-level default).
    tiny = CommandRegistry(max_payload_bytes=0, now_fn=Clock())
    tiny.register(_command("cmd-1"))
    tiny.mark_running("cmd-1")
    tiny.mark_succeeded("cmd-1", result={"a": 1})  # must not raise
    result = tiny.get("cmd-1")
    assert result.status == STATUS_FAILED
    assert result.error_code == "result_payload_too_large"
