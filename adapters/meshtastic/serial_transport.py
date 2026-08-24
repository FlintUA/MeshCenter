"""SerialTransport — the RadioTransport implementation over the Meshtastic
CLI's `--listen` subprocess and short-lived `SerialInterface` calls.

Task 44: extends (does not rewrite) meshsrv/meshtastic_transport.py per the
Backend Protocol v1 interface (meshsrv/radio_transport.py). Behavior is
carried over 1:1 from server.py's former listen_meshtastic()/
stop_listener()/wait_serial_release()/prepare_radio_command()/
radio_session()/_attempt_node_time_sync() and storage/waypoint_sender.py -
see each method's docstring for exactly which prior code it replaces.

This is the ONLY module in Core allowed to `import meshtastic` - that is
the whole point of Backend Protocol v1 (see docs/BACKEND_API.md "Where
this comes from"). meshsrv/schedule_actions.py already stopped importing
it as of this same Task 44 change; api/api_chat.py and api/api_waypoints.py
still do their own SerialInterface calls directly and are migrated in
Task 46, not here - see the Task 44 investigation report for why that is
deliberate, not an oversight.

DESIGN NOTE - listener seam (per user direction ahead of implementation):
the retry/reconnect loop and subprocess plumbing live here, but the actual
Meshtastic-protocol parsing (process_nodeinfo, extract_text_message, etc.)
stays in Core exactly as before - this class only ever hands Core a raw,
stripped stdout line via `on_raw_line`. When Task 48 moves this class
behind a subprocess boundary, only that one narrow seam needs to become a
normalized JSON event stream; nothing about the parsing logic has to move
or be rewritten.

DESIGN NOTE - radio_lock/pause_listen: still owned and constructed by
Core (server.py), passed in here by reference, exactly as they are today
passed into RadioConnectionManager. This is deliberate, not an oversight:
api/api_chat.py keeps using Core's own radio_session()/radio_lock/
pause_listen directly and unchanged until Task 46, so those objects can't
become private internals of this class yet - they're shared with code
this task does not touch.
"""
from __future__ import annotations

import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional, Sequence

from adapters.meshtastic._timeout_support import TimeoutEnforced
from meshsrv import meshtastic_transport
from meshsrv.node_time_sync import try_sync as try_node_time_sync
from meshsrv.radio_transport import (
    ChannelInfo,
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    NodeInfo,
    NodeUser,
    OutgoingMessage,
    OutgoingWaypoint,
    RadioTransport,
    SendResult,
    TransportError,
    TransportErrorCode,
    WaypointResult,
)
from meshsrv.runtime_identity import meshtastic_command


