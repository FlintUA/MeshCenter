"""ST1: router budget/priority, delivery-ACK resolution (serial listener, serial send
capture, TCP routing events), time-sync policy, listener-restart health.

Tests needing the real meshtastic library (pubsub topics, the fake radio server) skip
where it is not installed (Core CI has no GPL adapter dependencies)."""
import contextlib
import threading
import time
import types

import pytest

from adapters.meshtastic import _ack_support as ack
from meshsrv import ipc_protocol
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    OutgoingMessage,
    ReceivedBatch,
    ReceivedRoutingEvent,
    SendResult,
    TransportError,
    TransportErrorCode,
)
from meshsrv.transport_router import TransportRouter

try:
    from adapters.meshtastic.fake_radio_server import FakeMeshtasticTcpServer
    _HAVE_MESHTASTIC = True
except ImportError:  # pragma: no cover
    FakeMeshtasticTcpServer = None
    _HAVE_MESHTASTIC = False
needs_meshtastic = pytest.mark.skipif(not _HAVE_MESHTASTIC, reason="meshtastic library not installed")

ECHO_ID, ECHO_NUM = "!b0f14d2a", 0xB0F14D2A
BASE_NUM = 0x067A40FA


# ===========================================================================
# TransportRouter: budget floor, send priority, ACK-window hold
# ===========================================================================

class _SlowTransport:
    def __init__(self, hold=0.0):
        self.hold = hold
        self.calls = []

    def get_connection_info(self):
        return ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=None)

    def get_channels(self, *, timeout):
        self.calls.append(("get_channels", timeout))
        time.sleep(self.hold)
        return []

    def set_device_time(self, ts, *, timeout):
        self.calls.append(("set_device_time", timeout))

    def send_messages(self, messages, *, timeout):
        self.calls.append(("send_messages", timeout))
        time.sleep(self.hold)
        return []

    def send_text(self, message, *, timeout):
        self.calls.append(("send_text", timeout))


def test_a_call_whose_budget_was_eaten_by_the_lock_wait_is_busy_and_never_reaches_the_adapter():
    transport = _SlowTransport()
    router = TransportRouter(transport)
    router._lock.acquire()
    threading.Timer(1.5, router._lock.release).start()      # lock held for 1.5 s ...
    with pytest.raises(TransportError) as err:
        router.send_text(OutgoingMessage(text="x", destination_id="^all", channel_index=0), timeout=6.0)  # ... 6 s budget
    # 4.5 s left < MIN_CALL_BUDGET_S (5 s): not a 4.5 s call that is certain to time out
    assert err.value.code == TransportErrorCode.BUSY
    assert transport.calls == []


def test_a_call_with_enough_budget_left_is_made_normally():
    transport = _SlowTransport()
    router = TransportRouter(transport)
    router.send_text(OutgoingMessage(text="x", destination_id="^all", channel_index=0), timeout=15.0)
    assert [name for name, _ in transport.calls] == ["send_text"]


def test_short_budgets_are_unchanged():
    transport = _SlowTransport()
    router = TransportRouter(transport)
    router.get_channels(timeout=1.0)           # below the floor: the old behaviour
    assert transport.calls and transport.calls[0][0] == "get_channels"


def test_background_claims_are_busy_while_a_send_is_waiting_or_running():
    transport = _SlowTransport(hold=0.6)
    router = TransportRouter(transport)
    sender = threading.Thread(target=lambda: router.send_messages([], timeout=20.0))
    sender.start()
    time.sleep(0.15)
    with pytest.raises(TransportError) as err:
        router.get_channels(timeout=15.0)
    assert err.value.code == TransportErrorCode.BUSY and "being sent" in err.value.message
    sender.join(3)
    router.get_channels(timeout=15.0)            # the send is over: allowed again
    assert [name for name, _ in transport.calls] == ["send_messages", "get_channels"]


def test_a_background_caller_already_queued_gives_way_when_a_send_arrives():
    transport = _SlowTransport(hold=0.0)
    router = TransportRouter(transport)
    router._lock.acquire()                                    # something long holds the lock
    results = {}

    def background():
        try:
            router.get_channels(timeout=15.0)
        except TransportError as error:
            results["error"] = error

    waiting = threading.Thread(target=background)
    waiting.start()
    time.sleep(0.3)
    sender = threading.Thread(target=lambda: router.send_messages([], timeout=15.0))
    sender.start()                                            # a message arrives while discovery queues
    waiting.join(3)
    assert "error" in results and results["error"].code == TransportErrorCode.BUSY
    router._lock.release()
    sender.join(3)
    assert [name for name, _ in transport.calls] == ["send_messages"]


