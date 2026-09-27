"""Integration regression test for adapters/meshtastic/tcp_transport.py -
drives the REAL meshtastic.tcp_interface.TCPInterface (not a mock) end to
end through a local fake TCP radio peer on 127.0.0.1, reproducing the
specific observed failure mode: the TCP connection establishes, the
protocol exchange begins, and the Meshtastic init/config sync never
completes.

Complements tests/test_tcp_transport.py (fully mocked, exercises
TCPTransport's own logic in isolation) with a test that exercises the
real library's actual wire framing, actual protobuf handling, and actual
config-completion state machine (myInfo/nodes/channels via
MeshInterface.waitForConfig()) against a real socket - the one thing the
mocked suite structurally cannot prove.

Uses the real `meshtastic` package - skipped entirely (not an error) if
it isn't installed in this environment, since Core's own venv
deliberately has no dependency on it (see CLAUDE.md's "GPLv3 process
isolation" section) and this suite must stay runnable in that
environment too. No real hardware, no external network - the fake peer
listens on an ephemeral 127.0.0.1 port. This is a SYNTHETIC reproduction
of the CLASS of problem the regression acceptance test describes (a
firmware that accepts the TCP connection, sends some FromRadio traffic,
and never reaches config_complete) - not a byte-for-byte capture of any
specific firmware's actual output. The real acceptance criteria (a real
T-Beam, firmware 2.7.15.567b8ea positive / 2.7.26.54e0d8d regression) are
a separate, documented manual test procedure - see this feature's PR
description.
"""
import threading
import time

import pytest

pytest.importorskip("meshtastic")

from adapters.meshtastic.fake_radio_server import (  # noqa: E402
    FakeMeshtasticTcpServer,
    shrink_internal_handshake_timeout,
)
from adapters.meshtastic.tcp_transport import TCPTransport  # noqa: E402
from meshsrv.radio_transport import (  # noqa: E402
    ConnectionDescriptor,
    ConnectionState,
    ConnectionType,
    TransportError,
    TransportErrorCode,
)


def _descriptor(host: str, port: int) -> ConnectionDescriptor:
    return ConnectionDescriptor(type=ConnectionType.TCP, address=f"{host}:{port}")


def test_real_tcpinterface_completed_handshake_reaches_ready():
    """Positive counter-test, same mechanism as the regression test below:
    the fake server honestly finishes the handshake (sends
    config_complete_id), so the REAL TCPInterface should reach a genuine
    config-complete state and TCPTransport should reach READY. A real
    (not mocked) positive integration test, alongside the already-
    confirmed manual test on real T-Beam hardware."""
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        info = transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)

        assert info.state == ConnectionState.CONNECTED
        assert info.node_id == "!756f9960"
        assert transport.internal_state == "ready"
        assert transport.is_connected() is True
        assert server.accepted_want_config_id is not None

        # The real library's own _connected() synchronously fires a
        # one-time heartbeat write from its reader thread the instant
        # config_complete_id is processed (mesh_interface.py's
        # _startHeartbeat() runs its callback immediately, not only on
        # the first 300s timer tick) - a real, independently-verified
        # behavior of meshtastic itself, unrelated to tcp_transport.py's
        # own correctness. Give that one-time write a moment to land
        # before close() tears the socket down, so close() doesn't race
        # it into a harmless-but-noisy BrokenPipeError on the reader
        # thread (visible in stderr, but never propagated to this test -
        # it would still pass either way; this just keeps the run's
        # output clean).
        time.sleep(0.2)
    finally:
        transport.close()
        server.shutdown()


