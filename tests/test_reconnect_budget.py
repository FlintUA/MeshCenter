"""TCPTransport.reconnect(): `timeout` is ONE shrinking budget for the whole
call, not a fresh allowance per attempt.

It used to hand every one of the 6 backoff attempts the caller's full `timeout`
(auto-reconnect passes 150s), so slow attempts plus ~108s of backoff could run
for many multiples of it - past the outer AdapterSupervisor deadline, which then
SIGKILLs the adapter mid-attempt (all transport state lost, respawn + probe +
handshake again) and meanwhile holds Core's router lock. Runs on a fake clock:
attempts "cost" simulated time, sleeps advance it, nothing really waits.
"""
import types

import pytest

from adapters.meshtastic import tcp_transport as tcp_mod
from adapters.meshtastic.tcp_transport import TCPTransport
from meshsrv.radio_transport import ConnectionState, TransportError, TransportErrorCode

# The adapter-side budget is Core's timeout minus this margin (ipc_server.py),
# and Core kills the adapter at the Core-side timeout.
CORE_TIMEOUT = 150.0
ADAPTER_TIMEOUT = CORE_TIMEOUT - 2.0


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(
        tcp_mod, "time", types.SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep, time=fake.time)
    )
    return fake


def _transport(monkeypatch, clock, *, cost, disconnect_cost=0.0, succeed_on=None):
    """connect() attempts: each consumes min(cost, timeout) of simulated time
    (a hung handshake burns its whole timeout), then fails - or succeeds on
    attempt number `succeed_on`."""
    calls = []

    def fake_connect(self, descriptor, *, force=False, timeout=30.0):
        calls.append({"timeout": timeout, "started": clock.now})
        clock.now += min(cost, timeout)
        if succeed_on is not None and len(calls) == succeed_on:
            return self.get_connection_info()
        raise TransportError(TransportErrorCode.PROTOCOL_SYNC_TIMEOUT, "handshake did not complete")

    def fake_disconnect(self, *, timeout=15.0):
        clock.now += disconnect_cost

    monkeypatch.setattr(TCPTransport, "connect", fake_connect)
    monkeypatch.setattr(TCPTransport, "disconnect", fake_disconnect)
    # jitter would make the exact numbers noisy
    monkeypatch.setattr(TCPTransport, "_jittered_delay", staticmethod(lambda base: base))
    return TCPTransport(host="192.168.2.34"), calls


def _run(transport, clock, timeout):
    start = clock.now
    with pytest.raises(TransportError) as excinfo:
        transport.reconnect(timeout=timeout)
    return clock.now - start, excinfo.value


def test_hanging_attempts_cannot_exceed_the_budget_or_the_outer_deadline(monkeypatch, clock):
    """Each attempt hangs for its whole timeout. Before: 6 x 148s + 108s of
    sleeps, far past Core's 150s kill. Now the first attempt eats the budget
    and the call ends within it."""
    transport, calls = _transport(monkeypatch, clock, cost=10_000)

    elapsed, _ = _run(transport, clock, ADAPTER_TIMEOUT)

    assert elapsed <= ADAPTER_TIMEOUT
    assert elapsed < CORE_TIMEOUT, "would have been SIGKILLed by AdapterSupervisor.call()"
    assert len(calls) == 1
    assert calls[0]["timeout"] == pytest.approx(ADAPTER_TIMEOUT)


def test_each_attempt_gets_only_what_is_left(monkeypatch, clock):
    transport, calls = _transport(monkeypatch, clock, cost=30.0)

    elapsed, _ = _run(transport, clock, ADAPTER_TIMEOUT)

    assert elapsed <= ADAPTER_TIMEOUT
    assert len(calls) >= 3
    deadline = calls[0]["started"] + ADAPTER_TIMEOUT
    for call in calls:
        assert call["timeout"] == pytest.approx(deadline - call["started"]), "handed exactly the remaining budget"
    timeouts = [c["timeout"] for c in calls]
    assert timeouts == sorted(timeouts, reverse=True) and len(set(timeouts)) == len(timeouts)


def test_the_initial_disconnect_counts_against_the_budget(monkeypatch, clock):
    transport, calls = _transport(monkeypatch, clock, cost=10_000, disconnect_cost=15.0)

    elapsed, _ = _run(transport, clock, ADAPTER_TIMEOUT)

    assert calls[0]["timeout"] == pytest.approx(ADAPTER_TIMEOUT - 15.0)
    assert elapsed <= ADAPTER_TIMEOUT


def test_fast_failures_still_use_the_full_six_attempt_schedule(monkeypatch, clock):
    """Instant failures (e.g. connection refused): the documented 1/2/5/10/30s
    backoff is intact when the budget allows it."""
    transport, calls = _transport(monkeypatch, clock, cost=0.0)

    elapsed, _ = _run(transport, clock, ADAPTER_TIMEOUT)

    assert len(calls) == 6
    assert clock.sleeps == [1.0, 2.0, 5.0, 10.0, 30.0]
    assert elapsed == pytest.approx(48.0)


def test_a_small_budget_clips_the_backoff_and_stops_cleanly(monkeypatch, clock):
    transport, calls = _transport(monkeypatch, clock, cost=0.0)

    elapsed, _ = _run(transport, clock, 20.0)

    assert elapsed <= 20.0
    assert 1 < len(calls) < 6
    assert clock.sleeps[-1] < 10.0, "the 10s pause was clipped to leave room for a real attempt"
    # Every attempt that ran had at least the minimum useful budget (except
    # possibly the first).
    assert all(c["timeout"] >= tcp_mod._RECONNECT_MIN_ATTEMPT_S for c in calls[1:])


def test_no_sleep_into_the_deadline_when_no_further_attempt_fits(monkeypatch, clock):
    transport, calls = _transport(monkeypatch, clock, cost=6.0)

    elapsed, error = _run(transport, clock, 8.0)  # 2s left after the first failure: < the 5s minimum

    assert len(calls) == 1
    assert clock.sleeps == []
    assert elapsed == pytest.approx(6.0)
    assert error.code == TransportErrorCode.PROTOCOL_SYNC_TIMEOUT
    assert transport.get_connection_info().state == ConnectionState.ERROR


def test_success_still_returns_early_with_the_remaining_budget_intact(monkeypatch, clock):
    transport, calls = _transport(monkeypatch, clock, cost=3.0, succeed_on=3)

    info = transport.reconnect(timeout=ADAPTER_TIMEOUT)

    assert len(calls) == 3
    assert info is not None
    assert clock.now - calls[0]["started"] < ADAPTER_TIMEOUT


def test_the_last_error_is_what_gets_raised(monkeypatch, clock):
    transport, _ = _transport(monkeypatch, clock, cost=0.0)

    _, error = _run(transport, clock, ADAPTER_TIMEOUT)

    assert error.code == TransportErrorCode.PROTOCOL_SYNC_TIMEOUT
    assert transport._last_error is error
