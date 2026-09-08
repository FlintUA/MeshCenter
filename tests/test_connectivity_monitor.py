"""tests/test_connectivity_monitor.py -- meshsrv/connectivity_monitor.py
(ADR-0008 decision 2, Step 1.6A backend layer).

Covers: `can_attempt_relay()`'s fail-open behavior on an unregistered/
never-checked provider_id; `refresh()` deriving `ONLINE`/`OFFLINE`/
fallback internet status from per-Relay `/health` results; the three
`/health` outcomes (200 -> ONLINE, non-200 -> DEGRADED, connection error
-> UNREACHABLE with an incrementing failure counter and the resulting
backoff via `_due_for_health_check()`); `/v1/info` identity-mismatch
detection (`provider_id_mismatch`/`service_public_key_mismatch`/
`info_malformed`), gated to run rarely, not on every health tick;
`upload_readiness` as an independent field from `state`; a disabled
profile short-circuiting to `DISABLED` without any network call; the
fallback internet check only firing when zero profiles are registered;
and `record_check_result()` actually persisting into
`mca_provider_profiles` via the registry.

Uses the same `MockRelayStore`/`create_mock_relay_app`/Flask-test-client
session pattern established in tests/test_sender.py for the ONLINE/
IDENTITY_MISMATCH paths (real HTTP semantics via Flask, no live network),
plus a small hand-scripted fake session (this module's own, mirroring
that same `.request(method, url, ...)` interface) for the DEGRADED/
UNREACHABLE paths, since a real HTTP failure can't be produced through
the Flask test client.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import types

import pytest
import requests

from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.connectivity_monitor import (
    FALLBACK_INTERNET_CHECK_URL,
    MAX_CONCURRENT_RELAY_PROBES,
    RELAY_HEALTH_BACKOFF_CEILING_SECONDS,
    ConnectivityMonitor,
    InternetStatus,
    RelayState,
    UploadDecision,
    UploadReadiness,
    UploadRejectionReason,
    evaluate_upload_readiness,
)

BASE_URL = "https://mock-relay.test"


class _ResponseShim:
    def __init__(self, flask_response):
        self._flask_response = flask_response
        self.status_code = flask_response.status_code

    def json(self):
        return self._flask_response.get_json()


class _FlaskTestClientSession:
    """Same shape as tests/test_sender.py's own session shim: a
    `.request(method, url, ...)` session double backed by Flask's test
    client, matching `requests.Session`'s real interface (and the one
    `RelayClient`/`ConnectivityMonitor` both actually call)."""

    def __init__(self, flask_test_client, base_url: str):
        self._client = flask_test_client
        self._base_url = base_url

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        path = url[len(self._base_url):]
        flask_response = self._client.open(path, method=method, json=json, data=data, headers=headers or {})
        return _ResponseShim(flask_response)


class _ScriptedResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _ScriptedSession:
    """A fully hand-controlled fake session for the failure paths a real
    Flask test client cannot produce (connection errors, non-200
    statuses) - `handler(method, url)` decides the outcome per call."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append((method, url))
        return self._handler(method, url)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def registry(conn):
    return ProviderRegistry(conn, workspace_id="ws-1")


@pytest.fixture
def store():
    return MockRelayStore(base_url=BASE_URL)


@pytest.fixture
def flask_session(store):
    app = create_mock_relay_app(store)
    return _FlaskTestClientSession(app.test_client(), BASE_URL)


def _register(registry, store, **overrides):
    kwargs = dict(
        display_name="Mock Relay",
        base_url=BASE_URL,
        service_public_key=store.service_public_key,
        max_ciphertext_bytes=6 * 1024 * 1024,
        upload_allowed=True,
    )
    kwargs.update(overrides)
    return registry.register(**kwargs)


@pytest.fixture
def wsm(tmp_path):
    from meshsrv.attachments.workspace import MCAWorkspaceManager

    return MCAWorkspaceManager(str(tmp_path / "data"))


def _configure_upload_token(registry, conn, wsm, provider_id, token="mca_up_test-token"):
    from meshsrv.attachments import identity as identity_module

    principal = identity_module.ensure_principal(conn, wsm, "local")
    registry.set_upload_token(provider_id, wsm, principal.principal_id, token)


# ---- can_attempt_relay() -------------------------------------------------


def test_can_attempt_relay_fails_open_for_unknown_provider_id(registry):
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    assert monitor.can_attempt_relay("never-registered") is True


def test_can_attempt_relay_true_only_for_online_or_degraded(registry, store, flask_session):
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    monitor.refresh()
    assert monitor.can_attempt_relay(profile.provider_id) is True


# ---- ONLINE / internet status derivation --------------------------------


def test_refresh_marks_healthy_relay_online_and_internet_online(registry, store, flask_session):
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    snapshot = monitor.refresh()

    assert snapshot.internet == InternetStatus.ONLINE
    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.ONLINE
    assert status.upload_readiness == UploadReadiness.UPLOAD_TOKEN_MISSING  # no token set yet
    assert status.latency_ms is not None
    assert status.error_code is None


def test_upload_readiness_ready_once_token_configured(registry, store, flask_session, tmp_path):
    from meshsrv.attachments.workspace import MCAWorkspaceManager
    from meshsrv.attachments import identity

    wsm = MCAWorkspaceManager(str(tmp_path / "data"))
    principal = identity.ensure_principal(registry._conn, wsm, "local")
    profile = _register(registry, store)
    registry.set_upload_token(profile.provider_id, wsm, principal.principal_id, "mca_up_test-token")

    monitor = ConnectivityMonitor(registry, session=flask_session)
    snapshot = monitor.refresh()
    assert snapshot.relays[profile.provider_id].upload_readiness == UploadReadiness.READY


def test_upload_readiness_disabled_when_upload_not_allowed(registry, store, flask_session):
    profile = _register(registry, store, upload_allowed=False)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    snapshot = monitor.refresh()
    assert snapshot.relays[profile.provider_id].upload_readiness == UploadReadiness.UPLOAD_DISABLED


# ---- DEGRADED / UNREACHABLE ----------------------------------------------