def test_real_tcpinterface_stalled_handshake_reports_protocol_sync_timeout_cleanly(monkeypatch):
    """The regression acceptance scenario (firmware 2.7.26.54e0d8d: TCP
    connects, FromRadio packets partially received, config never
    completes), reproduced with the REAL TCPInterface against a real
    localhost socket: the fake server accepts the connection, parses the
    client's real want_config_id, sends one real my_info FromRadio frame
    (proving actual protocol traffic occurred, not silence from the very
    first byte), and then never sends config_complete_id.

    Must be reported cleanly within TCPTransport's own declared timeout
    budget - never an indefinite hang, never the generic ambiguous
    TIMEOUT code - and must not leave a non-daemon thread behind that
    would block process/test-suite exit.
    """
    shrink_internal_handshake_timeout(monkeypatch, seconds=3.0)
    server = FakeMeshtasticTcpServer(complete_handshake=False)
    transport = TCPTransport(host="127.0.0.1", port=server.port)

    threads_before = set(threading.enumerate())

    try:
        started = time.monotonic()
        with pytest.raises(TransportError) as excinfo:
            transport.connect(_descriptor("127.0.0.1", server.port), timeout=1.5)
        elapsed = time.monotonic() - started

        # The actual diagnostic classification this whole feature exists
        # to get right: a genuinely stalled handshake, not a connect
        # failure, not a generic timeout.
        assert excinfo.value.code == TransportErrorCode.PROTOCOL_SYNC_TIMEOUT
        assert transport.internal_state == "error"
        assert transport.is_connected() is False
        assert transport.get_connection_info().state == ConnectionState.ERROR

        # Released at/near the declared 1.5s budget - bounded, with
        # margin for CI/scheduling jitter, but nowhere near an
        # indefinite hang (and nowhere near the real firmware's silence,
        # which never resolves on its own at all).
        assert elapsed < 5.0

        # Real protocol traffic genuinely happened - this was not a
        # connect()-level failure (dns/refused/timeout at the TCP layer)
        # and not a mock: the fake server actually received and parsed a
        # real, client-generated want_config_id over a real socket.
        assert server.accepted_want_config_id is not None

        # TCPTransport's own watchdog thread (and the real TCPInterface's
        # reader thread it triggered) must never be able to block process
        # exit, even though - per the RadioTransport ABC's documented
        # tier-1/tier-2 timeout contract - the underlying library call is
        # merely ABANDONED here, not force-stopped, and keeps running in
        # the background until its own (patched-down) internal timeout
        # gives up. "Released promptly" only promises the CALLER isn't
        # blocked; it does not promise the background thread is gone yet
        # - so the correct, honest assertion is "every thread this
        # produced is a daemon thread", not "no new threads exist".
        threads_after = set(threading.enumerate())
        new_threads = threads_after - threads_before
        assert new_threads, "expected at least the watchdog thread to have been spawned"
        non_daemon = [t for t in new_threads if not t.daemon]
        assert non_daemon == [], (
            f"non-daemon thread(s) left behind after a protocol_sync_timeout, "
            f"would block process/test-suite exit: {[t.name for t in non_daemon]}"
        )
    finally:
        # The abandoned background connect() attempt is still running at
        # this point (real TCPInterface, real reader thread) - give the
        # patched-down internal timeout a moment to finish cleaning up on
        # its own before the fake server goes away, so it doesn't log a
        # connection-reset warning against a listener that already
        # vanished. Not required for this test's own assertions (already
        # complete above); purely to keep the suite's own output/teardown
        # quiet and deterministic.
        time.sleep(3.5)
        server.shutdown()


