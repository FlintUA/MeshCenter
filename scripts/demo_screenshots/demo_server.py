"""Demo server for the README screenshots: the real MeshCenter app on a throwaway data directory, filled with FAKE data only.

Nothing here comes from a real installation: the node names, ids and positions are invented (the positions sit around a
well-known public landmark), there are no keys, callsigns or addresses, and the "camera" shows a generated landscape
(drawn with Pillow, no photograph). Run it, then point scripts/demo_screenshots/capture.py at it.

    python scripts/demo_screenshots/demo_server.py          # http://127.0.0.1:8795

It borrows the sandbox helpers of the test suite (tests/conftest.py: a synthetic config, a fake CLI and serial port), so
it needs the repository checkout and the packages of requirements-dev.txt. It never touches a radio or a real data folder.
"""
import io
import math
import os
import pathlib
import random
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import conftest  # noqa: E402  (the test suite's sandbox helpers)

PORT = int(os.environ.get("PORT", "8795"))
LOCAL_ID = "!0000d000"
LOCAL_NAME = "Demo Base"
CENTER = (52.5163, 13.3777)  # a public landmark; everything else is an offset from it

sandbox = pathlib.Path(tempfile.mkdtemp(prefix="mcdemo-"))
config_dir = sandbox / "config_pkg"
config_dir.mkdir()
data_dir = sandbox / "data"
data_dir.mkdir()
fake_cli = sandbox / "fake_meshtastic"
fake_cli.write_text("#!/bin/sh\necho fake\n")
fake_port = sandbox / "fake_port"
fake_port.write_text("")
conftest._write_fake_config(config_dir, data_dir, fake_cli, fake_port)
config_file = config_dir / "config.py"
config_file.write_text(
    config_file.read_text(encoding="utf-8")
    .replace("!aabbccdd", LOCAL_ID).replace("Test Local Node", LOCAL_NAME).replace("test-instance", "demo"),
    encoding="utf-8",
)
conftest._stub_libcamera()
sys.path.insert(0, str(config_dir))
sys.modules.pop("config", None)
os.chdir(REPO)

import server as srv  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402
from camera.camera_driver import CameraDriver  # noqa: E402
from camera.camera_manager import CameraManager  # noqa: E402