def test_refresh_marks_non_200_health_as_degraded(registry, store):
    profile = _register(registry, store)
    session = _ScriptedSession(lambda m, u: _ScriptedResponse(status_code=503))
    monitor = ConnectivityMonitor(registry, session=session)
    snapshot = monitor.refresh()

    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.DEGRADED
    assert status.error_code == "http_503"
    # DEGRADED is deliberately in _ATTEMPTABLE_RELAY_STATES (ADR-0008 decision 2:
    # an attempt is still worth making against a slow-but-responding Relay).
    assert snapshot.internet == InternetStatus.ONLINE


def test_refresh_marks_connection_error_as_unreachable_with_failure_counter(registry, store):
    profile = _register(registry, store)

    def handler(method, url):
        raise requests.ConnectionError("connection refused")

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh()

    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.UNREACHABLE
    assert status.error_code == "ConnectionError"
    assert status.latency_ms is None
    assert monitor._consecutive_failures[profile.provider_id] == 1


def test_backoff_grows_with_consecutive_failures_and_caps_at_ceiling(registry, store):
    profile = _register(registry, store)
    now = [1_000_000.0]

    def handler(method, url):
        raise requests.ConnectionError("down")

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler), now_fn=lambda: now[0])

    monitor.refresh()  # failure 1 -> interval = 60 * 2**1 = 120s
    assert monitor._due_for_health_check(profile.provider_id, now[0] + 119) is False
    assert monitor._due_for_health_check(profile.provider_id, now[0] + 120) is True

    now[0] += 120
    monitor.refresh()  # failure 2 -> interval = 60 * 2**2 = 240s
    assert monitor._due_for_health_check(profile.provider_id, now[0] + 239) is False
    assert monitor._due_for_health_check(profile.provider_id, now[0] + 240) is True

    # Drive enough consecutive failures to exceed the 300s ceiling. force=True
    # bypasses the due-check itself so each call actually re-probes and
    # increments the failure counter, regardless of the (growing) interval.
    for _ in range(6):
        now[0] += 300
        monitor.refresh(force=True)
    assert monitor._consecutive_failures[profile.provider_id] >= 6
    checked_at = monitor._relay_statuses[profile.provider_id].checked_at
    assert monitor._due_for_health_check(profile.provider_id, checked_at + 299) is False
    assert monitor._due_for_health_check(profile.provider_id, checked_at + 300) is True


def test_not_due_health_check_reuses_previous_state_without_a_network_call(registry, store):
    profile = _register(registry, store)
    calls = []

    def handler(method, url):
        calls.append((method, url))
        return _ScriptedResponse(status_code=200, payload={"ok": True})

    now = [0.0]
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler), now_fn=lambda: now[0])
    monitor.refresh()
    assert len(calls) == 1

    now[0] += 1  # nowhere near the 60s interval
    monitor.refresh()
    assert len(calls) == 1  # no new /health call made
    assert monitor.snapshot().relays[profile.provider_id].state == RelayState.ONLINE


# ---- /v1/info identity mismatch ------------------------------------------


def test_identity_check_flags_provider_id_mismatch(registry, store):
    profile = _register(registry, store)

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        return _ScriptedResponse(
            status_code=200,
            payload={
                "provider_id": "not-the-real-provider-id",
                "service_key": {"public_key": "irrelevant"},
            },
        )

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh(force=True)  # force=True makes the /v1/info check run this tick

    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.IDENTITY_MISMATCH
    assert status.error_code == "provider_id_mismatch"


def test_identity_check_flags_service_public_key_mismatch(registry, store):
    profile = _register(registry, store)

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        return _ScriptedResponse(
            status_code=200,
            payload={"provider_id": profile.provider_id, "service_key": {"public_key": "wrong-key"}},
        )

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh(force=True)

    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.IDENTITY_MISMATCH
    assert status.error_code == "service_public_key_mismatch"


def test_identity_check_flags_malformed_info_payload(registry, store):
    profile = _register(registry, store)

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        return _ScriptedResponse(status_code=200, payload={"unexpected": "shape"})

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh(force=True)

    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.IDENTITY_MISMATCH
    assert status.error_code == "info_malformed"


def test_identity_check_passes_against_the_real_mock_relay(registry, store, flask_session):
    """End-to-end sanity check against the actual mock Relay's /v1/info
    shape (info_payload()), not just a hand-scripted payload."""
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    snapshot = monitor.refresh(force=True)
    assert snapshot.relays[profile.provider_id].state == RelayState.ONLINE


def test_identity_check_does_not_run_on_every_health_tick(registry, store):
    """This test's info payload deliberately doesn't match the real
    profile's pinned service_public_key, so the relay ends up
    IDENTITY_MISMATCH (not attemptable) - which correctly triggers this
    module's own independent fallback probe (this ADR-0008-hardening
    pass's own defect #3 fix). That fallback call is a distinct concern
    from what this test actually checks (the /v1/info call's own
    cadence) - the handler below buckets it separately so the two don't
    get conflated in one counter."""
    profile = _register(registry, store)
    info_calls = []
    fallback_calls = []

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        if url == FALLBACK_INTERNET_CHECK_URL:
            fallback_calls.append(url)
            return _ScriptedResponse(status_code=200)
        info_calls.append(url)
        return _ScriptedResponse(status_code=200, payload={"provider_id": profile.provider_id, "service_key": {"public_key": "x"}})

    now = [0.0]
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler), now_fn=lambda: now[0])
    monitor.refresh(force=True)
    assert len(info_calls) == 1
    assert len(fallback_calls) == 1  # identity mismatch - fallback correctly consulted once

    # Force a health re-check (well past the 60s interval) without forcing info.
    now[0] += 61
    monitor.refresh(force=False)
    assert len(info_calls) == 1  # still just the one from the forced check


# ---- DISABLED --------------------------------------------------------------