def test_the_ack_window_hold_defers_background_claims_until_released():
    transport = _SlowTransport()
    router = TransportRouter(transport)
    router.hold_background(60)
    assert router.background_hold_remaining() > 55
    with pytest.raises(TransportError) as err:
        router.get_channels(timeout=15.0)
    assert err.value.code == TransportErrorCode.BUSY and "acknowledgement" in err.value.message
    router.send_text(OutgoingMessage(text="x", destination_id="^all", channel_index=0), timeout=15.0)  # sends never held
    router.release_background_hold()
    router.get_channels(timeout=15.0)
    assert [name for name, _ in transport.calls] == ["send_text", "get_channels"]


# ===========================================================================
# ACK rule: only the destination's "NONE" is delivery
# ===========================================================================

def test_classify_ack_rule():
    assert ack.classify_ack("NONE", ECHO_NUM, ECHO_NUM) == "delivered"
    assert ack.classify_ack("NONE", BASE_NUM, ECHO_NUM) is None, "a relay's implicit ack is not delivery"
    assert ack.classify_ack("NONE", ECHO_NUM, None) is None
    assert ack.classify_ack("MAX_RETRANSMIT", BASE_NUM, ECHO_NUM) == "nak"
    assert ack.destination_num("!b0f14d2a") == ECHO_NUM and ack.destination_num("^all") is None


def test_routing_fields_from_the_pinned_librarys_packet_shape():
    packet = {"from": ECHO_NUM, "to": 1970248032, "id": 556,
              "decoded": {"portnum": "ROUTING_APP", "requestId": 123456, "routing": {"errorReason": "NONE"}}}
    assert ack.routing_fields(packet) == (123456, "NONE", ECHO_NUM, 1970248032, 556)
    assert ack.routing_fields({"from": 1, "decoded": {"requestId": 5, "routing": {}}})[1] == "NONE"   # omitted default
    assert ack.routing_fields({"from": 1, "decoded": {"portnum": "ROUTING_APP"}}) is None             # route discovery
    assert ack.routing_fields({"decoded": {"requestId": 5}}) is None
    assert ack.routing_fields("not a packet") is None


@pytest.fixture
def messages_env(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "save_messages", lambda: None)
    released = []
    monkeypatch.setattr(server_module.transport_router, "release_background_hold", lambda: released.append(1))
    added = []

    def add(chat_id=ECHO_ID, packet_id=777, status="sent"):
        message = {"id": f"m{len(added)}", "chat_id": chat_id, "packet_id": packet_id, "status": status,
                   "ack_requested": True, "ack_deadline": 9e12}
        server_module.messages.append(message)
        added.append(message)
        return message

    yield server_module, add, released
    for message in added:
        if message in server_module.messages:
            server_module.messages.remove(message)


def test_destination_ack_marks_delivered_and_releases_the_hold(messages_env):
    srv, add, released = messages_env
    message = add()
    assert srv.resolve_pending_ack(777, "NONE", ECHO_NUM) == "delivered"
    assert message["status"] == "delivered" and "ack_deadline" not in message and released == [1]


def test_a_relays_none_stays_sent(messages_env):
    srv, add, released = messages_env
    message = add()
    assert srv.resolve_pending_ack(777, "NONE", BASE_NUM) == "implicit"
    assert message["status"] == "sent" and "ack_deadline" in message and released == []


def test_unknown_sender_is_not_treated_as_delivery(messages_env):
    srv, add, _ = messages_env
    message = add()
    assert srv.resolve_pending_ack(777, "NONE", None) == "implicit"
    assert message["status"] == "sent"


def test_a_nak_from_anyone_is_unconfirmed_with_the_reason(messages_env):
    srv, add, _ = messages_env
    message = add()
    assert srv.resolve_pending_ack(777, "NO_ROUTE", BASE_NUM) == "unconfirmed"
    assert message["status"] == "unconfirmed" and message["error"] == "NO_ROUTE"


