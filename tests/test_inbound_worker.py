"""meshsrv/inbound_worker.py - the poller between the adapter's receive queue and
the shared Core ingest. Pure logic here (fake drain / fake ingest / fake clock);
the server wiring and the real-library chain are in test_inbound_worker_server.py
and test_tcp_transport_integration.py.
"""
import json

import pytest

from meshsrv import inbound_worker as iw
from meshsrv.inbound_worker import InboundWorker
from meshsrv.radio_transport import (
    ReceivedBatch,
    ReceivedNodeInfoEvent,
    ReceivedPositionEvent,
    ReceivedTelemetryEvent,
    ReceivedTextEvent,
    ReceivedWaypointEvent,
    TransportError,
    TransportErrorCode,
)

LOCAL = "!756f9960"


def text_event(n=1, text="hello"):
    return ReceivedTextEvent(
        from_node_id="!1fa065f0", to_node_id="^all", text=text, received_at=1.0,
        local_radio_node_id=LOCAL, packet_id=n,
    )


def waypoint_event(n=1):
    return ReceivedWaypointEvent(
        waypoint_id=n, sender_id="!1fa065f0", name="Cafe", description="", received_at=1.0,
        local_radio_node_id=LOCAL, latitude=50.0, longitude=30.0,
    )


def nodeinfo_event(n=1):
    return ReceivedNodeInfoEvent(
        node_id="!1fa065f0", sender_id="!1fa065f0", received_at=1.0,
        local_radio_node_id=LOCAL, packet_id=n, long_name="Test Node",
    )


def position_event(n=1):
    return ReceivedPositionEvent(
        sender_id="!1fa065f0", received_at=1.0, local_radio_node_id=LOCAL,
        packet_id=n, latitude=50.0, longitude=30.0,
    )


def telemetry_event(n=1):
    return ReceivedTelemetryEvent(
        sender_id="!1fa065f0", kind="device", metrics={"batteryLevel": 80}, received_at=1.0,
        local_radio_node_id=LOCAL, packet_id=n,
    )


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, eligibility=None, drains=None):
        self.clock = Clock()
        self.logs = []
        self.system_events = []
        self.drain_calls = []
        self.texts, self.waypoints = [], []
        self.nodeinfos, self.positions, self.telemetries = [], [], []
        self.drains = list(drains or [])
        self.eligibility_value = eligibility
        self.worker = InboundWorker(
            drain=self._drain,
            eligibility=lambda: self.eligibility_value,
            ingest_text=self.texts.append,
            ingest_waypoint=self.waypoints.append,
            ingest_nodeinfo=self.nodeinfos.append,
            ingest_position=self.positions.append,
            ingest_telemetry=self.telemetries.append,
            log=lambda message, **kw: self.logs.append(message),
            log_system_event=lambda **kw: self.system_events.append(kw),
            clock=self.clock,
            sleep=lambda s: None,
        )

    def _drain(self, **kwargs):
        self.drain_calls.append(kwargs)
        if not self.drains:
            return ReceivedBatch()
        item = self.drains.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def error(code, message="x"):
    return TransportError(code, message)


# --- eligibility -----------------------------------------------------------


@pytest.mark.parametrize("reason", ["not_tcp", "identity_mismatch", "identity_not_found",
                                    "identity_detection_error", "router_not_tcp"])
def test_nothing_is_drained_or_ingested_unless_eligible(reason):
    h = Harness(eligibility=reason, drains=[ReceivedBatch(events=(text_event(),))])

    assert h.worker.tick() == "idle"

    assert h.drain_calls == [] and h.texts == []
    stats = h.worker.stats()
    assert stats["status"] == "idle" and stats["waiting_reason"] == reason and stats["drained_events"] == 0


def test_eligible_drains_and_routes_each_event_to_its_ingest():
    h = Harness(drains=[ReceivedBatch(events=(text_event(1), waypoint_event(2), text_event(3)), connection_generation=4)])

    assert h.worker.tick() == "ingesting"

    assert [e.packet_id for e in h.texts] == [1, 3] and [e.waypoint_id for e in h.waypoints] == [2]
    stats = h.worker.stats()
    assert (stats["drained_events"], stats["text_events"], stats["waypoint_events"]) == (3, 2, 1)
    assert stats["connection_generation"] == 4 and stats["status"] == "ingesting"