def test_real_tcpinterface_reader_thread_death_fails_fast_instead_of_waiting_30s():
    """The actual bug investigated and fixed on pixel-111: a reader-thread
    death (real "Connection reset by peer") used to leave the REAL
    library's own MeshInterface._waitConnected() blocked for its full
    hardcoded 30s default (see mesh_interface.py: _disconnected()'s
    isConnected.clear() never wakes a thread already blocked in
    isConnected.wait() - only .set() does, and a failing connect never
    reaches the .set() call in _connected()). This drove the ~35s hangs
    ending in AdapterSupervisor's own SIGKILL, observed live via
    switch()'s own "held the router lock for 35.3s"/"35.4s" log lines
    across three separate reconnect attempts against the same T-Beam.

    tcp_transport.py's _open_interface() now returns a
    _FailFastTCPInterface that polls instead of blocking on one big
    wait, and fails fast the moment the reader thread is confirmed dead.
    This is the required regression guard: without the fix, this test
    would need ~30s (or the patched-down internal default) to complete;
    with the fix, it must complete within ~1s of the reset actually
    landing - proving the override is really in effect against the
    pinned library version, not just present in the source.
    """
    server = FakeMeshtasticTcpServer(complete_handshake=False, reset_after_accept=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)

    threads_before = set(threading.enumerate())

    try:
        started = time.monotonic()
        with pytest.raises(TransportError) as excinfo:
            # A generous outer budget (10s) specifically so this test
            # proves the FAIL-FAST override is what ended the attempt,
            # not TCPTransport's own outer TimeoutEnforced watchdog
            # (tier 1) racing it - if the fix regressed to a no-op
            # (e.g. a future meshtastic version silently changed
            # _waitConnected()'s shape), this budget is wide enough that
            # the elapsed-time assertion below would fail loudly instead
            # of the test accidentally passing for the wrong reason.
            transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)
        elapsed = time.monotonic() - started

        # TCP_CONNECTED: the raw socket connected fine, the Meshtastic
        # protocol layer then raised immediately - exactly the
        # classification _classify_sync_failure() assigns a non-socket,
        # non-OSError exception (MeshInterface.MeshInterfaceError isn't
        # an OSError subtype) raised during SYNCING, distinct from
        # PROTOCOL_SYNC_TIMEOUT (the stalled-handshake test above).
        assert excinfo.value.code == TransportErrorCode.TCP_CONNECTED
        assert transport.internal_state == "error"
        assert transport.is_connected() is False
        assert transport.get_connection_info().state == ConnectionState.ERROR

        # The actual regression guard: real evidence the fail-fast path
        # fired, not the 30s (or any multi-second) library default. Wide
        # margin for CI/scheduling jitter while still being nowhere near
        # the old ~35s hang this fixes.
        assert elapsed < 3.0, (
            f"expected the fail-fast override to end this attempt within ~1s of the "
            f"reset, took {elapsed:.2f}s instead - the override may have silently "
            f"stopped taking effect (e.g. a meshtastic version bump changed "
            f"_waitConnected()'s shape - see _FailFastTCPInterface's own docstring)"
        )

        threads_after = set(threading.enumerate())
        new_threads = threads_after - threads_before
        non_daemon = [t for t in new_threads if not t.daemon]
        assert non_daemon == [], (
            f"non-daemon thread(s) left behind after a reader-thread-death fail-fast, "
            f"would block process/test-suite exit: {[t.name for t in non_daemon]}"
        )
    finally:
        transport.close()
        server.shutdown()


# ---------------------------------------------------------------------------
# TCP lifecycle P0 - real TCPInterface, real socket, counted at the radio's
# end: how many clients did the "radio" actually see, and how many at once.
# The whole point of idempotent connect(): a radio that effectively serves
# one client must never be shown two from MeshCenter.
# ---------------------------------------------------------------------------

def _wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_repeated_connect_to_the_same_endpoint_is_one_real_connection_at_the_radio():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        for _ in range(5):
            info = transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)
            assert info.state == ConnectionState.CONNECTED

        assert server.real_connections == 1
        assert server.max_concurrent_connections == 1
        assert server.active_connections == 1
        time.sleep(0.2)  # let the library's one-time heartbeat write land before close()
    finally:
        transport.close()
        server.shutdown()


def test_switching_endpoints_closes_the_first_radios_session_before_opening_the_second():
    first = FakeMeshtasticTcpServer(complete_handshake=True)
    second = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=first.port)
    try:
        transport.connect(_descriptor("127.0.0.1", first.port), timeout=10.0)
        assert first.active_connections == 1

        info = transport.connect(_descriptor("127.0.0.1", second.port), timeout=10.0)

        assert info.state == ConnectionState.CONNECTED
        assert _wait_until(lambda: first.active_connections == 0), "old session was not closed"
        assert second.real_connections == 1
        assert first.max_concurrent_connections == 1
        assert second.max_concurrent_connections == 1
        time.sleep(0.2)
    finally:
        transport.close()
        first.shutdown()
        second.shutdown()


def test_forced_reconnect_cycles_never_show_the_radio_two_clients_at_once():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)
        for _ in range(3):
            time.sleep(0.2)
            transport.connect(_descriptor("127.0.0.1", server.port), force=True, timeout=10.0)

        assert server.real_connections == 4
        assert server.max_concurrent_connections == 1, (
            "the radio saw more than one MeshCenter client at the same time"
        )
        time.sleep(0.2)
    finally:
        transport.close()
        server.shutdown()