def test_nothing_matches_a_pending_or_unknown_message(messages_env):
    srv, add, _ = messages_env
    pending = add(status="pending")
    assert srv.resolve_pending_ack(777, "NONE", ECHO_NUM) is None and pending["status"] == "pending"
    assert srv.resolve_pending_ack(12345, "NONE", ECHO_NUM) is None


def test_the_serial_listener_parser_applies_the_same_rule(messages_env):
    srv, add, _ = messages_env
    line = ("DEBUG file:mesh_interface.py _handlePacketFromRadio line:1668 Publishing meshtastic.receive.routing: "
            "packet={'from': 2968603946, 'to': 1970248032, 'decoded': {'portnum': 'ROUTING_APP', "
            "'requestId': 777, 'routing': {'errorReason': 'NONE'}}, 'id': 556, 'fromId': None}")
    assert srv.extract_routing_from_num(line) == ECHO_NUM
    delivered = add()
    srv.process_routing_ack_line(line)
    assert delivered["status"] == "delivered"
    srv.messages.remove(delivered)
    relayed = add()
    srv.process_routing_ack_line(line.replace("2968603946", str(BASE_NUM)))
    assert relayed["status"] == "sent", "a relay's implicit ack no longer counts as delivered"


def test_update_message_status_applies_an_ack_captured_inside_the_send(messages_env, monkeypatch):
    srv, add, _ = messages_env
    monkeypatch.setattr(srv, "current_radio_transport", lambda: "serial")
    monkeypatch.setattr(srv.transport_router, "hold_background", lambda s: None)
    message = {"id": "mm1", "chat_id": ECHO_ID, "status": "pending"}
    srv.messages.append(message)
    try:
        srv.update_message_status("mm1", ECHO_ID, "sent", packet_id=99, ack_requested=True, ack_state="delivered")
        assert message["status"] == "delivered" and message["packet_id"] == 99
        message2 = {"id": "mm2", "chat_id": ECHO_ID, "status": "pending"}
        srv.messages.append(message2)
        srv.update_message_status("mm2", ECHO_ID, "sent", packet_id=98, ack_requested=True,
                                  ack_state="nak", ack_error="NO_ROUTE")
        assert message2["status"] == "unconfirmed" and message2["error"] == "NO_ROUTE"
    finally:
        srv.messages[:] = [m for m in srv.messages if m.get("id") not in ("mm1", "mm2")]


def test_a_sent_dm_without_an_ack_holds_background_claims_for_the_ack_window(messages_env, monkeypatch):
    srv, add, _ = messages_env
    holds = []
    monkeypatch.setattr(srv, "current_radio_transport", lambda: "serial")
    monkeypatch.setattr(srv.transport_router, "hold_background", lambda s: holds.append(s))
    message = {"id": "mm3", "chat_id": ECHO_ID, "status": "pending"}
    srv.messages.append(message)
    try:
        srv.update_message_status("mm3", ECHO_ID, "sent", packet_id=97, ack_requested=True)
        assert message["status"] == "sent" and "ack_deadline" in message and holds == [srv.ACK_TIMEOUT_SECONDS]
    finally:
        srv.messages[:] = [m for m in srv.messages if m.get("id") != "mm3"]


def test_bluetooth_never_stamps_an_ack_deadline(messages_env, monkeypatch):
    """BLE cannot receive: asking it for an ACK would flip every DM to unconfirmed after 60 s."""
    srv, add, _ = messages_env
    monkeypatch.setattr(srv, "current_radio_transport", lambda: "bluetooth")
    message = {"id": "mm4", "chat_id": ECHO_ID, "status": "pending"}
    srv.messages.append(message)
    try:
        srv.update_message_status("mm4", ECHO_ID, "sent", packet_id=96, ack_requested=True)
        assert message["status"] == "sent" and "ack_deadline" not in message and not message.get("ack_requested")
    finally:
        srv.messages[:] = [m for m in srv.messages if m.get("id") != "mm4"]


def test_ack_timeout_worker_runs_for_every_transport():
    import inspect
    import server

    assert "threading.Thread(target=ack_timeout_worker" in inspect.getsource(server.start_runtime)


# ===========================================================================
# IPC protocol
# ===========================================================================

