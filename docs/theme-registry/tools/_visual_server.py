"""Boots a real, hardware-free server.py instance for visual_regression.py.

Same bootstrap approach as tests/conftest.py's `server_module` fixture
(synthetic config.py, fake Meshtastic CLI/serial port, stubbed libcamera)
but as a plain function rather than a pytest fixture, since this needs to
run outside a pytest session, and seeds a couple of synthetic nodes
(one favorited) so PR 3's favorite-card state has something real to
render. Deliberately NOT sharing code with tests/conftest.py - that
module's helpers are private (leading underscore) and test-suite-scoped;
duplicating this ~60-line bootstrap keeps the two independent so either
can evolve without the other silently breaking.

Does not call start_runtime() - background threads (radio listener,
telemetry worker) are neither needed nor wanted for rendering pages; the
synthetic nodes are seeded directly into the in-memory `nodes` dict.
"""
import logging
import socket
import stat
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _stub_libcamera():
    if "libcamera" in sys.modules:
        return

    class _AutoVivifyingStub:
        def __getattr__(self, name):
            return _AutoVivifyingStub()

        def __call__(self, *args, **kwargs):
            return _AutoVivifyingStub()

    fake = types.ModuleType("libcamera")
    fake.Transform = _AutoVivifyingStub()
    fake.controls = _AutoVivifyingStub()
    sys.modules["libcamera"] = fake


def _write_fake_config(config_dir: Path, data_dir: Path, fake_cli: Path, fake_port: Path, http_port: int) -> None:
    config_source = f'''
"""Synthetic config.py for the visual regression harness."""
APP_HOST = "127.0.0.1"
APP_PORT = {http_port}

MESHTASTIC_CMD = {str(fake_cli)!r}
MESHTASTIC_PORT = {str(fake_port)!r}

LOCAL_NODE_ID = "!aabbccdd"
LOCAL_NODE_NAME = "Test Local Node"
INSTANCE_NAME = "visual-regression"

DATA_DIR = {str(data_dir)!r}
HISTORY_FILE = str(DATA_DIR + "/messages.json")
NODES_FILE = str(DATA_DIR + "/nodes.json")
SENSORS_FILE = str(DATA_DIR + "/sensors.json")
CHATS_FILE = str(DATA_DIR + "/chats.json")

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 2026-01-01T12:00:00Z - shared with visual_regression.py's frozen
# client-side Date, for whatever genuinely reads the client clock (the
# "System clock" widget, sourced from the mocked /api/time response).
# NOT used for node last_seen/age below - see that function's own
# comment for why an absolute epoch is the wrong tool for that one.
FROZEN_EPOCH_SECONDS = 1767268800

# server.py's age_text() (~line 3248) computes `int(time.time() -
# last_seen)` - a real server-side wall-clock read with no client-side
# equivalent at all, confirmed by reading it directly: freezing the
# client's Date (FROZEN_EPOCH_SECONDS above) has zero effect on it. An
# absolute, fixed last_seen (e.g. FROZEN_EPOCH_SECONDS itself) would
# still leak real time indirectly: age_text()'s result would grow by
# exactly one calendar day every real day that passes between the
# baseline's capture date and whenever the suite is next run - caught
# live during this round's independent re-verification ("246 d" in the
# committed baseline vs "247 d" one real day later, despite identical
# code and identical seed data). Anchoring last_seen to time.time() minus
# a fixed offset, computed fresh at every boot, makes age_text()'s
# subtraction always land on the same fixed number of days regardless of
# which real calendar date the harness happens to run on.
NODE_AGE_SECONDS = 200 * 86400  # 200 days - arbitrary but fixed and round


def _seed_nodes(server_module) -> None:
    """One plain node and one favorited node, so .node-card.favorite has a
    real element to render (PR 3's bug 1.2 fix needs this to be
    screenshotted/inspected meaningfully, not just an empty node list)."""
    last_seen = time.time() - NODE_AGE_SECONDS
    server_module.nodes["!aabbccdd"] = {
        "node_id": "!aabbccdd", "name": "Visual Regression Base", "short_name": "VRB",
        "last_seen": last_seen, "favorite": False, "ignored": False,
    }
    server_module.nodes["!11223344"] = {
        "node_id": "!11223344", "name": "Favorited Test Node", "short_name": "FAV",
        "last_seen": last_seen, "favorite": True, "ignored": False,
    }


def boot_server(sandbox_dir: Path):
    """Imports server.py against a synthetic config/data dir, seeds test
    nodes, and starts it on a background thread on a free local port.
    Returns (server_module, base_url). Caller doesn't need to stop it -
    it's a daemon thread that dies with the process."""
    config_dir = sandbox_dir / "config_pkg"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir = sandbox_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    fake_cli = sandbox_dir / "fake_meshtastic"
    fake_cli.write_text("#!/bin/sh\necho fake meshtastic cli\n", encoding="utf-8")
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    fake_port = sandbox_dir / "fake_serial_port"
    fake_port.write_text("", encoding="utf-8")

    http_port = _free_port()
    _write_fake_config(config_dir, data_dir, fake_cli, fake_port, http_port)
    _stub_libcamera()

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(config_dir))
    sys.modules.pop("config", None)
    sys.modules.pop("server", None)
    try:
        import server as server_module  # noqa: PLC0415
    finally:
        sys.path.remove(str(config_dir))

    _seed_nodes(server_module)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # drop the per-request access log noise

    thread = threading.Thread(
        target=lambda: server_module.app.run(host="127.0.0.1", port=http_port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()

    base_url = f"http://127.0.0.1:{http_port}"
    deadline = time.time() + 15
    last_error = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base_url + "/", timeout=1)
            return server_module, base_url
        except Exception as exc:  # noqa: BLE001 - genuinely any startup failure should just retry
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"server did not come up at {base_url} within 15s: {last_error}")