def test_disabled_profile_makes_no_per_relay_call_but_the_workspace_fallback_probe_still_runs(registry, store):
    """PR #231 review (2nd pass): a disabled profile's own /health check
    is still skipped entirely (_check_relay()'s own short-circuit,
    unchanged) - but refresh() as a whole must not stay silent about
    general internet reachability just because every registered Relay
    happens to be disabled right now. Before this fix, `_internet_status`
    would stay frozen at whatever it last was (typically UNKNOWN forever
    on an instance whose only registered Relay has always been disabled)
    since the fallback probe's own trigger condition explicitly excluded
    this case. Renamed from this file's former test of the same disabled-
    profile setup, which asserted zero network calls of any kind - no
    longer true by design."""
    profile = _register(registry, store)
    registry.update_profile(profile.provider_id, enabled=False)
    calls = []

    def handler(method, url):
        calls.append(url)
        return _ScriptedResponse(status_code=204)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh()

    # Exactly one call - the workspace-level fallback probe - never a
    # per-relay /health call for the disabled profile itself.
    assert calls == [FALLBACK_INTERNET_CHECK_URL]
    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.DISABLED
    assert status.upload_readiness == UploadReadiness.UPLOAD_DISABLED
    assert snapshot.internet == InternetStatus.ONLINE


def test_all_relays_disabled_still_reports_a_real_internet_status(registry, store):
    """The specific gap this fix closes: with every registered Relay
    disabled, internet status must still be independently determined via
    the fallback probe - not left stuck at UNKNOWN (or any other stale
    value) forever."""
    profile = _register(registry, store)
    registry.update_profile(profile.provider_id, enabled=False)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse(status_code=204)))
    assert monitor.snapshot().internet == InternetStatus.UNKNOWN  # nothing probed yet

    monitor.refresh()
    assert monitor.snapshot().internet == InternetStatus.ONLINE

    # And the reverse: a genuinely unreachable fallback while all relays
    # are disabled must report OFFLINE, not silently stay ONLINE/UNKNOWN.
    def down(method, url):
        raise requests.ConnectionError("no route")

    monitor_offline = ConnectivityMonitor(registry, session=_ScriptedSession(down), now_fn=lambda: 1_000_000.0)
    monitor_offline.refresh()
    assert monitor_offline.snapshot().internet == InternetStatus.OFFLINE


# ---- fallback internet check ----------------------------------------------


def test_fallback_internet_check_only_used_with_zero_providers_registered(registry):
    calls = []

    def handler(method, url):
        calls.append((method, url))
        return _ScriptedResponse(status_code=204)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh()

    assert len(calls) == 1
    assert calls[0][0] == "HEAD"
    assert snapshot.internet == InternetStatus.ONLINE


def test_fallback_internet_check_offline_on_connection_error(registry):
    def handler(method, url):
        raise requests.ConnectionError("no route")

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh()
    assert snapshot.internet == InternetStatus.OFFLINE


def test_fallback_internet_check_limited_on_5xx(registry):
    def handler(method, url):
        return _ScriptedResponse(status_code=502)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh()
    assert snapshot.internet == InternetStatus.LIMITED


def test_fallback_check_not_used_once_a_provider_exists(registry, store, flask_session):
    _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    snapshot = monitor.refresh()
    # Internet status is derived from the Relay's own health, not the fallback URL.
    assert snapshot.internet == InternetStatus.ONLINE


# ---- record_check_result() persistence -------------------------------------


def test_refresh_persists_check_result_into_the_registry(registry, store, flask_session):
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    monitor.refresh()

    persisted = registry.resolve(profile.provider_id)
    assert persisted.last_check_result == "online"
    assert persisted.last_checked_at is not None
    assert persisted.last_latency_ms is not None
    assert persisted.last_error_code is None


def test_refresh_persists_failure_details(registry, store):
    profile = _register(registry, store)

    def handler(method, url):
        raise requests.ConnectionError("down")

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    monitor.refresh()

    persisted = registry.resolve(profile.provider_id)
    assert persisted.last_check_result == "unreachable"
    assert persisted.last_error_code == "ConnectionError"


# ---- PR #231 review (3rd pass): bounded concurrent Relay probing ---------


class _SlowThenFastSession:
    """relay-a.example.net's /health blocks until explicitly released;
    relay-b.example.net answers immediately and signals `b_completed`
    when it does. Used to prove, deterministically (no fixed sleep
    needed to "win" the assertion), that a slow Relay does not prevent
    another due Relay from being probed in the same refresh() pass.
    Also answers /v1/info correctly for both profiles (matching their
    real provider_id/service_public_key) - the very first refresh() for
    a freshly-registered profile always has its info check due too
    (never checked before), so a malformed /v1/info response would
    otherwise turn this test's profiles IDENTITY_MISMATCH instead of
    ONLINE, unrelated to what this test is actually about."""

    def __init__(self, profile_a, profile_b) -> None:
        from meshsrv.attachments.provider_registry import b64url_encode as _b64

        def _info_payload(profile):
            return {
                "provider_id": profile.provider_id,
                "service_key": {"public_key": _b64(profile.service_public_key)},
            }

        self._info_payload_a = _info_payload(profile_a)
        self._info_payload_b = _info_payload(profile_b)
        self.a_entered = threading.Event()
        self.a_release = threading.Event()
        self.b_completed = threading.Event()

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        if "relay-a" in url:
            if url.endswith("/health"):
                self.a_entered.set()
                released = self.a_release.wait(timeout=10)
                if not released:
                    raise AssertionError("_SlowThenFastSession relay-a was never released - test bug")
                return _ScriptedResponse(status_code=200)
            if url.endswith("/v1/info"):
                return _ScriptedResponse(status_code=200, payload=self._info_payload_a)
        elif "relay-b" in url:
            if url.endswith("/health"):
                self.b_completed.set()
                return _ScriptedResponse(status_code=200)
            if url.endswith("/v1/info"):
                return _ScriptedResponse(status_code=200, payload=self._info_payload_b)
        raise AssertionError(f"unexpected URL in this test: {url}")


