"""Route-level tests for the TCP probe migration (TCP lifecycle P0, PR-B):
Discovery (/radio/detect), Accept (/radio/accept) and profile activation now
use the ephemeral probe, and Discovery -> Accept costs ONE probe, not two.

The view functions are called directly inside a test request context (auth/
CSRF are not what's under test - see tests/test_api_csrf.py). What IS under
test: how many probes each flow runs, that Accept never trusts a client-
supplied node_id on its own, that RADIO_CHANGED/RADIO_NOT_FOUND still fire,
and that a lost probe race surfaces as DETECTION_IN_PROGRESS.
"""
import pytest

from meshsrv.detection_cache import DetectionCache
from meshsrv.radio_identity import DETECTION_IN_PROGRESS

HOST, PORT = "192.168.2.34", 4403
NODE = "!1fa065f0"


class _Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


def _detection(node_id=NODE, error=None, error_code=None):
    detected = {"node_id": node_id, "long_name": "T-Beam", "short_name": "TBM", "hardware": "TBEAM"} if node_id else {}
    return (
        {
            "status": "MATCH" if node_id else "DETECTION_ERROR",
            "checked_at": "2026-09-25T10:00:00+00:00",
            "configured": {"host": HOST, "port": PORT},
            "detected": detected,
            "error": error,
            "error_code": error_code,
        },
        "",
    )


@pytest.fixture
def env(server_module, monkeypatch):
    original_identity = server_module.instance_manager.get()
    original_radio_result = dict(server_module.RADIO_IDENTITY_RESULT)

    clock = _Clock()
    monkeypatch.setattr(server_module, "tcp_detection_cache", DetectionCache(clock=clock))

    probes = []
    queue = []

    def _fake_probe(host, port, timeout=25):
        probes.append((host, port))
        return queue.pop(0) if queue else _detection()

    monkeypatch.setattr(server_module, "_probe_tcp_identity", _fake_probe)
    monkeypatch.setattr(server_module, "_restart_meshcenter_after_profile_switch", lambda: None)
    monkeypatch.setattr(
        server_module.profile_manager,
        "get_profile",
        lambda profile_id: {"profile_id": profile_id, "metadata": {"radio": {"node_id": NODE}}},
    )

    def _boom(*a, **k):
        raise AssertionError("no profile should have to be created in these tests")

    monkeypatch.setattr(server_module.profile_manager, "create_clean_profile", _boom)

    yield {"server": server_module, "probes": probes, "queue": queue, "clock": clock}

    server_module.instance_manager.save(original_identity)
    server_module.INSTANCE_IDENTITY = original_identity
    server_module.RADIO_IDENTITY_RESULT = original_radio_result


def _call(server_module, view, body, *args):
    with server_module.app.test_request_context("/x", method="POST", json=body):
        result = view(*args)
    if isinstance(result, tuple):
        return result[0].get_json(), result[1]
    return result.get_json(), result.status_code


def _detect(env, **body):
    return _call(env["server"], env["server"].api_detect_new_radio, {"host": HOST, "port": PORT, **body})


def _accept(env, **body):
    return _call(env["server"], env["server"].api_accept_detected_radio, {"host": HOST, "tcp_port": PORT, **body})


# --- Discovery -> Accept is one probe ------------------------------------


def test_detect_then_accept_runs_exactly_one_probe(env):
    data, status = _detect(env)
    assert status == 200 and data["ok"] is True

    data, status = _accept(env, node_id=NODE)

    assert status == 202 and data["ok"] is True
    assert len(env["probes"]) == 1, "Accept must reuse the probe Discovery just ran"


def test_accept_without_a_prior_detect_runs_one_fresh_probe(env):
    data, status = _accept(env, node_id=NODE)

    assert status == 202 and data["ok"] is True
    assert len(env["probes"]) == 1


def test_the_cache_entry_is_single_use(env):
    _detect(env)
    _accept(env, node_id=NODE)

    _accept(env, node_id=NODE)

    assert len(env["probes"]) == 2, "the second Accept must not replay the first detection"


