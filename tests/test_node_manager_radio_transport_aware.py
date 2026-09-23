"""Tests for /api/node-manager/radio/detect and /accept's transport-aware
branching (Radio TCP Transport part 2, correction pass #3): both routes
used to unconditionally run a serial USB scan (detect_connected_radio())
regardless of which transport was actually accepted/active, even though
a live TCP connection and TCP profile activation (api_activate_radio_profile())
were already working elsewhere. See api_detect_new_radio()/
api_accept_detected_radio()'s own docstrings in server.py for the fix.

Correction pass #5 (below, "explicit TCP endpoint" section): pass #3
alone only ever reprobed the ACCEPTED radio's own transport - an
accepted-serial (or accepted-TCP-at-a-different-endpoint) profile had
no way to search for a genuinely NEW radio over TCP at all. An explicit
{"host": ..., "port"/"tcp_port": ...} request body now means "look for
a radio at THIS TCP endpoint", independent of the accepted profile's
own transport - see _detect_tcp_radio_response()/_accept_tcp_radio()'s
own docstrings in server.py.


Uses the server_module fixture (server.py already imported, start_runtime()
never called) with Flask's test_request_context() to call the route
functions directly with a real request context (so request.get_json()
works), rather than app.test_client()'s full WSGI dispatch - no test in
this suite drives server.py's own routes through test_client() (every
other server.py test calls functions directly - see
tests/test_server_startup_tcp_transport.py for the same convention), and
dispatch would additionally require reasoning about CSRF/auth middleware
this suite doesn't otherwise need to touch.
"""
import pytest


@pytest.fixture
def _preserve_node_manager_state(server_module):
    original_identity = server_module.instance_manager.get()
    yield
    server_module.instance_manager.save(original_identity)
    server_module.INSTANCE_IDENTITY = original_identity


def _set_accepted_radio(server_module, radio, active_profile_id=""):
    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = radio
    updated["active_profile_id"] = active_profile_id
    saved = server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = saved
    return saved


def _call(server_module, view_func, path, payload=None):
    with server_module.app.test_request_context(path, method="POST", json=payload or {}):
        result = view_func()
    if isinstance(result, tuple):
        response, status = result
    else:
        response, status = result, 200
    return response.get_json(), status


def _fake_tcp_detection(node_id="!deadc0de", long_name="Test TCP Radio", found=True, error=None, error_code=None):
    def _detect(transport, host, port, timeout=25):
        detected = {"node_id": node_id, "long_name": long_name, "short_name": "TST", "hardware": "TBEAM", "role": ""} if found else {}
        return ({
            "status": "MATCH" if found else "NOT_FOUND",
            "checked_at": "2026-09-23T00:00:00+00:00",
            "configured": {},
            "detected": detected,
            "error": error,
            "error_code": error_code,
        }, "")
    return _detect


# ---------------------------------------------------------------------------
# /api/node-manager/radio/detect
# ---------------------------------------------------------------------------

def test_serial_active_detect_still_runs_the_usb_scan(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #1: an active serial transport must reach the original,
    unmodified USB-scan code path exactly as before this correction."""
    _set_accepted_radio(server_module, {
        "node_id": "!aabbccdd", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
    })
    calls = []

    def _fake_detect_connected_radio(*a, **k):
        calls.append((a, k))
        return {"ok": False, "error": "no radio", "attempts": [], "candidates": []}

    monkeypatch.setattr(server_module, "detect_connected_radio", _fake_detect_connected_radio)
    # RadioConnectionManager.release()'s real port-busy check shells out to
    # a POSIX-only utility (unavailable on this Windows sandbox, unrelated
    # to this test's own concern) - report "already released" so the route
    # proceeds straight to the USB scan this test is actually about.
    monkeypatch.setattr(
        server_module.radio_connection_manager, "status", lambda listener_running=False: {"mode": "released"}
    )

    data, status = _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")

    assert len(calls) == 1
    assert status == 409
    assert data["code"] == "RADIO_NOT_FOUND"


def test_tcp_active_detect_never_runs_the_usb_scan(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #2: TCP-active must never fall through to a USB scan."""
    _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })

    def _fail_if_called(*a, **k):
        raise AssertionError("USB scan must not run while TCP is the active transport")

    monkeypatch.setattr(server_module, "detect_connected_radio", _fail_if_called)
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_tcp_detection())

    _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")
    # No AssertionError raised above = regression #2 holds.


def test_tcp_detect_uses_the_accepted_radios_saved_host_and_port(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #3: the endpoint probed comes from the accepted radio's
    own normalized record, not a USB scan or client input."""
    _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        return _fake_tcp_detection()(transport, host, port, timeout)

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")

    assert calls == [("192.168.2.34", 4403)]


def test_tcp_detect_returns_detected_node_and_profile_status(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #4: successful TCP detection reports the detected node
    and correct (non-faked) profile_exists/profile shape - no profile for
    this never-before-seen test node id exists yet."""
    _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_tcp_detection(node_id="!f00dcafe"))

    data, status = _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")

    assert status == 200
    assert data["ok"] is True
    assert data["detected"]["node_id"] == "!f00dcafe"
    assert data["profile_exists"] is False
    assert data["profile"] is None
    # Serial-only concepts, not faked for TCP.
    assert "candidates" not in data
    assert "attempts" not in data