def test_a_slow_relay_does_not_prevent_another_due_relay_from_being_probed(store):
    # A dedicated check_same_thread=False connection, not the shared conn/
    # registry fixtures - this test deliberately calls refresh() from a
    # background thread (to observe blocking behavior from the main test
    # thread while it's in flight), and AttachmentsService's own real
    # wiring gives ConnectivityMonitor a check_same_thread=False
    # connection too (mca_runtime.py), so this matches production.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    migrate(conn)
    registry = ProviderRegistry(conn, workspace_id="ws-1")
    profile_a = _register(registry, store, display_name="A", base_url="https://relay-a.example.net")
    profile_b = _register(registry, store, display_name="B", base_url="https://relay-b.example.net")
    session = _SlowThenFastSession(profile_a, profile_b)
    monitor = ConnectivityMonitor(registry, session=session)

    refresh_thread = threading.Thread(target=monitor.refresh, daemon=True)
    refresh_thread.start()

    assert session.a_entered.wait(timeout=5), "relay-a was never probed"
    # The whole point: B must complete its own probe while A is still
    # blocked - proven by waiting for b_completed BEFORE releasing A, not
    # by timing/ordering assumptions.
    assert session.b_completed.wait(timeout=5), "relay-b's probe never ran while relay-a was still blocked"
    assert refresh_thread.is_alive(), "refresh() returned before relay-a was released - it should still be blocked"

    session.a_release.set()
    refresh_thread.join(timeout=5)
    assert not refresh_thread.is_alive()

    snapshot = monitor.snapshot()
    assert snapshot.relays[profile_a.provider_id].state == RelayState.ONLINE
    assert snapshot.relays[profile_b.provider_id].state == RelayState.ONLINE


class _TrackedSession:
    """Mimics `requests.Session`'s real per-instance shape - independent
    mutable state per instance, with a real `.close()` this test can
    observe - standing in for "a production-style session object", not a
    stateless test double. Used with `session_factory=` (not `session=`)
    so each call to the factory produces a genuinely distinct instance,
    proving PR #231 review (4th pass)'s "one Session per probe" fix:
    concurrent probes must never share one session object."""

    _next_id = 1

    def __init__(self, on_request=None) -> None:
        self.instance_id = _TrackedSession._next_id
        _TrackedSession._next_id += 1
        self.closed = False
        self._on_request = on_request

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        if self._on_request is not None:
            self._on_request(self, method, url)
        return _ScriptedResponse(status_code=200)

    def close(self) -> None:
        self.closed = True


def test_concurrent_probes_use_separate_session_instances_each_closed_after_use(store):
    """PR #231 review (4th pass), the core regression test for
    "concurrent HTTP client safety": proves, against production-style
    session objects (not a single shared fake), that (a) relay-a and
    relay-b are probed using two genuinely DIFFERENT session instances,
    (b) both were simultaneously "in flight" (relay-b's probe completes
    while relay-a's is still blocked, the same deterministic technique
    as the slow-relay test above), and (c) every session this monitor's
    factory created is closed by the time refresh() returns."""
    # A dedicated check_same_thread=False connection, not the shared conn/
    # registry fixtures - refresh() runs in a background thread here (to
    # observe blocking behavior from the main test thread while it's in
    # flight), same reasoning as the slow-relay test above.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    migrate(conn)
    registry = ProviderRegistry(conn, workspace_id="ws-1")
    profile_a = _register(registry, store, display_name="A", base_url="https://relay-a.example.net")
    profile_b = _register(registry, store, display_name="B", base_url="https://relay-b.example.net")

    created_sessions: List[_TrackedSession] = []
    a_entered = threading.Event()
    a_release = threading.Event()
    a_instance_id_holder = []
    b_instance_id_holder = []

    def on_request(session, method, url):
        if "relay-a" in url:
            a_instance_id_holder.append(session.instance_id)
            a_entered.set()
            released = a_release.wait(timeout=10)
            if not released:
                raise AssertionError("relay-a was never released - test bug")
        elif "relay-b" in url:
            b_instance_id_holder.append(session.instance_id)

    def factory():
        session = _TrackedSession(on_request=on_request)
        created_sessions.append(session)
        return session

    monitor = ConnectivityMonitor(registry, session_factory=factory)

    refresh_thread = threading.Thread(target=monitor.refresh, daemon=True)
    refresh_thread.start()

    assert a_entered.wait(timeout=5), "relay-a was never probed"
    # relay-b must complete - using its OWN session instance - while
    # relay-a's own session is still blocked inside its own .request().
    deadline = time.time() + 5
    while not b_instance_id_holder and time.time() < deadline:
        time.sleep(0.01)
    assert b_instance_id_holder, "relay-b's probe never completed while relay-a was still blocked"
    assert refresh_thread.is_alive(), "refresh() returned before relay-a was released - it should still be blocked"

    # The actual point of this test: two concurrently in-flight probes
    # used two genuinely different session instances, not one shared
    # object - proven while relay-a's own probe is STILL blocked, so
    # this isn't just "they happened to differ after the fact".
    assert a_instance_id_holder[0] != b_instance_id_holder[0]

    a_release.set()
    refresh_thread.join(timeout=5)
    assert not refresh_thread.is_alive()

    assert len(created_sessions) >= 2
    instance_ids = [s.instance_id for s in created_sessions]
    assert len(instance_ids) == len(set(instance_ids))  # every instance is genuinely distinct

    # Every session this monitor's factory ever created is closed by now.
    for session in created_sessions:
        assert session.closed, f"session {session.instance_id} was never closed"


class _ConcurrencyTrackingSession:
    """Tracks how many `.request()` calls are simultaneously in flight -
    used to pin the actual concurrency ceiling, not just "a slow one
    doesn't block a fast one". A short, fixed hold per request widens the
    overlap window so the peak is reliably observed regardless of exact
    thread scheduling - unlike the other tests in this file, this one
    genuinely needs *some* in-request delay to make concurrent overlap
    observable at all, not to make an assertion "win" a race."""

    def __init__(self, hold_seconds: float = 0.05) -> None:
        self._lock = threading.Lock()
        self._current = 0
        self.max_seen = 0
        self._hold_seconds = hold_seconds

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        with self._lock:
            self._current += 1
            self.max_seen = max(self.max_seen, self._current)
        time.sleep(self._hold_seconds)
        with self._lock:
            self._current -= 1
        return _ScriptedResponse(status_code=200)


def test_concurrent_relay_probes_never_exceed_the_bound(registry, store):
    for i in range(5):
        _register(registry, store, display_name=f"relay-{i}", base_url=f"https://relay-{i}.example.net")

    session = _ConcurrencyTrackingSession()
    monitor = ConnectivityMonitor(registry, session=session)
    monitor.refresh()

    assert session.max_seen <= MAX_CONCURRENT_RELAY_PROBES
    assert session.max_seen >= 2  # confirms probes actually overlapped at all, not accidentally serialized


