"""Shared fixtures for importing server.py in a hardware-free CI/dev environment.

server.py does real work at *module import time* (git describe, Meshtastic
CLI/serial-port resolution, instance/profile bootstrap, camera route
registration) - it was written to run on a Raspberry Pi with a real radio
attached, not to be imported by a test runner. To test the parsing/validation
functions that live inside it (the actual point of this test suite - see
CLAUDE.md's "fragile by nature" note on the CLI-output parsers) without a Pi,
this fixture:

  1. Synthesizes a throwaway config.py (all required_vars + the extra
     variables server.py reads unconditionally at import time, e.g. the
     WEATHER_* block) pointed at a temp DATA_DIR, and puts it first on
     sys.path so `from config import *` resolves to it instead of any real
     config.py a developer might have locally.
  2. Points MESHTASTIC_CMD/MESHTASTIC_PORT at a fake-but-real executable
     file and a fake-but-real regular file, so
     meshsrv.runtime_identity.resolve_meshtastic_cli()/resolve_serial_port()
     succeed without any actual radio or `meshtastic` CLI install.
  3. Stubs `libcamera` in sys.modules - camera/camera.py does
     `from libcamera import Transform, controls` at module level, and that
     package only exists on a Pi with libcamera's Python bindings built
     against --system-site-packages (see CLAUDE.md). The stub only needs to
     exist for the import to succeed; actual camera capture is never
     exercised by these tests.
  4. Imports server exactly once per test session (re-importing a module
     this size, with this many module-level side effects, per test would be
     slow and is not needed - individual tests reset the bits of shared
     state they touch instead).

None of this changes server.py itself - it is pure test-side setup.
"""

import os
import stat
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _AutoVivifyingStub:
    """Returns another instance of itself for any attribute access, so code
    like `libcamera.controls.AwbModeEnum.Auto` resolves to *something*
    without needing to enumerate every enum libcamera happens to define -
    camera.py only ever reads these as opaque values to pass back into the
    (also absent) Picamera2 API, never inspects them."""

    def __getattr__(self, name):
        return _AutoVivifyingStub()

    def __call__(self, *args, **kwargs):
        return _AutoVivifyingStub()


def _stub_libcamera():
    if "libcamera" in sys.modules:
        return
    fake = types.ModuleType("libcamera")
    fake.Transform = _AutoVivifyingStub()
    fake.controls = _AutoVivifyingStub()
    sys.modules["libcamera"] = fake


def _write_fake_config(config_dir: Path, data_dir: Path, fake_meshtastic_cli: Path, fake_serial_port: Path) -> None:
    config_source = f'''
"""Synthetic config.py for the test suite - see tests/conftest.py."""
from pathlib import Path

APP_HOST = "127.0.0.1"
APP_PORT = 5000

MESHTASTIC_CMD = {str(fake_meshtastic_cli)!r}
MESHTASTIC_PORT = {str(fake_serial_port)!r}

LOCAL_NODE_ID = "!aabbccdd"
LOCAL_NODE_NAME = "Test Local Node"
INSTANCE_NAME = "test-instance"

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


@pytest.fixture(scope="session")
def server_module(tmp_path_factory):
    """Import server.py once, against a synthetic config/data dir. Returns the module."""
    sandbox = tmp_path_factory.mktemp("meshcenter_sandbox")
    config_dir = sandbox / "config_pkg"
    config_dir.mkdir()
    data_dir = sandbox / "data"
    data_dir.mkdir()

    fake_meshtastic_cli = sandbox / "fake_meshtastic"
    fake_meshtastic_cli.write_text("#!/bin/sh\necho fake meshtastic cli\n", encoding="utf-8")
    fake_meshtastic_cli.chmod(fake_meshtastic_cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    fake_serial_port = sandbox / "fake_serial_port"
    fake_serial_port.write_text("", encoding="utf-8")

    _write_fake_config(config_dir, data_dir, fake_meshtastic_cli, fake_serial_port)

    _stub_libcamera()

    sys.path.insert(0, str(config_dir))
    # A real config.py, if one exists in the repo root (a developer's local
    # install), must not shadow the synthetic one - config_dir was just
    # inserted ahead of it, but drop any cached "config" module too in case
    # something already imported it in this interpreter.
    sys.modules.pop("config", None)
    sys.modules.pop("server", None)

    try:
        import server as server_module_  # noqa: PLC0415
    finally:
        sys.path.remove(str(config_dir))

    return server_module_


@pytest.fixture(autouse=True)
def _reset_server_state(request):
    """Snapshot/restore server.py's mutable module-level dicts around every
    test that uses server_module, so tests can't leak node/chat/message state
    into each other."""
    if "server_module" not in request.fixturenames:
        yield
        return

    server = request.getfixturevalue("server_module")
    snapshots = {}
    for name in ("nodes", "chats", "messages", "settings", "seen_ids", "seen_recent_texts"):
        value = getattr(server, name, None)
        if isinstance(value, dict):
            snapshots[name] = dict(value)
        elif isinstance(value, list):
            snapshots[name] = list(value)
        elif isinstance(value, set):
            snapshots[name] = set(value)

    yield

    for name, snapshot in snapshots.items():
        current = getattr(server, name)
        current.clear()
        if isinstance(current, dict):
            current.update(snapshot)
        elif isinstance(current, list):
            current.extend(snapshot)
        elif isinstance(current, set):
            current.update(snapshot)