def test_tcp_detect_failure_leaves_the_active_profile_untouched(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #7 (detect half): an unreachable TCP radio must not
    change INSTANCE_IDENTITY.active_profile_id/radio at all - detection is
    provisional/read-only."""
    before = _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, active_profile_id="deadc0de")
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_tcp_detection(found=False, error="connect_timeout", error_code="connect_timeout"))

    data, status = _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")

    assert status == 409
    assert data["code"] == "RADIO_NOT_FOUND"
    assert data["error_code"] == "connect_timeout"
    assert server_module.INSTANCE_IDENTITY["active_profile_id"] == before["active_profile_id"]
    assert server_module.INSTANCE_IDENTITY["radio"] == before["radio"]


def test_bluetooth_detect_is_explicitly_unsupported(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #8 (detect half): Bluetooth stays explicitly refused,
    never silently falls through to a USB scan."""
    _set_accepted_radio(server_module, {
        "node_id": "!756f9960", "transport": "bluetooth", "endpoint": {"address": "3C:DC:75:6F:99:61", "label": "FLT2"},
    })

    def _fail_if_called(*a, **k):
        raise AssertionError("no detection call should be made for an unsupported transport")

    monkeypatch.setattr(server_module, "detect_connected_radio", _fail_if_called)
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fail_if_called)

    data, status = _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")

    assert status == 501
    assert data["code"] == "TRANSPORT_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# /api/node-manager/radio/accept
# ---------------------------------------------------------------------------

def test_tcp_accept_reprobes_the_same_endpoint_detect_used(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #5: accept re-verifies the identical TCP endpoint -
    since both routes derive it the same way (from the accepted radio's
    own normalized record), a single fake capturing both calls proves
    they match without threading anything through the request body."""
    _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        return _fake_tcp_detection(node_id="!f00dcafe")(transport, host, port, timeout)

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")
    data, status = _call(server_module, server_module.api_accept_detected_radio, "/api/node-manager/radio/accept", {"node_id": "!f00dcafe"})

    assert status == 202
    assert data["ok"] is True
    assert calls == [("192.168.2.34", 4403), ("192.168.2.34", 4403)]

    profile = server_module.profile_manager.get_profile(data["profile_id"])
    assert profile["metadata"]["radio"]["transport"] == "tcp"
    assert profile["metadata"]["radio"]["endpoint"] == {"host": "192.168.2.34", "port": 4403}


def test_tcp_accept_rejects_a_radio_that_changed_since_detection(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #6: a node_id mismatch between what the caller believed
    was detected and what accept's own fresh re-probe finds blocks
    acceptance - the active profile/identity must stay untouched."""
    before = _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, active_profile_id="deadc0de")
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_tcp_detection(node_id="!f00dcafe"))

    data, status = _call(
        server_module, server_module.api_accept_detected_radio, "/api/node-manager/radio/accept",
        {"node_id": "!11112222"},
    )

    assert status == 409
    assert data["code"] == "RADIO_CHANGED"
    assert server_module.INSTANCE_IDENTITY["active_profile_id"] == before["active_profile_id"]


def test_tcp_accept_failure_leaves_the_active_profile_untouched(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #7 (accept half): a failed re-probe at accept time must
    not create a profile or touch INSTANCE_IDENTITY."""
    before = _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, active_profile_id="deadc0de")
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_tcp_detection(found=False, error="connect_refused", error_code="connect_refused"))

    data, status = _call(server_module, server_module.api_accept_detected_radio, "/api/node-manager/radio/accept")

    assert status == 409
    assert data["code"] == "RADIO_NOT_FOUND"
    assert server_module.INSTANCE_IDENTITY["active_profile_id"] == before["active_profile_id"]
    assert server_module.INSTANCE_IDENTITY["radio"] == before["radio"]


def test_bluetooth_accept_is_explicitly_unsupported(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #8 (accept half)."""
    _set_accepted_radio(server_module, {
        "node_id": "!756f9960", "transport": "bluetooth", "endpoint": {"address": "3C:DC:75:6F:99:61", "label": "FLT2"},
    })

    def _fail_if_called(*a, **k):
        raise AssertionError("no detection call should be made for an unsupported transport")

    monkeypatch.setattr(server_module, "detect_connected_radio", _fail_if_called)
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fail_if_called)

    data, status = _call(server_module, server_module.api_accept_detected_radio, "/api/node-manager/radio/accept")

    assert status == 501
    assert data["code"] == "TRANSPORT_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# Correction pass #5: explicit TCP endpoint in the request body - searching