def test_refresh_mutates_state_only_on_the_calling_thread(registry, store):
    """PR #231 review (3rd pass) requirement: ConnectivityMonitor state
    mutation and ProviderRegistry/SQLite writes stay on the owning
    (calling) thread even though probes themselves run concurrently on a
    small pool. Asserts this indirectly but concretely: every
    record_check_result() call (the SQLite write) must have happened by
    the time refresh() returns, on the thread that called refresh() -
    verified by monkeypatching record_check_result() to record which
    thread called it."""
    for i in range(4):
        _register(registry, store, display_name=f"relay-{i}", base_url=f"https://relay-{i}.example.net")

    calling_threads = []
    original_record = registry.record_check_result

    def _tracking_record(*args, **kwargs):
        calling_threads.append(threading.current_thread())
        return original_record(*args, **kwargs)

    registry.record_check_result = _tracking_record
    monitor = ConnectivityMonitor(registry, session=_ConcurrencyTrackingSession(hold_seconds=0.02))
    this_thread = threading.current_thread()

    monitor.refresh()

    assert len(calling_threads) == 4
    assert all(t is this_thread for t in calling_threads)


# ---- PR #231 review, section 6 --------------------------------------------


def test_limit_exceeded_upload_readiness_was_removed():
    """PR #231 review, section 10: nothing in this codebase computes a
    per-Relay upload quota, so LIMIT_EXCEEDED could never actually be
    returned - removed rather than kept as permanently-dead state."""
    assert {member.value for member in UploadReadiness} == {"ready", "upload_token_missing", "upload_disabled"}


def test_can_upload_to_requires_both_reachability_and_local_upload_config(registry, store, flask_session):
    """PR #231 review (2nd pass), "contextual upload readiness":
    can_upload_to() must combine live reachability with local upload
    config - neither RelayStatus.state nor .upload_readiness alone
    answers this."""
    from meshsrv.attachments.workspace import MCAWorkspaceManager
    from meshsrv.attachments import identity as identity_module

    profile = _register(registry, store, upload_allowed=True)
    monitor = ConnectivityMonitor(registry, session=flask_session)

    # Reachable (ONLINE after refresh), but no upload token configured yet.
    monitor.refresh()
    assert monitor.snapshot().relays[profile.provider_id].state == RelayState.ONLINE
    assert monitor.can_upload_to(profile.provider_id) is False

    # Configure the token - now both halves agree.
    import tempfile

    wsm = MCAWorkspaceManager(tempfile.mkdtemp())
    principal = identity_module.ensure_principal(registry._conn, wsm, "local")
    registry.set_upload_token(profile.provider_id, wsm, principal.principal_id, "mca_up_test-token")
    monitor.refresh()
    assert monitor.can_upload_to(profile.provider_id) is True


def test_can_upload_to_is_false_when_relay_is_unreachable_even_with_upload_configured(registry, store):
    from meshsrv.attachments.workspace import MCAWorkspaceManager
    from meshsrv.attachments import identity as identity_module
    import tempfile

    profile = _register(registry, store, upload_allowed=True)
    wsm = MCAWorkspaceManager(tempfile.mkdtemp())
    principal = identity_module.ensure_principal(registry._conn, wsm, "local")
    registry.set_upload_token(profile.provider_id, wsm, principal.principal_id, "mca_up_test-token")

    def handler(method, url):
        raise requests.ConnectionError("down")

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    monitor.refresh()
    assert monitor.snapshot().relays[profile.provider_id].state == RelayState.UNREACHABLE
    assert monitor.can_upload_to(profile.provider_id) is False


def test_can_upload_to_is_false_for_an_unknown_provider_id(registry):
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    assert monitor.can_upload_to("never-registered") is False


# ---- PR #231 review (3rd pass): evaluate_upload_decision() - table-driven -


def test_evaluate_upload_decision_profile_not_found(registry):
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    decision = monitor.evaluate_upload_decision("never-registered")
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.PROFILE_NOT_FOUND)


def test_evaluate_upload_decision_profile_disabled_even_before_the_first_refresh(registry, store):
    """PR #231 review (3rd pass) explicit requirement: a disabled
    profile must be rejected even before refresh() has ever run.
    Satisfied differently as of the 4th pass: __init__ eagerly builds
    self._profile_snapshot once at construction time (see that method's
    own docstring) - evaluate_upload_decision() itself never touches the
    registry/SQLite directly any more (see the dedicated thread-identity
    test below), but the snapshot it reads from already has this
    profile's current enabled=False by the time this method is called."""
    profile = _register(registry, store, upload_allowed=True)
    registry.update_profile(profile.provider_id, enabled=False)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    # Deliberately no monitor.refresh() call anywhere in this test.
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.PROFILE_DISABLED)


def test_evaluate_upload_decision_upload_not_allowed(registry, store):
    profile = _register(registry, store, upload_allowed=False)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.UPLOAD_NOT_ALLOWED)


def test_evaluate_upload_decision_upload_token_missing(registry, store):
    profile = _register(registry, store, upload_allowed=True)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.UPLOAD_TOKEN_MISSING)


def test_evaluate_upload_decision_relay_not_yet_checked(registry, store, conn, wsm):
    """Unlike can_attempt_relay()'s deliberate fail-open on a never-
    checked profile, evaluate_upload_decision() must NOT claim readiness
    for a Relay ConnectivityMonitor has not actually confirmed reachable
    yet - a human-facing "can I upload" decision, not an internal
    scheduling heuristic."""
    profile = _register(registry, store, upload_allowed=True)
    _configure_upload_token(registry, conn, wsm, profile.provider_id)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_NOT_YET_CHECKED)


def test_evaluate_upload_decision_relay_unreachable(registry, store, conn, wsm):
    profile = _register(registry, store, upload_allowed=True)
    _configure_upload_token(registry, conn, wsm, profile.provider_id)

    def handler(method, url):
        raise requests.ConnectionError("down")

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    monitor.refresh()
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_UNREACHABLE)


def test_evaluate_upload_decision_relay_identity_mismatch(registry, store, conn, wsm):
    profile = _register(registry, store, upload_allowed=True)
    _configure_upload_token(registry, conn, wsm, profile.provider_id)

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        return _ScriptedResponse(status_code=200, payload={"provider_id": "wrong", "service_key": {"public_key": "x"}})

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    monitor.refresh(force=True)
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_IDENTITY_MISMATCH)