class SerialTransport(TimeoutEnforced, RadioTransport):
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
        self._last_error: Optional[TransportError] = None
        # _call_with_timeout()/_executor now live in TimeoutEnforced
        # (adapters/meshtastic/_timeout_support.py, extracted in Task 45
        # so BLETransport doesn't duplicate the same watchdog pattern).
        # Real hardware serialization is still this class's own job (see
        # _claim_radio's radio_lock) - the shared mixin only gives each
        # call its own thread to abandon on timeout, never serializes.
        TimeoutEnforced.__init__(self, thread_name_prefix="serial-transport-watchdog")

    # ------------------------------------------------------------------
    # Internal radio-claim helpers - own copies of server.py's former
    # stop_listener()/wait_serial_release()/prepare_radio_command()/
    # radio_session(), using the SAME radio_lock/pause_listen Core passed
    # in. Deliberately does not check RadioConnectionManager.commands_
    # allowed() - that Core-level policy gate (external configuration
    # mode) stays in Core; callers are expected to check
    # is_radio_available()-equivalent state before calling into this
    # transport, same as _process_send_batch() already does today.
    # ------------------------------------------------------------------
    def _stop_listener_process(self) -> bool:
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
            print(f"[SerialTransport] Error stopping listener: {e}", flush=True)
            return False
        finally:
            with self._radio_lock:
                self._listen_process = None
            time.sleep(1.0)

    def _wait_serial_release(self, timeout: float = 8) -> bool:
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
                print(f"[SerialTransport] wait_serial_release error: {e}", flush=True)
            time.sleep(0.2)

        print(f"[SerialTransport] Serial port still busy after {timeout}s: {self._port}", flush=True)
        return False

    def _prepare_radio_command(self, timeout: float = 8) -> bool:
        self._pause_listen.set()
        self._stop_listener_process()
        return self._wait_serial_release(timeout=timeout)

    @contextmanager
    def _claim_radio(self, timeout: float = 8, cooldown: float = 2.0):
        """Same pause/stop/wait/hold-lock/resume dance as server.py's former
        radio_session(), scoped to this instance's own lock/event.

        DELIBERATE DIVERGENCE from radio_session(): that function calls
        prepare_radio_command() (pause+stop+wait) BEFORE acquiring
        radio_lock, so concurrent callers can all enter the prepare phase
        in parallel and only serialize once they reach `with radio_lock:`
        - each individual read/write of the shared process handle is
        itself lock-protected, so this was never a data race, but with
        this transport's executor now allowed more than one worker
        (see the ThreadPoolExecutor comment in __init__), several
        threads could genuinely run _stop_listener_process()/
        _wait_serial_release() redundantly at once - wasteful, and not
        the strictly-sequential behavior that existed when the executor
        was max_workers=1. Since radio_lock is an RLock, holding it for
        the ENTIRE prepare+work+cooldown span (not just the yield) is
        safe from self-deadlock and fully serializes the prepare phase
        too, at the cost of a caller possibly blocking here for another
        caller's whole claim (prepare included) instead of only its
        interface work - judged the safer trade given "serial port
        contention" is a named, previously-real regression risk for
        this project. Verified by tests/test_serial_transport_timeout.py::
        test_concurrent_connect_and_send_do_not_race_prepare_phase.
        """
        with self._radio_lock:
            prepared = self._prepare_radio_command(timeout=timeout)
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

    def _open_interface(self):
        from meshtastic.serial_interface import SerialInterface

        return SerialInterface(devPath=self._port)

    # ------------------------------------------------------------------
    # RadioTransport - connection lifecycle
    # ------------------------------------------------------------------
    def connect(
        self, descriptor: ConnectionDescriptor, *, force: bool = False, timeout: float = 30.0
    ) -> ConnectionInfo:
        """PRECONDITION carried over from today's behavior: this only
        does something useful once run_listener() is already running in
        its own (Core-owned) thread - same as server.py's listener
        thread today, which starts unconditionally at process startup
        whenever RADIO_IDENTITY_RESULT matches, with pause_listen
        cleared by default (threading.Event() starts unset). There was
        never a distinct "connect" action in the pre-migration code;
        connect()/disconnect() here are the pause_listen.clear()/
        stop_listener()+set() pair that already existed, reframed to fit
        the ABC. Calling connect() before run_listener() has ever been
        started will just time out waiting for is_connected()."""
        if descriptor.type != ConnectionType.SERIAL:
            raise TransportError(
                TransportErrorCode.UNSUPPORTED, f"SerialTransport cannot connect to {descriptor.type}"
            )
        self._port = descriptor.address or self._port

        if force:
            # Same reasoning as _claim_radio()'s "DELIBERATE DIVERGENCE"
            # comment: hold radio_lock for the whole stop+wait sequence,
            # not just individual reads/writes of _listen_process, so a
            # concurrent send_text()/send_messages()/etc. (which now goes
            # through _claim_radio() under the same lock) cannot race a
            # force-reconnect's teardown.
            with self._radio_lock:
                self._stop_listener_process()
                self._wait_serial_release(timeout=timeout)

        def _do_connect():
            self._pause_listen.clear()
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.is_connected():
                    return
                time.sleep(0.2)
            raise TransportError(TransportErrorCode.TIMEOUT, f"connect() exceeded {timeout}s")

        self._call_with_timeout(_do_connect, timeout=timeout, what="connect()")
        self._connected_since = time.time()
        self._last_error = None
        return self.get_connection_info()

    def disconnect(self, *, timeout: float = 15.0) -> None:
        def _do_disconnect():
            self._stop_listener_process()

        self._call_with_timeout(_do_disconnect, timeout=timeout, what="disconnect()")
        self._connected_since = None

    def reconnect(self, *, timeout: float = 30.0) -> ConnectionInfo:
        self.disconnect(timeout=min(timeout, 15.0))
        return self.connect(
            ConnectionDescriptor(type=ConnectionType.SERIAL, address=self._port),
            force=True,
            timeout=timeout,
        )

    def is_connected(self) -> bool:
        with self._radio_lock:
            proc = self._listen_process
        return proc is not None and proc.poll() is None

    def get_listener_pid(self) -> Optional[int]:
        """NOT part of the RadioTransport ABC - Serial-specific status
        introspection for Core's /api/node-manager/dashboard, replacing
        its former direct read of the module-level `listen_process`
        global (which this class's internal _listen_process now
        supersedes)."""
        with self._radio_lock:
            proc = self._listen_process
        return int(proc.pid) if proc is not None and proc.poll() is None else None

    def get_connection_info(self) -> ConnectionInfo:
        return ConnectionInfo(
            state=(ConnectionState.CONNECTED if self.is_connected() else ConnectionState.DISCONNECTED),
            descriptor=ConnectionDescriptor(type=ConnectionType.SERIAL, address=self._port),
            node_id=None,
            connected_since=self._connected_since,
            last_error=self._last_error,
        )

    def close(self) -> None:
        self.disconnect(timeout=15.0)
        self._shutdown_executor()

    # ------------------------------------------------------------------
    # RadioTransport - listener (Stage A: this class owns the subprocess
    # and raw-line plumbing; Core still owns the background thread that
    # calls run_listener(), and all Meshtastic-protocol parsing - see
    # module docstring's "DESIGN NOTE - listener seam").
    #
    # NOT part of the RadioTransport ABC: BLETransport (Task 45) has no
    # equivalent textual --listen stream, so this is SerialTransport-
    # specific, not a protocol requirement.
    # ------------------------------------------------------------------
    def run_listener(self) -> None:
        """Blocking retry loop - replaces server.py's former
        listen_meshtastic() 1:1, minus the Meshtastic-protocol parsing
        (now delivered line-by-line to on_raw_line instead of parsed
        inline). Call this from Core's own daemon thread, same as before."""
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
                        print(f"[SerialTransport] on_raw_line error: {e}", flush=True)

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
                    print(f"[SerialTransport] Listener process ended with code: {return_code}", flush=True)
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0

                self._on_lifecycle_event("listener_stop")
                with self._radio_lock:
                    self._listen_process = None

            except Exception as e:
                consecutive_errors += 1
                print(f"[SerialTransport] run_listener (attempt {consecutive_errors}): {e}", flush=True)
                delay = min(consecutive_errors * 2, 30)
                time.sleep(delay)

            if consecutive_errors > max_consecutive_errors:
                consecutive_errors = 0
                time.sleep(5)
            else:
                time.sleep(2)

    # ------------------------------------------------------------------
    # RadioTransport - sending
    # ------------------------------------------------------------------
    def send_text(self, message: OutgoingMessage, *, timeout: float = 15.0) -> SendResult:
        results = self.send_messages([message], timeout=timeout)
        return results[0]

    def send_packet(
        self,
        payload: bytes,
        destination_id: str,
        *,
        port_num: int,
        want_ack: bool = False,
        timeout: float = 15.0,
    ) -> SendResult:
        def _do_send():
            with self._claim_radio(timeout=timeout, cooldown=2.0):
                interface = self._open_interface()
                try:
                    sent = interface.sendData(
                        payload,
                        destinationId=destination_id,
                        portNum=port_num,
                        wantAck=want_ack,
                    )
                    packet_id = getattr(sent, "id", None)
                    return SendResult(accepted=True, packet_id=int(packet_id) if packet_id is not None else None)
                finally:
                    try:
                        interface.close()
                    except Exception:
                        pass

        try:
            return self._call_with_timeout(_do_send, timeout=timeout, what="send_packet()")
        except TransportError as error:
            return SendResult(accepted=False, error=error)

    def send_messages(
        self, messages: Sequence[OutgoingMessage], *, timeout: float = 30.0
    ) -> list[SendResult]:
        """One connection for the whole batch - see docs/BACKEND_API.md
        'Batching'. Mirrors api/api_chat.py's _process_send_batch(),
        which stays as its own independent implementation in Task 44/45
        (migrated to call through this transport in Task 46)."""

        def _do_send_all():
            results: list[SendResult] = []
            with self._claim_radio(timeout=timeout, cooldown=2.0):
                interface = self._open_interface()
                try:
                    for message in messages:
                        try:
                            destination = message.destination_id
                            sent = interface.sendText(
                                text=message.text,
                                destinationId=destination,
                                wantAck=message.want_ack,
                                channelIndex=message.channel_index,
                                replyId=message.reply_id,
                            )
                            packet_id = getattr(sent, "id", None)
                            results.append(
                                SendResult(
                                    accepted=True,
                                    packet_id=int(packet_id) if packet_id is not None else None,
                                )
                            )
                        except Exception as e:
                            results.append(
                                SendResult(
                                    accepted=False,
                                    error=TransportError(TransportErrorCode.UNKNOWN, str(e)),
                                )
                            )
                finally:
                    try:
                        interface.close()
                    except Exception:
                        pass
            return results

        try:
            return self._call_with_timeout(_do_send_all, timeout=timeout, what="send_messages()")
        except TransportError as error:
            return [SendResult(accepted=False, error=error) for _ in messages]

    def send_waypoint(self, waypoint: OutgoingWaypoint, *, timeout: float = 15.0) -> WaypointResult:
        """Replaces storage/waypoint_sender.py's subprocess script -
        same sendWaypoint()/sendText() calls, now an internal detail of
        this transport instead of a separate one-shot .py file invoked
        over stdin/stdout (per Task 44 section 3: 'сохраняя текущее
        one-shot-подключение как внутреннюю деталь SerialTransport')."""
        import secrets

        def _waypoint_id() -> int:
            return secrets.randbelow(1_000_000_000 - 1) + 1

        def _do_send():
            with self._claim_radio(timeout=timeout, cooldown=2.0):
                interface = self._open_interface()
                try:
                    waypoint_id = int(waypoint.waypoint_id or _waypoint_id())
                    waypoint_packet = interface.sendWaypoint(
                        name=waypoint.name,
                        description=waypoint.description,
                        icon=int(waypoint.icon),
                        expire=int(waypoint.expire_at),
                        waypoint_id=waypoint_id,
                        latitude=float(waypoint.latitude),
                        longitude=float(waypoint.longitude),
                        channelIndex=int(waypoint.channel_index),
                        wantAck=True,
                        wantResponse=False,
                    )

                    notification_packet_id = None
                    if waypoint.post_notification and waypoint.notification_text.strip():
                        notification_packet = interface.sendText(
                            text=waypoint.notification_text,
                            destinationId="^all",
                            channelIndex=int(waypoint.channel_index),
                            wantAck=False,
                            wantResponse=False,
                        )
                        notification_packet_id = int(notification_packet.id)

                    time.sleep(1.5)
                    return WaypointResult(
                        waypoint_id=waypoint_id,
                        waypoint_packet_id=int(waypoint_packet.id),
                        notification_packet_id=notification_packet_id,
                    )
                finally:
                    try:
                        interface.close()
                    except Exception:
                        pass

        return self._call_with_timeout(_do_send, timeout=timeout, what="send_waypoint()")

    # ------------------------------------------------------------------
    # RadioTransport - reads
    #
    # Not wired to any Core call site yet (that is Task 46's scope for
    # server.py's own get_nodes/get_local_node consumers and api/
    # api_chat.py's discover_radio_channels() -> get_channels()) - these
    # exist now only because RadioTransport is an ABC and every method
    # must be concrete for SerialTransport to be instantiable at all.
    # Implemented by reusing the exact pattern already proven in api/
    # api_chat.py's discover_radio_channels() (wait_for_config + settle
    # sleep + reduce-to-primitives at the call site), not new design.
    # ------------------------------------------------------------------
    def get_nodes(self, *, timeout: float = 15.0) -> list[NodeInfo]:
        def _do_get():
            with self._claim_radio(timeout=timeout, cooldown=2.0):
                interface = self._open_interface()
                try:
                    self._settle(interface)
                    raw_nodes = getattr(interface, "nodes", None) or {}
                    return [self._to_node_info(node_id, data) for node_id, data in raw_nodes.items()]
                finally:
                    try:
                        interface.close()
                    except Exception:
                        pass

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_nodes()")

    def get_local_node(self, *, timeout: float = 15.0) -> NodeInfo:
        def _do_get():
            with self._claim_radio(timeout=timeout, cooldown=2.0):
                interface = self._open_interface()
                try:
                    self._settle(interface)
                    local = getattr(interface, "localNode", None)
                    node_num = getattr(local, "nodeNum", None)
                    raw_nodes = getattr(interface, "nodes", None) or {}
                    for node_id, data in raw_nodes.items():
                        if data.get("num") == node_num:
                            return self._to_node_info(node_id, data)
                    raise TransportError(TransportErrorCode.UNKNOWN, "Local node not found in node list")
                finally:
                    try:
                        interface.close()
                    except Exception:
                        pass

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_local_node()")

    def get_channels(self, *, timeout: float = 15.0) -> list[ChannelInfo]:
        def _do_get():
            with self._claim_radio(timeout=timeout, cooldown=2.0):
                interface = self._open_interface()
                try:
                    self._settle(interface)
                    raw_channels = getattr(getattr(interface, "localNode", None), "channels", None) or []
                    channels = []
                    for fallback_index, channel in enumerate(raw_channels):
                        index = getattr(channel, "index", fallback_index)
                        try:
                            index = int(index)
                        except (TypeError, ValueError):
                            index = fallback_index
                        if index < 0 or index > 7:
                            continue
                        settings_obj = getattr(channel, "settings", None)
                        name = getattr(settings_obj, "name", "") if settings_obj is not None else ""
                        role = getattr(channel, "role", None)
                        # Live-tested on TAP2 (Task 44 review requirement):
                        # `role` deserializes as a plain int (0/1/2), not a
                        # protobuf enum wrapper with a name-producing str() -
                        # str(role) gave "1"/"2", not "PRIMARY"/"SECONDARY",
                        # violating this method's own ABC docstring contract.
                        # The old api/api_chat.py discover_radio_channels()
                        # this was ported from never actually returned role
                        # as a string (only used it locally to filter
                        # DISABLED), so it never surfaced this. Meshtastic's
                        # mesh_pb2.Channel.Role: 0=DISABLED, 1=PRIMARY,
                        # 2=SECONDARY.
                        role_name = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}.get(role, str(role))
                        if role == 0:
                            continue
                        channels.append(ChannelInfo(index=index, name=name, role=role_name))
                    return channels
                finally:
                    try:
                        interface.close()
                    except Exception:
                        pass

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_channels()")

    def get_metadata(self, *, timeout: float = 15.0) -> dict:
        """NAMED GAP: the RadioTransport ABC docstring promises "firmware
        version, hwModel, capability flags" - structured fields. This
        implementation does NOT parse them out; it returns the raw
        `meshtastic --info` stdout/stderr text as-is (same output
        server.py's update_base_status_from_info() already knows how to
        pick apart with extract_json_block()/json.loads(), but that
        parsing logic is not duplicated here). Not a problem for Task 44
        (nothing calls this yet), but whoever wires get_metadata() into
        Core in Task 46 must not assume a structured dict with those
        fields - it will need to add the parsing step itself."""

        def _do_get():
            result = meshtastic_transport.get_info(self._cli_path, serial_port=self._port, timeout=timeout)
            return {"stdout": result.stdout, "stderr": result.stderr}

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_metadata()")

    def _settle(self, interface) -> None:
        wait_for_config = getattr(interface, "waitForConfig", None)
        if callable(wait_for_config):
            try:
                wait_for_config()
            except Exception as e:
                print(f"[SerialTransport] waitForConfig() warning: {e}", flush=True)
        time.sleep(1.5)

    @staticmethod
    def _to_node_info(node_id: str, data: dict) -> NodeInfo:
        user_data = data.get("user") or {}
        user = NodeUser(
            id=user_data.get("id", node_id),
            long_name=user_data.get("longName", ""),
            short_name=user_data.get("shortName", ""),
            hw_model=user_data.get("hwModel", ""),
            is_licensed=bool(user_data.get("isLicensed", False)),
        ) if user_data else None
        return NodeInfo(
            node_id=node_id,
            num=data.get("num", 0),
            user=user,
            last_heard=data.get("lastHeard"),
            snr=data.get("snr"),
            rssi=data.get("rssi"),
            hop_count=data.get("hopsAway"),
            is_favorite=bool(data.get("isFavorite", False)),
            device_metrics=data.get("deviceMetrics") or {},
            environment_metrics=data.get("environmentMetrics") or {},
            power_metrics=data.get("powerMetrics") or {},
            position=data.get("position"),
        )

    # ------------------------------------------------------------------
    # RadioTransport - device time
    # ------------------------------------------------------------------
    def set_device_time(self, epoch_seconds: int, *, timeout: float = 15.0) -> bool:
        """Replaces server.py's former _attempt_node_time_sync() - same
        radio_session()+SerialInterface dance (now _claim_radio()), same
        call into meshsrv/node_time_sync.py's try_sync(), which is
        UNCHANGED (it takes `interface` as a parameter and never imports
        meshtastic itself, so it never needed to move).

        KNOWN SIGNATURE MISMATCH (flagged, not silently papered over):
        `epoch_seconds` is accepted per the RadioTransport interface but
        NOT passed through to try_sync() - try_sync() always derives the
        target time itself from meshsrv.time_service.get_status(), and
        additionally gates on is_trusted() and MIN_SYNC_INTERVAL_S. This
        is the exact behavior server.py's _attempt_node_time_sync() had
        before this migration; changing it to actually honor
        `epoch_seconds` would mean bypassing the trust/throttle gates,
        which is new functionality Task 44 explicitly excludes. Left as
        a named gap for whoever revisits this signature later."""

        def _do_sync():
            with self._claim_radio(timeout=timeout, cooldown=2.0):
                interface = self._open_interface()
                try:
                    self._settle(interface)
                    outcome = try_node_time_sync(
                        interface=interface,
                        log_fn=lambda msg, level="INFO": self._on_log(msg, level),
                    )
                    return outcome == "synced"
                finally:
                    try:
                        interface.close()
                    except Exception:
                        pass

        return self._call_with_timeout(_do_sync, timeout=timeout, what="set_device_time()")
