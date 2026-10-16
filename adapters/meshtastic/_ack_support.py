"""Delivery-ACK observation shared by the Serial and TCP transports (ST1).

A DM sent with want_ack is answered by a ROUTING_APP packet whose
`decoded.requestId` is the id of the packet we sent and whose
`decoded.routing.errorReason` is "NONE" (acknowledged) or a Routing.Error name
(a NAK). The library publishes it on pubsub topic `meshtastic.receive.routing`
(`packet["from"]` = the replying node, an int; `fromId` is None for a node the
radio has not told us about, so the number is what is used).

Not every "NONE" means delivery: a relay that rebroadcasts our packet makes the
radio report an *implicit* ack that looks the same but comes from the relay. Only
a reply whose `from` is the destination itself proves the DM arrived; a NAK is a
NAK from whoever sent it.

GPLv3 tree (imports the library's pubsub lazily). Pure helpers below are
dependency-free and unit-tested directly."""
from __future__ import annotations

import threading
import time
from typing import Optional

ROUTING_TOPIC = "meshtastic.receive.routing"

#: how long a serial send keeps its port claim open to catch the ACK (the claim
#: already stopped the listener, so waiting here costs no reception)
SERIAL_ACK_WAIT_S = 12.0


def routing_fields(packet) -> Optional[tuple]:
    """(request_id, error_reason, from_num, to_num, packet_id) from a library routing
    packet dict, or None if it is not a usable routing reply."""
    if not isinstance(packet, dict):
        return None
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return None
    request_id = decoded.get("requestId")
    routing = decoded.get("routing")
    reason = routing.get("errorReason") if isinstance(routing, dict) else None
    from_num = packet.get("from")
    if type(request_id) is not int or request_id <= 0 or type(from_num) is not int:
        return None
    if not isinstance(reason, str) or not reason:
        reason = "NONE"  # protobuf default: the field is omitted when it is NONE
    to_num = packet.get("to") if type(packet.get("to")) is int else None
    packet_id = packet.get("id") if type(packet.get("id")) is int else None
    return request_id, reason, from_num, to_num, packet_id


def destination_num(destination_id) -> Optional[int]:
    text = str(destination_id or "")
    if len(text) == 9 and text.startswith("!"):
        try:
            return int(text[1:], 16)
        except ValueError:
            return None
    return None


def classify_ack(error_reason: str, from_num: int, dest_num: Optional[int]) -> Optional[str]:
    """"delivered" | "nak" | None (implicit ack from a relay - not delivery)."""
    if error_reason != "NONE":
        return "nak"
    if dest_num is not None and from_num == dest_num:
        return "delivered"
    return None


class RoutingAckWaiter:
    """Collects the acknowledgement of the want_ack packets one interface sent.

    Subscribes to the routing topic for its own lifetime, filters on its own
    interface (pubsub is process-global: the same adapter hosts other
    transports), and also remembers replies that arrive before `expect()` is
    called (an ACK can beat the return of sendText by milliseconds)."""

    def __init__(self, interface) -> None:
        self._interface = interface
        self._cond = threading.Condition()
        self._expected: dict[int, Optional[int]] = {}   # packet_id -> destination node number
        self._results: dict[int, tuple] = {}            # packet_id -> (state, error_text)
        self._seen: list[tuple] = []                    # routing replies not yet matched
        self._subscribed = False
        try:
            from pubsub import pub
        except ImportError:  # pragma: no cover - the library itself depends on it
            self._pub = None
            return
        self._pub = pub
        pub.subscribe(self._on_routing, ROUTING_TOPIC)
        self._subscribed = True

    def _on_routing(self, packet, interface=None) -> None:
        if interface is not self._interface:
            return
        fields = routing_fields(packet)
        if fields is None:
            return
        with self._cond:
            self._seen.append(fields)
            self._match_locked()
            self._cond.notify_all()

    def _match_locked(self) -> None:
        for request_id, reason, from_num, _to, _pid in list(self._seen):
            if request_id not in self._expected or request_id in self._results:
                continue
            state = classify_ack(reason, from_num, self._expected[request_id])
            if state is not None:
                self._results[request_id] = (state, None if state == "delivered" else reason)

    def expect(self, packet_id: Optional[int], destination_id) -> None:
        if not isinstance(packet_id, int) or packet_id <= 0:
            return
        with self._cond:
            self._expected[packet_id] = destination_num(destination_id)
            self._match_locked()

    def wait(self, deadline: float) -> dict:
        """Block until every expected packet has an outcome or `deadline`
        (time.monotonic()) passes; returns {packet_id: (state, error_text)}."""
        with self._cond:
            while True:
                pending = [pid for pid in self._expected if pid not in self._results]
                remaining = deadline - time.monotonic()
                if not pending or remaining <= 0:
                    return dict(self._results)
                self._cond.wait(timeout=min(remaining, 0.5))

    def close(self) -> None:
        if self._subscribed and self._pub is not None:
            try:
                self._pub.unsubscribe(self._on_routing, ROUTING_TOPIC)
            except Exception:
                pass
            self._subscribed = False