def test_evaluate_upload_decision_relay_incompatible(registry, store, conn, wsm):
    from meshsrv.attachments.provider_registry import b64url_encode

    profile = _register(registry, store, upload_allowed=True)
    _configure_upload_token(registry, conn, wsm, profile.provider_id)

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        return _ScriptedResponse(
            status_code=200,
            payload={
                "provider_id": profile.provider_id,
                "service_key": {"public_key": b64url_encode(profile.service_public_key)},
                "protocol_version": "99",
            },
        )

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    monitor.refresh(force=True)
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_INCOMPATIBLE)


def _ready_profile(registry, store, conn, wsm, flask_session, **overrides):
    """Registers a profile, configures its upload token, and drives one
    real refresh() (against the mock Relay app, so /v1/info naturally
    matches) so it reaches a genuinely upload-ready baseline - the
    shared setup every ciphertext-size/TTL test below starts from."""
    profile = _register(registry, store, upload_allowed=True, **overrides)
    _configure_upload_token(registry, conn, wsm, profile.provider_id)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    monitor.refresh(force=True)
    return monitor, profile


def test_evaluate_upload_decision_ciphertext_too_large(registry, store, conn, wsm, flask_session):
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session, max_ciphertext_bytes=1000)
    decision = monitor.evaluate_upload_decision(profile.provider_id, ciphertext_bytes=1001)
    assert decision.ready is False
    assert decision.reason == UploadRejectionReason.CIPHERTEXT_TOO_LARGE
    assert decision.detail == "1001 > 1000"


def test_evaluate_upload_decision_ciphertext_within_limit_is_ready(registry, store, conn, wsm, flask_session):
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session, max_ciphertext_bytes=1000)
    decision = monitor.evaluate_upload_decision(profile.provider_id, ciphertext_bytes=1000)
    assert decision == UploadDecision(ready=True, reason=None)


def test_evaluate_upload_decision_ttl_below_minimum(registry, store, conn, wsm, flask_session):
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session, min_ttl_seconds=3600, max_ttl_seconds=86400)
    decision = monitor.evaluate_upload_decision(profile.provider_id, requested_ttl_seconds=1800)
    assert decision.ready is False
    assert decision.reason == UploadRejectionReason.TTL_BELOW_MINIMUM
    assert decision.detail == "1800 < 3600"


def test_evaluate_upload_decision_ttl_above_maximum(registry, store, conn, wsm, flask_session):
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session, min_ttl_seconds=3600, max_ttl_seconds=86400)
    decision = monitor.evaluate_upload_decision(profile.provider_id, requested_ttl_seconds=90000)
    assert decision.ready is False
    assert decision.reason == UploadRejectionReason.TTL_ABOVE_MAXIMUM
    assert decision.detail == "90000 > 86400"


def test_evaluate_upload_decision_ttl_within_bounds_is_ready(registry, store, conn, wsm, flask_session):
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session, min_ttl_seconds=3600, max_ttl_seconds=86400)
    decision = monitor.evaluate_upload_decision(profile.provider_id, requested_ttl_seconds=7200)
    assert decision == UploadDecision(ready=True, reason=None)


def test_evaluate_upload_decision_unbounded_ttl_profile_accepts_any_requested_ttl(registry, store, conn, wsm, flask_session):
    """min_ttl_seconds/max_ttl_seconds are both optional (Optional[int] =
    None) - a profile that never set either bound must not reject any
    requested_ttl_seconds value."""
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session)
    decision = monitor.evaluate_upload_decision(profile.provider_id, requested_ttl_seconds=10_000_000)
    assert decision == UploadDecision(ready=True, reason=None)


def test_evaluate_upload_decision_ready_when_no_size_or_ttl_given(registry, store, conn, wsm, flask_session):
    """Omitting ciphertext_bytes/requested_ttl_seconds skips those two
    checks entirely - a caller asking "is this Relay even usable in
    principle" before a file has been encrypted has nothing to check
    ciphertext size against yet."""
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session)
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision == UploadDecision(ready=True, reason=None)


def test_evaluate_upload_decision_check_order_profile_disabled_wins_over_upload_not_allowed(registry, store):
    """Confirms the documented fixed check order: a disabled profile is
    rejected for PROFILE_DISABLED even when it would also fail a later
    check (upload not allowed) - the caller learns about the first,
    most-fundamental problem, not an arbitrary one."""
    profile = _register(registry, store, upload_allowed=False)
    registry.update_profile(profile.provider_id, enabled=False)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    decision = monitor.evaluate_upload_decision(profile.provider_id)
    assert decision.reason == UploadRejectionReason.PROFILE_DISABLED


def test_can_upload_to_is_a_thin_wrapper_over_evaluate_upload_decision(registry, store, conn, wsm, flask_session):
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session)
    assert monitor.can_upload_to(profile.provider_id) is True
    assert monitor.can_upload_to("never-registered") is False


def test_evaluate_upload_decision_performs_no_sqlite_operation_from_a_simulated_rest_thread(registry, store, conn, wsm, flask_session):
    """PR #231 review (4th pass), "preserve the single-owner SQLite
    model": deterministically proves evaluate_upload_decision() (the
    method AttachmentsService.evaluate_upload_readiness() delegates to -
    the surface a future Step 1.6A REST handler would call, on a Flask
    request thread, not the AttachmentsService worker thread) makes NO
    SQLite call at all when invoked from a different thread, by wrapping
    the real connection's own .execute() to record which thread called
    it and asserting that list stays empty across the simulated call."""
    monitor, profile = _ready_profile(registry, store, conn, wsm, flask_session)

    # sqlite3.Connection.execute is a read-only attribute on the C-level
    # Connection object itself (can't be monkeypatched directly) - instead,
    # wrap the one Python-level object ConnectivityMonitor could possibly
    # reach SQLite through at all: self._provider_registry. Every one of
    # its methods ultimately calls self._conn.execute() internally
    # (provider_registry.py), so tracking calls to the registry object
    # itself is an exact, sufficient proxy for "touched SQLite" here -
    # there is no other SQLite handle anywhere in ConnectivityMonitor.
    sql_call_threads = []
    real_registry = monitor._provider_registry

    class _TrackingRegistryProxy:
        def __getattr__(self, name):
            real_attr = getattr(real_registry, name)
            if not callable(real_attr):
                return real_attr

            def _tracked(*args, **kwargs):
                sql_call_threads.append(threading.current_thread())
                return real_attr(*args, **kwargs)

            return _tracked

    monitor._provider_registry = _TrackingRegistryProxy()

    result_holder = {}

    def _simulated_rest_call():
        result_holder["decision"] = monitor.evaluate_upload_decision(
            profile.provider_id, ciphertext_bytes=1000, requested_ttl_seconds=3600
        )

    rest_thread = threading.Thread(target=_simulated_rest_call)
    rest_thread.start()
    rest_thread.join(timeout=5)

    assert not rest_thread.is_alive()
    # Sanity check the call actually did real work (not a no-op that
    # trivially made no SQL calls because it also did nothing useful).
    assert result_holder["decision"] == UploadDecision(ready=True, reason=None)
    assert sql_call_threads == [], (
        f"evaluate_upload_decision() executed SQL from the calling thread: {sql_call_threads}"
    )


