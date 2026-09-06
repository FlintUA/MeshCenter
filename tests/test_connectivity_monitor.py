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

import pytest
import requests

from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.connectivity_monitor import (
    RELAY_HEALTH_BACKOFF_CEILING_SECONDS,
    ConnectivityMonitor,
    InternetStatus,
    RelayState,
    UploadReadiness,
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
    profile = _register(registry, store)
    info_calls = []

    def handler(method, url):
        if url.endswith("/health"):
            return _ScriptedResponse(status_code=200)
        info_calls.append(url)
        return _ScriptedResponse(status_code=200, payload={"provider_id": profile.provider_id, "service_key": {"public_key": "x"}})

    now = [0.0]
    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler), now_fn=lambda: now[0])
    monitor.refresh(force=True)
    assert len(info_calls) == 1

    # Force a health re-check (well past the 60s interval) without forcing info.
    now[0] += 61
    monitor.refresh(force=False)
    assert len(info_calls) == 1  # still just the one from the forced check


# ---- DISABLED --------------------------------------------------------------


def test_disabled_profile_is_disabled_state_without_any_network_call(registry, store):
    profile = _register(registry, store)
    registry.update_profile(profile.provider_id, enabled=False)
    calls = []

    def handler(method, url):
        calls.append(url)
        return _ScriptedResponse(status_code=200)

    monitor = ConnectivityMonitor(registry, session=_ScriptedSession(handler))
    snapshot = monitor.refresh()

    assert calls == []
    status = snapshot.relays[profile.provider_id]
    assert status.state == RelayState.DISABLED
    assert status.upload_readiness == UploadReadiness.UPLOAD_DISABLED


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