# ---------------------------------------------------------------------------
# One TCP connection per connect() - the root cause of the reconnect storms.
# A throwaway reachability probe (connect + close) right before the real
# handshake made a real T-Beam reset the handshake: measured on pixel-111,
# probe + handshake 1 of 32, handshake alone 26 of 32.
# ---------------------------------------------------------------------------


def test_one_connect_opens_exactly_one_tcp_connection_to_the_radio():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        info = transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)

        assert info.state == ConnectionState.CONNECTED
        assert server.accepted_connections == 1, "no throwaway probe connection"
        assert server.real_connections == 1
        time.sleep(0.2)
    finally:
        transport.close()
        server.shutdown()


def test_every_forced_reconnect_is_also_exactly_one_connection():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        for expected in (1, 2, 3):
            transport.connect(_descriptor("127.0.0.1", server.port), force=True, timeout=10.0)
            time.sleep(0.2)
            assert server.accepted_connections == expected
            assert server.max_concurrent_connections == 1
    finally:
        transport.close()
        server.shutdown()


def test_the_library_adopts_the_socket_we_opened_and_it_is_blocking():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)

        interface = transport._interface
        assert interface.socket is not None
        assert interface.socket.gettimeout() is None, "blocking mode for the reader thread's recv()"
        time.sleep(0.2)
    finally:
        transport.close()
        server.shutdown()


# ---------------------------------------------------------------------------
# Inbound end to end: the REAL library receives a packet from the (fake) radio,
# publishes it through the REAL pypubsub, and it comes out of drain_received()
# as a neutral event - through the interface filter, with the real callback
# signature, and without opening a second TCP connection.
# ---------------------------------------------------------------------------


def _drain_until(transport, count, timeout=10.0):
    deadline = time.monotonic() + timeout
    events, dropped, malformed = [], 0, 0
    while time.monotonic() < deadline and len(events) < count:
        batch = transport.drain_received()
        events.extend(batch.events)
        dropped += batch.dropped
        malformed += batch.malformed
        time.sleep(0.05)
    return events, dropped, malformed


def test_real_library_text_and_waypoint_arrive_as_neutral_events():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)
        time.sleep(0.3)  # let the library's one-time heartbeat settle

        server.send_text("Привет TCP 👋", packet_id=101, channel=1)
        server.send_waypoint(waypoint_id=4242, name="Cafe", description="meet here")
        events, dropped, malformed = _drain_until(transport, 2)

        assert (dropped, malformed) == (0, 0)
        assert [type(e).__name__ for e in events] == ["ReceivedTextEvent", "ReceivedWaypointEvent"]
        text, waypoint = events
        assert text.text == "Привет TCP 👋"  # exact, unicode intact
        assert (text.from_node_id, text.to_node_id, text.packet_id, text.channel_index) == ("!1fa065f0", "^all", 101, 1)
        assert text.local_radio_node_id == "!756f9960"  # the radio the FAKE server says it is
        assert (waypoint.waypoint_id, waypoint.name, waypoint.description) == (4242, "Cafe", "meet here")
        assert (waypoint.latitude, waypoint.longitude) == pytest.approx((50.4501, 30.5234))
        assert waypoint.local_radio_node_id == "!756f9960"

        import json
        from meshsrv import ipc_protocol
        wire = json.dumps(ipc_protocol.received_batch_to_dict(type(transport.drain_received())(events=tuple(events))))
        assert "raw" not in wire and "payload" not in wire

        assert server.accepted_connections == 1, "receiving needs no second TCP connection"
    finally:
        transport.close()
        server.shutdown()