def test_evaluate_upload_readiness_is_a_real_public_function(registry, store):
    profile = _register(registry, store, upload_allowed=True)
    assert evaluate_upload_readiness(profile) == UploadReadiness.UPLOAD_TOKEN_MISSING

    disabled_profile = _register(registry, store, base_url="https://second.example.net", upload_allowed=False)
    assert evaluate_upload_readiness(disabled_profile) == UploadReadiness.UPLOAD_DISABLED


def test_checking_enum_members_were_removed():
    """CHECKING was never actually set anywhere - dead, unreachable enum
    members that snapshot() could never really return. Pinning their
    absence directly, rather than only relying on "no code sets this",
    which a future change could silently reintroduce without this test
    noticing."""
    assert not hasattr(InternetStatus, "CHECKING")
    assert not hasattr(RelayState, "CHECKING")
    assert {member.value for member in InternetStatus} == {"unknown", "online", "offline", "limited"}
    assert {member.value for member in RelayState} == {
        "unknown", "online", "degraded", "unreachable", "identity_mismatch", "incompatible", "disabled",
    }


def test_refresh_prunes_status_for_a_deleted_provider(registry, store, flask_session, tmp_path):
    from meshsrv.attachments.workspace import MCAWorkspaceManager
    from meshsrv.attachments import identity

    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=flask_session)
    monitor.refresh()
    assert profile.provider_id in monitor.snapshot().relays

    wsm = MCAWorkspaceManager(str(tmp_path / "data"))
    principal = identity.ensure_principal(registry._conn, wsm, "local")
    registry.remove_or_disable(profile.provider_id, wsm, principal.principal_id)

    monitor.refresh()
    assert profile.provider_id not in monitor.snapshot().relays
    assert profile.provider_id not in monitor._consecutive_failures
    assert profile.provider_id not in monitor._last_info_check


def test_disabling_a_provider_reflects_immediately_not_after_the_health_interval(registry, store, flask_session):
    """PR #231 review, section 6: the old code let a just-disabled
    profile keep answering can_attempt_relay() (and snapshot()) with its
    last cached ONLINE reading until _due_for_health_check()'s own
    interval/backoff next elapsed - up to RELAY_HEALTH_INTERVAL_SECONDS,
    longer under backoff. A user disabling a Relay is a deliberate
    action that must be visible on the very next refresh(), not
    minutes later."""
    profile = _register(registry, store)
    now = [0.0]
    monitor = ConnectivityMonitor(registry, session=flask_session, now_fn=lambda: now[0])
    monitor.refresh()
    assert monitor.snapshot().relays[profile.provider_id].state == RelayState.ONLINE

    registry.update_profile(profile.provider_id, enabled=False)
    now[0] += 1  # nowhere near the 60s health-check interval
    monitor.refresh()

    status = monitor.snapshot().relays[profile.provider_id]
    assert status.state == RelayState.DISABLED
    assert monitor.can_attempt_relay(profile.provider_id) is False


def test_transient_info_check_failure_does_not_defer_the_next_real_attempt(registry, store):
    """PR #231 review, section 6: _last_info_check used to be stamped
    unconditionally, even when the /v1/info call itself raised - so one
    flaky request could silently defer the next *real* identity check by
    up to RELAY_INFO_MIN_INTERVAL_SECONDS (an hour). A transient failure
    must leave _last_info_check exactly as it was (never checked, here),
    so the very next forced check still actually attempts the call."""
    profile = _register(registry, store)
    info_attempts = []

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        info_attempts.append(url)
        raise requests.ConnectionError("flaky /v1/info")

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh(force=True)

    # Inconclusive, not a mismatch - the Relay is still reported ONLINE
    # (health succeeded; only the identity check itself was flaky).
    assert snapshot.relays[profile.provider_id].state == RelayState.ONLINE
    assert profile.provider_id not in monitor._last_info_check
    assert len(info_attempts) == 1

    # A second forced call must attempt /v1/info again immediately - it
    # must not have been silently deferred by the failed attempt above.
    monitor.refresh(force=True)
    assert len(info_attempts) == 2


def test_identity_check_flags_unsupported_protocol_version(registry, store):
    from meshsrv.attachments.provider_registry import b64url_encode

    profile = _register(registry, store)

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        return _ScriptedResponse(
            status_code=200,
            payload={
                "provider_id": profile.provider_id,
                "service_key": {"public_key": b64url_encode(profile.service_public_key)},
                "protocol_version": "99",
            },
        )

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh(force=True)

    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.INCOMPATIBLE
    assert status.error_code == "unsupported_protocol_version:99"
    assert monitor.can_attempt_relay(profile.provider_id) is False


def test_identity_check_accepts_the_supported_protocol_version(registry, store):
    from meshsrv.attachments.provider_registry import b64url_encode

    profile = _register(registry, store)

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        return _ScriptedResponse(
            status_code=200,
            payload={
                "provider_id": profile.provider_id,
                "service_key": {"public_key": b64url_encode(profile.service_public_key)},
                "protocol_version": "1",
            },
        )

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh(force=True)
    assert snapshot.relays[profile.provider_id].state == RelayState.ONLINE


# ---- profile_snapshot() accessor (§3.2) -----------------------------------