def test_nodeinfo_position_and_telemetry_route_to_their_own_ingest():
    h = Harness(drains=[ReceivedBatch(events=(nodeinfo_event(1), position_event(2), telemetry_event(3)))])

    assert h.worker.tick() == "ingesting"

    assert [e.packet_id for e in h.nodeinfos] == [1]
    assert [e.packet_id for e in h.positions] == [2]
    assert [e.packet_id for e in h.telemetries] == [3]
    stats = h.worker.stats()
    assert (stats["nodeinfo_events"], stats["position_events"], stats["telemetry_events"]) == (1, 1, 1)
    assert stats["malformed_events"] == 0


def test_a_broken_eligibility_check_never_takes_the_worker_down():
    h = Harness()
    h.worker._eligibility = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    assert h.worker.tick() == "waiting"
    assert h.worker.stats()["waiting_reason"] == "eligibility_error"


# --- waiting quietly ------------------------------------------------------------


@pytest.mark.parametrize("code", [
    TransportErrorCode.BUSY, TransportErrorCode.NOT_CONNECTED, TransportErrorCode.ADAPTER_UNAVAILABLE,
    TransportErrorCode.TIMEOUT, TransportErrorCode.UNSUPPORTED,
])
def test_expected_link_states_wait_without_an_error_every_second(code):
    h = Harness(drains=[error(code)] * 400)

    for _ in range(300):  # five minutes of 1s ticks
        assert h.worker.tick() == "waiting"
        h.clock.now += 1.0

    assert h.worker.stats()["waiting_reason"] == code.value
    assert h.worker.stats()["soft_errors"] == 300
    assert len(h.logs) <= 2, f"a 5 minute outage produced {len(h.logs)} log lines: {h.logs}"
    assert all("ERROR" not in line.upper() for line in h.logs)
    assert h.system_events == []


def test_it_resumes_when_the_link_is_back():
    h = Harness(drains=[error(TransportErrorCode.BUSY), error(TransportErrorCode.NOT_CONNECTED),
                        ReceivedBatch(events=(text_event(7),))])

    assert [h.worker.tick() for _ in range(3)] == ["waiting", "waiting", "ingesting"]
    assert [e.packet_id for e in h.texts] == [7]


def test_an_unexpected_transport_error_is_reported_but_rate_limited():
    h = Harness(drains=[error(TransportErrorCode.UNKNOWN)] * 200)

    for _ in range(150):
        h.worker.tick()
        h.clock.now += 1.0

    assert 1 <= len(h.logs) <= 3, h.logs
    assert "unknown" in h.logs[0]


def test_a_non_transport_exception_from_drain_is_contained():
    h = Harness(drains=[RuntimeError("secret message text that must not be logged"), ReceivedBatch(events=(text_event(),))])

    assert h.worker.tick() == "waiting"
    assert h.worker.tick() == "ingesting"
    assert h.texts and not any("secret" in line for line in h.logs)


# --- batches -----------------------------------------------------------------


def test_a_full_batch_is_followed_by_another_in_the_same_tick():
    full = ReceivedBatch(events=tuple(text_event(i) for i in range(iw.DRAIN_LIMIT)))
    rest = ReceivedBatch(events=(text_event(999),))
    h = Harness(drains=[full, rest])

    h.worker.tick()

    assert len(h.drain_calls) == 2 and len(h.texts) == iw.DRAIN_LIMIT + 1
    assert all(call == {"limit": iw.DRAIN_LIMIT, "timeout": iw.DRAIN_TIMEOUT_S} for call in h.drain_calls)


def test_a_short_batch_stops_the_tick():
    h = Harness(drains=[ReceivedBatch(events=(text_event(),)), ReceivedBatch(events=(text_event(2),))])

    h.worker.tick()

    assert len(h.drain_calls) == 1


def test_a_permanently_full_queue_cannot_starve_the_loop():
    full = ReceivedBatch(events=tuple(text_event(i) for i in range(iw.DRAIN_LIMIT)))
    h = Harness(drains=[full] * 50)

    h.worker.tick()

    assert len(h.drain_calls) == iw.MAX_BATCHES_PER_TICK


# --- one bad event, overflow ---------------------------------------------------


def test_one_event_that_cannot_be_ingested_costs_only_that_event():
    h = Harness(drains=[ReceivedBatch(events=(text_event(1), text_event(2, "PRIVATE words"), text_event(3)))])
    ingested = []

    def picky(event):
        if event.packet_id == 2:
            raise ValueError(f"bad event carrying {event.text}")
        ingested.append(event.packet_id)

    h.worker._ingest_text = picky
    h.worker.tick()

    assert ingested == [1, 3]
    stats = h.worker.stats()
    assert stats["ingest_errors"] == 1 and stats["drained_events"] == 3
    assert h.logs and "ValueError" in h.logs[0]
    assert not any("PRIVATE" in line for line in h.logs), "an exception message can hold text; only its type is logged"