def test_real_library_events_survive_a_reconnect_to_the_same_radio():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        transport.connect(_descriptor("127.0.0.1", server.port), timeout=10.0)
        time.sleep(0.3)
        server.send_text("before the reconnect", packet_id=1)
        # captured but not yet drained
        deadline = time.monotonic() + 10
        while transport.get_receive_stats()["received_text"] < 1 and time.monotonic() < deadline:
            time.sleep(0.05)

        transport.reconnect(timeout=30.0)
        time.sleep(0.3)
        server.send_text("after the reconnect", packet_id=2)
        events, dropped, malformed = _drain_until(transport, 2)

        assert [e.text for e in events] == ["before the reconnect", "after the reconnect"]
        assert (dropped, malformed) == (0, 0)
        assert transport.drain_received().connection_generation == 2
    finally:
        transport.close()
        server.shutdown()


def test_real_library_a_second_transport_in_the_process_does_not_receive_this_ones_packets():
    """`pub` is process-global; two TCPTransport instances (or a Serial/BLE one)
    subscribe to the same topics. Each must only queue its OWN interface's events."""
    server_a = FakeMeshtasticTcpServer(complete_handshake=True, node_num=0x11111111)
    server_b = FakeMeshtasticTcpServer(complete_handshake=True, node_num=0x22222222)
    a = TCPTransport(host="127.0.0.1", port=server_a.port)
    b = TCPTransport(host="127.0.0.1", port=server_b.port)
    try:
        a.connect(_descriptor("127.0.0.1", server_a.port), timeout=10.0)
        b.connect(_descriptor("127.0.0.1", server_b.port), timeout=10.0)
        time.sleep(0.3)

        server_a.send_text("only for A", packet_id=1)
        events_a, _, _ = _drain_until(a, 1)
        time.sleep(0.5)  # give a wrongly-shared subscription time to leak into B

        assert [e.text for e in events_a] == ["only for A"]
        assert events_a[0].local_radio_node_id == "!11111111"
        assert b.drain_received().events == ()
    finally:
        a.close()
        b.close()
        server_a.shutdown()
        server_b.shutdown()


# ---------------------------------------------------------------------------
# The whole chain: the (fake) radio hears a packet -> REAL TCPInterface -> REAL
# pypubsub -> adapter queue -> TransportRouter -> InboundWorker -> the shared
# Core ingest -> messages / chats / waypoints.db. No mocks between the socket and
# the stores.
# ---------------------------------------------------------------------------


def _chain(server_module, monkeypatch, server):
    from meshsrv.radio_transport import ConnectionDescriptor as CD, ConnectionType as CT
    from meshsrv.transport_router import TransportRouter

    srv = server_module
    original = srv.instance_manager.get()
    updated = dict(original)
    updated["radio"] = {
        **dict(original.get("radio") or {}), "node_id": "!756f9960", "transport": "tcp",
        "endpoint": {"host": "127.0.0.1", "port": server.port},
    }
    srv.INSTANCE_IDENTITY = srv.instance_manager.save(updated)
    monkeypatch.setattr(srv, "LOCAL_NODE_ID", "!756f9960")
    monkeypatch.setattr(srv, "RADIO_IDENTITY_RESULT", {"status": "MATCH", "detected": {}, "error": None})

    transport = TCPTransport(host="127.0.0.1", port=server.port)
    router = TransportRouter(transport)
    monkeypatch.setattr(srv, "transport_router", router)
    srv.nodes.clear(); srv.chats.clear(); srv.messages.clear(); srv.seen_ids.clear(); srv.seen_recent_texts.clear()
    srv.waypoint_store.delete_all()
    from meshsrv import inbound_events
    inbound_events.reset_inbound_stats()
    srv.inbound_worker.__init__(
        drain=srv.inbound_worker._drain, eligibility=srv.inbound_worker._eligibility,
        ingest_text=srv.inbound_worker._ingest_text, ingest_waypoint=srv.inbound_worker._ingest_waypoint,
        log=lambda *a, **k: None, log_system_event=lambda **k: None,
    )
    transport.connect(CD(type=CT.TCP, address=f"127.0.0.1:{server.port}"), timeout=10.0)
    time.sleep(0.3)
    return srv, transport, router, original


