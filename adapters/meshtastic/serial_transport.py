"""SerialTransport — the RadioTransport implementation over short-lived
`SerialInterface` calls (connect/disconnect/send_*/get_*/set_device_time).

Task 44: extends (does not rewrite) meshsrv/meshtastic_transport.py per the
Backend Protocol v1 interface (meshsrv/radio_transport.py).

This is the ONLY module in Core allowed to `import meshtastic` - that is
the whole point of Backend Protocol v1 (see docs/BACKEND_API.md "Where
this comes from"), and as of the stabilization follow-up below, the only
thing left in this module that actually needs it.

DESIGN NOTE - listener subprocess moved out (stabilization follow-up, P0
#1 of the independent audit, after Task 44/48/49): the `--listen`
subprocess retry/reconnect loop and the exclusive-access-claim logic this
class used to own directly (run_listener()/get_listener_pid()/
claim_for_external_command()/the private _stop_listener_process()/
_wait_serial_release() pair) moved to meshsrv/serial_port_supervisor.py's
SerialPortSupervisor - MIT, stdlib-only, no meshtastic import. Not because
that logic was "Core-only" (it wasn't - it's used by this class's own
send_*/get_*/connect() too, on this instance's own composed supervisor,
constructed with its own local radio_lock/pause_listen, never shared with
Core's), but because server.py previously imported THIS class directly
just to reach that slice of behavior - a real boundary smell (a class
living in a GPLv3-labeled directory, imported by Core) even though the
meshtastic import itself is lazy and never reached via that path. This
class now composes a SerialPortSupervisor (self._supervisor, see
__init__) instead of implementing that logic itself; server.py composes
its own SerialPortSupervisor directly and no longer imports anything from
adapters/ at all. Meshtastic-protocol parsing (process_nodeinfo,
extract_text_message, etc.) stays in Core, unchanged - the supervisor only
ever hands Core a raw, stripped stdout line via `on_raw_line`, exactly as
before this move.

DESIGN NOTE - radio_lock/pause_listen: still owned and constructed by
Core (server.py) for Core's own SerialPortSupervisor, passed by reference;
this class's own composed SerialPortSupervisor gets its own separate,
locally-constructed pair instead (see adapters/meshtastic/ipc_server.py's
construction) - the two never share a lock, by design (Stage A: this
instance never runs a listener subprocess, so there is nothing for Core's
listener and this instance's own claims to actually contend over
directly - contention across the process boundary is what
claim_exclusive_access() on Core's own supervisor coordinates instead,
see meshsrv/adapter_ipc_client.py).
"""
from __future__ import annotations

