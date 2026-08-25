"""Task 48 Core-side IPC client: spawns/supervises the adapter
subprocess and implements RadioTransport by talking to it over
newline-delimited JSON on stdin/stdout (docs/BACKEND_API.md's wire
shape, plus the stateless `transport_type` routing field - see
adapters/meshtastic/ipc_server.py's module docstring for why).

THREE-TIER TIMEOUT CONTRACT (Task 48 review, explicit ordering):
  (c) TransportRouter's caller-declared budget (unchanged, Task 47.5)
      splits into lock-wait-remaining + this proxy's own call budget.
  (b) THIS module's AdapterSupervisor.call(): waits up to the full
      caller-declared budget for a response line, using it verbatim as
      both its own read-deadline AND the `timeout` field sent in the
      request. On expiry: kills the adapter subprocess (SIGKILL) and
      raises TransportError(TIMEOUT) - the caller's thread is released
      either way, never left hanging on a wedged adapter.
  (a) The adapter's own internal TimeoutEnforced watchdog (unchanged,
      inside SerialTransport/BLETransport) uses timeout MINUS a fixed
      margin (_ADAPTER_TIMEOUT_MARGIN_S, applied in ipc_server.py) - so
      the adapter always has a window to report its own graceful
      TransportError(TIMEOUT) over the pipe before (b)'s clock would
      fire and kill it for the same call. (a) < (b) is the whole point;
      breaking that ordering would mean Core kills adapters that were
      about to answer correctly.

BLE OS-LEVEL CLEANUP ON KILL (Task 48 review): a SIGKILL'd adapter
process's file descriptors (a serial port) are unconditionally reclaimed
by the kernel - nothing extra needed there. A BLE GATT session is
brokered through BlueZ over D-Bus and can outlive the process that
opened it (live-confirmed, Task 43) - so a kill that happened mid-BLE-
operation runs `bluetoothctl disconnect <address>` itself, from Core's
own process (a system utility invocation, not a meshtastic import - same
arm's-length-CLI license reasoning already established for
`meshtastic --info`/`--listen`), mirroring
BLETransport._force_disconnect_os_level()'s existing logic, re-homed to
Core since the adapter that would normally run it is the thing that just
died.

ORPHANED-ADAPTER PROTECTION IF CORE ITSELF DIES (Task 48 review, a gap
in the original design): the kill/respawn logic above covers a wedged
*adapter*. It does not by itself cover Core dying unexpectedly (crash,
external kill -9, OOM-killer) with the adapter subprocess still alive -
two independent layers handle that instead of one, since neither alone
is complete:
  1. deploy/meshcenter.service's KillMode=control-group (systemd's
     default, live-confirmed via `systemctl show -p KillMode` on TAP2,
     not assumed from documentation) - a normal `systemctl restart`/
     `stop`, and the Restart=on-failure auto-restart path (systemd's
     cgroup tracking reacts to any unexpected exit the same way, crash
     or otherwise), take the whole cgroup down, adapter included, since
     it's never detached from Core's process group below.
  2. PR_SET_PDEATHSIG (_set_pdeathsig_to_sigkill(), Linux-only, wired
     via preexec_fn below) - covers the case (1) doesn't: Core running
     outside systemd entirely (`python server.py`, this project's own
     everyday dev/test workflow, not just an exotic prod edge case). The
     kernel delivers SIGKILL to the adapter the moment its parent dies,
     by any means, with no systemd/cgroup involvement needed.
Neither layer is optional-if-the-other-exists; they cover genuinely
different deployment shapes.

Not wired into server.py yet - this is the Core-side proxy itself,
buildable and testable independent of server.py's construction site,
which is the next increment.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional, Sequence

from meshsrv import ipc_protocol
from meshsrv.radio_transport import (
    ChannelInfo,
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    NodeInfo,
    OutgoingMessage,
    OutgoingWaypoint,
    RadioTransport,
    SendResult,
    TransportError,
    TransportErrorCode,
    WaypointResult,
)

# Task 48 review: starting, reasoned estimate - not yet live-measured on
# real hardware (that is Task 48's own live-verification step, same
# discipline as every other timeout constant in this project, e.g. BLE's
# 90s/30s history). Applied inside ipc_server.py, not here - Core sends
# the FULL, un-reduced timeout in the request; the adapter is the one
# that subtracts this margin before using it as its own internal budget.
ADAPTER_TIMEOUT_MARGIN_S = 2.0

# How long the supervisor waits for the adapter process to exit cleanly
# after SIGKILL before giving up on the wait() call itself (the kill
# itself is unconditional either way - this only bounds how long we wait
# to confirm it, so a stuck adapter can never wedge the killer too).
_KILL_WAIT_TIMEOUT_S = 5.0

_BLUETOOTHCTL_DISCONNECT_TIMEOUT_S = 10.0

# Task 48 review, unaddressed gap in the original design: KillMode=
# control-group in deploy/meshcenter.service (confirmed live on TAP2 via
# `systemctl show -p KillMode` - not assumed from documentation) means a
# normal `systemctl restart`/`stop`, AND the Restart=on-failure
# auto-restart path (systemd's cgroup tracking reacts the same way to
# any unexpected exit of the tracked process - crash, external kill -9,
# OOM-killer), take the adapter subprocess down with it, since
# subprocess.Popen() below never detaches it from the parent's process
# group. But that protection is systemd-specific - it does not exist for
# a plain `python server.py` run (this project's own everyday dev/test
# workflow, not just an exotic prod edge case), where a hard-killed Core
# process would leave the adapter orphaned - and if it held a live BLE
# GATT session at that moment, the exact stale-OS-session problem
# already caught live three times this project (Task 43/45/47), now at
# the process level instead of the thread level.
#
# PR_SET_PDEATHSIG (Linux-only; irrelevant to gate on since this project
# only ever runs on a Pi) tells the kernel to deliver SIGKILL to the
# child the moment its parent thread/process dies, by any means -
# crash, kill -9, OOM - with no systemd/cgroup involvement needed. Set
# via preexec_fn, which subprocess.Popen runs in the child right after
# fork(), before exec() - the standard Python idiom for this, not a
# novel mechanism. subprocess.Popen doesn't support preexec_fn on
# Windows at all (raises ValueError if passed there), hence the
# sys.platform guard - this project's tests (this dev machine is
# Windows) exercise the rest of AdapterSupervisor without it.
_PR_SET_PDEATHSIG = 1

# SAFETY (review finding, real bug in this module's first draft - not a
# hypothetical): Python's subprocess docs warn preexec_fn is unsafe in a
# multi-threaded process, and Core genuinely is one (radio_lock,
# pause_listen, TransportRouter's own lock, this module's own watchdog/
# reader threads, run_listener()'s thread, ...). The hazard is real and
# specific here, not generic caution: ctypes.CDLL(...) calls dlopen()
# internally (confirmed by reading CPython's own ctypes.CDLL.__init__ -
# `self._handle = self._load_library(...)`), and dlopen()/dlsym() (the
# latter triggered by resolving .prctl as an attribute) both take
# glibc's internal dynamic-linker lock and can allocate memory. If
# ANOTHER thread in Core held that lock (or malloc's arena lock) at the
# exact instant fork() happened, the CHILD inherits it already locked
# forever - no thread left to release it - and preexec_fn deadlocks
# silently before the adapter can even exec(). The first draft of this
# module called ctypes.CDLL() *inside* the preexec_fn body itself - which
# runs post-fork, in the unsafe window, on every single respawn while
# Core's other threads are genuinely live and busy. Fixed by resolving
# the library handle AND binding the prctl symbol (attribute access
# triggers dlsym(), same lock) HERE, at module import time in the
# parent - well before any fork() - so preexec_fn's own body, below,
# calls only an already-bound C function: a bare prctl() syscall
# trampoline, no dynamic-linker interaction, no allocation, nothing that
# could contend with a lock some other Core thread held at fork time.
try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    _libc.prctl.restype = ctypes.c_int
except Exception:
    _libc = None


def _set_pdeathsig_to_sigkill() -> None:
    """preexec_fn body - runs in the child, after fork(), before exec().
    Calls only the already-resolved _libc.prctl (see the module-load-time
    block above for why resolution happens there, not here) - no
    dlopen()/dlsym()/allocation in this function itself. Best-effort: if
    _libc failed to resolve at import time (non-Linux, exotic libc), or
    the syscall itself is rejected for some reason, this silently does
    nothing rather than preventing the adapter from starting - defense
    in depth on top of the KillMode=control-group protection above, not
    the only thing standing between a dead Core and an orphaned adapter."""
    if _libc is None:
        return
    try:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except Exception:
        pass


def _adapter_unavailable_info(node_id: Optional[str] = None) -> ConnectionInfo:
    """Task 48 review requirement: the cache's initial/never-reached
    state must be explicitly distinguishable from an ordinary
    DISCONNECTED radio, not a default-dataclass-value accident. State is
    ERROR (never CONNECTING/DISCONNECTED, which both read as "a normal
    radio-level condition") with ADAPTER_UNAVAILABLE specifically in
    last_error, so a caller/UI can tell "radio not connected" apart from
    "the whole adapter is missing" without string-matching a message."""
    return ConnectionInfo(
        state=ConnectionState.ERROR,
        descriptor=None,
        node_id=node_id,
        connected_since=None,
        last_error=TransportError(
            TransportErrorCode.ADAPTER_UNAVAILABLE,
            "adapter subprocess has not been successfully reached yet",
        ),
    )


class AdapterSupervisor:
    """Owns the adapter subprocess's lifecycle - spawn, one request/
    response round-trip with a hard deadline, kill+respawn. Shared by
    every AdapterIPCTransport instance (one persistent adapter process
    multiplexed by the `transport_type` field per request, not one
    process per transport type - see ipc_server.py)."""

    def __init__(
        self,
        *,
        adapter_python: str,
        project_dir: str,
        serial_port: str,
        meshtastic_cli: str,
        on_log: Optional[Callable[[str, str], None]] = None,
        command: Optional[list[str]] = None,
    ) -> None:
        self._adapter_python = adapter_python
        self._project_dir = project_dir
        self._serial_port = serial_port
        self._meshtastic_cli = meshtastic_cli
        self._on_log = on_log or (lambda msg, level="INFO": None)
        # Test-only seam: a real adapter needs meshtastic/bleak installed,
        # which tests deliberately don't depend on - passing an explicit
        # `command` (e.g. spawning a tiny fake-adapter script instead of
        # `-m adapters.meshtastic.ipc_server`) lets tests exercise this
        # class's real spawn/write/read/kill/respawn behavior against a
        # real subprocess and real pipes, without any adapter dependency.
        # None (the production path) builds the real invocation below.
        self._command_override = command
        # Guards the whole spawn/write/read/kill sequence - TransportRouter
        # already serializes callers to at most one in-flight IPC call
        # (Task 47.5), this is defense in depth, not the primary guarantee.
        self._proc_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

    def _spawn_locked(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(self._project_dir)}
        command = self._command_override or [
            str(self._adapter_python),
            "-m",
            "adapters.meshtastic.ipc_server",
            "--serial-port",
            str(self._serial_port),
            "--meshtastic-cli",
            str(self._meshtastic_cli),
        ]
        self._proc = subprocess.Popen(
            command,
            cwd=str(self._project_dir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            preexec_fn=_set_pdeathsig_to_sigkill if sys.platform == "linux" else None,
        )

    def call(self, request: dict, *, timeout: float, ble_address_for_cleanup: Optional[str]) -> dict:
        """Send one request, wait up to `timeout` for one response line.
        On any failure to complete within that window - no response, a
        write error, a dead process - kills the adapter (+ BLE cleanup
        if `ble_address_for_cleanup` is set) and raises TransportError,
        so the caller's thread is always released by `timeout`, never
        left waiting on the subprocess itself."""
        with self._proc_lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn_locked()
            proc = self._proc

            try:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
            except Exception as error:
                self._kill_locked(ble_address_for_cleanup)
                raise TransportError(
                    TransportErrorCode.ADAPTER_UNAVAILABLE, f"failed to write to adapter subprocess: {error}"
                ) from error

            result_box: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

            def _read() -> None:
                try:
                    result_box.put(("ok", proc.stdout.readline()))
                except Exception as exc:  # noqa: BLE001
                    result_box.put(("error", exc))

            reader = threading.Thread(target=_read, daemon=True, name="adapter-ipc-reader")
            reader.start()

            try:
                status, payload = result_box.get(timeout=timeout)
            except queue.Empty:
                self._kill_locked(ble_address_for_cleanup)
                raise TransportError(
                    TransportErrorCode.TIMEOUT,
                    f"adapter subprocess did not respond within {timeout}s - killed, will respawn on next call",
                )

            if status == "error":
                self._kill_locked(ble_address_for_cleanup)
                raise TransportError(
                    TransportErrorCode.ADAPTER_UNAVAILABLE, f"failed to read from adapter subprocess: {payload}"
                )

            line = str(payload).strip()
            if not line:
                self._kill_locked(ble_address_for_cleanup)
                raise TransportError(
                    TransportErrorCode.ADAPTER_UNAVAILABLE,
                    "adapter subprocess closed its output unexpectedly (crashed or exited)",
                )

            try:
                return json.loads(line)
            except json.JSONDecodeError as error:
                self._kill_locked(ble_address_for_cleanup)
                raise TransportError(
                    TransportErrorCode.UNKNOWN, f"malformed response from adapter subprocess: {error}"
                ) from error

    def _kill_locked(self, ble_address_for_cleanup: Optional[str]) -> None:
        """Caller must already hold self._proc_lock."""
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=_KILL_WAIT_TIMEOUT_S)
            except Exception as error:
                self._on_log(f"adapter subprocess kill/wait warning: {error}", "WARNING")

        if ble_address_for_cleanup:
            try:
                subprocess.run(
                    ["bluetoothctl", "disconnect", ble_address_for_cleanup],
                    capture_output=True,
                    text=True,
                    timeout=_BLUETOOTHCTL_DISCONNECT_TIMEOUT_S,
                )
            except Exception as error:
                self._on_log(f"bluetoothctl disconnect after adapter kill failed: {error}", "WARNING")


class AdapterIPCTransport(RadioTransport):
    """Core-side proxy implementing RadioTransport for one transport_type
    ("serial" or "bluetooth") by delegating over IPC to the (possibly
    shared) adapter subprocess. get_connection_info()/is_connected()
    never cross the IPC boundary - served from a local cache updated
    after every successful round-trip, starting at
    _adapter_unavailable_info() until the first one succeeds."""

    def __init__(
        self,
        transport_type: ConnectionType,
        supervisor: AdapterSupervisor,
        *,
        ble_address_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self._transport_type = transport_type
        self._supervisor = supervisor
        self._ble_address_provider = ble_address_provider
        self._cached_info = _adapter_unavailable_info()

    def _ble_cleanup_address(self) -> Optional[str]:
        if self._transport_type != ConnectionType.BLUETOOTH:
            return None
        return self._ble_address_provider() if self._ble_address_provider else None

    def _call(self, operation: str, params: dict, timeout: float):
        request = {
            "protocol_version": ipc_protocol.PROTOCOL_VERSION,
            "operation": operation,
            "transport_type": self._transport_type.value,
            "params": params,
            "timeout": timeout,
        }
        try:
            response = self._supervisor.call(
                request, timeout=timeout, ble_address_for_cleanup=self._ble_cleanup_address()
            )
        except TransportError as error:
            self._cached_info = ConnectionInfo(
                state=ConnectionState.ERROR,
                descriptor=self._cached_info.descriptor,
                node_id=self._cached_info.node_id,
                connected_since=None,
                last_error=error,
            )
            raise

        if not response.get("ok"):
            error = ipc_protocol.error_from_dict(response.get("error")) or TransportError(
                TransportErrorCode.UNKNOWN, "adapter reported failure with no error detail"
            )
            self._cached_info = ConnectionInfo(
                state=ConnectionState.ERROR,
                descriptor=self._cached_info.descriptor,
                node_id=self._cached_info.node_id,
                connected_since=None,
                last_error=error,
            )
            raise error

        return response.get("result")

    # ------------------------------------------------------------------
    # RadioTransport - connection lifecycle
    # ------------------------------------------------------------------
    def connect(self, descriptor: ConnectionDescriptor, *, force: bool = False, timeout: float = 30.0) -> ConnectionInfo:
        result = self._call(
            "connect", {"descriptor": ipc_protocol.descriptor_to_dict(descriptor), "force": force}, timeout
        )
        info = ipc_protocol.connection_info_from_dict(result)
        self._cached_info = info
        return info

    def disconnect(self, *, timeout: float = 15.0) -> None:
        result = self._call("disconnect", {}, timeout)
        self._cached_info = ipc_protocol.connection_info_from_dict(result)

    def reconnect(self, *, timeout: float = 30.0) -> ConnectionInfo:
        result = self._call("reconnect", {}, timeout)
        info = ipc_protocol.connection_info_from_dict(result)
        self._cached_info = info
        return info

    # ------------------------------------------------------------------
    # RadioTransport - non-blocking, cache-only (Task 48 design - never
    # cross the IPC boundary, per the approved investigation report).
    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        return self._cached_info.state == ConnectionState.CONNECTED

    def get_connection_info(self) -> ConnectionInfo:
        return self._cached_info

    # ------------------------------------------------------------------
    # RadioTransport - sending
    # ------------------------------------------------------------------
    def send_text(self, message: OutgoingMessage, *, timeout: float = 15.0) -> SendResult:
        result = self._call("send_text", {"message": ipc_protocol.outgoing_message_to_dict(message)}, timeout)
        return ipc_protocol.send_result_from_dict(result)

    def send_packet(
        self,
        payload: bytes,
        destination_id: str,
        *,
        port_num: int,
        want_ack: bool = False,
        timeout: float = 15.0,
    ) -> SendResult:
        result = self._call(
            "send_packet",
            {
                "payload_hex": payload.hex(),
                "destination_id": destination_id,
                "port_num": port_num,
                "want_ack": want_ack,
            },
            timeout,
        )
        return ipc_protocol.send_result_from_dict(result)

    def send_messages(self, messages: Sequence[OutgoingMessage], *, timeout: float = 30.0) -> list[SendResult]:
        result = self._call(
            "send_messages",
            {"messages": [ipc_protocol.outgoing_message_to_dict(m) for m in messages]},
            timeout,
        )
        return [ipc_protocol.send_result_from_dict(r) for r in result]

    def send_waypoint(self, waypoint: OutgoingWaypoint, *, timeout: float = 15.0) -> WaypointResult:
        result = self._call("send_waypoint", {"waypoint": ipc_protocol.outgoing_waypoint_to_dict(waypoint)}, timeout)
        return ipc_protocol.waypoint_result_from_dict(result)

    # ------------------------------------------------------------------
    # RadioTransport - reads
    # ------------------------------------------------------------------
    def get_nodes(self, *, timeout: float = 15.0) -> list[NodeInfo]:
        result = self._call("get_nodes", {}, timeout)
        return [ipc_protocol.node_info_from_dict(n) for n in result]

    def get_local_node(self, *, timeout: float = 15.0) -> NodeInfo:
        result = self._call("get_local_node", {}, timeout)
        return ipc_protocol.node_info_from_dict(result)

    def get_channels(self, *, timeout: float = 15.0) -> list[ChannelInfo]:
        result = self._call("get_channels", {}, timeout)
        return [ipc_protocol.channel_info_from_dict(c) for c in result]

    def get_metadata(self, *, timeout: float = 15.0) -> dict:
        return self._call("get_metadata", {}, timeout)

    def set_device_time(self, epoch_seconds: int, *, timeout: float = 15.0) -> bool:
        result = self._call("set_device_time", {"epoch_seconds": epoch_seconds}, timeout)
        return bool(result.get("ok"))

    def close(self) -> None:
        self._call("close", {}, 15.0)

    # ------------------------------------------------------------------
    # Not part of RadioTransport - BLE-specific, mirrors BLETransport's
    # own scan() (adapters/meshtastic/ble_transport.py), which is also
    # not part of the ABC for the same reason (device discovery for the
    # Settings "Scan" button, Task 46). Only meaningful when
    # self._transport_type is BLUETOOTH.
    # ------------------------------------------------------------------
    def scan(self, *, timeout: float = 15.0) -> list[dict]:
        if self._transport_type != ConnectionType.BLUETOOTH:
            # Explicit, checked guard rather than relying solely on the
            # server.py wiring convention ("only ble_ipc_transport is
            # ever passed into the ble_transport slot") - cheap, and a
            # far clearer error than letting this reach ipc_server.py
            # and come back as "unknown: 'SerialTransport' object has no
            # attribute 'scan'" if that convention is ever accidentally
            # broken later.
            raise TransportError(
                TransportErrorCode.UNSUPPORTED, f"scan() is not supported for {self._transport_type.value} transport"
            )
        return self._call("scan", {}, timeout)
