"""Verify, against the installed `meshtastic` library, the receive contract the
inbound path (ReceivedTextEvent / ReceivedWaypointEvent) is built on.

Run it inside a venv with the version under test - it is how the pinned range
in adapters/meshtastic/requirements.txt was checked, and how it must be
re-checked when that pin moves:

    python -m venv /tmp/v && /tmp/v/bin/pip install "meshtastic==2.7.9"
    /tmp/v/bin/python adapters/meshtastic/verify_receive_topics.py

A real FromRadio frame (a text message and a waypoint) is pushed through
MeshInterface and the script checks, against what the library really does
rather than what its source appears to do:

  * `meshtastic.receive.text` and `meshtastic.receive.waypoint` fire, exactly
    once each, for the right packet - and `meshtastic.receive.data.TEXT_MESSAGE_APP` /
    `.WAYPOINT_APP` do NOT (the library replaces that topic name for known protocols);
  * the delivered packet dict has the keys the adapter will read
    (`from`, `to`, `id`, `channel`, `rxTime`, ..., `decoded.text`,
    `decoded.waypoint.{id,latitudeI,longitudeI,name,description,expire}`);
  * `fromId` may be None (sender not in the local NodeDB), so the sender id
    must be derived from the numeric `from`;
  * the packet carries protobuf/bytes fields (`raw`, `decoded.payload`,
    `decoded.waypoint.raw`) - the things that must never cross the IPC
    boundary. If a version stops carrying them, that is fine; if it starts
    carrying different ones, the serializer whitelist still holds.

Exit status 0 = the contract holds; non-zero prints what differed.
This file lives under adapters/meshtastic/ (GPLv3 side): Core never imports it.
"""
import json
import sys
import time
from importlib.metadata import version

from pubsub import pub

import meshtastic
from meshtastic import mesh_pb2, portnums_pb2
from meshtastic.mesh_interface import MeshInterface

PACKET_KEYS = {"from", "to", "id", "channel", "decoded", "rxTime", "rxSnr", "rxRssi", "hopLimit", "raw"}
WAYPOINT_KEYS = {"id", "latitudeI", "longitudeI", "name", "description", "expire"}
TOPICS = ("meshtastic.receive.text", "meshtastic.receive.waypoint")
# The library first builds "meshtastic.receive.data.<PORTNUM>" and then REPLACES it
# with "meshtastic.receive.<protocol name>" for a known protocol - so these must
# stay silent for the same two packets (a subscription to them would receive nothing).
REPLACED_TOPICS = ("meshtastic.receive.data.TEXT_MESSAGE_APP", "meshtastic.receive.data.WAYPOINT_APP")

received = {topic: [] for topic in TOPICS + REPLACED_TOPICS}
problems = []


def _listener_for(topic_name):
    def on_receive(packet=None, interface=None, topic=pub.AUTO_TOPIC, **kwargs):
        received[topic_name].append(packet)
    return on_receive


_keep_alive = [_listener_for(topic) for topic in TOPICS + REPLACED_TOPICS]  # pubsub holds weak references
for listener, topic in zip(_keep_alive, TOPICS + REPLACED_TOPICS):
    pub.subscribe(listener, topic)


def _frame(portnum, payload, packet_id):
    from_radio = mesh_pb2.FromRadio()
    packet = from_radio.packet
    packet.id = packet_id
    setattr(packet, "from", 0x1FA065F0)
    packet.to = 0xFFFFFFFF
    packet.channel = 1
    packet.rx_time = int(time.time())
    packet.rx_snr = 5.5
    packet.rx_rssi = -80
    packet.hop_limit = 3
    packet.decoded.portnum = portnum
    packet.decoded.payload = payload
    return from_radio.SerializeToString()


def main() -> int:
    waypoint = mesh_pb2.Waypoint()
    waypoint.id = 4242
    waypoint.latitude_i = int(50.4501 * 1e7)
    waypoint.longitude_i = int(30.5234 * 1e7)
    waypoint.name = "Cafe"
    waypoint.description = "meet here"
    waypoint.expire = int(time.time()) + 3600

    interface = MeshInterface(noProto=True)
    interface.nodesByNum = {}  # empty NodeDB: the sender is unknown, like a first packet after connect
    interface.nodes = {}
    interface._handleFromRadio(_frame(portnums_pb2.PortNum.TEXT_MESSAGE_APP, "hello mesh".encode(), 101))
    interface._handleFromRadio(_frame(portnums_pb2.PortNum.WAYPOINT_APP, waypoint.SerializeToString(), 102))
    time.sleep(1.5)  # the library publishes from its own thread
    interface.close()

    for topic in TOPICS:
        if len(received[topic]) != 1:
            problems.append(f"{topic}: fired {len(received[topic])} times, expected 1")

    for topic in REPLACED_TOPICS:
        if received[topic]:
            problems.append(f"{topic} fired {len(received[topic])} times - it was expected to be replaced")

    text = received[TOPICS[0]][0] if received[TOPICS[0]] else {}
    waypoint_packet = received[TOPICS[1]][0] if received[TOPICS[1]] else {}

    for label, packet in (("text", text), ("waypoint", waypoint_packet)):
        missing = PACKET_KEYS - set(packet)
        if missing:
            problems.append(f"{label} packet lacks keys {sorted(missing)}")
        if packet.get("from") != 0x1FA065F0:
            problems.append(f"{label} packet: numeric 'from' is {packet.get('from')!r}")
    if (text.get("decoded") or {}).get("text") != "hello mesh":
        problems.append(f"decoded.text is {(text.get('decoded') or {}).get('text')!r}")
    decoded_waypoint = (waypoint_packet.get("decoded") or {}).get("waypoint") or {}
    missing = WAYPOINT_KEYS - set(decoded_waypoint)
    if missing:
        problems.append(f"decoded.waypoint lacks keys {sorted(missing)}")
    if decoded_waypoint.get("latitudeI") != int(50.4501 * 1e7):
        problems.append(f"decoded.waypoint.latitudeI is {decoded_waypoint.get('latitudeI')!r}")

    report = {
        "meshtastic": version("meshtastic"),
        "topics_fired": {topic: len(received[topic]) for topic in TOPICS},
        "replaced_topics_fired": {topic: len(received[topic]) for topic in REPLACED_TOPICS},
        "protocol_names": sorted(
            p.name for p in meshtastic.protocols.values() if p.name in ("text", "waypoint")
        ),
        "fromId_when_sender_unknown": text.get("fromId"),
        "toId_broadcast": text.get("toId"),
        "non_plain_fields": sorted(
            [k for k, v in text.items() if isinstance(v, (bytes, bytearray)) or type(v).__module__.startswith("meshtastic")]
            + [f"decoded.{k}" for k, v in (text.get("decoded") or {}).items() if isinstance(v, (bytes, bytearray))]
            + [f"decoded.waypoint.{k}" for k, v in decoded_waypoint.items() if k == "raw"]
        ),
        "problems": problems,
    }
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