def test_an_object_that_is_not_an_event_is_counted_and_skipped():
    h = Harness()
    h.worker._handle_batch(types_batch(("junk", text_event(5))))

    assert [e.packet_id for e in h.texts] == [5] and h.worker.stats()["malformed_events"] == 1


def types_batch(events):
    """A batch object that bypasses ReceivedBatch's own validation."""
    import types
    return types.SimpleNamespace(events=events, dropped=0, malformed=0, connection_generation=None)


def test_adapter_side_malformed_and_overflow_are_counted_and_the_overflow_is_reported_once():
    h = Harness(drains=[
        ReceivedBatch(events=(text_event(1),), dropped=44, malformed=2),
        ReceivedBatch(events=(text_event(2),), dropped=10, malformed=1),
    ])

    h.worker.tick()
    h.worker.tick()

    stats = h.worker.stats()
    assert (stats["overflow_dropped"], stats["malformed_events"]) == (54, 3)
    assert len(h.system_events) == 1, "rate limited: one WARNING per minute, not per batch"
    assert h.system_events[0]["level"] == "WARNING" and "44" in h.system_events[0]["details"]

    h.clock.now += 61
    h.drains.append(ReceivedBatch(dropped=1))
    h.worker.tick()
    assert len(h.system_events) == 2


def test_statistics_never_contain_message_text():
    h = Harness(drains=[ReceivedBatch(events=(text_event(1, "a very private sentence"),))])

    h.worker.tick()

    wire = json.dumps(h.worker.stats())
    assert "private" not in wire
    assert set(h.worker.stats()) >= {
        "ticks", "drains", "drained_events", "text_events", "waypoint_events",
        "nodeinfo_events", "position_events", "telemetry_events", "ingest_errors",
        "malformed_events", "overflow_dropped", "discarded_on_identity_refusal", "soft_errors",
        "status", "waiting_reason", "last_drain_age_s", "connection_generation",
    }


def test_last_drain_age_is_reported():
    h = Harness(drains=[ReceivedBatch()])
    assert h.worker.stats()["last_drain_age_s"] is None

    h.worker.tick()
    h.clock.now += 7.0

    assert h.worker.stats()["last_drain_age_s"] == 7.0


# --- discarding on an identity refusal (section 46) ------------------------------


def test_discard_pending_empties_the_queue_without_ingesting():
    h = Harness()
    batches = [ReceivedBatch(events=tuple(text_event(i) for i in range(iw.DRAIN_LIMIT))),
               ReceivedBatch(events=tuple(text_event(i) for i in range(30)))]

    discarded = h.worker.discard_pending(lambda **kw: batches.pop(0))

    assert discarded == iw.DRAIN_LIMIT + 30
    assert h.texts == [] and h.waypoints == []
    assert h.worker.stats()["discarded_on_identity_refusal"] == iw.DRAIN_LIMIT + 30


def test_discard_pending_tolerates_an_unreachable_adapter():
    h = Harness()

    def unreachable(**kw):
        raise error(TransportErrorCode.ADAPTER_UNAVAILABLE)

    assert h.worker.discard_pending(unreachable) == 0
    assert h.worker.discard_pending(lambda **kw: (_ for _ in ()).throw(AttributeError("no drain_received"))) == 0


def test_discard_pending_is_bounded():
    h = Harness()
    calls = []

    def endless(**kw):
        calls.append(1)
        return ReceivedBatch(events=tuple(text_event(i) for i in range(iw.DRAIN_LIMIT)))

    h.worker.discard_pending(endless, max_batches=3)

    assert len(calls) == 3


# --- pacing ----------------------------------------------------------------------


def test_run_forever_polls_fast_while_active_and_slowly_when_idle():
    h = Harness()
    slept = []

    class Stop(Exception):
        pass

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 4:
            raise Stop

    h.worker._sleep = sleep
    states = iter(["ingesting", "waiting", "idle", "idle"])
    h.worker.tick = lambda: next(states)

    with pytest.raises(Stop):
        h.worker.run_forever()

    assert slept == [iw.POLL_INTERVAL_S, iw.POLL_INTERVAL_S, iw.IDLE_INTERVAL_S, iw.IDLE_INTERVAL_S]
    assert iw.POLL_INTERVAL_S <= 1.0