import threading
import time
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
from meshsrv.serial_port_supervisor import SerialPortSupervisor


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
        supervisor: Optional[SerialPortSupervisor] = None,
    ) -> None:
        self._cli_path = cli_path
        self._port = port
        self._radio_lock = radio_lock
        self._pause_listen = pause_listen
        self._on_raw_line = on_raw_line or (lambda line: None)
        self._on_lifecycle_event = on_lifecycle_event or (lambda event: None)
        self._on_log = on_log or (lambda msg, level="INFO": None)

        # Stabilization follow-up (P0 #1, independent audit): the
        # listener-subprocess-management + exclusive-access-claim logic
        # that used to live directly on this class moved to
        # meshsrv/serial_port_supervisor.py's SerialPortSupervisor - MIT,
        # stdlib-only, no meshtastic import - so server.py can depend on
        # it without importing anything from adapters/. This class now
        # composes one instead of implementing that logic itself.
        # `supervisor` is a backward-compatible addition, not a change to
        # existing behavior: both existing construction call sites
        # (server.py's Core-owned instance, adapters/meshtastic/
        # ipc_server.py's adapter-owned instance) keep working unchanged,
        # each getting its own default-constructed supervisor sharing
        # this instance's own radio_lock/pause_listen/cli_path/port -
        # `supervisor=` exists purely as a test-only DI seam, matching
        # the AdapterSupervisor(command=...) precedent in
        # meshsrv/adapter_ipc_client.py.
        self._supervisor = supervisor or SerialPortSupervisor(
            cli_path=cli_path,
            port=port,
            radio_lock=radio_lock,
            pause_listen=pause_listen,
            on_raw_line=on_raw_line,
            on_lifecycle_event=on_lifecycle_event,
            on_log=on_log,
        )

        self._connected_since: Optional[float] = None
        self._last_error: Optional[TransportError] = None
        # Task 48 follow-up (live-caught): on the adapter-side instance
        # (this one, never run_listener()'d - Stage A keeps that on
        # Core's own separate instance), _listen_process is permanently
        # None, so is_connected() could never have reflected reality
        # here even after connect()'s probe fix stopped raising - it
        # would have kept reporting DISCONNECTED after a proven-good
        # connect(). This instance's role is fully stateless-per-call
        # (open/use/close every time, same as send_packet()/get_*()) -
        # _last_probe_ok is the ONLY state connect()/reconnect()/
        # disconnect() track here, set explicitly by them, never
        # inferred from a listener process this instance never owns.
        self._last_probe_ok: bool = False
        # _call_with_timeout()/_executor now live in TimeoutEnforced
        # (adapters/meshtastic/_timeout_support.py, extracted in Task 45
        # so BLETransport doesn't duplicate the same watchdog pattern).
        # Real hardware serialization is still this class's own job (see
        # self._supervisor.claim_exclusive_access()'s radio_lock) - the
        # shared mixin only gives each call its own thread to abandon on
        # timeout, never serializes.
        TimeoutEnforced.__init__(self, thread_name_prefix="serial-transport-watchdog")

    def _open_interface(self):
        from meshtastic.serial_interface import SerialInterface

        return SerialInterface(devPath=self._port)

    # ------------------------------------------------------------------
    # RadioTransport - connection lifecycle
    # ------------------------------------------------------------------
    def connect(
        self, descriptor: ConnectionDescriptor, *, force: bool = False, timeout: float = 30.0
    ) -> ConnectionInfo:
        """Task 48 follow-up (live-caught, two related but distinct bugs
        fixed together): this used to poll is_connected(), which checked
        the listen-process state - a valid connectivity proxy pre-Task-48
        (single process, one shared object owned both roles) but always
        empty on THIS instance post-split (Stage A: the listener subprocess
        only ever runs via Core's own, separate SerialPortSupervisor - see
        meshsrv/serial_port_supervisor.py's run_listener()). Two
        consequences, both fixed here: (1) the poll loop could never succeed, so
        connect()/reconnect() always burned their full timeout and raised
        TIMEOUT - confirmed live on TAP2, an 87s failure on a 90s budget,
        not radio-side slowness. (2) even after fixing the probe itself,
        the OLD final `return self.get_connection_info()` would still
        have reported DISCONNECTED (same is_connected() problem) despite
        a just-proven-good connection - the exact "state lies about a
        successful call" class of bug Task 47's fail-closed recovery work
        was built to prevent, just newly reintroduced at a different
        layer.

        Fix: connect() now actually PROVES connectivity by opening a real
        SerialInterface and closing it - matching send_packet()/get_*()'s
        already-established per-call open/use/close pattern. This is not
        a bare OS-level port open: meshtastic.stream_interface.
        StreamInterface.__init__ (default connectNow=True, noProto=False
        - unchanged by _open_interface()) already calls connect()+
        waitForConfig() synchronously inside the constructor and raises
        if the device never responds (verified by reading the actually-
        installed library source on TAP2, not assumed) - so a clean
        open+close is a real protocol-level handshake, not a false
        positive for "something else is plugged into this port". Success/
        failure is recorded in self._last_probe_ok (see __init__), which
        is_connected()/get_connection_info() now read instead of listener
        state - this instance's role is fully stateless-per-call, so
        that's the only state left for them to reflect.
        """
        if descriptor.type != ConnectionType.SERIAL:
            raise TransportError(
                TransportErrorCode.UNSUPPORTED, f"SerialTransport cannot connect to {descriptor.type}"
            )
        self._port = descriptor.address or self._port

        if force:
            # self._supervisor.wait_serial_release() (a real OS-level
            # `lsof` check against a leftover process still holding the
            # port) stays - genuinely useful regardless of which process's
            # state is being asked about. Stopping a listener process is
            # dropped: this instance's own SerialPortSupervisor never runs
            # one (Stage A - see run_listener()'s docstring), so that was
            # already a guaranteed no-op here, just one that wasted 1.5s
            # (pause_listen.set(); time.sleep(1.5)) setting a local Event
            # nothing else in this process reads meaningfully.
            with self._radio_lock:
                self._supervisor.wait_serial_release(timeout=timeout)

        def _do_connect():
            interface = self._open_interface()  # blocks on the real handshake, raises on failure
            interface.close()

        # Task 48 follow-up review requirement: _last_probe_ok must NOT be
        # written from inside _do_connect() - that function runs on a
        # tier-1-abandoned background thread (_call_with_timeout()'s own
        # documented tier-1/tier-2 gap: a timeout releases the CALLER
        # immediately, but does not kill the thread still running _fn_).
        # Writing shared instance state from that thread would let it
        # finish LATE - after the caller already received
        # TransportError(TIMEOUT) - and silently overwrite
        # _last_probe_ok back to True behind the caller's back.
        #
        # Both writes below happen on THIS (the calling) thread only,
        # never inside _do_connect(): pessimistically False before the
        # attempt (so a failed retry after a previous success correctly
        # downgrades, instead of leaving a stale True), then True only
        # if _call_with_timeout() actually returns - which happens ONLY
        # on a genuine in-budget success. An abandoned thread that
        # finishes later has nothing left to mutate, no matter how long
        # it takes - _do_connect() itself never touches this field.
        self._last_probe_ok = False
        self._call_with_timeout(_do_connect, timeout=timeout, what="connect()")
        self._last_probe_ok = True
        self._connected_since = time.time()
        self._last_error = None
        return self.get_connection_info()

    def disconnect(self, *, timeout: float = 15.0) -> None:
        """No-op by design, not by accident (Task 48 follow-up, explicit
        decision): this instance never holds a persistent interface
        between calls (every operation opens/uses/closes its own, same as
        send_packet()/get_*()/the fixed connect() above) - there is
        nothing to release. Still marks _last_probe_ok False, so a
        subsequent get_connection_info() (e.g. the one connect() itself
        returns after a later reconnect()) doesn't keep reporting a stale
        CONNECTED from before this call."""
        self._connected_since = None
        self._last_probe_ok = False

    def reconnect(self, *, timeout: float = 30.0) -> ConnectionInfo:
        self.disconnect(timeout=min(timeout, 15.0))
        return self.connect(
            ConnectionDescriptor(type=ConnectionType.SERIAL, address=self._port),
            force=True,
            timeout=timeout,
        )

    def is_connected(self) -> bool:
        # Task 48 follow-up: NOT listener-process state (see __init__'s
        # comment on _last_probe_ok) - this instance never runs a
        # listener subprocess, so that state belongs exclusively to
        # Core's own, separate SerialPortSupervisor and was never
        # meaningful here.
        return self._last_probe_ok

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
            with self._supervisor.claim_exclusive_access(timeout=timeout, cooldown=2.0):
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
            with self._supervisor.claim_exclusive_access(timeout=timeout, cooldown=2.0):
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
            with self._supervisor.claim_exclusive_access(timeout=timeout, cooldown=2.0):
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
            with self._supervisor.claim_exclusive_access(timeout=timeout, cooldown=2.0):
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
            with self._supervisor.claim_exclusive_access(timeout=timeout, cooldown=2.0):
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
            with self._supervisor.claim_exclusive_access(timeout=timeout, cooldown=2.0):
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
        radio_session()+SerialInterface dance (now self._supervisor.
        claim_exclusive_access()), same
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
            with self._supervisor.claim_exclusive_access(timeout=timeout, cooldown=2.0):
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