def test_ipc_round_trips_the_ack_fields_and_routing_events():
    result = SendResult(accepted=True, packet_id=5, ack_state="delivered", ack_error=None)
    assert ipc_protocol.send_result_from_dict(ipc_protocol.send_result_to_dict(result)) == result
    legacy = ipc_protocol.send_result_from_dict({"accepted": True, "packet_id": 5})
    assert legacy.ack_state is None, "an older adapter's reply still decodes"

    event = ReceivedRoutingEvent(request_id=5, error_reason="NONE", from_num=ECHO_NUM, received_at=1.0,
                                 local_radio_node_id="!756f9960", to_num=1970248032, packet_id=9)
    assert ipc_protocol.received_event_from_dict(ipc_protocol.received_event_to_dict(event)) == event
    assert ReceivedBatch(events=[event]).events == (event,)


# ===========================================================================
# Serial send: the ACK is captured inside the claim
# ===========================================================================

class _FakeSupervisor:
    @contextlib.contextmanager
    def claim_exclusive_access(self, *, timeout, cooldown):
        yield


@needs_meshtastic
def test_serial_send_captures_the_destinations_ack_inside_the_claim(monkeypatch):
    from pubsub import pub
    from adapters.meshtastic import serial_transport as st

    monkeypatch.setattr(st.time, "sleep", lambda *a, **k: None)
    interface = types.SimpleNamespace(closed=False)

    def send_text(**kwargs):
        # the destination answers a moment after the packet went out (relay's implicit ack first)
        def later():
            time.sleep(0.2)
            relay = {"from": BASE_NUM, "to": 1, "decoded": {"requestId": 4242, "routing": {"errorReason": "NONE"}}}
            dest = {"from": ECHO_NUM, "to": 1, "decoded": {"requestId": 4242, "routing": {"errorReason": "NONE"}}}
            pub.sendMessage("meshtastic.receive.routing", packet=relay, interface=interface)
            pub.sendMessage("meshtastic.receive.routing", packet=dest, interface=interface)
        threading.Thread(target=later, daemon=True).start()
        return types.SimpleNamespace(id=4242)

    interface.sendText = send_text
    interface.close = lambda: setattr(interface, "closed", True)
    transport = st.SerialTransport(cli_path="x", port="/dev/ttyFAKE", radio_lock=threading.RLock(),
                                   pause_listen=threading.Event(), supervisor=_FakeSupervisor())
    transport._open_interface = lambda: interface
    started = time.monotonic()
    results = transport.send_messages(
        [OutgoingMessage(text="hi", destination_id=ECHO_ID, channel_index=1, want_ack=True)], timeout=30.0)
    assert results[0].accepted and results[0].packet_id == 4242
    assert results[0].ack_state == "delivered" and results[0].ack_error is None
    assert time.monotonic() - started < 5, "returns as soon as the ACK is seen, not after the whole wait"
    assert interface.closed


