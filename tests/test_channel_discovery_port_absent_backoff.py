"""Review round 4, item 4 (H2-C Phase 2 live round, 2026-10-04): a failed
channel discovery never updated channel_cache's own timestamp, so once the
serial port was genuinely absent (observed live during a by-id path
change), the 10s UI poll re-attempted radio_transport.get_channels() on
every single call - each one claiming and failing against the router lock
- for as long as the radio stayed away (8+ minutes, live). is_radio_
available() doesn't catch this window either: it reads RADIO_IDENTITY_
RESULT, which a disconnect doesn't update until the supervisor's own
re-verification actually runs.

api/api_chat.py's discover_radio_channels() now recognizes an ENOENT-style
error specifically and backs off for CHANNEL_DISCOVERY_ABSENT_BACKOFF_S
instead of retrying every poll - these tests exercise that behavior
through the real /api/chats route (discover_radio_channels() itself is a
private closure with no direct test seam).
"""
import time as time_module

import pytest

from meshsrv.radio_transport import ChannelInfo


@pytest.fixture
def _clean_channel_state(server_module, monkeypatch):
    """channel_cache/channel_discovery_backoff are closures private to
    register_chat_routes() with no reset hook, and server_module is
    session-scoped - a PRIOR test's backoff would otherwise leak into this
    one and make even a would-be-successful warm-up call get skipped (the
    backoff check runs BEFORE get_channels() is ever attempted, by
    design). Jump the clock far enough forward that any leftover backoff
    has certainly expired, do one real successful call to clear it for
    good, then restore real time before yielding to the actual test."""
    real_now = time_module.time()
    with monkeypatch.context() as warmup:
        warmup.setattr(server_module, "is_radio_available", lambda: True)
        warmup.setattr(
            server_module.transport_router, "get_channels",
            lambda timeout=15: [ChannelInfo(index=0, name="LongFast", role="PRIMARY")],
        )
        warmup.setattr(time_module, "time", lambda: real_now + 10_000)
        response = server_module.app.test_client().get("/api/chats?refresh_channels=1")
        assert response.status_code == 200

    yield server_module


def _enoent_error():
    return FileNotFoundError(2, "No such file or directory")


def test_repeated_enoent_backs_off_instead_of_retrying_every_poll(_clean_channel_state, monkeypatch):
    server_module = _clean_channel_state
    monkeypatch.setattr(server_module, "is_radio_available", lambda: True)

    call_count = []

    def _raising_get_channels(timeout=15):
        call_count.append(1)
        raise _enoent_error()

    monkeypatch.setattr(server_module.transport_router, "get_channels", _raising_get_channels)

    client = server_module.app.test_client()
    for _ in range(3):
        response = client.get("/api/chats?refresh_channels=1")
        assert response.status_code == 200

    assert len(call_count) == 1, (
        "a genuine port-absent error must trigger a backoff - repeated "
        "force=True requests inside that window must not re-attempt "
        "get_channels() and hammer the router lock again"
    )


def test_a_non_enoent_discovery_error_is_not_backed_off(_clean_channel_state, monkeypatch):
    """Regression guard: only the specific 'port doesn't exist' signal
    gets this treatment - an ordinary busy-port/timeout/protocol error
    must keep retrying on every poll exactly as before (those ARE
    expected to resolve on their own within the UI's normal 10s cadence,
    unlike a device that's genuinely gone)."""
    server_module = _clean_channel_state
    monkeypatch.setattr(server_module, "is_radio_available", lambda: True)

    call_count = []

    def _raising_get_channels(timeout=15):
        call_count.append(1)
        raise TimeoutError("get_channels() exceeded 1.0s")

    monkeypatch.setattr(server_module.transport_router, "get_channels", _raising_get_channels)

    client = server_module.app.test_client()
    for _ in range(3):
        response = client.get("/api/chats?refresh_channels=1")
        assert response.status_code == 200

    assert len(call_count) == 3, "a non-ENOENT error must not trigger the absent-port backoff"


def test_a_successful_discovery_clears_a_leftover_backoff(_clean_channel_state, monkeypatch):
    """Once the radio genuinely answers again, discovery must resume
    immediately rather than waiting out a stale backoff window."""
    server_module = _clean_channel_state
    monkeypatch.setattr(server_module, "is_radio_available", lambda: True)

    call_count = []
    should_fail = {"value": True}

    def _flaky_get_channels(timeout=15):
        call_count.append(1)
        if should_fail["value"]:
            raise _enoent_error()
        return [ChannelInfo(index=0, name="LongFast", role="PRIMARY")]

    monkeypatch.setattr(server_module.transport_router, "get_channels", _flaky_get_channels)

    client = server_module.app.test_client()
    first = client.get("/api/chats?refresh_channels=1")
    assert first.status_code == 200
    assert len(call_count) == 1

    # Still within the backoff window - must be skipped.
    second = client.get("/api/chats?refresh_channels=1")
    assert second.status_code == 200
    assert len(call_count) == 1

    # The radio answers now - flip the fake and jump the clock past the
    # backoff window (a real recovery would simply outlast it; this test
    # isn't going to sleep CHANNEL_DISCOVERY_ABSENT_BACKOFF_S real seconds
    # to prove the same point).
    should_fail["value"] = False
    real_now = time_module.time()
    monkeypatch.setattr(time_module, "time", lambda: real_now + 9999)
    third = client.get("/api/chats?refresh_channels=1")
    assert third.status_code == 200
    assert len(call_count) == 2, "once the backoff window has passed, discovery must try again"
