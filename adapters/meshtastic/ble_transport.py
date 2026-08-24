"""BLETransport — the RadioTransport implementation over
`meshtastic.ble_interface.BLEInterface`.

Task 45: happy-path only, per section 5 of the license/BLE plan. Wraps
BLEInterface - does not implement GATT itself. No pairing/PIN UI: this
transport connects to a device already paired at the OS (BlueZ) level,
confirmed sufficient by the live Task 43 test on TAP2 (`bluetoothctl pair`
with the node's PIN, then `meshtastic --ble <addr> --info` succeeded).

OWNERSHIP MODEL - different from SerialTransport, on purpose (per review
discussion ahead of implementation): SerialTransport opens a fresh
SerialInterface inside every send_*/get_* call and closes it in a
`finally` (radio_lock-guarded, because it competes with the --listen
subprocess and api/api_chat.py for the one serial port). BLETransport
has no such competitor and BLE connection setup is comparatively
expensive/slow (live-tested: several seconds, and reopening on every call
risks reproducing the >90s hang from Task 43). So here, a single
`self._interface` is opened once in connect() and reused by every
subsequent call until disconnect()/close(). Any send_*/get_* called while
`self._state != ConnectionState.CONNECTED` raises/returns
TransportError(NOT_CONNECTED) - it never tries to auto-connect.

One consequence: send_messages()'s "one connection for the whole batch"
requirement (docs/BACKEND_API.md "Batching") is trivial here - it is
already one persistent connection for *everything*, not just one batch.
No _claim_radio()-style prepare/cooldown choreography is needed or
present.

NOT this class's responsibility: a Meshtastic node accepts only one
active link (serial OR BLE) at a time - confirmed live in Task 43 (BLE
connect failed until the USB cable was physically removed). BLETransport
does not check or enforce this; Core (Task 46/47) is responsible for
making sure SerialTransport's listener is fully stopped before a
BLETransport connect is attempted.
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Callable, Optional, Sequence

from adapters.meshtastic._timeout_support import TimeoutEnforced
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

# Fixed reconnect attempts with growing backoff - "naive reconnect" per
# plan section 5.5, not exponential/jittered/configurable.
_RECONNECT_DELAYS_S = (1.0, 2.0, 4.0)


class BLETransport(TimeoutEnforced, RadioTransport):
    def __init__(
        self,
        address: str,
        name: str = "",
        expected_node_id: Optional[str] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        TimeoutEnforced.__init__(self, thread_name_prefix="ble-transport-watchdog")
        self._address = address
        self._name = name
        self._expected_node_id = expected_node_id
        self._on_log = on_log or (lambda msg, level="INFO": None)

        # Guards every mutation of _interface/_state (connect/disconnect/
        # close) and every read+use of _interface (_require_connected()
        # and all send_*/get_* bodies). Added per review: without it,
        # a concurrent disconnect()/reconnect() (e.g. a user hitting
        # "Switch to Serial" in the UI, Task 46/47) could null out
        # self._interface while a send_*/get_* call already in flight is
        # still using it - the same class of bug already caught and
        # fixed once in SerialTransport's _prepare_radio_command() race
        # (Task 44 review). NOT reentrant - _require_connected() must
        # only ever be called by code that already holds this lock, never
        # acquires it itself.
        self._lock = threading.Lock()

        self._interface = None
        self._state = ConnectionState.DISCONNECTED
        self._connected_since: Optional[float] = None
        self._last_error: Optional[TransportError] = None
        self._node_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _require_connected(self) -> None:
        """Caller MUST already hold self._lock - this does not acquire
        it (self._lock is a plain, non-reentrant Lock)."""
        if self._state != ConnectionState.CONNECTED or self._interface is None:
            raise TransportError(TransportErrorCode.NOT_CONNECTED, "BLETransport is not connected")

    def _force_disconnect_os_level(self) -> None:
        """Live Task 43 finding: a stale OS-level (BlueZ) GATT session
        from a previous attempt silently blocked a fresh connect until
        `bluetoothctl disconnect <address>` was run. connect(force=True)
        reproduces that exact fix instead of assuming a clean slate."""
        try:
            subprocess.run(
                ["bluetoothctl", "disconnect", self._address],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:
            print(f"[BLETransport] force disconnect warning: {e}", flush=True)

    @staticmethod
    def _local_node_id(interface) -> Optional[str]:
        my_info = getattr(interface, "myInfo", None)
        num = getattr(my_info, "my_node_num", None)
        if num is None:
            return None
        return f"!{int(num):08x}"

    def _detach_and_close_async(self, *, timeout: float) -> None:
        """Synchronously detaches self._interface and flips self._state
        to DISCONNECTED (under self._lock, so no other call can ever
        observe a stale CONNECTED pointing at an interface mid-teardown -
        the exact bug this replaced, per review), THEN closes the
        detached interface in the background via _call_with_timeout.
        `timeout` bounds how long the CALLER waits for that close() to
        actually finish - it never affects the correctness of this
        transport's own state, which is already updated before this
        method's background call even starts. Used by both disconnect()
        and connect(force=True)'s teardown of a stale interface - neither
        one may block its caller's thread for the full close() duration
        (live-measured on TAP2 at 60s+) without this timeout wrapper."""
        with self._lock:
            interface_to_close = self._interface
            self._interface = None
            self._state = ConnectionState.DISCONNECTED
            self._connected_since = None

        def _do_close():
            if interface_to_close is not None:
                try:
                    interface_to_close.close()
                except Exception:
                    pass

        try:
            self._call_with_timeout(_do_close, timeout=timeout, what="close interface")
        except TransportError as error:
            # Own state is already correct (detached above) regardless -
            # this is purely "how long did the background close() take",
            # not a correctness signal, so it's logged, not re-raised.
            self._on_log(f"BLE interface close() did not finish within {timeout}s: {error}", "WARNING")

    # ------------------------------------------------------------------
    # RadioTransport - connection lifecycle
    # ------------------------------------------------------------------
    def connect(
        self, descriptor: ConnectionDescriptor, *, force: bool = False, timeout: float = 90.0
    ) -> ConnectionInfo:
        """Default raised from the ABC docstring's 30.0 to 90.0 - the ABC
        only requires the method to exist, Python doesn't enforce
        subclasses matching its documented default. Live-measured on
        TAP2 twice, consistently: a real connect() took 71.5-71.8s. With
        the ABC default, this isn't "a bit slow" - a caller that doesn't
        remember to override `timeout` on every single call gets
        TransportError(TIMEOUT) on fully working hardware, nearly always.
        Same reasoning as disconnect()'s 15s -> 30s raise: not chasing an
        exact number, taking a reasonable margin over what was actually
        observed twice."""
        if descriptor.type != ConnectionType.BLUETOOTH:
            raise TransportError(
                TransportErrorCode.UNSUPPORTED, f"BLETransport cannot connect to {descriptor.type}"
            )

        # ROLLBACK (live Task 47 finding on TAP2): self._address/_name are
        # about to be overwritten unconditionally below, before the new
        # address has even been tried - a failed connect() used to leave
        # them pointing at the bad value permanently, so a later bare
        # reconnect() (which by contract reuses self._address, not a
        # fresh descriptor) would retry the same bad address forever
        # instead of the last-known-good one. Snapshot before any
        # mutation - not just under force=True, since the mutation two
        # lines down is unconditional for every connect() call, forced
        # or not - and restore on every failure path below.
        previous_address, previous_name = self._address, self._name

        self._address = descriptor.address or self._address
        self._name = descriptor.label or self._name
        with self._lock:
            self._state = ConnectionState.CONNECTING

        if force:
            # Deliberately not fixed: tearing down a known-good session
            # before confirming the new address is reachable is a real
            # window (the old link is gone before the new one is proven),
            # but there is no cheap way to validate a BLE address's
            # connectability without actually attempting the connection -
            # scan() doesn't guarantee connectability and adds ~10s to
            # every call. Same trade-off already accepted elsewhere this
            # project (predictable failure over a hidden race) - the
            # caller is responsible for its own recovery (see
            # api/api_meshtastic.py's fail-closed switch() recovery),
            # this method does not attempt to silently restore the old
            # connection itself.
            self._detach_and_close_async(timeout=timeout)
            self._force_disconnect_os_level()

        def _do_connect():
            from meshtastic.ble_interface import BLEInterface

            try:
                return BLEInterface(address=self._address, timeout=int(timeout))
            except Exception as exc:
                # Point recognition for the single most common real
                # failure (per review): map it to the ABC's already-
                # defined-but-previously-never-raised DEVICE_NOT_FOUND
                # instead of letting it fall through to the generic
                # UNKNOWN wrap in _call_with_timeout. Anything else
                # (adapter off, permission error, etc.) still falls
                # through to UNKNOWN there.
                message = str(exc)
                lowered = message.lower()
                if "no meshtastic ble peripheral" in lowered or "not found" in lowered or "no such device" in lowered:
                    raise TransportError(TransportErrorCode.DEVICE_NOT_FOUND, message) from exc
                raise

        try:
            interface = self._call_with_timeout(_do_connect, timeout=timeout, what="connect()")
        except TransportError as error:
            with self._lock:
                self._state = ConnectionState.ERROR
                self._last_error = error
                self._address, self._name = previous_address, previous_name
            raise

        node_id = self._local_node_id(interface)

        if self._expected_node_id and node_id != self._expected_node_id:
            # IDENTITY MISMATCH TEARDOWN (per review discussion): close
            # the just-opened GATT session before reporting ERROR - never
            # leave a live connection to the wrong device dangling, same
            # spirit as the ABC's disconnect()/close() completion
            # guarantee.
            try:
                interface.close()
            except Exception:
                pass
            error = TransportError(
                TransportErrorCode.IDENTITY_MISMATCH,
                f"Connected BLE node {node_id!r} does not match expected {self._expected_node_id!r}",
            )
            with self._lock:
                self._state = ConnectionState.ERROR
                self._last_error = error
                self._address, self._name = previous_address, previous_name
            raise error

        with self._lock:
            self._interface = interface
            self._node_id = node_id
            self._state = ConnectionState.CONNECTED
            self._connected_since = time.time()
            self._last_error = None
        return self.get_connection_info()

    def disconnect(self, *, timeout: float = 30.0) -> None:
        """Default raised from 15s to 30s (live-measured on TAP2: a real
        BLEInterface.close() call took 60s+ once) - moderate, not a
        guess at a number that will never time out (variance observed
        was too large for that to be realistic on this hardware). Since
        the fix below makes this transport's own state correct
        regardless of how long the background close() actually takes,
        `timeout` only controls UI responsiveness, not correctness - see
        docs/BACKEND_API.md and Task 47 for surfacing "this can take up
        to a minute on some devices" in the UI rather than chasing a
        timeout number that covers every observed case.

        OWNERSHIP TRANSFER (fixed per review, was a real bug): does
        NOT do "call interface.close() -> then update state", because if
        that close() timed out, the state update after it would never
        run - self._state would stay CONNECTED and self._interface would
        stay pointing at an object a now-abandoned daemon thread is
        concurrently closing in the background. Anything calling
        is_connected()/_require_connected() right after a timed-out
        disconnect() would see a stale CONNECTED and race that same
        background thread for self._interface - the exact class of bug
        self._lock exists to prevent, so this fix and that lock depend
        on each other. See _detach_and_close_async()."""
        self._detach_and_close_async(timeout=timeout)

    def reconnect(self, *, timeout: float = 90.0) -> ConnectionInfo:
        """Naive reconnect (plan section 5.5): fixed attempts with
        growing backoff, not exponential/unbounded.

        Default raised to match connect()'s (see that method's
        docstring) - this `timeout` is passed straight through to each
        connect() attempt in the retry loop below, so leaving this at
        the old 30.0 would silently reintroduce the same "fails on fully
        working hardware" defect connect() itself just had fixed."""
        self.disconnect(timeout=min(timeout, 30.0))

        last_error: Optional[TransportError] = None
        for attempt, delay in enumerate(_RECONNECT_DELAYS_S, start=1):
            try:
                return self.connect(
                    ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address=self._address, label=self._name),
                    force=True,
                    timeout=timeout,
                )
            except TransportError as error:
                last_error = error
                self._on_log(f"BLE reconnect attempt {attempt} failed: {error}", "WARNING")
                time.sleep(delay)

        with self._lock:
            self._state = ConnectionState.ERROR
            self._last_error = last_error
        raise last_error or TransportError(TransportErrorCode.CONNECT_FAILED, "reconnect() exhausted all attempts")

    def is_connected(self) -> bool:
        with self._lock:
            return self._state == ConnectionState.CONNECTED and self._interface is not None

    def get_connection_info(self) -> ConnectionInfo:
        with self._lock:
            return ConnectionInfo(
                state=self._state,
                descriptor=ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address=self._address, label=self._name),
                node_id=self._node_id,
                connected_since=self._connected_since,
                last_error=self._last_error,
            )

    def close(self) -> None:
        self.disconnect(timeout=30.0)
        self._shutdown_executor()

    # ------------------------------------------------------------------
    # NOT part of the RadioTransport ABC: device discovery for the
    # future Settings "Scan" button (Task 46). Live-tested against
    # meshtastic.ble_interface.BLEInterface.scan() (re-read fresh ahead
    # of this implementation, not from memory): a @staticmethod, no
    # arguments, blocks ~10s, already filters by the Meshtastic service
    # UUID internally. Returns a list of bleak BLEDevice objects - a
    # third-party library type, so reduced to plain dicts here per the
    # same "no library types out of the transport" rule as everywhere
    # else in this package.
    # ------------------------------------------------------------------
    def scan(self, *, timeout: float = 15.0) -> list[dict]:
        def _do_scan():
            from meshtastic.ble_interface import BLEInterface

            devices = BLEInterface.scan()
            return [{"name": d.name, "address": d.address} for d in devices]

        return self._call_with_timeout(_do_scan, timeout=timeout, what="scan()")

    # ------------------------------------------------------------------
    # RadioTransport - sending. Persistent self._interface (see module
    # docstring's OWNERSHIP MODEL) - not reopened per call.
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
            with self._lock:
                self._require_connected()
                sent = self._interface.sendData(
                    payload,
                    destinationId=destination_id,
                    portNum=port_num,
                    wantAck=want_ack,
                )
                packet_id = getattr(sent, "id", None)
                return SendResult(accepted=True, packet_id=int(packet_id) if packet_id is not None else None)

        try:
            return self._call_with_timeout(_do_send, timeout=timeout, what="send_packet()")
        except TransportError as error:
            return SendResult(accepted=False, error=error)

    def send_messages(
        self, messages: Sequence[OutgoingMessage], *, timeout: float = 30.0
    ) -> list[SendResult]:
        """Trivial loop over the already-open self._interface - no
        connect/disconnect-per-batch choreography needed (see module
        docstring's OWNERSHIP MODEL); "one connection for the whole
        batch" is automatically true because it's one connection for
        everything until disconnect()."""

        def _do_send_all():
            with self._lock:
                self._require_connected()
                results: list[SendResult] = []
                for message in messages:
                    try:
                        sent = self._interface.sendText(
                            text=message.text,
                            destinationId=message.destination_id,
                            wantAck=message.want_ack,
                            channelIndex=message.channel_index,
                            replyId=message.reply_id,
                        )
                        packet_id = getattr(sent, "id", None)
                        results.append(
                            SendResult(accepted=True, packet_id=int(packet_id) if packet_id is not None else None)
                        )
                    except Exception as e:
                        results.append(SendResult(accepted=False, error=TransportError(TransportErrorCode.UNKNOWN, str(e))))
                return results

        try:
            return self._call_with_timeout(_do_send_all, timeout=timeout, what="send_messages()")
        except TransportError as error:
            return [SendResult(accepted=False, error=error) for _ in messages]

    def send_waypoint(self, waypoint: OutgoingWaypoint, *, timeout: float = 15.0) -> WaypointResult:
        import secrets

        def _waypoint_id() -> int:
            return secrets.randbelow(1_000_000_000 - 1) + 1

        def _do_send():
            with self._lock:
                self._require_connected()
                waypoint_id = int(waypoint.waypoint_id or _waypoint_id())
                waypoint_packet = self._interface.sendWaypoint(
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
                    notification_packet = self._interface.sendText(
                        text=waypoint.notification_text,
                        destinationId="^all",
                        channelIndex=int(waypoint.channel_index),
                        wantAck=False,
                        wantResponse=False,
                    )
                    notification_packet_id = int(notification_packet.id)

                return WaypointResult(
                    waypoint_id=waypoint_id,
                    waypoint_packet_id=int(waypoint_packet.id),
                    notification_packet_id=notification_packet_id,
                )

        return self._call_with_timeout(_do_send, timeout=timeout, what="send_waypoint()")

    # ------------------------------------------------------------------
    # RadioTransport - reads. Same "not wired to any Core call site yet"
    # status as SerialTransport's - Task 46 territory.
    # ------------------------------------------------------------------
    def get_nodes(self, *, timeout: float = 15.0) -> list[NodeInfo]:
        def _do_get():
            with self._lock:
                self._require_connected()
                raw_nodes = getattr(self._interface, "nodes", None) or {}
                return [self._to_node_info(node_id, data) for node_id, data in raw_nodes.items()]

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_nodes()")

    def get_local_node(self, *, timeout: float = 15.0) -> NodeInfo:
        def _do_get():
            with self._lock:
                self._require_connected()
                local = getattr(self._interface, "localNode", None)
                node_num = getattr(local, "nodeNum", None)
                raw_nodes = getattr(self._interface, "nodes", None) or {}
                for node_id, data in raw_nodes.items():
                    if data.get("num") == node_num:
                        return self._to_node_info(node_id, data)
                raise TransportError(TransportErrorCode.UNKNOWN, "Local node not found in node list")

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_local_node()")

    def get_channels(self, *, timeout: float = 15.0) -> list[ChannelInfo]:
        def _do_get():
            with self._lock:
                self._require_connected()
                raw_channels = getattr(getattr(self._interface, "localNode", None), "channels", None) or []
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
                    # Same role-int-to-name mapping as SerialTransport.get_channels()
                    # (live-tested in Task 44) - mesh_pb2.Channel.Role: 0=DISABLED,
                    # 1=PRIMARY, 2=SECONDARY.
                    role_name = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}.get(role, str(role))
                    if role == 0:
                        continue
                    channels.append(ChannelInfo(index=index, name=name, role=role_name))
                return channels

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_channels()")

    def get_metadata(self, *, timeout: float = 15.0) -> dict:
        """Unlike SerialTransport.get_metadata() (which shells out to a
        second, independent `meshtastic --info` CLI call), this does NOT
        open a second BLE connection to fetch metadata - a Meshtastic
        node's BLE stack was not verified to accept concurrent
        connections, and doing so while self._interface already holds
        one risks reproducing the Task 43 hang. Instead this reads the
        already-open self._interface's own `.metadata` field (a
        `mesh_pb2.DeviceMetadata` populated from the config stream during
        connect() - see meshtastic/mesh_interface.py's onResponseTryConfig
        `fromRadio.HasField("metadata")` handler), same source the CLI's
        own `--info` "Metadata:" line uses internally
        (mesh_interface.py's getLongName()-adjacent showInfo(), which
        calls the same MessageToJson on self.metadata).

        NAMED GAP, same spirit as SerialTransport.get_metadata(): returns
        the metadata as a JSON *string* (via protobuf's MessageToJson),
        not a parsed dict with individual fields - whoever wires this
        into Core in Task 46 must parse it if structured fields are
        needed."""

        def _do_get():
            with self._lock:
                self._require_connected()
                from google.protobuf.json_format import MessageToJson

                metadata = getattr(self._interface, "metadata", None)
                if metadata is None:
                    return {"metadata_json": ""}
                return {"metadata_json": MessageToJson(metadata)}

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_metadata()")

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
        """Same KNOWN SIGNATURE MISMATCH as SerialTransport.set_device_time()
        (epoch_seconds accepted per the ABC but not passed through -
        try_sync() derives system time itself and gates on is_trusted()/
        MIN_SYNC_INTERVAL_S) - see that method's docstring for the full
        rationale, unchanged here."""

        def _do_sync():
            with self._lock:
                self._require_connected()
                outcome = try_node_time_sync(
                    interface=self._interface,
                    log_fn=lambda msg, level="INFO": self._on_log(msg, level),
                )
                return outcome == "synced"

        return self._call_with_timeout(_do_sync, timeout=timeout, what="set_device_time()")
