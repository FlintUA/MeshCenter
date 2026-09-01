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

SERIAL PORT CLAIM ACROSS THE PROCESS BOUNDARY (Task 48 follow-up, a real
gap live-caught on TAP2, not theoretical): radio_lock/pause_listen are
plain threading primitives - they cannot cross into the adapter's own
process, so Core's own listener subprocess (still running, Stage A) and
the adapter's freshly-opened SerialInterface had nothing coordinating
which of them actually owns /dev/ttyACM0 at a given moment. Live symptom
before this fix: a real set_device_time() call raced the listener, hit
its own internal watchdog timeout, got killed per tier (b) above, and
the very next serial-type call (get_channels) lost the same race on the
respawned process. AdapterIPCTransport._call() now wraps every
SERIAL-type call in core_serial_transport.claim_exclusive_access()
(originally SerialTransport.claim_for_external_command() - Task 48's
original design intent, previously written but never actually called
from anywhere - confirmed by grep before this fix; renamed to
claim_exclusive_access() and moved to meshsrv/serial_port_supervisor.py
during the P0 #1 stabilization follow-up, once the same method turned
out to be called from SerialTransport's own internal send/get methods
too, not just "externally" as the old name implied) - pause Core's own
listener, confirm the port is genuinely free, only then let the adapter
open its own SerialInterface. BLE gets no such wrapping - it never
shares Core's listener/serial port.

