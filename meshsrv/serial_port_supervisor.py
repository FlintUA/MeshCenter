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

import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from meshsrv.radio_transport import TransportError, TransportErrorCode


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
                        print(f"[SerialPortSupervisor] on_raw_line error: {e}", flush=True)

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
                    print(f"[SerialPortSupervisor] Listener process ended with code: {return_code}", flush=True)
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

                self._on_lifecycle_event("listener_stop")
                with self._radio_lock:
                    self._listen_process = None

            except Exception as e:
                consecutive_errors += 1
                print(f"[SerialPortSupervisor] run_listener (attempt {consecutive_errors}): {e}", flush=True)
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
            print(f"[SerialPortSupervisor] Error stopping listener: {e}", flush=True)
            return False
        finally:
            with self._radio_lock:
                self._listen_process = None
            time.sleep(1.0)

    def wait_serial_release(self, timeout: float = 8) -> bool:
        if not self._port:
            return True

        start = time.time()
        while time.time() - start < timeout:
            try:
                result = subprocess.run(
                    ["lsof", "-t", self._port],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if not result.stdout.strip():
                    return True
            except Exception as e:
                print(f"[SerialPortSupervisor] wait_serial_release error: {e}", flush=True)
            time.sleep(0.2)

        print(f"[SerialPortSupervisor] Serial port still busy after {timeout}s: {self._port}", flush=True)
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
