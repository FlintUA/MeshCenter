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
from meshsrv.serial_reconnect import (
    DeviceIdentity,
    capture_device_identity,
    device_identity_changed,
    identity_retry_delay,
    line_signals_disconnect,
)


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
        resolve_port: Optional[Callable[[], str]] = None,
        verify_identity: Optional[Callable[[str], tuple[str, str]]] = None,
        on_identity_mismatch: Optional[Callable[[str], None]] = None,
        on_match: Optional[Callable[[str, str], None]] = None,
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
        # H2-C Phase 2 (serial hot-reconnect): resolve_port() is consulted
        # before every (re)start, not just once at construction - the
        # default just keeps returning the fixed self._port, matching this
        # class's original, always-static behavior for any caller that
        # doesn't inject real by-id resolution (meshsrv/serial_reconnect.py's
        # resolve_by_id_target(), wired in by server.py). verify_identity()
        # is only ever consulted after a REAL disconnect was detected (see
        # _disconnect_detected below) - never on an ordinary crash-loop
        # retry, since a --info probe is comparatively slow. Returns
        # (status, output): status is the same vocabulary meshsrv/
        # radio_identity.py already uses ("MATCH"/"MISMATCH"/
        # "DETECTION_ERROR"/"NOT_FOUND"); output is the raw --info text the
        # probe captured (review round 2, item 3 - needed so on_match()
        # below can be called with it AFTER radio_lock is released,
        # instead of the caller running its own post-MATCH side effects
        # from inside _verify_identity() itself, while still holding the
        # lock). The default (("MATCH", "") always) preserves the
        # pre-H2-C behavior of just retrying blindly, for any caller that
        # doesn't inject a real verifier.
        self._resolve_port = resolve_port or (lambda: self._port)
        self._verify_identity = verify_identity or (lambda port: ("MATCH", ""))
        self._on_identity_mismatch = on_identity_mismatch or (lambda port: None)
        # Review round 2, item 3: called on a fresh MATCH, AFTER
        # radio_lock has already been released (see
        # _await_identity_before_restart() below) - verify_identity()
        # itself runs INSIDE that lock (it's the slow --info probe the
        # lock exists to serialize against a concurrent claim), so any
        # caller-side work that doesn't need the lock (seeding node/
        # telemetry state, starting background workers) must happen here
        # instead, not from within verify_identity() - doing it there
        # would hold radio_lock for that whole duration too, blocking
        # every claim and adding a radio_lock -> state_lock ordering edge
        # that doesn't need to exist.
        self._on_match = on_match or (lambda port, output: None)

        self._listen_process: Optional[subprocess.Popen] = None
        self._connected_since: Optional[float] = None

        # H2-C Phase 2 state - see run_listener()'s own docstring and
        # meshsrv/serial_reconnect.py's module docstring for the three
        # failure modes this closes.
        self._device_identity_snapshot: Optional[DeviceIdentity] = None
        self._disconnect_detected = threading.Event()
        self._mismatch_active = threading.Event()
        self._mismatch_port: str = ""
        self._identity_retry_attempt = 0
        self._consecutive_errors = 0
        # Review follow-up: the device last PROVEN to match the accepted
        # radio (set only by _await_identity_before_restart()'s own MATCH
        # branch, or mark_device_verified() for the boot-time case below).
        # _listener_cycle()'s own pre-Popen check compares against this on
        # every single restart attempt, not just ones a disconnect marker
        # or health-tick already flagged - closing the gap where a plain
        # nonzero-exit crash-loop (no marker at all) or _resolve_port()'s
        # own by-id-missing fallback could Popen against an unverified
        # device.
        self._verified_device: Optional[DeviceIdentity] = None

    # ------------------------------------------------------------------
    # Listener subprocess (Stage A - see adapters/meshtastic/
    # serial_transport.py's module docstring "DESIGN NOTE - listener
    # subprocess moved out"). Only ever run on Core's own instance of
    # this class - SerialTransport's own composed instance never calls
    # this (Stage A: a full move of the listener into the adapter
    # process is a separate, not-yet-done "Stage B").
    # ------------------------------------------------------------------
    def run_listener(self) -> None:
        """Blocking retry loop - itself a 1:1 replacement for server.py's
        former listen_meshtastic() (Task 44), minus the Meshtastic-protocol
        parsing (delivered line-by-line to on_raw_line instead of parsed
        inline). Call this from Core's own daemon thread, same as before.

        H2-C Phase 2 (serial hot-reconnect): the per-iteration body lives in
        _listener_cycle() so it's directly testable (construct a supervisor,
        monkeypatch subprocess.Popen, call _listener_cycle() once) without
        needing a real infinite loop or a real subprocess - this method
        itself is just the loop."""
        self._consecutive_errors = 0
        while True:
            self._listener_cycle()

    _MAX_CONSECUTIVE_ERRORS = 10

    def _listener_cycle(self) -> None:
        """One full iteration of run_listener()'s loop - see that method's
        own docstring for why this is split out."""
        if self._pause_listen.is_set():
            # An intentional claim (Release radio, Node Tools, any
            # claim_exclusive_access() caller) always wins over the
            # disconnect-recovery/mismatch state below - it must suppress
            # re-detection entirely until the claim ends and/or Reconnect
            # is used, never race a --info probe against whatever the
            # claim itself is doing with the port.
            time.sleep(0.5)
            return

        if self._mismatch_active.is_set():
            # A different radio than the accepted one answered on this
            # port (see _verify_identity() below) - stay halted, no
            # Popen, until clear_mismatch() is called externally (the
            # Reconnect-radio flow, or a profile switch). Polled slowly;
            # this is a wait state, not a retry loop.
            time.sleep(2.0)
            return

        if self._disconnect_detected.is_set():
            if not self._await_identity_before_restart():
                return
            # _await_identity_before_restart() only returns True once a
            # fresh MATCH was confirmed - fall through to the ordinary
            # Popen path below, same as any other restart.

        with self._radio_lock:
            self._listen_process = None

        try:
            time.sleep(0.5)

            with self._radio_lock:
                if self._pause_listen.is_set():
                    return

                from meshsrv.runtime_identity import meshtastic_command

                # Resolved fresh before every (re)start, not just once at
                # construction - the default resolver just returns the
                # fixed self._port (pre-H2-C behavior), but server.py
                # wires in by-id resolution here so a replug that changes
                # /dev/ttyACMx is picked up without needing the identity-
                # verification detour above (that only runs after a
                # detected disconnect - an ordinary crash-loop retry with
                # the device never actually having moved still just works).
                resolved_port = self._resolve_port()
                if resolved_port:
                    self._port = resolved_port

                # Review follow-up: the invariant that actually closes the
                # fallback hole - only ever Popen on a device node whose
                # identity was proven to match the accepted radio since it
                # last appeared (_verified_device, set only by
                # _await_identity_before_restart()'s own MATCH branch or
                # mark_device_verified() at boot). This subsumes both the
                # stdout-marker and health-tick detection paths below: a
                # plain nonzero exit with no marker, or _resolve_port()'s
                # own by-id-missing fallback resolving to a DIFFERENT
                # device than the one last verified, both land here - a
                # single cheap os.stat() checked on every attempt, so a
                # healthy crash-loop against the SAME unchanged device
                # costs nothing extra (no --info probe).
                current_identity = capture_device_identity(self._port)
                if current_identity is None or current_identity != self._verified_device:
                    # Live-round diagnostic (review round 2, item 1) - see
                    # terminate_if_device_changed()'s matching print for
                    # the same reasoning.
                    print(
                        f"[SerialPortSupervisor] Pre-Popen identity mismatch on {self._port}: "
                        f"current={current_identity!r} verified={self._verified_device!r}",
                        file=sys.stderr, flush=True,
                    )
                    self._disconnect_detected.set()
                    return
                self._device_identity_snapshot = current_identity

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
                self._consecutive_errors = 0

            disconnect_signal_seen = False
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
                # H2-C Phase 2: the Meshtastic library's own disconnect
                # warning means the device is genuinely gone even though
                # this process hasn't exited (and, left alone, might
                # never exit - the "hung-but-alive" failure mode found
                # live on dev, 2026-10-03). Detected here, acted on right
                # below instead of waiting for a process exit that may
                # never come.
                if line_signals_disconnect(line):
                    disconnect_signal_seen = True
                try:
                    self._on_raw_line(line)
                except Exception as e:
                    print(f"[SerialPortSupervisor] on_raw_line error: {e}", file=sys.stderr, flush=True)
                if disconnect_signal_seen:
                    break

            if disconnect_signal_seen:
                self._terminate_process(proc)
                self._disconnect_detected.set()
                self._identity_retry_attempt = 0

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
                    self._terminate_process(current)
                self._on_lifecycle_event("listener_stop", intentional=True)
                with self._radio_lock:
                    self._listen_process = None
                time.sleep(0.5)
                return

            if return_code is not None and return_code != 0:
                print(
                    f"[SerialPortSupervisor] Listener process ended with code: {return_code}",
                    file=sys.stderr, flush=True,
                )
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0

            self._on_lifecycle_event("listener_stop", intentional=stop_was_intentional)
            with self._radio_lock:
                self._listen_process = None

        except Exception as e:
            self._consecutive_errors += 1
            print(
                f"[SerialPortSupervisor] run_listener (attempt {self._consecutive_errors}): {e}",
                file=sys.stderr, flush=True,
            )
            delay = min(self._consecutive_errors * 2, 30)
            time.sleep(delay)
            return

        if self._consecutive_errors > self._MAX_CONSECUTIVE_ERRORS:
            self._consecutive_errors = 0
            time.sleep(5)
        else:
            time.sleep(2)

    @staticmethod
    def _terminate_process(proc: "subprocess.Popen") -> None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # H2-C Phase 2: disconnect -> re-resolve -> re-verify identity, instead
    # of blindly retrying the same fixed port forever. Only entered after a
    # REAL disconnect was detected (the stdout marker above, or
    # terminate_if_device_changed() below) - never on an ordinary crash-
    # loop retry, since --info is comparatively slow (seconds on a Pi
    # Zero 2W) and most crashes have nothing to do with the device itself.
    # ------------------------------------------------------------------
    def _await_identity_before_restart(self) -> bool:
        """Returns True once a fresh MATCH clears the way to restart the
        listener normally; False means "not yet" (caller should just
        return and let the next cycle check again) - either because the
        device isn't back yet, identity verification errored transiently
        (backed off per identity_retry_delay()), or a MISMATCH just halted
        everything (_mismatch_active handles that from here on, not this
        method)."""
        resolved_port = self._resolve_port()
        if not resolved_port or not os.path.exists(resolved_port):
            delay = identity_retry_delay(self._identity_retry_attempt)
            self._identity_retry_attempt += 1
            time.sleep(delay)
            return False

        # Review follow-up: an --info probe is comparatively slow (a real
        # CLI invocation, seconds on a Pi Zero 2W) - without holding the
        # same lock the Popen path holds, a Release/Node Tools claim could
        # start mid-probe and race it for the port. Holding radio_lock for
        # the probe's own duration serializes against claim_exclusive_
        # access() (which acquires this same lock before its own
        # prepare+work+cooldown span) by construction; the pause_listen
        # checks on both sides of the probe additionally catch
        # prepare_radio_command()'s own un-bounded phase (CLAUDE.md's
        # documented "radio_lock bounded, but prepare_radio_command()'s
        # own phase is not" trade-off - that path can set pause_listen
        # without needing this lock first). Either way: discard the
        # result entirely rather than act on a probe that ran
        # concurrently with, or was immediately followed by, someone
        # else's claim - the next cycle naturally retries once the claim
        # clears pause_listen (_listener_cycle()'s own top-level check
        # suppresses everything while paused).
        with self._radio_lock:
            if self._pause_listen.is_set():
                return False
            status, output = self._verify_identity(resolved_port)
            if self._pause_listen.is_set():
                return False

        if status == "MATCH":
            self._disconnect_detected.clear()
            self._identity_retry_attempt = 0
            self._port = resolved_port
            self._verified_device = capture_device_identity(resolved_port)
            # Live-round diagnostic (review round 2, item 1) - the freshly
            # verified identity, captured right after a confirmed MATCH.
            print(
                f"[SerialPortSupervisor] Identity verified MATCH on {resolved_port}: "
                f"{self._verified_device!r}",
                file=sys.stderr, flush=True,
            )
            # Review round 2, item 3: called here, OUTSIDE the `with
            # self._radio_lock:` block above - radio_lock is already
            # released by this point, so on_match()'s own work (state_lock,
            # file writes, starting threads) never happens while this
            # listener thread is still holding radio_lock.
            self._on_match(resolved_port, output)
            return True

        if status == "MISMATCH":
            self._mismatch_active.set()
            self._mismatch_port = resolved_port
            self._disconnect_detected.clear()
            self._identity_retry_attempt = 0
            self._on_identity_mismatch(resolved_port)
            return False

        # DETECTION_ERROR / NOT_FOUND - the radio hasn't proven itself yet,
        # try again after backing off. Stays in _disconnect_detected state
        # (not cleared) so the next cycle re-enters this same method.
        delay = identity_retry_delay(self._identity_retry_attempt)
        self._identity_retry_attempt += 1
        time.sleep(delay)
        return False

    def terminate_if_device_changed(self) -> bool:
        """Called periodically from OUTSIDE this thread (server.py's
        radio_health_worker(), ~every 30s) - detects a hung-but-alive
        listener process (the Meshtastic library noticed its own
        disconnect and logged it, see line_signals_disconnect(), but the
        Python process itself never exited) by comparing the device node
        currently at self._port against the snapshot captured when this
        process was started. Kills it if changed/gone, which hands control
        back to run_listener()'s own unintentional-stop-and-recover path -
        the normal crash-and-retry flow, just triggered externally instead
        of by the process exiting on its own. Returns True if it acted.

        Deliberately conservative: does nothing while an intentional claim
        (pause_listen) is in progress - this is a safety net for a
        process that's ALIVE but silently dead-ended, not a replacement
        for the normal claim/release dance."""
        with self._radio_lock:
            proc = self._listen_process
            if proc is None or proc.poll() is not None:
                return False
            if self._pause_listen.is_set():
                return False
            snapshot = self._device_identity_snapshot
            port = self._port

        if not device_identity_changed(snapshot, port):
            return False

        # Live-round diagnostic (review round 2, item 1): captures exactly
        # which field(s) actually differ on a real replug - requested so
        # the live hardware round can confirm USB busnum/devnum behaves as
        # expected (monotonic per plug-in) versus st_ino (which CI showed
        # can be reused), without needing to re-run with extra logging
        # later.
        current = capture_device_identity(port)
        print(
            f"[SerialPortSupervisor] Device identity changed on {port}: "
            f"old={snapshot!r} new={current!r}",
            file=sys.stderr, flush=True,
        )

        self._terminate_process(proc)
        with self._radio_lock:
            if self._listen_process is proc:
                self._listen_process = None
        self._disconnect_detected.set()
        self._identity_retry_attempt = 0
        return True

    def clear_mismatch(self) -> None:
        """Called by the Reconnect-radio flow / a profile switch to leave
        the halted-on-mismatch state and try again from scratch (a fresh
        by-id resolve + identity check, not an assumption that the
        mismatch is resolved just because the user clicked something)."""
        self._mismatch_active.clear()
        self._mismatch_port = ""
        self._disconnect_detected.set()
        self._identity_retry_attempt = 0

    def start_in_recovery_state(self, *, mismatch: bool = False, port: str = "") -> None:
        """Called once, before run_listener(), when boot-time identity
        verification wasn't already a confirmed MATCH (server.py's
        listen_meshtastic()) - seeds the SAME disconnect-recovery/mismatch
        state machine a live disconnect would use, instead of the old
        behavior of refusing to ever start the listener thread at all.

        `mismatch=True` (boot-time MISMATCH - a different, known radio
        answered) halts immediately with no Popen, same as a live
        mismatch - there's nothing to "wait for" since the wrong radio is
        already confirmed. `mismatch=False` (DETECTION_ERROR/NOT_FOUND/
        NOT_CHECKED - the radio simply hasn't answered yet, or nothing
        was checked) enters the normal wait-for-device-then-verify loop,
        same as after a live disconnect."""
        if mismatch:
            self._mismatch_active.set()
            self._mismatch_port = port
        else:
            self._disconnect_detected.set()

    def mark_device_verified(self, port: str) -> None:
        """The boot-time counterpart to _await_identity_before_restart()'s
        own MATCH branch - the only OTHER way _verified_device is ever
        set. Called once by server.py's listen_meshtastic() when boot-time
        identity verification already confirmed MATCH, so the very first
        Popen attempt doesn't immediately fail the pre-Popen verified-
        device check (_listener_cycle() would otherwise treat a freshly-
        started process as "never verified" and loop straight back into
        disconnect-recovery before ever sending a single --listen)."""
        self._verified_device = capture_device_identity(port)

    def is_mismatch_active(self) -> bool:
        return self._mismatch_active.is_set()

    @property
    def mismatch_port(self) -> str:
        return self._mismatch_port

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