Budget split for this claim is DYNAMIC, mirroring TransportRouter.
_delegate()'s existing remaining = timeout - elapsed mechanic (Task
47.5), not a fixed proportion of the caller's declared timeout: the
claim itself gets its own small, independent budget (claim_exclusive_
access()'s own default, ~8s - unrelated to the caller's
timeout, since "wait for the listener to actually let go of the port"
is a fixed-magnitude operation, not something that should shrink just
because the caller declared a short timeout) using time.monotonic() to
measure what it *actually* took, and the delegated IPC call then gets
max(1.0, caller_timeout - actual_claim_elapsed) - never a stacked flat
addition (which would silently inflate a caller's declared budget) and
never a fixed percentage (which would starve the actual operation after
a fast claim, or overrun the caller's stated budget after a slow one -
the same class of bug already fixed once in TransportRouter._delegate()
during Task 47.5's review). A claim that fails to free the port in time
raises TransportError(BUSY) - claim_exclusive_access()'s existing,
unchanged behavior (originally _claim_radio(), see the rename note
above); nothing here catches or reclassifies it, it propagates through
_call()'s existing TransportError handling exactly like any other
failure.

KNOWN TRADE-OFF, explicitly accepted rather than silently introduced
(Task 48 follow-up review; the radio_session() bound mentioned below was
since closed by Task 49 - see meshsrv/serial_port_supervisor.py and
server.py's radio_session() for the current state): server.py's own
radio_lock/pause_listen are the SAME objects passed into Core's own
SerialPortSupervisor instance (`SerialPortSupervisor(radio_lock=
radio_lock, pause_listen=pause_listen, ...)` in server.py, originally a
SerialTransport instance before the P0 #1 stabilization follow-up) - the
same lock server.py's radio_session() acquires for the send worker,
channel discovery, and api/api_node_tools.py's traceroute/telemetry
actions (`radio_session(timeout=10, ...)`). Since claim_exclusive_access()
holds that same radio_lock for the FULL duration of the wrapped IPC call
(not just the port-preparation phase - releasing it earlier would
reopen exactly the race this fix closes), a Node Tools action that
starts while a long serial IPC call (e.g. connect/reconnect, tens of
seconds) is in flight now waits on an *unbounded* `with radio_lock:`
acquire - server.py's radio_lock is a plain threading.RLock with no
timeout on acquisition, so radio_session()'s own timeout=10 parameter
(which only bounds the port-release-polling phase, entered only AFTER
the lock is already held) does not help here. Previously, in the
single-process model, radio_lock was only ever held for a fast, local
SerialInterface open/close - this widens that window to a full
process-boundary round-trip. Accepted for this fix because narrowing it
back down would mean not holding the lock for the adapter's actual
serial work, reopening the contention bug this change exists to close -
but this is a real, load-bearing UX regression risk for Node Tools
(a traceroute button could now hang up to the same duration as a
concurrent connect/reconnect instead of failing fast), not dismissed as
hypothetical. Follow-up needed: bound radio_session()'s lock acquisition
itself (e.g. radio_lock.acquire(timeout=...) instead of a bare `with`)
so Node Tools fails fast with "radio busy" instead of blocking
indefinitely - out of scope for this fix since it touches radio_session()
callers well beyond this module (send worker, channel discovery, --info
calls) and deserves its own focused review.
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
import time
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

# P0 stabilization follow-up (Droidian-caught stdout-corruption cascade):
# before this, ANY non-JSON line on the adapter's stdout - even one
# stray print() from a source not yet audited - killed the adapter
# outright on the very first bad line, no tolerance at all. These bound
# how many/how much malformed content this reader will skip past before
# giving up (still within the caller's own overall `timeout` budget -
# this never extends it). Kept deliberately small (5 lines / 4KB, not
# tens): tolerating a couple of stray lines absorbs the "one print() slipped
# through despite ipc_server.py's redirect_stdout + the serial_port_
# supervisor.py source fix" case this whole effort exists to paper over
# as a last resort - but a stream producing MORE than a handful of bad
# lines in a row is not a one-off print(), it's a sign the adapter's
# stdout protocol is genuinely broken (wrong Python launched, a crash mid-
# write, something writing a real log stream to stdout) and should fail
# fast and diagnosably (ADAPTER_PROTOCOL_ERROR) rather than mask a bigger
# problem behind a long tolerant wait.
MAX_NON_JSON_LINES = 5
MAX_NON_JSON_BYTES = 4096

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
        # P0 stabilization follow-up: stderr=PIPE above gives the adapter
        # subprocess a real OS pipe with a finite kernel buffer (typically
        # 64KB on Linux) - before this drain thread, nothing ever read it.
        # A writer blocks once that buffer fills, so an adapter that logs
        # enough to stderr (more likely now that ipc_server.py's
        # redirect_stdout sends stray prints there too - see that
        # module's serve_forever()) could wedge itself indefinitely on a
        # stderr write nobody was ever going to drain. One daemon thread
        # per spawned process (not shared across respawns - proc.stderr
        # is a fresh pipe each time), forwarding each line to on_log()
        # rather than discarding it, since it's real diagnostic
        # information (exactly the print()s this whole effort relocated
        # there), not noise to throw away.
        proc = self._proc
        threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True, name="adapter-stderr-drain"
        ).start()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                if line:
                    self._on_log(f"[adapter stderr] {line}", "WARNING")
        except Exception:
            # The pipe closing (process exited/killed) or any other read
            # error ends the drain silently - this thread's only job is
            # to keep the pipe from filling, not to report its own
            # lifecycle; a killed/respawned adapter gets a fresh drain
            # thread from the next _spawn_locked() call regardless.
            pass

    def call(self, request: dict, *, timeout: float, ble_address_for_cleanup: Optional[str]) -> dict:
        """Send one request, wait up to `timeout` for one valid JSON
        response line - tolerating up to MAX_NON_JSON_LINES/
        MAX_NON_JSON_BYTES of stray non-JSON output first (P0
        stabilization follow-up: the adapter's stdout is also its IPC
        channel, so one stray print() used to be fatal - see
        adapters/meshtastic/ipc_server.py's serve_forever() and
        meshsrv/serial_port_supervisor.py for where that could come from
        and how it's now guarded at the source too). On any failure to
        complete within the overall `timeout` window - no response, a
        write error, a dead process, or malformed output past the
        tolerance bounds (TransportErrorCode.ADAPTER_PROTOCOL_ERROR,
        distinct from ADAPTER_UNAVAILABLE/UNKNOWN so this failure mode is
        diagnosable on its own) - kills the adapter (+ BLE cleanup if
        `ble_address_for_cleanup` is set) and raises TransportError, so
        the caller's thread is always released by `timeout`, never left
        waiting on the subprocess itself.

        Radio-stability review, P0-A: also kills (but does NOT raise a
        different error - the caller still sees the same TransportError
        it always would have) when the adapter itself responds cleanly
        but that response IS a TIMEOUT - the adapter's own internal
        TimeoutEnforced watchdog fired first and reported it as a
        well-formed error, but the daemon thread that was running the
        timed-out operation is only abandoned, never stopped (see
        adapters/meshtastic/_timeout_support.py), so it can still be
        holding the serial port. Every OTHER `ok: false` domain error
        (NOT_CONNECTED, UNSUPPORTED, IDENTITY_MISMATCH, ...) means the
        adapter's call stack already unwound cleanly and is deliberately
        NOT killed here - see the check's own comment below for why this
        stays narrow to TIMEOUT specifically."""
        with self._proc_lock:
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._spawn_locked()
                except Exception as error:
                    # Most commonly: adapter_python doesn't exist yet (no
                    # separate adapter venv provisioned) - must degrade to
                    # the same clean, expected error every other failure
                    # path in this method produces, not a raw
                    # FileNotFoundError escaping to whichever caller
                    # (including background threads like the node-time-sync
                    # worker) didn't anticipate this specific failure mode.
                    raise TransportError(
                        TransportErrorCode.ADAPTER_UNAVAILABLE, f"failed to launch adapter subprocess: {error}"
                    ) from error
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
                # P0 stabilization follow-up: parsing now happens IN this
                # thread, not back in call() - lets it tolerate a bounded
                # number of non-JSON lines (MAX_NON_JSON_LINES/
                # MAX_NON_JSON_BYTES, see those constants' own comment for
                # why) before giving up, instead of the previous behavior
                # of killing the adapter on the very first malformed line
                # - the actual mechanism of the observed Droidian
                # corruption cascade (a single stray print() from
                # wait_serial_release() was enough to kill a healthy
                # adapter and cascade into serial contention). Still
                # bounded by call()'s own `result_box.get(timeout=timeout)`
                # below either way - tolerating bad lines never extends
                # the caller's overall deadline, it only avoids treating
                # ONE stray line as fatal within it.
                bad_lines = 0
                bad_bytes = 0
                try:
                    while True:
                        raw = proc.stdout.readline()
                        if not raw:
                            result_box.put(("closed", None))
                            return
                        stripped = raw.strip()
                        if not stripped:
                            continue
                        try:
                            parsed = json.loads(stripped)
                        except json.JSONDecodeError:
                            bad_lines += 1
                            bad_bytes += len(raw)
                            if bad_lines > MAX_NON_JSON_LINES or bad_bytes > MAX_NON_JSON_BYTES:
                                result_box.put((
                                    "protocol_error",
                                    f"{bad_lines} malformed line(s) totaling {bad_bytes} bytes "
                                    f"before giving up (limits: {MAX_NON_JSON_LINES} lines / "
                                    f"{MAX_NON_JSON_BYTES} bytes)",
                                ))
                                return
                            continue
                        result_box.put(("ok", parsed))
                        return
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

            if status == "closed":
                self._kill_locked(ble_address_for_cleanup)
                raise TransportError(
                    TransportErrorCode.ADAPTER_UNAVAILABLE,
                    "adapter subprocess closed its output unexpectedly (crashed or exited)",
                )

            if status == "protocol_error":
                self._kill_locked(ble_address_for_cleanup)
                raise TransportError(TransportErrorCode.ADAPTER_PROTOCOL_ERROR, str(payload))

            # Radio-stability review, P0-A: a fifth case the four branches
            # above don't cover. The adapter's own internal TimeoutEnforced
            # watchdog (adapters/meshtastic/_timeout_support.py) can fire
            # first and report this back as a well-formed JSON response
            # (ipc_protocol.make_error_response() -> {"ok": false, "error":
            # {"code": "timeout", ...}}) - status is "ok" here (a valid
            # response WAS read), so none of the branches above trigger,
            # and this used to fall straight through to `return payload`
            # with no kill at all. Unlike the other four branches, the
            # adapter process itself isn't untrustworthy here - it
            # responded correctly - but the operation it was running IS
            # orphaned inside it: TimeoutEnforced's own docstring is
            # explicit that the daemon thread executing the timed-out call
            # is never stopped, only abandoned, so it can still be holding
            # the serial port when the next call is dispatched to this
            # same "still alive" subprocess. Narrow to TIMEOUT specifically
            # - an ordinary domain error (NOT_CONNECTED, UNSUPPORTED,
            # IDENTITY_MISMATCH, ...) means the adapter's call stack
            # already unwound cleanly, and killing the subprocess for
            # those would be pure churn with no safety benefit.
            if isinstance(payload, dict) and payload.get("ok") is False:
                error_code = (payload.get("error") or {}).get("code")
                if error_code == TransportErrorCode.TIMEOUT.value:
                    self._kill_locked(ble_address_for_cleanup)

            return payload

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
        core_serial_transport: Optional[object] = None,
    ) -> None:
        self._transport_type = transport_type
        self._supervisor = supervisor
        self._ble_address_provider = ble_address_provider
        # Only ever passed for the SERIAL instance - duck-typed (needs
        # only .claim_exclusive_access()), not imported for typing, to
        # keep this Core-side module decoupled from adapters/*.py - see
        # this module's own SERIAL PORT CLAIM docstring section for why
        # this exists.
        self._core_serial_transport = core_serial_transport
        self._cached_info = _adapter_unavailable_info()

    def _ble_cleanup_address(self) -> Optional[str]:
        if self._transport_type != ConnectionType.BLUETOOTH:
            return None
        return self._ble_address_provider() if self._ble_address_provider else None

    def _call(self, operation: str, params: dict, timeout: float):
        def _do_call(call_timeout: float) -> dict:
            request = {
                "protocol_version": ipc_protocol.PROTOCOL_VERSION,
                "operation": operation,
                "transport_type": self._transport_type.value,
                "params": params,
                "timeout": call_timeout,
            }
            return self._supervisor.call(
                request, timeout=call_timeout, ble_address_for_cleanup=self._ble_cleanup_address()
            )

        try:
            if self._transport_type == ConnectionType.SERIAL and self._core_serial_transport is not None:
                start = time.monotonic()
                # Own independent budget (claim_exclusive_access()'s own
                # default, ~8s) - NOT sliced from the caller's timeout,
                # since freeing the port is a fixed-magnitude operation.
                # What it actually took IS subtracted from the caller's
                # budget below - dynamic, not a fixed proportion (see
                # module docstring). KNOWN TRADE-OFF: the more thorough
                # (and more correct) the port-busy check inside this
                # claim is, the slower a worst-case claim can be on slow
                # hardware, and the less of `remaining` is left for the
                # actual call() below - see
                # SerialPortSupervisor.check_port_release_once()'s own
                # docstring for the specific trade-off this was measured
                # against (P0 stabilization follow-up).
                with self._core_serial_transport.claim_exclusive_access():
                    elapsed = time.monotonic() - start
                    remaining = max(1.0, timeout - elapsed)
                    response = _do_call(remaining)
            else:
                response = _do_call(timeout)
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
