"""Verify, against the installed `meshtastic` library, the receive contract the
inbound path (ReceivedTextEvent / ReceivedWaypointEvent / ReceivedNodeInfoEvent /
ReceivedPositionEvent / ReceivedTelemetryEvent) is built on.

Run it inside a venv with the version under test - it is how the pinned range
in adapters/meshtastic/requirements.txt was checked, and how it must be
re-checked when that pin moves:

    python -m venv /tmp/v && /tmp/v/bin/pip install "meshtastic==2.7.9"
    /tmp/v/bin/python adapters/meshtastic/verify_receive_topics.py

Real FromRadio frames (text, waypoint, user/nodeinfo, position, and each of the
three telemetry variants) are pushed through MeshInterface and the script
checks, against what the library really does rather than what its source
appears to do:

  * `meshtastic.receive.{text,waypoint,user,position,telemetry}` each fire,
    exactly once per pushed packet, SEPARATELY (not one aggregated dump the
    way the serial CLI's own --listen output sometimes is) - and
    `meshtastic.receive.data.<PORTNUM>` does NOT fire for any of them (the
    library replaces that topic name for every known protocol);
  * the delivered packet dict has the keys the adapter will read;
  * `fromId` may be None (sender not in the local NodeDB), so the sender id
    must be derived from the numeric `from`;
  * User/Position/Telemetry protobuf enum and bytes fields (role, hwModel,
    macaddr, publicKey) arrive through MessageToDict as plain strings, and a
    proto3 default (role CLIENT, isLicensed False, ...) is OMITTED from the
    dict entirely - a model must not assume a key is always present;
  * Position's `latitude`/`longitude` (plain floats) are already provided by
    the library's own _fixupPosition, alongside the raw latitudeI/longitudeI -
    unlike Waypoint, no manual /1e7 conversion is needed;
  * Telemetry is a oneof: exactly one of deviceMetrics/environmentMetrics/
    powerMetrics is present per packet, as a plain nested dict of numbers;
  * every packet carries protobuf/bytes fields (`raw`, `decoded.payload`) -
    the things that must never cross the IPC boundary. If a version stops
    carrying them, that is fine; if it starts carrying different ones, the
    serializer whitelist still holds.

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
from meshtastic.protobuf import telemetry_pb2

PACKET_KEYS = {"from", "to", "id", "channel", "decoded", "rxTime", "rxSnr", "rxRssi", "hopLimit", "raw"}
WAYPOINT_KEYS = {"id", "latitudeI", "longitudeI", "name", "description", "expire"}
USER_KEYS = {"id", "longName", "shortName", "hwModel", "role", "isLicensed"}
POSITION_KEYS = {"latitudeI", "longitudeI", "latitude", "longitude", "altitude", "time", "groundSpeed", "satsInView"}
TELEMETRY_VARIANT_KEY = {"device": "deviceMetrics", "environment": "environmentMetrics", "power": "powerMetrics"}

TOPICS = (
    "meshtastic.receive.text", "meshtastic.receive.waypoint", "meshtastic.receive.user",
    "meshtastic.receive.position", "meshtastic.receive.telemetry",
)
# The library first builds "meshtastic.receive.data.<PORTNUM>" and then REPLACES it
# with "meshtastic.receive.<protocol name>" for a known protocol - so these must
# stay silent for every packet below (a subscription to them would receive nothing).
REPLACED_TOPICS = tuple(f"meshtastic.receive.data.{name}" for name in (
    "TEXT_MESSAGE_APP", "WAYPOINT_APP", "NODEINFO_APP", "POSITION_APP", "TELEMETRY_APP",
))

received = {topic: [] for topic in TOPICS + REPLACED_TOPICS}
problems = []


def _listener_for(topic_name):
    def on_receive(packet=None, interface=None, topic=pub.AUTO_TOPIC, **kwargs):
        received[topic_name].append(packet)
    return on_receive


_keep_alive = [_listener_for(topic) for topic in TOPICS + REPLACED_TOPICS]  # pubsub holds weak references
for listener, topic in zip(_keep_alive, TOPICS + REPLACED_TOPICS):
    pub.subscribe(listener, topic)


def _frame(portnum, payload, packet_id, *, from_num=0x1FA065F0):
    from_radio = mesh_pb2.FromRadio()
    packet = from_radio.packet
    packet.id = packet_id
    setattr(packet, "from", from_num)
    packet.to = 0xFFFFFFFF
    packet.channel = 1
    packet.rx_time = int(time.time())
    packet.rx_snr = 5.5
    packet.rx_rssi = -80
    packet.hop_limit = 3
    packet.decoded.portnum = portnum
    packet.decoded.payload = payload
    return from_radio.SerializeToString()


def _telemetry(variant: str) -> bytes:
    telemetry = telemetry_pb2.Telemetry()
    telemetry.time = int(time.time())
    if variant == "device":
        telemetry.device_metrics.battery_level = 80
        telemetry.device_metrics.voltage = 3.9
        telemetry.device_metrics.channel_utilization = 1.2
        telemetry.device_metrics.air_util_tx = 0.5
        telemetry.device_metrics.uptime_seconds = 1000
    elif variant == "environment":
        telemetry.environment_metrics.temperature = 21.5
        telemetry.environment_metrics.relative_humidity = 40.0
    else:
        telemetry.power_metrics.ch1_voltage = 5.0
        telemetry.power_metrics.ch1_current = 0.5
    return telemetry.SerializeToString()


def main() -> int:
    waypoint = mesh_pb2.Waypoint()
    waypoint.id = 4242
    waypoint.latitude_i = int(50.4501 * 1e7)
    waypoint.longitude_i = int(30.5234 * 1e7)
    waypoint.name = "Cafe"
    waypoint.description = "meet here"
    waypoint.expire = int(time.time()) + 3600

    user = mesh_pb2.User()
    user.id = "!1fa065f0"
    user.long_name = "Test Node"
    user.short_name = "TST"
    user.hw_model = 9  # RAK4631
    user.role = 2  # ROUTER
    user.is_licensed = True
    user.macaddr = b"\x01\x02\x03\x04\x05\x06"
    user.public_key = b"\x00" * 32

    position = mesh_pb2.Position()
    position.latitude_i = int(50.4501 * 1e7)
    position.longitude_i = int(30.5234 * 1e7)
    position.altitude = 123
    position.time = int(time.time())
    position.ground_speed = 5
    position.sats_in_view = 8

    interface = MeshInterface(noProto=True)
    interface.nodesByNum = {}  # empty NodeDB: the sender is unknown, like a first packet after connect
    interface.nodes = {}
    interface._handleFromRadio(_frame(portnums_pb2.PortNum.TEXT_MESSAGE_APP, "hello mesh".encode(), 101))
    interface._handleFromRadio(_frame(portnums_pb2.PortNum.WAYPOINT_APP, waypoint.SerializeToString(), 102))
    interface._handleFromRadio(_frame(portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString(), 103))
    interface._handleFromRadio(_frame(portnums_pb2.PortNum.POSITION_APP, position.SerializeToString(), 104))
    for offset, variant in enumerate(("device", "environment", "power")):
        interface._handleFromRadio(_frame(portnums_pb2.PortNum.TELEMETRY_APP, _telemetry(variant), 105 + offset))
    time.sleep(1.5)  # the library publishes from its own thread
    interface.close()

    for topic in TOPICS:
        expected = 3 if topic == "meshtastic.receive.telemetry" else 1
        if len(received[topic]) != expected:
            problems.append(f"{topic}: fired {len(received[topic])} times, expected {expected}")

    for topic in REPLACED_TOPICS:
        if received[topic]:
            problems.append(f"{topic} fired {len(received[topic])} times - it was expected to be replaced")

    text = received["meshtastic.receive.text"][0] if received["meshtastic.receive.text"] else {}
    waypoint_packet = received["meshtastic.receive.waypoint"][0] if received["meshtastic.receive.waypoint"] else {}
    user_packet = received["meshtastic.receive.user"][0] if received["meshtastic.receive.user"] else {}
    position_packet = received["meshtastic.receive.position"][0] if received["meshtastic.receive.position"] else {}
    telemetry_packets = received["meshtastic.receive.telemetry"]

    for label, packet in (
        ("text", text), ("waypoint", waypoint_packet), ("user", user_packet), ("position", position_packet),
    ):
        missing = PACKET_KEYS - set(packet)
        if missing:
            problems.append(f"{label} packet lacks keys {sorted(missing)}")
        if packet.get("from") != 0x1FA065F0:
            problems.append(f"{label} packet: numeric 'from' is {packet.get('from')!r}")
    for packet in telemetry_packets:
        if packet.get("from") != 0x1FA065F0:
            problems.append(f"telemetry packet: numeric 'from' is {packet.get('from')!r}")

    if (text.get("decoded") or {}).get("text") != "hello mesh":
        problems.append(f"decoded.text is {(text.get('decoded') or {}).get('text')!r}")

    decoded_waypoint = (waypoint_packet.get("decoded") or {}).get("waypoint") or {}
    missing = WAYPOINT_KEYS - set(decoded_waypoint)
    if missing:
        problems.append(f"decoded.waypoint lacks keys {sorted(missing)}")
    if decoded_waypoint.get("latitudeI") != int(50.4501 * 1e7):
        problems.append(f"decoded.waypoint.latitudeI is {decoded_waypoint.get('latitudeI')!r}")

    decoded_user = (user_packet.get("decoded") or {}).get("user") or {}
    missing = USER_KEYS - set(decoded_user)
    if missing:
        problems.append(f"decoded.user lacks keys {sorted(missing)}")
    if decoded_user.get("id") != "!1fa065f0":
        problems.append(f"decoded.user.id is {decoded_user.get('id')!r}")
    if not isinstance(decoded_user.get("macaddr"), str) or not isinstance(decoded_user.get("publicKey"), str):
        problems.append("decoded.user.macaddr/publicKey are not plain strings (bytes fields via MessageToDict)")
    if not isinstance(decoded_user.get("isLicensed"), bool):
        problems.append(f"decoded.user.isLicensed is not bool: {type(decoded_user.get('isLicensed'))}")
    if decoded_user.get("hwModel") != "RAK4631" or decoded_user.get("role") != "ROUTER":
        problems.append(f"enum fields not decoded to name strings: hwModel={decoded_user.get('hwModel')!r} role={decoded_user.get('role')!r}")
    # A default-valued field (isLicensed=False on an otherwise-bare User) must be
    # OMITTED, not present-as-False - proto3's own MessageToDict default omission.
    bare_user = mesh_pb2.User()
    bare_user.id = "!deadbeef"
    bare_dict = None
    try:
        from google.protobuf.json_format import MessageToDict
        bare_dict = MessageToDict(bare_user)
    except Exception as error:
        problems.append(f"could not probe default-omission directly: {error}")
    if bare_dict is not None and ("isLicensed" in bare_dict or "role" in bare_dict or "hwModel" in bare_dict):
        problems.append(f"a default User does NOT omit its default fields: {bare_dict}")

    decoded_position = (position_packet.get("decoded") or {}).get("position") or {}
    missing = POSITION_KEYS - set(decoded_position)
    if missing:
        problems.append(f"decoded.position lacks keys {sorted(missing)}")
    if decoded_position.get("latitude") != 50.4501 or decoded_position.get("longitude") != 30.5234:
        problems.append(
            f"decoded.position.latitude/longitude are not pre-converted plain floats: "
            f"{decoded_position.get('latitude')!r}, {decoded_position.get('longitude')!r}"
        )

    telemetry_report = []
    for variant, packet in zip(("device", "environment", "power"), telemetry_packets):
        decoded_telemetry = (packet.get("decoded") or {}).get("telemetry") or {}
        variant_key = TELEMETRY_VARIANT_KEY[variant]
        present_variants = [key for key in TELEMETRY_VARIANT_KEY.values() if key in decoded_telemetry]
        if present_variants != [variant_key]:
            problems.append(f"telemetry[{variant}]: expected only {variant_key!r} present, got {present_variants}")
        metrics = decoded_telemetry.get(variant_key) or {}
        if not metrics or not all(isinstance(v, (int, float)) for v in metrics.values()):
            problems.append(f"telemetry[{variant}].{variant_key} is not a flat dict of numbers: {metrics}")
        telemetry_report.append({"variant": variant, "keys": sorted(metrics)})

    non_plain = sorted(
        [k for k, v in text.items() if isinstance(v, (bytes, bytearray)) or type(v).__module__.startswith("meshtastic")]
        + [f"decoded.{k}" for k, v in (text.get("decoded") or {}).items() if isinstance(v, (bytes, bytearray))]
        + [f"decoded.waypoint.{k}" for k, v in decoded_waypoint.items() if k == "raw"]
        + [f"decoded.user.{k}" for k, v in decoded_user.items()
           if isinstance(v, (bytes, bytearray)) or type(v).__module__.startswith("meshtastic")]
        + [f"decoded.position.{k}" for k, v in decoded_position.items() if type(v).__module__.startswith("meshtastic")]
    )

    report = {
        "meshtastic": version("meshtastic"),
        "topics_fired": {topic: len(received[topic]) for topic in TOPICS},
        "replaced_topics_fired": {topic: len(received[topic]) for topic in REPLACED_TOPICS},
        "protocol_names": sorted(
            p.name for p in meshtastic.protocols.values()
            if p.name in ("text", "waypoint", "user", "position", "telemetry")
        ),
        "fromId_when_sender_unknown": text.get("fromId"),
        "toId_broadcast": text.get("toId"),
        "non_plain_fields": non_plain,
        "telemetry_variants": telemetry_report,
        "problems": problems,
    }
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