@needs_meshtastic
def test_serial_send_reports_a_nak_and_leaves_unacked_sends_alone(monkeypatch):
    from pubsub import pub
    from adapters.meshtastic import serial_transport as st

    monkeypatch.setattr(st.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(ack, "SERIAL_ACK_WAIT_S", 0.6)
    interface = types.SimpleNamespace()
    ids = iter([11, 12, 13])

    def send_text(**kwargs):
        packet_id = next(ids)
        if packet_id == 11:
            threading.Timer(0.1, lambda: pub.sendMessage(
                "meshtastic.receive.routing", interface=interface,
                packet={"from": BASE_NUM, "to": 1, "decoded": {"requestId": 11, "routing": {"errorReason": "NO_ROUTE"}}})).start()
        return types.SimpleNamespace(id=packet_id)

    interface.sendText = send_text
    interface.close = lambda: None
    transport = st.SerialTransport(cli_path="x", port="/dev/ttyFAKE", radio_lock=threading.RLock(),
                                   pause_listen=threading.Event(), supervisor=_FakeSupervisor())
    transport._open_interface = lambda: interface
    results = transport.send_messages([
        OutgoingMessage(text="a", destination_id=ECHO_ID, channel_index=1, want_ack=True),   # NAK
        OutgoingMessage(text="b", destination_id=ECHO_ID, channel_index=1, want_ack=True),   # nothing heard
        OutgoingMessage(text="c", destination_id="^all", channel_index=1, want_ack=False),   # broadcast: no wait
    ], timeout=30.0)
    assert (results[0].ack_state, results[0].ack_error) == ("nak", "NO_ROUTE")
    assert results[1].ack_state is None and results[2].ack_state is None


def test_serial_send_without_want_ack_never_subscribes_or_waits(monkeypatch):
    from adapters.meshtastic import serial_transport as st

    monkeypatch.setattr(st.time, "sleep", lambda *a, **k: None)
    interface = types.SimpleNamespace(sendText=lambda **k: types.SimpleNamespace(id=1), close=lambda: None)
    transport = st.SerialTransport(cli_path="x", port="/dev/ttyFAKE", radio_lock=threading.RLock(),
                                   pause_listen=threading.Event(), supervisor=_FakeSupervisor())
    transport._open_interface = lambda: interface
    started = time.monotonic()
    results = transport.send_messages([OutgoingMessage(text="x", destination_id="^all", channel_index=0)], timeout=30.0)
    assert results[0].accepted and results[0].ack_state is None and time.monotonic() - started < 2


# ===========================================================================
# TCP: routing events end to end (fake radio -> adapter -> Core resolution)
# ===========================================================================

@needs_meshtastic
def test_tcp_routing_ack_end_to_end_with_the_fake_radio(server_module, monkeypatch):
    from adapters.meshtastic.tcp_transport import TCPTransport
    from meshsrv import inbound_events

    server = FakeMeshtasticTcpServer(complete_handshake=True, node_num=0x756F9960)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    monkeypatch.setattr(server_module, "save_messages", lambda: None)
    message = {"id": "t1", "chat_id": ECHO_ID, "packet_id": 31337, "status": "sent",
               "ack_requested": True, "ack_deadline": 9e12}
    server_module.messages.append(message)
    monkeypatch.setattr(server_module, "active_radio_node_id", lambda: "!756f9960")
    try:
        transport.connect(ConnectionDescriptor(type=ConnectionType.TCP, address=f"127.0.0.1:{server.port}"), timeout=15)
        server.send_routing(31337, error_reason="NONE", from_num=BASE_NUM)          # relay's implicit ack
        server.send_routing(31337, error_reason="NONE", from_num=ECHO_NUM)          # the destination
        server.send_routing(555, error_reason="NONE", from_num=ECHO_NUM, to_num=0x11111111)   # not for us
        deadline = time.monotonic() + 5
        events = []
        while time.monotonic() < deadline and len(events) < 2:
            events += list(transport.drain_received().events)
            time.sleep(0.1)
        assert [type(e).__name__ for e in events] == ["ReceivedRoutingEvent"] * 2
        assert {e.from_num for e in events} == {BASE_NUM, ECHO_NUM} and all(e.request_id == 31337 for e in events)
        assert all(e.local_radio_node_id == "!756f9960" for e in events)

        deps = server_module._inbound_deps()
        assert [inbound_events.ingest_received_routing(e, deps) for e in events] == ["implicit", "delivered"]
        assert message["status"] == "delivered"
    finally:
        if message in server_module.messages:
            server_module.messages.remove(message)
        transport.close()
        server.shutdown()


@needs_meshtastic
def test_tcp_nak_and_wrong_radio_events(server_module, monkeypatch):
    from meshsrv import inbound_events

    monkeypatch.setattr(server_module, "save_messages", lambda: None)
    monkeypatch.setattr(server_module, "active_radio_node_id", lambda: "!756f9960")
    message = {"id": "t2", "chat_id": ECHO_ID, "packet_id": 4040, "status": "sent", "ack_requested": True, "ack_deadline": 9e12}
    server_module.messages.append(message)
    try:
        deps = server_module._inbound_deps()
        stale = ReceivedRoutingEvent(request_id=4040, error_reason="NONE", from_num=ECHO_NUM, received_at=1.0,
                                     local_radio_node_id="!deadbeef")
        assert inbound_events.ingest_received_routing(stale, deps) == inbound_events.STALE_RADIO
        assert message["status"] == "sent", "an event from another radio never touches this profile"
        nak = ReceivedRoutingEvent(request_id=4040, error_reason="MAX_RETRANSMIT", from_num=ECHO_NUM, received_at=1.0,
                                   local_radio_node_id="!756f9960")
        assert inbound_events.ingest_received_routing(nak, deps) == "unconfirmed"
        assert message["status"] == "unconfirmed" and message["error"] == "MAX_RETRANSMIT"
    finally:
        server_module.messages.remove(message)


# ===========================================================================
# Time sync policy and listener-restart handling
# ===========================================================================

@pytest.fixture
def sync_env(server_module, monkeypatch):
    attempts = []

    class _Thread:
        def __init__(self, target=None, **kw):
            self._target = target

        def start(self):
            attempts.append(getattr(self._target, "__name__", "?"))

    monkeypatch.setattr(server_module, "threading", types.SimpleNamespace(Thread=_Thread, Lock=threading.Lock))
    monkeypatch.setattr(server_module, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(server_module, "_node_time_sync_last_attempt_ts", 0.0)
    monkeypatch.setattr(server_module, "_node_time_sync_last_claim_stop_ts", float("-inf"))
    original = server_module.radio_health.get("listener_running")
    server_module.radio_health["listener_running"] = False
    yield server_module, attempts
    server_module.radio_health["listener_running"] = original


def test_the_first_listener_start_syncs_the_clock(sync_env):
    srv, attempts = sync_env
    srv.radio_event("listener_start")
    assert attempts == ["_attempt_node_time_sync"]


def test_a_listener_restart_that_ends_a_claim_never_syncs(sync_env):
    srv, attempts = sync_env
    srv.radio_event("listener_stop", intentional=True)       # a claim stopped it
    srv.radio_health["listener_running"] = False
    srv.radio_event("listener_start")                         # ... and the claim ended
    assert attempts == []


def test_a_genuine_reconnect_still_syncs_after_the_cooldown(sync_env, monkeypatch):
    srv, attempts = sync_env
    monkeypatch.setattr(srv, "_node_time_sync_last_attempt_ts", time.time() - srv.NODE_TIME_SYNC_ATTEMPT_COOLDOWN_S - 5)
    srv.radio_event("listener_stop", intentional=False)       # the radio vanished / crashed
    srv.radio_health["listener_running"] = False
    srv.radio_event("listener_start")
    assert attempts == ["_attempt_node_time_sync"]


def test_refresh_is_slow_six_hours(sync_env, monkeypatch):
    srv, attempts = sync_env
    now = time.time()
    monkeypatch.setattr(srv, "_node_time_sync_last_attempt_ts", now - 3600)
    assert srv.maybe_refresh_node_time_sync(now) is False and attempts == []
    assert srv.maybe_refresh_node_time_sync(now + srv.NODE_TIME_SYNC_REFRESH_S) is True and attempts == ["_attempt_node_time_sync"]
    assert srv.maybe_refresh_node_time_sync(now + srv.NODE_TIME_SYNC_REFRESH_S + 60) is False, "once per interval"


def test_health_after_a_claim_is_paused_not_listener_down(server_module):
    status = server_module.compute_radio_health_status(
        active_transport="serial", is_released=False, is_paused=False, listener_running=False,
        packet_age=10, transport_state={"state": "connected"}, restarting_after_claim=True)
    assert status[0] == "PAUSED"
    down = server_module.compute_radio_health_status(
        active_transport="serial", is_released=False, is_paused=False, listener_running=False,
        packet_age=10, transport_state={"state": "connected"})
    assert down[0] == "LISTENER_DOWN", "a real outage is still reported"


def test_a_stop_shortly_after_our_own_request_is_classified_intentional():
    import threading as _t
    from meshsrv import serial_port_supervisor as spv

    events = []
    supervisor = spv.SerialPortSupervisor(
        cli_path="x", port="/dev/ttyACM0", radio_lock=_t.RLock(), pause_listen=_t.Event(),
        on_lifecycle_event=lambda name, intentional=None: events.append((name, intentional)))
    supervisor._last_stop_request_at = time.monotonic()        # a claim asked for the stop ...
    assert supervisor._pause_listen.is_set() is False            # ... and has already finished
    classified = supervisor._pause_listen.is_set() or (
        time.monotonic() - supervisor._last_stop_request_at < spv.INTENTIONAL_STOP_GRACE_S)
    assert classified is True
    supervisor._last_stop_request_at = time.monotonic() - spv.INTENTIONAL_STOP_GRACE_S - 1
    assert (supervisor._pause_listen.is_set() or time.monotonic() - supervisor._last_stop_request_at
            < spv.INTENTIONAL_STOP_GRACE_S) is False, "a crash long after the last claim is still an error"
    assert supervisor.restarting_after_claim() is False
    supervisor._claim_released_at = time.monotonic()
    assert supervisor.restarting_after_claim() is True


@needs_meshtastic
def test_the_serial_ack_waiter_and_the_tcp_listener_share_one_topic_signature():
    """pubsub fixes a topic's signature at its first subscriber; both adapters' subscribers
    live in one process and must be declared identically."""
    from adapters.meshtastic.tcp_transport import TCPTransport

    interface = object()
    waiter = ack.RoutingAckWaiter(interface)
    transport = TCPTransport(host="127.0.0.1")
    try:
        transport._ensure_receive_subscription()          # raises ListenerMismatchError if they disagree
    finally:
        transport._unsubscribe_receive()
        waiter.close()


def test_a_call_that_simply_asked_for_the_minimum_budget_is_made_as_asked():
    """drain_received(timeout=5.0) and friends: no lock wait, so no BUSY."""
    transport = _SlowTransport()
    router = TransportRouter(transport)
    router.get_channels(timeout=5.0)
    router.get_channels(timeout=5.0)
    assert len(transport.calls) == 2


@needs_meshtastic
def test_the_ack_wait_leaves_the_claims_tail_inside_the_callers_budget(monkeypatch):
    """Live regression (.104): a 12 s wait with only 3 s kept back let the adapter watchdog fire
    after the message had gone out. The wait is capped by `budget - SERIAL_ACK_TAIL_RESERVE_S`."""
    from adapters.meshtastic import serial_transport as st

    monkeypatch.setattr(st.time, "sleep", lambda *a, **k: None)
    interface = types.SimpleNamespace(sendText=lambda **k: types.SimpleNamespace(id=77), close=lambda: None)
    transport = st.SerialTransport(cli_path="x", port="/dev/ttyFAKE", radio_lock=threading.RLock(),
                                   pause_listen=threading.Event(), supervisor=_FakeSupervisor())
    transport._open_interface = lambda: interface
    started = time.monotonic()
    results = transport.send_messages(
        [OutgoingMessage(text="x", destination_id=ECHO_ID, channel_index=1, want_ack=True)], timeout=10.0)
    waited = time.monotonic() - started
    assert results[0].accepted and results[0].ack_state is None
    assert waited <= 10.0 - ack.SERIAL_ACK_TAIL_RESERVE_S + 1.0, waited     # ~2 s, never the full 12 s


def test_failed_time_sync_is_retried_after_the_cooldown_not_after_six_hours(monkeypatch):
    """Live on .104 after a restart: the boot-time sync hit a 5 s budget behind the
    boot channel read and failed; the next try was 6 h away."""
    import server as srv

    monkeypatch.setattr(srv, "STARTUP_SYNC_DELAY_S", 0)
    monkeypatch.setitem(srv.RADIO_IDENTITY_RESULT, "status", "MATCH")

    def boom(*a, **k):
        raise RuntimeError("timeout: set_device_time() exceeded 5.28s")

    monkeypatch.setattr(srv.transport_router, "set_device_time", boom)
    monkeypatch.setattr(srv, "_node_time_sync_last_attempt_ts", time.time())
    srv._attempt_node_time_sync()

    now = time.time()
    assert srv.NODE_TIME_SYNC_RETRY_AFTER_FAILURE_S < srv.NODE_TIME_SYNC_REFRESH_S
    assert now - srv._node_time_sync_last_attempt_ts == pytest.approx(
        srv.NODE_TIME_SYNC_REFRESH_S - srv.NODE_TIME_SYNC_RETRY_AFTER_FAILURE_S, abs=5
    )


def test_time_sync_gives_the_serial_claim_enough_time(monkeypatch):
    """timeout=10 made the adapter watchdog fire at ~5 s, before the claim (~10 s) could
    finish: every attempt failed on .104."""
    import server as srv

    seen = {}
    monkeypatch.setattr(srv, "STARTUP_SYNC_DELAY_S", 0)
    monkeypatch.setitem(srv.RADIO_IDENTITY_RESULT, "status", "MATCH")
    monkeypatch.setattr(srv.transport_router, "set_device_time", lambda *a, **k: seen.update(k) or True)
    srv._attempt_node_time_sync()
    assert seen["timeout"] >= 30