def test_accept_after_the_ttl_falls_back_to_a_fresh_probe_not_an_error(env):
    _detect(env)
    env["clock"].now += 61

    data, status = _accept(env, node_id=NODE)

    assert status == 202 and data["ok"] is True
    assert len(env["probes"]) == 2


def test_accept_never_trusts_a_client_node_id_that_differs_from_the_cached_detection(env):
    """Cached detection says NODE; the client claims to be confirming
    another radio. Must not use the cache - a fresh probe decides, and the
    RADIO_CHANGED check still fires against what the probe actually saw."""
    _detect(env)

    data, status = _accept(env, node_id="!deadbeef")

    assert status == 409 and data["code"] == "RADIO_CHANGED"
    assert len(env["probes"]) == 2


def test_accept_fresh_probe_that_finds_nothing_is_radio_not_found(env):
    env["queue"].append(_detection(node_id=None, error="connect_failed", error_code="connect_failed"))

    data, status = _accept(env, node_id=NODE)

    assert status == 409 and data["code"] == "RADIO_NOT_FOUND"


def test_a_failed_detect_does_not_populate_the_cache(env):
    env["queue"].append(_detection(node_id=None, error="refused", error_code="connect_refused"))
    data, status = _detect(env)
    assert status == 409 and data["code"] == "RADIO_NOT_FOUND"

    _accept(env, node_id=NODE)

    assert len(env["probes"]) == 2, "nothing was cached, so Accept had to probe"


# --- lost probe race -------------------------------------------------------


def test_detect_reports_detection_in_progress(env):
    env["queue"].append(_detection(node_id=None, error="Another radio detection is already in progress.",
                                   error_code=DETECTION_IN_PROGRESS))

    data, status = _detect(env)

    assert status == 409
    assert data["code"] == "DETECTION_IN_PROGRESS"
    assert "in progress" in data["error"]


def test_accept_reports_detection_in_progress_without_touching_the_profile(env):
    env["queue"].append(_detection(node_id=None, error="Another radio detection is already in progress.",
                                   error_code=DETECTION_IN_PROGRESS))
    before = env["server"].instance_manager.get()

    data, status = _accept(env, node_id=NODE)

    assert status == 409 and data["code"] == "DETECTION_IN_PROGRESS"
    assert env["server"].instance_manager.get() == before


# --- profile activation ------------------------------------------------------


def _tcp_profile(env, monkeypatch, node_id="!b0f14d2a"):
    metadata = {
        "radio": {
            "node_id": node_id,
            "long_name": "Other",
            "transport": "tcp",
            "endpoint": {"host": "192.168.2.50", "port": 4403},
        }
    }
    monkeypatch.setattr(
        env["server"].profile_manager,
        "get_profile",
        lambda profile_id: {"profile_id": profile_id, "metadata": metadata},
    )


def test_activate_tcp_profile_uses_the_probe_once_on_its_own_endpoint(env, monkeypatch):
    _tcp_profile(env, monkeypatch)
    env["queue"].append(_detection(node_id="!b0f14d2a"))

    data, status = _call(env["server"], env["server"].api_activate_radio_profile, {}, "b0f14d2a")

    assert status == 202 and data["ok"] is True
    assert env["probes"] == [("192.168.2.50", 4403)]


def test_activate_tcp_profile_mismatch_is_still_refused(env, monkeypatch):
    _tcp_profile(env, monkeypatch)
    env["queue"].append(_detection(node_id="!aaaaaaaa"))

    data, status = _call(env["server"], env["server"].api_activate_radio_profile, {}, "b0f14d2a")

    assert status == 409 and data["code"] == "RADIO_MISMATCH"


def test_activate_reports_detection_in_progress(env, monkeypatch):
    _tcp_profile(env, monkeypatch)
    env["queue"].append(_detection(node_id=None, error="Another radio detection is already in progress.",
                                   error_code=DETECTION_IN_PROGRESS))

    data, status = _call(env["server"], env["server"].api_activate_radio_profile, {}, "b0f14d2a")

    assert status == 409 and data["code"] == "DETECTION_IN_PROGRESS"
