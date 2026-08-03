"""One-shot Meshtastic waypoint sender used while the main listener is paused."""

from __future__ import annotations

import json
import secrets
import sys
import time

from meshtastic.serial_interface import SerialInterface


def _waypoint_id() -> int:
    # Meshtastic waypoint IDs are independent from packet IDs and fit in the
    # same practical range used by the official clients.
    return secrets.randbelow(1_000_000_000 - 1) + 1


def main() -> int:
    payload = json.loads(sys.stdin.read())
    waypoint_id = int(payload.get("waypoint_id") or _waypoint_id())
    post_notification = bool(payload.get("post_notification", True))

    interface = SerialInterface(devPath=payload["port"])
    try:
        waypoint_packet = interface.sendWaypoint(
            name=payload["name"],
            description=payload.get("description", ""),
            icon=int(payload.get("icon", 128205)),
            expire=int(payload["expire_at"]),
            waypoint_id=waypoint_id,
            latitude=float(payload["latitude"]),
            longitude=float(payload["longitude"]),
            channelIndex=int(payload.get("channel_index", 0)),
            wantAck=True,
            wantResponse=False,
        )

        notification_packet_id = None
        if post_notification:
            text = str(payload.get("notification_text") or "").strip()
            if text:
                notification_packet = interface.sendText(
                    text=text,
                    destinationId="^all",
                    channelIndex=int(payload.get("channel_index", 0)),
                    wantAck=False,
                    wantResponse=False,
                )
                notification_packet_id = int(notification_packet.id)

        time.sleep(1.5)
        print(json.dumps({
            "ok": True,
            "waypoint_id": waypoint_id,
            "waypoint_packet_id": int(waypoint_packet.id),
            "notification_packet_id": notification_packet_id,
        }))
        return 0
    finally:
        interface.close()


if __name__ == "__main__":
    raise SystemExit(main())
