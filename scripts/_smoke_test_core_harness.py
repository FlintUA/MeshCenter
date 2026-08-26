"""Standalone (no pytest) smoke-test harness for a built
meshcenter-core-<version> archive.

Run as: <archive-venv>/bin/python _smoke_test_core_harness.py <extracted-archive-dir>

Synthesizes a throwaway config.py + a libcamera stub the same way
tests/conftest.py does for the dev test suite (see that file for why -
server.py does real work at import time and needs both to import
cleanly off a Pi), but standalone: a shipped release archive doesn't
include tests/, and this has to run against the extracted tree with
only what requirements.txt installed, not the dev/test dependencies.

The actual point of this harness (P0 #2/#3 stabilization follow-up):
prove the Core archive imports and serves a request with adapters/
*entirely absent from disk* - not just an unconfigured/missing venv,
which meshsrv/adapter_ipc_client.py's own unit tests already cover.
get_connection_info() is a pure local-cache read (see that module's
AdapterIPCTransport docstring) - it starts at ADAPTER_UNAVAILABLE and
never attempts to spawn the adapter subprocess on its own, so this is
safe to assert immediately after import, no risk of hanging or
touching real hardware in CI.

Fails loud: any exception here is a real build defect - a Core archive
that can't import or serve a basic request is broken, not just
"couldn't verify."
"""
import stat
import sys
import tempfile
import types
from pathlib import Path

if len(sys.argv) != 2:
    print("usage: _smoke_test_core_harness.py <extracted-archive-dir>", file=sys.stderr)
    sys.exit(2)

EXTRACTED_DIR = Path(sys.argv[1]).resolve()

print("==> checking adapters/ is physically absent from the extracted archive")
assert not (EXTRACTED_DIR / "adapters").exists(), (
    "adapters/ is present in the extracted Core archive - CORE_WHITELIST leaked it, "
    "or this ran against the wrong directory. The whole point of this smoke test is "
    "proving Core runs without adapters/ on disk, not just without its venv - fix the "
    "whitelist rather than the assumption."
)

sys.path.insert(0, str(EXTRACTED_DIR))

try:
    import adapters  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise AssertionError(
        "`import adapters` succeeded - some adapters package is importable "
        "(shadowed from outside the extracted archive?). This smoke test's guarantee "
        "only holds if adapters really isn't importable at all, not just absent as a "
        "directory under EXTRACTED_DIR."
    )


class _AutoVivifyingStub:
    """See tests/conftest.py's identical helper - camera.py only ever
    reads libcamera's enum-like values as opaque tokens, never inspects
    them, so a stub that resolves any attribute/call to itself is
    enough to satisfy the module-level `from libcamera import ...`."""

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


sandbox = Path(tempfile.mkdtemp(prefix="meshcenter_core_smoke_"))
config_dir = sandbox / "config_pkg"
config_dir.mkdir()
data_dir = sandbox / "data"
data_dir.mkdir()

fake_meshtastic_cli = sandbox / "fake_meshtastic"
fake_meshtastic_cli.write_text("#!/bin/sh\necho fake meshtastic cli\n", encoding="utf-8")
fake_meshtastic_cli.chmod(fake_meshtastic_cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

fake_serial_port = sandbox / "fake_serial_port"
fake_serial_port.write_text("", encoding="utf-8")

config_source = f'''
from pathlib import Path
APP_HOST = "127.0.0.1"
APP_PORT = 5000
MESHTASTIC_CMD = {str(fake_meshtastic_cli)!r}
MESHTASTIC_PORT = {str(fake_serial_port)!r}
LOCAL_NODE_ID = "!aabbccdd"
LOCAL_NODE_NAME = "Smoke Test Node"
INSTANCE_NAME = "smoke-test-instance"
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

_stub_libcamera()
sys.path.insert(0, str(config_dir))

print("==> importing server.py from the extracted core archive (adapters/ deliberately absent)")
import server  # noqa: E402

from meshsrv.radio_transport import ConnectionState, TransportErrorCode  # noqa: E402

print("==> checking transport status degrades to ADAPTER_UNAVAILABLE, not a crash")
info = server.serial_ipc_transport.get_connection_info()
assert info.state == ConnectionState.ERROR, f"expected ERROR state, got {info.state!r}"
assert info.last_error is not None and info.last_error.code == TransportErrorCode.ADAPTER_UNAVAILABLE, (
    f"expected ADAPTER_UNAVAILABLE, got {info.last_error!r}"
)

print("==> checking a basic HTTP round-trip")
client = server.app.test_client()
response = client.get("/api/base_status")
assert response.status_code == 200, f"expected 200 from /api/base_status, got {response.status_code}"

print("==> core smoke test OK - archive imports and serves without adapters/ present")
