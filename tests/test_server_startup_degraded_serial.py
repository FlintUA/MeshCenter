"""Regression test for the Task 47 live finding on TAP2: physically
disconnecting the Meshtastic radio and restarting the service put
gunicorn into a crash-restart loop, because server.py's module-level
resolve_serial_port(MESHTASTIC_PORT) call raised RuntimeError and that
was converted straight to SystemExit(1) - before app = Flask(...) even
exists, so no degraded mode was possible; the whole module failed to
import.

This does its own isolated import of server.py (separate from the
shared session-scoped server_module fixture in conftest.py, which
always points at a serial port file that exists) against a synthetic
config pointing MESHTASTIC_PORT at a path that does not exist, and
asserts that import now succeeds (no SystemExit) rather than mocking
resolve_serial_port() in isolation - the point is to prove the actual
module-level call site behaves correctly, not just the helper function
in meshsrv/runtime_identity.py it calls (that function's own behavior -
raising RuntimeError for a missing port - is correct and unchanged;
what changed is how server.py's own startup code reacts to it).
"""
import stat
import sys
import types
from pathlib import Path

import pytest


def _stub_libcamera():
    if "libcamera" in sys.modules:
        return
    fake = types.ModuleType("libcamera")

    class _Stub:
        def __getattr__(self, name):
            return _Stub()

        def __call__(self, *args, **kwargs):
            return _Stub()

    fake.Transform = _Stub()
    fake.controls = _Stub()
    sys.modules["libcamera"] = fake


def _write_fake_config(config_dir: Path, data_dir: Path, fake_meshtastic_cli: Path, missing_serial_port: Path) -> None:
    config_source = f'''
"""Synthetic config.py pointing at a serial port that does not exist -
see tests/test_server_startup_degraded_serial.py."""
from pathlib import Path

APP_HOST = "127.0.0.1"
APP_PORT = 5001

MESHTASTIC_CMD = {str(fake_meshtastic_cli)!r}
MESHTASTIC_PORT = {str(missing_serial_port)!r}

LOCAL_NODE_ID = "!aabbccdd"
LOCAL_NODE_NAME = "Test Local Node"
INSTANCE_NAME = "test-instance-degraded-serial"

DATA_DIR = {str(data_dir)!r}
HISTORY_FILE = str(Path(DATA_DIR) / "messages.json")
NODES_FILE = str(Path(DATA_DIR) / "nodes.json")
SENSORS_FILE = str(Path(DATA_DIR) / "sensors.json")
CHATS_FILE = str(Path(DATA_DIR) / "chats.json")

MAX_HISTORY_MESSAGES = 1000
CHANNEL_CHAT_ID = "channel"
CHANNEL_CHAT_NAME = "LongFast"

KNOWN_NODES = {{}}
KNOWN_NODE_INFO = {{}}

OPENWEATHER_API_KEY = ""
WEATHERAPI_API_KEY = ""
WEATHER_PROVIDER = "openweather"
WEATHER_LATITUDE = None
WEATHER_LONGITUDE = None
WEATHER_LOCATION_NAME = ""
WEATHER_LANGUAGE = "en"
WEATHER_CACHE_SECONDS = 600

EPAPER_ENABLED = False
AUTH_ENABLED = False
AUTH_PASSWORD_HASH = ""
'''
    (config_dir / "config.py").write_text(config_source, encoding="utf-8")


def test_server_imports_successfully_with_no_serial_radio_connected(tmp_path, capsys):
    sandbox = tmp_path
    config_dir = sandbox / "config_pkg"
    config_dir.mkdir()
    data_dir = sandbox / "data"
    data_dir.mkdir()

    fake_meshtastic_cli = sandbox / "fake_meshtastic"
    fake_meshtastic_cli.write_text("#!/bin/sh\necho fake meshtastic cli\n", encoding="utf-8")
    fake_meshtastic_cli.chmod(fake_meshtastic_cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Deliberately never created - simulates the radio being physically
    # unplugged at process startup.
    missing_serial_port = sandbox / "no_such_serial_port"
    assert not missing_serial_port.exists()

    _write_fake_config(config_dir, data_dir, fake_meshtastic_cli, missing_serial_port)
    _stub_libcamera()

    sys.path.insert(0, str(config_dir))
    sys.modules.pop("config", None)
    sys.modules.pop("server", None)

    try:
        import server as server_module  # noqa: PLC0415 - deliberately imported inside the test, not at module scope
    except SystemExit as exit_error:
        pytest.fail(
            f"server.py raised SystemExit({exit_error.code}) on import with no serial "
            "radio connected - it must start in a degraded state instead, per the "
            "Task 47 fix. Missing serial port is a recoverable, expected-to-happen "
            "condition, not a broken installation."
        )
    finally:
        sys.path.remove(str(config_dir))
        # Leave this test's server module out of sys.modules afterward -
        # other tests' server_module fixture (conftest.py, session-scoped,
        # pointed at a real fake port) must import its own fresh copy, not
        # reuse this degraded one.
        sys.modules.pop("server", None)

    assert hasattr(server_module, "app"), "Flask app must exist even when the serial port is missing"

    # The port is left as configured (unresolved) rather than silently
    # replaced with something misleading - everything downstream already
    # tolerates a stale/nonexistent MESHTASTIC_PORT (see the review
    # discussion this round: SerialTransport.__init__ just stores it,
    # detect_radio_identity() catches its own failures, run_listener()
    # never starts unless identity status is MATCH).
    assert server_module.MESHTASTIC_PORT == str(missing_serial_port)

    captured = capsys.readouterr()
    assert "MESHTASTIC SERIAL PORT NOT FOUND" in captured.out