def test_profile_snapshot_is_a_read_only_view_of_registered_profiles(registry, store):
    """The provider snapshot accessor (Step 1.6A.1, §3.2) must expose the
    atomically-published `_profile_snapshot` as a read-only mapping that
    request threads (or the facade) can enumerate without SQLite - and
    which a caller cannot mutate."""
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))

    snapshot = monitor.profile_snapshot()
    assert profile.provider_id in snapshot
    assert snapshot[profile.provider_id].display_name == "Mock Relay"
    # Read-only: a caller cannot mutate the worker's published state.
    with pytest.raises(TypeError):
        snapshot[profile.provider_id] = None


def test_profile_snapshot_is_refreshed_when_the_registry_changes(registry, store):
    """The accessor must reflect a new registration after a refresh()
    (the worker's single-owner SQLite pass), not a stale construction-time
    copy."""
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))
    assert profile.provider_id in monitor.profile_snapshot()

    # A second provider registered after construction is not visible until
    # the worker refreshes the snapshot (which refresh() does).
    second = _register(registry, store, base_url="https://third.example.net")
    assert second.provider_id not in monitor.profile_snapshot()

    monitor.refresh()
    assert second.provider_id in monitor.profile_snapshot()


def test_profile_snapshot_performs_no_sqlite_call_from_the_reading_thread(registry, store):
    """Same single-owner SQLite guarantee as evaluate_upload_decision(): the
    accessor reads the already-published in-memory dict, never `conn`."""
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse()))

    sql_call_threads = []
    real_registry = monitor._provider_registry

    class _TrackingRegistryProxy:
        def __getattr__(self, name):
            real_attr = getattr(real_registry, name)
            if not callable(real_attr):
                return real_attr

            def _tracked(*args, **kwargs):
                sql_call_threads.append(threading.current_thread())
                return real_attr(*args, **kwargs)

            return _tracked

    monitor._provider_registry = _TrackingRegistryProxy()

    result_holder = {}

    def _simulated_rest_call():
        result_holder["profile"] = monitor.profile_snapshot().get(profile.provider_id)

    rest_thread = threading.Thread(target=_simulated_rest_call)
    rest_thread.start()
    rest_thread.join(timeout=5)

    assert not rest_thread.is_alive()
    assert result_holder["profile"] is not None
    assert sql_call_threads == [], (
        f"profile_snapshot() executed SQL from the calling thread: {sql_call_threads}"
    )


# --- atomic publication of the connectivity snapshot (correction #3) --------


def test_snapshot_returns_the_single_published_immutable_object(registry, store):
    """`snapshot()` must return the one atomically-published object (never a
    fresh per-call copy assembled from the worker's live dicts), and its
    relay mapping must be genuinely immutable (a `MappingProxyType`), so a
    reader can neither observe a torn state nor mutate the published view."""
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse(status_code=200)))

    before = monitor.snapshot()
    assert before is monitor._published  # noqa: SLF001
    assert isinstance(before.relays, types.MappingProxyType)
    assert before.internet == InternetStatus.UNKNOWN

    monitor.refresh()
    after = monitor.snapshot()
    assert after is monitor._published  # noqa: SLF001
    assert isinstance(after.relays, types.MappingProxyType)
    assert profile.provider_id in after.relays

    # A reader can never mutate the published relay mapping.
    with pytest.raises(TypeError):
        after.relays["x"] = None  # type: ignore[index]


def test_published_snapshot_is_isolated_from_later_worker_mutation(registry, store):
    """The published snapshot copies the worker's live relay dict at publish
    time; mutating the live dict afterwards must not change a snapshot a
    reader already holds, and `snapshot()` must keep returning that same
    published object (it is not a live view of `_relay_statuses`)."""
    profile = _register(registry, store)
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(lambda m, u: _ScriptedResponse(status_code=200)))
    monitor.refresh()
    snap = monitor.snapshot()
    assert profile.provider_id in snap.relays

    # The worker mutates its live dict in place (as a subsequent refresh
    # would before republishing) - the already-published snapshot is a copy
    # and must be unaffected.
    del monitor._relay_statuses[profile.provider_id]  # noqa: SLF001
    assert profile.provider_id in snap.relays
    assert monitor.snapshot() is snap


def test_concurrent_readers_never_observe_a_partial_or_mutable_snapshot(registry, store):
    """Deterministic concurrent-reader invariant (correction #3): after the
    first complete publish, every snapshot a reader observes must carry the
    *complete* two-provider relay set (never some-providers-updated) as an
    immutable `MappingProxyType` - because `refresh()` republishes by one
    whole-object reference assignment and `snapshot()` returns that object.
    The two profiles are given distinct origins and driven to ONLINE (the
    `/v1/info` check is made inconclusive, so no fallback internet probe
    runs) so every refresh republishes a complete 2-relay set."""
    profile_a = _register(registry, store, display_name="A", base_url="https://relay-a.example.net")
    profile_b = _register(registry, store, display_name="B", base_url="https://relay-b.example.net")

    def handler(method, url):
        if "/v1/info" in url:
            raise requests.ConnectionError("info inconclusive for this test")
        return _ScriptedResponse(status_code=200)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    monitor.refresh(force=True)  # establish the complete 2-relay published set
    expected = {profile_a.provider_id, profile_b.provider_id}
    assert set(monitor.snapshot().relays) == expected

    stop = threading.Event()
    violations = []

    def reader():
        while not stop.is_set():
            snap = monitor.snapshot()
            if not isinstance(snap.relays, types.MappingProxyType):
                violations.append("mutable-relay-mapping")
                return
            if set(snap.relays) != expected:
                violations.append(f"partial-relay-set: {sorted(snap.relays)}")
                return

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        # Each refresh() constructs a fresh ThreadPoolExecutor(max_workers=2),
        # which is the slow part on Windows (~300ms/iteration for thread
        # creation). 30 republishes still gives the continuously-running
        # reader thousands of snapshot() observations to catch any non-atomic
        # swap, without the ~65s the 200-iteration loop took on Windows.
        for _ in range(30):
            monitor.refresh(force=True)
    finally:
        stop.set()
        reader_thread.join(timeout=5)
        assert not reader_thread.is_alive(), "reader thread did not stop"

    assert violations == [], violations