def landscape(width=1280, height=720, shift=0):
    """A generated scene: dusk sky, a low sun, layered hills and a lake. Deterministic, no photograph."""
    rng = random.Random(7)
    image = Image.new("RGB", (width, height))
    pixels = ImageDraw.Draw(image)
    horizon = int(height * 0.62)
    for y in range(horizon):
        t = y / horizon
        pixels.line([(0, y), (width, y)], fill=(int(40 + 190 * t), int(70 + 110 * t), int(130 + 40 * t - 60 * t * t)))
    sun_x, sun_y = int(width * (0.68 + shift)), int(horizon - height * 0.06)
    glow = Image.new("RGB", (width, height), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([sun_x - 120, sun_y - 120, sun_x + 120, sun_y + 120], fill=(255, 190, 110))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    image = Image.composite(Image.new("RGB", (width, height), (255, 205, 130)), image, glow.convert("L").point(lambda v: min(255, v)))
    draw = ImageDraw.Draw(image)
    draw.ellipse([sun_x - 34, sun_y - 34, sun_x + 34, sun_y + 34], fill=(255, 238, 190))
    for layer, (base, amp, color) in enumerate(((0.55, 0.07, (70, 92, 118)), (0.60, 0.06, (52, 78, 92)), (0.66, 0.05, (36, 62, 64)))):
        phase = rng.random() * 6
        points = [(0, height)]
        for x in range(0, width + 8, 8):
            y = height * base + height * amp * (math.sin(x / 170 + phase) + 0.5 * math.sin(x / 61 + phase * 2))
            points.append((x, y))
        points.append((width, height))
        draw.polygon(points, fill=color)
    for y in range(int(height * 0.74), height):
        t = (y - height * 0.74) / (height * 0.26)
        draw.line([(0, y), (width, y)], fill=(int(40 - 20 * t), int(70 - 25 * t), int(88 - 30 * t)))
    for _ in range(26):
        x = rng.randint(0, width)
        y = rng.randint(int(height * 0.76), height - 4)
        draw.line([(x, y), (x + rng.randint(30, 140), y)], fill=(255, 215, 160), width=1)
    return image.filter(ImageFilter.GaussianBlur(0.6))


class DemoCamera(CameraDriver):
    def __init__(self, ident, name):
        self.id, self.device_type, self.display_name, self.running = ident, "camera", name, False
        buffer = io.BytesIO()
        landscape().save(buffer, "JPEG", quality=88)
        self._jpeg = buffer.getvalue()

    def detect(self):
        return {"model": self.display_name}

    def start(self, **options):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def get_status(self):
        return {"model": self.display_name, "running": self.running, "resolution": "1280x720"}

    def stream_mjpeg(self):
        self.running = True
        while self.running:
            yield self._jpeg
            time.sleep(0.2)

    def capture_photo(self, resolution=None):
        return self._jpeg

    def list_resolutions(self):
        return ["640x480", "1280x720"]


srv.camera_manager_state["manager"] = CameraManager({"usb:demo": DemoCamera("usb:demo", "Demo USB Camera")}, active_id="usb:demo")

# ---- nodes: invented names and ids, positions as small offsets around CENTER
now = time.time()
NODES = [
    # id, name, short, hardware, role, d_lat, d_lon, seconds since heard, favourite, hops, snr, rssi
    ("!0000d001", "Demo Relay North", "RLYN", "RAK4631", "ROUTER", 0.0210, 0.0040, 180, True, 0, 7.5, -78),
    ("!0000d002", "Demo Tower East", "TWRE", "HELTEC_V3", "ROUTER", 0.0020, 0.0310, 420, True, 1, 5.0, -96),
    ("!0000d003", "Demo Walker", "WLKR", "TRACKER_T1000_E", "CLIENT", -0.0090, -0.0120, 90, False, 0, 9.0, -70),
    ("!0000d004", "Demo Balcony", "BLCN", "T_ECHO", "CLIENT", 0.0040, -0.0060, 600, True, 0, 8.5, -74),
    ("!0000d005", "Demo Garden Sensor", "GRDN", "RAK3312", "SENSOR", -0.0150, 0.0100, 1500, False, 2, 2.0, -108),
    ("!0000d006", "Demo Hill Repeater", "HILL", "STATION_G2", "ROUTER", 0.0300, -0.0200, 2700, False, 2, 1.0, -112),
    ("!0000d007", "Demo Cyclist", "CYCL", "TBEAM", "CLIENT", -0.0030, 0.0190, 5400, False, 1, 4.0, -101),
    ("!0000d008", "Demo Cabin", "CABN", "HELTEC_MESH_POCKET", "CLIENT", -0.0280, -0.0150, 9000, False, 3, -2.0, -117),
]
srv.nodes[LOCAL_ID] = {
    "node_id": LOCAL_ID, "name": LOCAL_NAME, "short_name": "BASE", "hw_model": "RAK4631", "role": "CLIENT", "last_seen": now,
    "last_time": srv.now(), "ignored": False, "favorite": True,
    "position": {"latitude": CENTER[0], "longitude": CENTER[1], "altitude": 40},
}
for nid, name, short, hw, role, d_lat, d_lon, age, favorite, hops, snr, rssi in NODES:
    srv.nodes[nid] = {
        "node_id": nid, "name": name, "short_name": short, "hw_model": hw, "role": role, "last_seen": now - age,
        "last_time": srv.now(), "rssi": str(rssi), "snr": str(snr), "hop_start": 3, "hops_away": hops, "relay_node": "",
        "last_text": "", "ignored": False, "favorite": favorite, "last_direct": now - age if hops == 0 else None,
        "position": {"latitude": round(CENTER[0] + d_lat, 5), "longitude": round(CENTER[1] + d_lon, 5), "altitude": 35 + hops * 20},
    }
srv.save_nodes()

# ---- conversations (invented, friendly, English)
CH = srv.CHANNEL_CHAT_ID
clock = [now - 7200]


def message(kind, sender, text, node_id, chat_id, packet_id, rx=None):
    clock[0] += 240
    stored = srv.add_message(kind, sender, text, node_id=node_id, chat_id=chat_id, packet_id=packet_id, rx=rx)
    if stored is not None:
        stored["ts"] = clock[0]
        stored["time"] = time.strftime("%H:%M:%S", time.localtime(clock[0]))


message("rx", "Demo Relay North", "Good morning, mesh! Relay on the roof is up and running.", "!0000d001", CH, 5001,
        {"snr": 7.5, "rssi": -78, "hop_start": 3, "hop_limit": 3, "relay_node": 1, "relay": {"status": "direct", "node_id": "!0000d001", "short_name": "RLYN", "basis": "direct"}})
message("rx", "Demo Walker", "Walking along the river, signal is strong here.", "!0000d003", CH, 5002,
        {"snr": 9.0, "rssi": -70, "hop_start": 3, "hop_limit": 3})
message("me", "Me", "Nice! Can you see the tower to the east?", srv.LOCAL_NODE_ID, CH, 5003)
message("rx", "Demo Tower East", "Yes, the tower is reachable over one hop. Battery 87%.", "!0000d002", CH, 5004,
        {"snr": 5.0, "rssi": -96, "hop_start": 3, "hop_limit": 2, "relay_node": 1, "relay": {"status": "resolved", "node_id": "!0000d001", "short_name": "RLYN", "basis": "direct24h"}})
message("rx", "Demo Cyclist", "Heading to the hill repeater, will report back.", "!0000d007", CH, 5005,
        {"snr": 4.0, "rssi": -101, "hop_start": 3, "hop_limit": 2})
message("me", "Me", "Rx 10:02:11\nSNR 5 dB | RSSI -96 dBm\nHops 1\nVia RLYN", srv.LOCAL_NODE_ID, CH, 5006)
for i, (kind, text) in enumerate((("rx", "Hi! Is the garden sensor online?"), ("me", "Yes, it reports every 30 minutes."),
                                   ("rx", "Great, thanks. Temperature looks good."), ("me", "18.4 C and 61% humidity right now."))):
    message(kind, "Demo Balcony" if kind == "rx" else "Me", text, "!0000d004" if kind == "rx" else srv.LOCAL_NODE_ID, "!0000d004", 5100 + i,
            {"snr": 8.5, "rssi": -74, "hop_start": 3, "hop_limit": 3} if kind == "rx" else None)
message("rx", "Demo Cabin", "Cabin is back online after the storm.", "!0000d008", "!0000d008", 5200, {"snr": -2.0, "rssi": -117, "hop_start": 3, "hop_limit": 0})
srv.save_messages()
srv.save_chats()
# ---- MCAttach (Files tab): start the service on the throwaway data directory so the tab shows its real empty state
try:
    from meshsrv.attachments import mca_runtime  # noqa: E402

    mca_runtime.start_attachments_service(srv.DATA_DIR, srv.transport_router, 1)
except Exception as error:  # the Files tab then says "not ready"; every other view is unaffected
    print("DEMO: MCAttach service not started:", type(error).__name__, error, flush=True)

# ---- a calm radio status: the sandbox has no listener, so keep the stored health at "OK" and never "busy"
import threading  # noqa: E402

srv.listener_supervisor.busy_reason = lambda *args, **kwargs: ""


def keep_status_ok():
    while True:
        with srv.state_lock:
            srv.radio_health.update(status="OK", level="OK", listener_running=True, last_packet=time.time() - 5,
                                    status_reason="Recent radio activity detected")
        time.sleep(3)


threading.Thread(target=keep_status_ok, daemon=True).start()
print("DEMO data in", data_dir, flush=True)
srv.app.run("127.0.0.1", PORT, threaded=True, use_reloader=False)
