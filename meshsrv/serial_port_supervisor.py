"""SerialPortSupervisor - exclusive-access + listener-subprocess management
for the physical Meshtastic serial port, extracted out of
adapters/meshtastic/serial_transport.py's SerialTransport (stabilization
follow-up, P0 #1 of the independent audit).

Why this exists as its own MIT-owned module: server.py previously imported
SerialTransport directly (from adapters.meshtastic.serial_transport import
SerialTransport) purely to reach four of its methods -
run_listener()/get_listener_pid()/claim_for_external_command()/the private
_stop_listener_process()/_wait_serial_release() pair - none of which ever
touch the meshtastic package. That's a real boundary smell even though the
meshtastic import itself is lazy and never reached via this path: a class
living in a GPLv3-labeled directory, imported directly by Core.

The methods below are not Core-exclusive, though - they were never solely
"Core's listener-management leaking into a shared class". _claim_radio()
(now claim_exclusive_access()) is used by SerialTransport's own
send_packet()/send_messages()/get_nodes()/get_local_node()/get_channels()/
set_device_time() too, on the ADAPTER's own SerialTransport instance
(constructed fresh in adapters/meshtastic/ipc_server.py's main(), with its
own local radio_lock/pause_listen, never shared with Core's). So this is a
genuinely shared exclusive-access primitive both roles need - Core's
listener-management instance (this module, used directly) and the
adapter's own per-call instance (SerialTransport, composing one of these
internally). Extracting it here lets both compose against the same
implementation instead of one inheriting it and the other reaching into
"private" methods of a class it shouldn't otherwise depend on.

Behavior carried over 1:1 from SerialTransport's former
run_listener()/get_listener_pid()/_stop_listener_process()/
_wait_serial_release()/_prepare_radio_command()/_claim_radio()/
claim_for_external_command() - this is code that was already carefully
verified twice, live, on real hardware (Task 44's run_listener()/pause-
stop-wait choreography, Task 48's claim_for_external_command() and the
live-caught serial-port race between Core's listener and the adapter
subprocess) - moved, not rewritten. The one deliberate naming change:
claim_for_external_command() is renamed to claim_exclusive_access() - the
old name stopped being accurate once the same method started being called
from SerialTransport's own internal send/get methods too, not just
"externally" by meshsrv/adapter_ipc_client.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from meshsrv.radio_transport import TransportError, TransportErrorCode


class PortReleaseOutcome(str, Enum):
    """P0 stabilization follow-up (Droidian-caught): wait_serial_release()
    used to collapse every non-PORT_FREE case - a real busy port, an
    `lsof` timeout, `lsof` erroring, `lsof` missing entirely - into the
    same boolean False, indistinguishable from each other in both the
    return value and the log line. Root cause of the observed corruption
    cascade: on Droidian, `lsof` itself apparently hits its own 2s
    subprocess timeout unreliably (slower/different I/O than the
    Raspberry Pi hardware this was developed and live-verified against),
    which every prior version of this code treated as "port busy" -
    which then unnecessarily lengthened the exclusive-access claim,
    increasing the odds a stray print() (see this module's print() call
    sites, all now file=sys.stderr) would land on the adapter's stdout
    protocol channel mid-claim."""
    PORT_FREE = "port_free"
    PORT_BUSY = "port_busy"
    CHECK_TIMEOUT = "check_timeout"
    CHECK_FAILED = "check_failed"
    UTILITY_MISSING = "utility_missing"


class SerialPortSupervisor:
    def __init__(
        self,
        cli_path: str,
        port: str,
        radio_lock: threading.RLock,
        pause_listen: threading.Event,
        on_raw_line: Optional[Callable[[str], None]] = None,
        on_lifecycle_event: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._cli_path = cli_path
        self._port = port
        self._radio_lock = radio_lock
        self._pause_listen = pause_listen
        self._on_raw_line = on_raw_line or (lambda line: None)
        self._on_lifecycle_event = on_lifecycle_event or (lambda event: None)
        self._on_log = on_log or (lambda msg, level="INFO": None)

        self._listen_process: Optional[subprocess.Popen] = None
        self._connected_since: Optional[float] = None

    # ------------------------------------------------------------------
    # Listener subprocess (Stage A - see adapters/meshtastic/
    # serial_transport.py's module docstring "DESIGN NOTE - listener
    # subprocess moved out"). Only ever run on Core's own instance of
    # this class - SerialTransport's own composed instance never calls
    # this (Stage A: a full move of the listener into the adapter
    # process is a separate, not-yet-done "Stage B").
    # ------------------------------------------------------------------
    def run_listener(self) -> None:
        """Blocking retry loop - unchanged from SerialTransport's former
        run_listener(), itself a 1:1 replacement for server.py's former
        listen_meshtastic() (Task 44), minus the Meshtastic-protocol
        parsing (delivered line-by-line to on_raw_line instead of parsed
        inline). Call this from Core's own daemon thread, same as
        before."""
        consecutive_errors = 0
        max_consecutive_errors = 10

        while True:
            if self._pause_listen.is_set():
                time.sleep(0.5)
                continue

            with self._radio_lock:
                self._listen_process = None

            try:
                time.sleep(0.5)

                with self._radio_lock:
                    if self._pause_listen.is_set():
                        continue

                    from meshsrv.runtime_identity import meshtastic_command

                    listener_cmd = meshtastic_command(self._cli_path, self._port, "--listen")
                    proc = subprocess.Popen(
                        listener_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        errors="ignore",
                    )
                    self._listen_process = proc
                    self._connected_since = time.time()
                    self._on_lifecycle_event("listener_start")
                    consecutive_errors = 0

                for line in proc.stdout:
                    if self._pause_listen.is_set():
                        break
                    # Every line - including one that strips to empty - is
                    # handed to on_raw_line, same as the original inline
                    # loop called radio_event("packet") unconditionally
                    # before checking for emptiness. Filtering blank lines
                    # out here instead would silently drop that signal;
                    # the empty check belongs to the Core-side handler.
                    line = line.strip()
                    try:
                        self._on_raw_line(line)
                    except Exception as e:
                        print(f"[SerialPortSupervisor] on_raw_line error: {e}", file=sys.stderr, flush=True)

                with self._radio_lock:
                    current = self._listen_process
                return_code = current.poll() if current is not None else None

                if self._pause_listen.is_set():
                    if current is not None:
                        try:
                            current.terminate()
                            current.wait(timeout=3)
                        except Exception:
                            try:
                                current.kill()
                            except Exception:
                                pass
                    self._on_lifecycle_event("listener_stop")
                    with self._radio_lock:
                        self._listen_process = None
                    time.sleep(0.5)
                    continue

                if return_code is not None and return_code != 0:
                    print(
                        f"[SerialPortSupervisor] Listener process ended with code: {return_code}",
                        file=sys.stderr, flush=True,
                    )
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

                self._on_lifecycle_event("listener_stop")
                with self._radio_lock:
                    self._listen_process = None

            except Exception as e:
                consecutive_errors += 1
                print(
                    f"[SerialPortSupervisor] run_listener (attempt {consecutive_errors}): {e}",
                    file=sys.stderr, flush=True,
                )
                delay = min(consecutive_errors * 2, 30)
                time.sleep(delay)

            if consecutive_errors > max_consecutive_errors:
                consecutive_errors = 0
                time.sleep(5)
            else:
                time.sleep(2)

    def get_listener_pid(self) -> Optional[int]:
        """Status introspection for Core's /api/node-manager/dashboard -
        unchanged from SerialTransport's former get_listener_pid(),
        including the Task 49 fix: radio_lock.acquire() is bounded
        (_LISTENER_PID_LOCK_TIMEOUT_S) - once claim_exclusive_access()
        can hold this same lock for a full adapter IPC round-trip
        (Task 48), an unbounded acquire here could stall the whole
        dashboard page behind an unrelated long-running radio call. On a
        busy lock, returns None (fail-safe) rather than raising - this
        is a passive status field, not an action."""
        if not self._radio_lock.acquire(timeout=self._LISTENER_PID_LOCK_TIMEOUT_S):
            return None
        try:
            proc = self._listen_process
        finally:
            self._radio_lock.release()
        return int(proc.pid) if proc is not None and proc.poll() is None else None

    # Task 49 precedent this reuses: meshsrv/transport_router.py's own
    # _INFO_LOCK_TIMEOUT_S (same value, same reasoning - a passive,
    # non-raising status read gets its own short constant, not the same
    # budget as an action).
    _LISTENER_PID_LOCK_TIMEOUT_S = 3.0

    # ------------------------------------------------------------------
    # Exclusive-access claim - port/subprocess semantics only, no
    # "radio" framing (this is about who currently owns the OS-level
    # serial device and the --listen subprocess, not about the radio
    # protocol itself). stop_listener_process()/wait_serial_release() are
    # public, not just internal to claim_exclusive_access() below: both
    # are also called directly by external code - server.py's own
    # stop_listener()/wait_serial_release() thin wrappers (used by
    # api/api_chat.py and meshsrv/radio_manager.py's RadioConnectionManager)
    # call stop_listener_process(), and SerialTransport's own
    # connect(force=True) branch calls wait_serial_release() directly - a
    # bare "confirm the port is free" check without the full pause/stop/
    # hold-lock/cooldown dance claim_exclusive_access() does. Both being
    # public, real methods (not underscored internals reached into from
    # outside the class) is the actual fix for the encapsulation half of
    # this stabilization task, not just the import-boundary half.
    # ------------------------------------------------------------------
    def stop_listener_process(self) -> bool:
        self._pause_listen.set()
        time.sleep(1.5)

        with self._radio_lock:
            proc = self._listen_process

        if proc is None:
            return True

        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            return True
        except Exception as e:
            print(f"[SerialPortSupervisor] Error stopping listener: {e}", file=sys.stderr, flush=True)
            return False
        finally:
            with self._radio_lock:
                self._listen_process = None
            time.sleep(1.0)

    # ------------------------------------------------------------------
    # Port-release check (P0 stabilization follow-up) - layered, cheapest
    # and most reliable check first, `lsof` (an external process, subject
    # to its own timeout/missing-binary/hang risk - the actual root cause
    # of the Droidian corruption cascade) only as a last resort. Each
    # layer returns a PortReleaseOutcome or None ("inconclusive, ask the
    # next layer") rather than collapsing straight to a bool.
    # ------------------------------------------------------------------
    def _check_known_pid(self) -> Optional[PortReleaseOutcome]:
        """Cheapest, most limited check: is OUR OWN listener process still
        holding the port? This answers "does our own listener still grip
        me back", NOT "is the port free in general" - an orphaned process
        left over from a previous adapter crash, or an entirely unrelated
        process on the device, is invisible to this check by design. It
        exists to short-circuit the common case (we know for a fact our
        own listener is still up) cheaply; the layers below exist
        precisely to catch what this one structurally cannot. Never treat
        this returning None as "port confirmed free" - it only means "not
        held by the process we already know about".
        """
        with self._radio_lock:
            proc = self._listen_process
        if proc is not None and proc.poll() is None:
            return PortReleaseOutcome.PORT_BUSY
        return None

    def _check_proc_fd_scan(self) -> Optional[PortReleaseOutcome]:
        """Scan /proc/*/fd for an open file descriptor resolving to this
        port - pure in-process file I/O, no subprocess spawn, so unlike
        `lsof` it cannot itself hit an external-command timeout. Linux-
        only; returns None (inconclusive, fall through to the next layer)
        wherever /proc isn't usable rather than guessing."""
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return None
        try:
            target = os.path.realpath(self._port)
        except OSError:
            return None

        try:
            pid_dirs = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
        except OSError:
            return PortReleaseOutcome.CHECK_FAILED

        for pid_dir in pid_dirs:
            try:
                fd_entries = list((pid_dir / "fd").iterdir())
            except (OSError, PermissionError):
                # Not our process, or it exited mid-scan - not a scan
                # failure, just an entry we can't see into.
                continue
            for fd_entry in fd_entries:
                try:
                    if os.path.realpath(fd_entry) == target:
                        return PortReleaseOutcome.PORT_BUSY
                except OSError:
                    continue
        return PortReleaseOutcome.PORT_FREE

    def _check_external_tool(self, tool: str, args: list[str], *, busy_timeout: float = 2.0) -> PortReleaseOutcome:
        """Shared shape for the two external-process fallbacks (`fuser`,
        `lsof`) - both can hang/time out/be missing, which is the actual
        root cause this whole layered rework exists to stop conflating
        with a genuinely busy port."""
        try:
            result = subprocess.run([tool, *args], capture_output=True, text=True, timeout=busy_timeout)
        except FileNotFoundError:
            return PortReleaseOutcome.UTILITY_MISSING
        except subprocess.TimeoutExpired:
            return PortReleaseOutcome.CHECK_TIMEOUT
        except Exception:
            return PortReleaseOutcome.CHECK_FAILED

        if result.stdout.strip():
            return PortReleaseOutcome.PORT_BUSY
        return PortReleaseOutcome.PORT_FREE

    def _check_fuser(self) -> PortReleaseOutcome:
        # fuser prints the PIDs holding the file to stdout (nothing if
        # free) - same "non-empty stdout means busy" shape as the lsof
        # check below, just a lighter external tool tried first.
        return self._check_external_tool("fuser", [self._port])

    def _check_lsof(self) -> PortReleaseOutcome:
        return self._check_external_tool("lsof", ["-t", self._port])

    def check_port_release_once(self) -> PortReleaseOutcome:
        """One pass through the full layered strategy: known PID -> /proc
        fd scan -> fuser -> lsof. Returns as soon as a layer gives a
        definitive PORT_FREE/PORT_BUSY answer; an inconclusive layer
        (None, or CHECK_FAILED/CHECK_TIMEOUT/UTILITY_MISSING from an
        external tool) falls through to the next one. The final layer's
        own outcome (including an inconclusive one) is returned as-is if
        every layer was inconclusive - the caller (wait_serial_release())
        decides how to treat that, this method never silently upgrades
        "couldn't tell" into "busy"."""
        outcome = self._check_known_pid()
        if outcome is not None:
            return outcome

        outcome = self._check_proc_fd_scan()
        if outcome is not None and outcome != PortReleaseOutcome.CHECK_FAILED:
            return outcome

        outcome = self._check_fuser()
        if outcome not in (
            PortReleaseOutcome.CHECK_FAILED,
            PortReleaseOutcome.CHECK_TIMEOUT,
            PortReleaseOutcome.UTILITY_MISSING,
        ):
            return outcome

        return self._check_lsof()

    def wait_serial_release(self, timeout: float = 8) -> bool:
        """Public bool contract unchanged (existing callers - server.py's
        thin wrapper, SerialTransport's connect(force=True) branch,
        claim_exclusive_access() below - all just need "did it free up in
        time"). What changed: every retry now goes through the layered
        check_port_release_once() instead of an unconditional `lsof`
        call, so a busy port, an inconclusive/timed-out/missing-tool
        check, and a genuinely free port are distinguished internally
        (see PortReleaseOutcome) even though only the final True/False
        crosses this method's own boundary - callers that need the
        distinction can call check_port_release_once() directly."""
        if not self._port:
            return True

        start = time.time()
        last_outcome: Optional[PortReleaseOutcome] = None
        while time.time() - start < timeout:
            last_outcome = self.check_port_release_once()
            if last_outcome == PortReleaseOutcome.PORT_FREE:
                return True
            time.sleep(0.2)

        detail = last_outcome.value if last_outcome is not None else "no check completed"
        print(
            f"[SerialPortSupervisor] Serial port not confirmed free after {timeout}s "
            f"(last outcome: {detail}): {self._port}",
            file=sys.stderr, flush=True,
        )
        return False

    def _prepare_command(self, timeout: float = 8) -> bool:
        self._pause_listen.set()
        self.stop_listener_process()
        return self.wait_serial_release(timeout=timeout)

    @contextmanager
    def claim_exclusive_access(self, *, timeout: float = 8, cooldown: float = 2.0):
        """Claim exclusive access to the serial port for the duration of
        the block - pause the listener, stop it, wait for the OS to
        actually free the device, hold radio_lock for the whole
        prepare+work+cooldown span, then resume the listener.

        Renamed from claim_for_external_command()/_claim_radio()
        (stabilization follow-up): the old name stopped being accurate
        once this same method started being called from SerialTransport's
        own internal send_*/get_*() methods too (on the adapter's own
        instance, a different SerialPortSupervisor with its own local
        radio_lock/pause_listen, never shared with Core's) - not just
        "externally" by meshsrv/adapter_ipc_client.py on Core's instance.
        One name, same behavior, used identically by both callers.

        DELIBERATE DIVERGENCE from server.py's radio_session(): that
        function calls its own prepare phase (pause+stop+wait) BEFORE
        acquiring radio_lock, so concurrent callers can all enter the
        prepare phase in parallel and only serialize once they reach
        `with radio_lock:`. Holding radio_lock for the ENTIRE
        prepare+work+cooldown span here instead (not just the yield) is
        safe from self-deadlock (radio_lock is an RLock) and fully
        serializes the prepare phase too, at the cost of a caller
        possibly blocking here for another caller's whole claim (prepare
        included) instead of only its interface work - judged the safer
        trade given "serial port contention" is a named, previously-real
        regression risk for this project (Task 44's original choice,
        unchanged by this move). Verified by
        tests/test_serial_transport_timeout.py's
        test_concurrent_connect_and_send_do_not_race_prepare_phase
        (stayed in that file - it exercises SerialTransport's connect()/
        send_text() through a composed SerialPortSupervisor via the
        supervisor= DI seam, not SerialPortSupervisor in isolation).
        """
        with self._radio_lock:
            prepared = self._prepare_command(timeout=timeout)
            try:
                if not prepared:
                    raise TransportError(
                        TransportErrorCode.BUSY, f"Serial port busy: {self._port or 'auto-detect'}"
                    )
                yield
            finally:
                if cooldown:
                    time.sleep(cooldown)
                self._pause_listen.clear()
