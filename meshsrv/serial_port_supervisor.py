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
        on_lifecycle_event: Optional[Callable[[str, Optional[bool]], None]] = None,
        on_log: Optional[Callable[..., None]] = None,
    ) -> None:
        self._cli_path = cli_path
        self._port = port
        self._radio_lock = radio_lock
        self._pause_listen = pause_listen
        self._on_raw_line = on_raw_line or (lambda line: None)
        self._on_lifecycle_event = on_lifecycle_event or (lambda event, intentional=None: None)
        # Task 7 (listener-stop-intent logging) follow-up: accepts optional
        # title/source kwargs now, mirroring how on_lifecycle_event was
        # extended with **kwargs/intentional in PR #168 - same pattern, same
        # file. The no-op default below just needs to not blow up on them.
        self._on_log = on_log or (lambda msg, level="INFO", **kwargs: None)

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
                    self._on_lifecycle_event("listener_start", intentional=None)
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

                # P1-B stabilization follow-up: this is the ONLY moment
                # that actually knows whether the stop about to be
                # reported was intentional (pause_listen already set) or
                # not - captured once, into a plain bool, and threaded
                # through to on_lifecycle_event() below instead of being
                # re-read later by whatever handles the event. Between
                # this read and that later handling, a DIFFERENT caller
                # (radio_session()/prepare_radio_command() elsewhere)
                # can legitimately set/clear this same shared
                # threading.Event - a re-read at that later, asynchronous
                # point can observe a value that no longer reflects what
                # was true at the actual transition, misclassifying a
                # routine, intentional stop as an unexpected one (or vice
                # versa). See server.py's radio_event() for the consumer
                # side of this fix.
                #
                # KNOWN, DEFERRED (live-observed on dev during this same
                # fix's own soak test, not fixed here): this read is
                # synchronous and correct for the bug above, but it's
                # still a plain, un-locked read of a shared
                # threading.Event - a DIFFERENT, concurrent, overlapping
                # claim_exclusive_access()/radio_session() call can still
                # toggle pause_listen in the narrow window between "the
                # listener process actually dies" and "this thread gets
                # scheduled to reach this line", producing one
                # occasional, isolated "Listener stopped (unexpected)"
                # even though the stop really was contention-driven, not
                # a genuine crash. Live-confirmed: happened once during
                # dev's own post-deploy soak, self-recovered within
                # seconds, no sustained outage. A real fix would mean
                # holding radio_lock across this whole notice-and-report
                # sequence, claim_exclusive_access()-style (see that
                # method's own DELIBERATE DIVERGENCE note) - a bigger,
                # architectural change that overlaps with the P1-A
                # follow-up, not attempted here. Tracked as backlog, not
                # scheduled separately (rare, narrow, self-recovering).
                stop_was_intentional = self._pause_listen.is_set()

                if stop_was_intentional:
                    if current is not None:
                        try:
                            current.terminate()
                            current.wait(timeout=3)
                        except Exception:
                            try:
                                current.kill()
                            except Exception:
                                pass
                    self._on_lifecycle_event("listener_stop", intentional=True)
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

                self._on_lifecycle_event("listener_stop", intentional=stop_was_intentional)
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
        # Task 7 (observability follow-up): log intent at the ONE place
        # it's actually known for certain - this is the single choke point
        # both callers that ever intentionally stop the listener go
        # through (server.py's prepare_radio_command() via stop_listener(),
        # and claim_exclusive_access()'s internal _prepare_command()).
        # Task 6's investigation hit a wall reconstructing "was this stop
        # intentional" after the fact from run_listener()'s own
        # ended-with-code/stop_was_intentional read - both a genuine crash
        # and a legitimate stop can print an identical shutdown signature,
        # making them indistinguishable in hindsight. Logging the request
        # here, at the moment it's issued, means a future "ERROR: Listener
        # stopped - exited unexpectedly" with no "Listener stop requested"
        # in the few seconds before it is real signal, not an inference.
        self._on_log(
            "Listener stop requested (intentional)", "INFO",
            title="Listener Control", source="radio",
        )
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
        wherever /proc isn't usable rather than guessing.

        ASYMMETRIC RELIABILITY (review finding, not hypothetical): a
        PORT_BUSY answer from this scan is trustworthy unconditionally -
        finding even one fd resolving to the port is proof, regardless of
        whose process it belongs to. A PORT_FREE answer is NOT
        equivalent-strength: `except (OSError, PermissionError): continue`
        below means a PID directory this process lacks permission to read
        into is silently skipped, not reported as inconclusive - so a
        holder running as a different user (e.g. someone's root-owned
        `screen /dev/ttyACM0` debugging session) is invisible to this
        scan and would make it report PORT_FREE while the port is
        genuinely busy. This is exactly the class of mistake the whole
        layered rework exists to stop making (collapsing "couldn't find
        evidence" into "confirmed absent") - so check_port_release_once()
        below deliberately does NOT treat this method's PORT_FREE as
        final; only its PORT_BUSY short-circuits the chain. See that
        method's own docstring.
        """
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
                # Not our process, or it exited mid-scan, OR (see the
                # ASYMMETRIC RELIABILITY note above) a different user's
                # process we simply can't see into - these three cases
                # are indistinguishable from here, which is exactly why a
                # clean scan below only ever produces a provisional
                # PORT_FREE, never a final one.
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
        fd scan -> fuser -> lsof.

        DELIBERATELY ASYMMETRIC (review finding): PORT_BUSY from ANY
        layer short-circuits immediately and is trusted unconditionally -
        a positive finding (something IS holding the port) doesn't
        depend on having looked everywhere, so it can never be a false
        positive this way, no matter which layer produced it.

        PORT_FREE is different, and is NOT treated the same way. Every
        layer before the last one can only fail to find evidence of a
        holder, not prove none exists - _check_known_pid() only ever
        knows about OUR OWN listener process by design, and
        _check_proc_fd_scan()/_check_fuser() can each silently miss a
        holder running as a different user they lack permission to
        inspect (see _check_proc_fd_scan()'s own ASYMMETRIC RELIABILITY
        note for the concrete scenario this isn't hypothetical for - a
        root-owned debugging session on the port, invisible to a
        non-root scan). So PORT_FREE/None/any inconclusive outcome from
        every layer up to (not including) the final one falls through to
        the next layer instead of terminating - only the LAST layer's
        answer (lsof, the existing, previously-sole check this whole
        rework is layered in front of) is trusted as a final PORT_FREE on
        its own. Droidian follow-up: if lsof itself can't answer in time
        (its own timing is unreliable on some hardware - see the
        CHECK_TIMEOUT/CHECK_FAILED/UTILITY_MISSING fallback below this
        docstring), a PORT_FREE from BOTH fd_scan AND fuser together is
        also trusted as final - two independent affirmative confirmations
        outweighing one slow/flaky last-resort tool failing to finish,
        without touching busy-detection on any layer.

        This is the exact principle the whole rework exists to enforce,
        applied to the layers among themselves too, not just to
        `lsof` alone: never collapse "couldn't find evidence of X" into
        "confirmed not-X" - that exact collapse (an lsof timeout treated
        as a busy port) was the root cause of the corruption cascade this
        module was rewritten to fix.

        KNOWN TRADE-OFF, explicitly accepted (review follow-up, not a
        free improvement): falling through past every inconclusive layer
        means a genuinely stuck check can now try known-PID, /proc scan,
        `fuser`, AND `lsof` in sequence before giving up, instead of just
        `lsof` alone - on hardware where BOTH external tools are slow
        (not just `lsof`, the one originally caught live on Droidian),
        one call to this method can now take longer in the worst case
        than the old lsof-only version did. That eats into
        AdapterIPCTransport._call()'s own
        `remaining = max(1.0, timeout - elapsed)` budget split
        (meshsrv/adapter_ipc_client.py) for the actual IPC round-trip
        that follows the claim - a slower claim phase here can mean less
        of the caller's declared timeout is left for the adapter call
        itself, which can surface as more frequent "adapter subprocess
        did not respond within {remaining}s" kills on such hardware.
        Live-measured on the Droidian node this was written for: the
        combined effect (this correctness fix plus the stdout-corruption
        fix it shipped alongside) still reduced that kill frequency
        roughly 3.7x (~1/11s before both fixes -> ~1/41s after), so this
        is a real trade-off being made deliberately, not a regression
        being introduced - but if a future device turns up where `fuser`
        is ALSO systematically slow (not just `lsof`), a higher kill
        frequency for short-timeout callers is the expected, already-
        accepted consequence of this design choice, not a new bug to
        rediscover from scratch.
        """
        known_pid = self._check_known_pid()
        if known_pid == PortReleaseOutcome.PORT_BUSY:
            return known_pid

        fd_scan = self._check_proc_fd_scan()
        if fd_scan == PortReleaseOutcome.PORT_BUSY:
            return fd_scan

        fuser = self._check_fuser()
        if fuser == PortReleaseOutcome.PORT_BUSY:
            return fuser

        lsof = self._check_lsof()
        if lsof == PortReleaseOutcome.PORT_FREE:
            return lsof

        # Droidian follow-up: lsof is the slowest, flakiest layer (a
        # subprocess spawn with no guaranteed latency, unlike the
        # in-process /proc scan) - live-measured on that device, its own
        # 2s busy_timeout is regularly too short (lsof itself commonly
        # takes 1.7-2.8s there), turning a perfectly free port into
        # CHECK_TIMEOUT on every send attempt. When BOTH independent,
        # already-completed cheaper layers affirmatively found no owner
        # (not merely "inconclusive" - an actual PORT_FREE from each),
        # trust that combination over lsof failing to finish in time,
        # rather than making every caller wait out the full retry budget
        # for a check that structurally can't reliably complete on this
        # hardware. This does NOT weaken busy-detection on any layer -
        # every PORT_BUSY above still short-circuits immediately,
        # unchanged; this only strengthens what counts as a confirmed
        # PORT_FREE in the one case where lsof specifically (not fd_scan,
        # not fuser) is the layer that couldn't answer.
        if (
            fd_scan == PortReleaseOutcome.PORT_FREE
            and fuser == PortReleaseOutcome.PORT_FREE
            and lsof in (
                PortReleaseOutcome.CHECK_TIMEOUT,
                PortReleaseOutcome.CHECK_FAILED,
                PortReleaseOutcome.UTILITY_MISSING,
            )
        ):
            self._on_log(
                f"Serial port release inferred free (proc+fuser clean, "
                f"lsof {lsof.value}): {self._port}",
                "WARNING",
            )
            return PortReleaseOutcome.PORT_FREE

        return lsof

    def _wait_for_release_outcome(self, timeout: float = 8) -> PortReleaseOutcome:
        """Retry check_port_release_once() until it reports PORT_FREE or
        `timeout` elapses, returning the actual final PortReleaseOutcome -
        the detail wait_serial_release()'s bool contract used to discard
        (Droidian follow-up: that loss is exactly what let a mere
        CHECK_TIMEOUT surface to the user as a false "Serial port busy",
        indistinguishable from a real PORT_BUSY - see
        claim_exclusive_access() below, the actual consumer that needed
        this distinction preserved)."""
        if not self._port:
            return PortReleaseOutcome.PORT_FREE

        start = time.time()
        last_outcome: Optional[PortReleaseOutcome] = None
        while time.time() - start < timeout:
            last_outcome = self.check_port_release_once()
            if last_outcome == PortReleaseOutcome.PORT_FREE:
                return last_outcome
            time.sleep(0.2)

        detail = last_outcome.value if last_outcome is not None else "no check completed"
        print(
            f"[SerialPortSupervisor] Serial port not confirmed free after {timeout}s "
            f"(last outcome: {detail}): {self._port}",
            file=sys.stderr, flush=True,
        )
        return last_outcome if last_outcome is not None else PortReleaseOutcome.CHECK_FAILED

    def wait_serial_release(self, timeout: float = 8) -> bool:
        """Public bool contract unchanged (existing callers - server.py's
        thin wrapper, SerialTransport's connect(force=True) branch - all
        just need "did it free up in time"). claim_exclusive_access()
        below no longer goes through this method - it calls
        _wait_for_release_outcome() directly so it can keep the
        distinction this bool boundary still discards for these other
        callers, none of which currently need it."""
        return self._wait_for_release_outcome(timeout=timeout) == PortReleaseOutcome.PORT_FREE

    def _prepare_command(self, timeout: float = 8) -> PortReleaseOutcome:
        self._pause_listen.set()
        self.stop_listener_process()
        return self._wait_for_release_outcome(timeout=timeout)

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

        Droidian follow-up: _prepare_command() now returns the actual
        PortReleaseOutcome instead of a bare bool, so this can raise a
        TransportError that honestly reflects what happened - BUSY only
        for a confirmed PORT_BUSY (a real owner was found), and the
        distinct PORT_CHECK_INCONCLUSIVE for anything else that isn't
        PORT_FREE (the check itself couldn't reach a definitive answer -
        an external tool timed out/errored/is missing). Previously both
        cases raised the identical BUSY error, which is what let a mere
        checking failure surface to the user as a false "Serial port
        busy" claim.
        """
        with self._radio_lock:
            outcome = self._prepare_command(timeout=timeout)
            try:
                if outcome != PortReleaseOutcome.PORT_FREE:
                    if outcome == PortReleaseOutcome.PORT_BUSY:
                        raise TransportError(
                            TransportErrorCode.BUSY, f"Serial port busy: {self._port or 'auto-detect'}"
                        )
                    raise TransportError(
                        TransportErrorCode.PORT_CHECK_INCONCLUSIVE,
                        f"Could not confirm serial port release ({outcome.value}): "
                        f"{self._port or 'auto-detect'}",
                    )
                yield
            finally:
                if cooldown:
                    time.sleep(cooldown)
                self._pause_listen.clear()