# a genuinely NEW radio over TCP, independent of the accepted profile's own
# transport (the gap pass #3 alone left: accepted=serial had no way to
# discover a new radio over TCP at all).
# ---------------------------------------------------------------------------

def test_explicit_tcp_detect_probes_that_endpoint_when_accepted_is_serial(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #1: accepted profile = serial, discover with an
    explicit TCP host/port probes THAT endpoint, not a USB scan."""
    _set_accepted_radio(server_module, {
        "node_id": "!067a40fa", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
    })

    def _fail_if_called(*a, **k):
        raise AssertionError("USB scan must not run when an explicit TCP host/port is given")

    monkeypatch.setattr(server_module, "detect_connected_radio", _fail_if_called)
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        return _fake_tcp_detection(node_id="!f00dcafe")(transport, host, port, timeout)

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    data, status = _call(
        server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect",
        {"host": "192.168.2.34", "port": 4403},
    )

    assert status == 200
    assert data["ok"] is True
    assert data["detected"]["node_id"] == "!f00dcafe"
    assert calls == [("192.168.2.34", 4403)]


def test_detect_without_host_still_reprobes_the_accepted_transport(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #2: no host/port given -> unchanged pass-#3 behavior
    (reprobe the accepted radio's own transport)."""
    _set_accepted_radio(server_module, {
        "node_id": "!deadc0de", "transport": "tcp", "endpoint": {"host": "10.0.0.5", "port": 4403},
    })
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        return _fake_tcp_detection(node_id="!deadc0de")(transport, host, port, timeout)

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    data, status = _call(server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect")

    assert status == 200
    assert calls == [("10.0.0.5", 4403)]  # the ACCEPTED endpoint, not a new one


def test_explicit_tcp_port_defaults_and_validates(server_module, _preserve_node_manager_state, monkeypatch):
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_tcp_detection(node_id="!f00dcafe"))

    data, status = _call(
        server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect",
        {"host": "192.168.2.34"},
    )
    assert status == 200  # defaults to 4403, no error

    data, status = _call(
        server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect",
        {"host": "192.168.2.34", "port": "not-a-number"},
    )
    assert status == 400
    assert data["error_code"] == "tcp_port_invalid"

    data, status = _call(
        server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect",
        {"host": "192.168.2.34", "port": 70000},
    )
    assert status == 400
    assert data["error_code"] == "tcp_port_invalid"


def test_explicit_tcp_accept_creates_profile_for_that_endpoint_while_serial_is_accepted(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #3: accept with an explicitly-passed TCP endpoint
    creates/confirms a profile for exactly that endpoint, not the
    currently-accepted (serial) profile's own transport."""
    before = _set_accepted_radio(server_module, {
        "node_id": "!067a40fa", "long_name": "Flint Base", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
    }, active_profile_id="067a40fa")
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        return _fake_tcp_detection(node_id="!f00dcafe", long_name="T-Beam")(transport, host, port, timeout)

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    data, status = _call(
        server_module, server_module.api_accept_detected_radio, "/api/node-manager/radio/accept",
        {"host": "192.168.2.34", "tcp_port": 4403, "node_id": "!f00dcafe"},
    )

    assert status == 202
    assert data["ok"] is True
    assert calls == [("192.168.2.34", 4403)]
    assert data["radio"]["node_id"] == "!f00dcafe"
    assert data["radio"]["transport"] == "tcp"
    assert data["radio"]["endpoint"] == {"host": "192.168.2.34", "port": 4403}

    profile = server_module.profile_manager.get_profile(data["profile_id"])
    assert profile["metadata"]["radio"]["node_id"] == "!f00dcafe"
    assert profile["metadata"]["radio"]["transport"] == "tcp"

    # Regression #4: the NEW radio becomes the active profile only because
    # this was an explicit, confirmed /accept call (never automatic) -
    # before this call, the accepted identity was still Flint Base/serial.
    assert before["radio"]["node_id"] == "!067a40fa"
    assert server_module.INSTANCE_IDENTITY["active_profile_id"] == data["profile_id"]


def test_explicit_tcp_detect_does_not_touch_the_currently_accepted_profile(server_module, _preserve_node_manager_state, monkeypatch):
    """Regression #4 (detect half): provisional discovery via an explicit
    TCP endpoint never mutates the currently-accepted profile/identity -
    only /accept (an explicit, separate confirmation) can."""
    before = _set_accepted_radio(server_module, {
        "node_id": "!067a40fa", "long_name": "Flint Base", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
    }, active_profile_id="067a40fa")
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_tcp_detection(node_id="!f00dcafe"))

    _call(
        server_module, server_module.api_detect_new_radio, "/api/node-manager/radio/detect",
        {"host": "192.168.2.34", "port": 4403},
    )

    assert server_module.INSTANCE_IDENTITY["radio"] == before["radio"]
    assert server_module.INSTANCE_IDENTITY["active_profile_id"] == before["active_profile_id"]