def _tick_until(srv, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        srv.inbound_worker.tick()
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_chain_real_radio_traffic_ends_up_in_messages_chats_and_waypoints(server_module, monkeypatch):
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    srv, transport, router, original = _chain(server_module, monkeypatch, server)
    mca = []
    monkeypatch.setattr(srv.mca_runtime, "handle_incoming_meshtastic_text",
                        lambda text, node_id, r, **kw: mca.append((text, node_id, kw)))
    try:
        local = 0x756F9960
        server.send_text("Привет TCP 👋", packet_id=101, channel=1)                       # channel text, unicode
        server.send_text("psst, only you", packet_id=102, to_num=local, channel=0)        # direct message
        server.send_text("MCA1:control", packet_id=103, to_num=local, channel=0)          # MCAttach control text
        server.send_text("Привет TCP 👋", packet_id=101, channel=1)                       # the same packet again
        server.send_waypoint(waypoint_id=940001, name="Cafe")
        server.send_waypoint(waypoint_id=940001, name="Bar", latitude=50.5)               # an update, same id

        assert _tick_until(srv, lambda: len(srv.messages) >= 3 and srv.waypoint_store.count() == 1
                           and (srv.waypoint_store.get(940001) or {}).get("name") == "Bar")

        texts = [(m["chat_id"], m["text"]) for m in srv.messages]
        assert ("channel:1", "Привет TCP 👋") in texts, "exact unicode, on the right channel"
        assert ("!1fa065f0", "psst, only you") in texts, "a DM lands in the sender's chat"
        assert texts.count(("channel:1", "Привет TCP 👋")) == 1, "the replayed packet was not stored twice"
        assert srv.chats["channel:1"]["unread"] == 1 and srv.chats["!1fa065f0"]["unread"] >= 2
        assert [c[0] for c in mca] == ["MCA1:control"] and mca[0][1] == "!1fa065f0"
        assert srv.waypoint_store.get(940001)["latitude"] == pytest.approx(50.5), "the update won, still one row"

        assert server.accepted_connections == 1, "no second TCP connection was opened to receive"
        stats = srv.inbound_worker.stats()
        assert stats["malformed_events"] == 0 and stats["overflow_dropped"] == 0 and stats["ingest_errors"] == 0
    finally:
        transport.close()
        server.shutdown()
        srv.waypoint_store.delete_all()
        srv.instance_manager.save(original)
        srv.INSTANCE_IDENTITY = original


def test_chain_keeps_receiving_after_a_reconnect_without_restarting_anything(server_module, monkeypatch):
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    srv, transport, router, original = _chain(server_module, monkeypatch, server)
    try:
        server.send_text("before the reconnect", packet_id=201, channel=1)
        assert _tick_until(srv, lambda: len(srv.messages) == 1)

        router.reconnect(timeout=30.0)
        time.sleep(0.3)
        server.send_text("after the reconnect", packet_id=202, channel=1)

        assert _tick_until(srv, lambda: len(srv.messages) == 2)
        assert [m["text"] for m in srv.messages] == ["before the reconnect", "after the reconnect"]
        assert srv.inbound_worker.stats()["connection_generation"] == 2
        assert server.max_concurrent_connections == 1
    finally:
        transport.close()
        server.shutdown()
        srv.instance_manager.save(original)
        srv.INSTANCE_IDENTITY = original


def test_chain_events_from_a_radio_that_is_not_the_accepted_one_are_never_stored(server_module, monkeypatch):
    """The fake radio identifies as !756f9960; the accepted profile is another node."""
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    srv, transport, router, original = _chain(server_module, monkeypatch, server)
    identity = dict(srv.INSTANCE_IDENTITY)
    identity["radio"] = {**identity["radio"], "node_id": "!0badf00d"}
    monkeypatch.setattr(srv, "INSTANCE_IDENTITY", identity)
    monkeypatch.setattr(srv, "LOCAL_NODE_ID", "!0badf00d")
    try:
        server.send_text("from the wrong radio", packet_id=301, channel=1)
        server.send_waypoint(waypoint_id=940002)

        from meshsrv import inbound_events
        assert _tick_until(srv, lambda: inbound_events.get_inbound_stats()["stale_identity_dropped"] >= 2)

        assert srv.messages == [] and srv.waypoint_store.count() == 0
        assert inbound_events.get_inbound_stats()["stale_identity_dropped"] == 2
    finally:
        transport.close()
        server.shutdown()
        srv.instance_manager.save(original)
        srv.INSTANCE_IDENTITY = original
