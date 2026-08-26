"""Standalone smoke-test harness for a built
meshcenter-meshtastic-adapter-<version> archive.

Run as: <archive-venv>/bin/python _smoke_test_adapter_harness.py <extracted-archive-dir>

Unlike the Core harness, this doesn't attempt a real radio session -
no hardware in CI - the meaningful health check is import success
itself: it proves the archive's ADAPTER_WHITELIST actually contains
everything adapters/meshtastic/*.py needs at import time. That's a real
bug class here, not a hypothetical one - see build-release.sh's
ADAPTER_WHITELIST comment for the transitive meshsrv/hardware
dependency chain (adapters.meshtastic.serial_transport ->
meshsrv.node_time_sync -> meshsrv.time_service -> hardware.rtc_service
-> hardware.i2c_service) this smoke test exists to catch: an archive
containing only adapters/meshtastic/ imports would fail here with
ModuleNotFoundError: No module named 'meshsrv'.

Fails loud: any exception here is a real build defect.
"""
import sys
from pathlib import Path

if len(sys.argv) != 2:
    print("usage: _smoke_test_adapter_harness.py <extracted-archive-dir>", file=sys.stderr)
    sys.exit(2)

EXTRACTED_DIR = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(EXTRACTED_DIR))

print("==> importing adapters.meshtastic.serial_transport")
from adapters.meshtastic.serial_transport import SerialTransport  # noqa: E402

print("==> importing adapters.meshtastic.ble_transport")
from adapters.meshtastic.ble_transport import BLETransport  # noqa: E402

print("==> importing adapters.meshtastic.ipc_server")
from adapters.meshtastic import ipc_server  # noqa: E402,F401

print("==> checking both transport classes actually implement RadioTransport (Backend Protocol v1)")
from meshsrv.radio_transport import RadioTransport  # noqa: E402

assert issubclass(SerialTransport, RadioTransport), "SerialTransport no longer implements RadioTransport"
assert issubclass(BLETransport, RadioTransport), "BLETransport no longer implements RadioTransport"

print("==> adapter smoke test OK - archive is self-sufficient (adapters/ + its meshsrv/hardware closure)")
