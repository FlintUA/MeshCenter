"""Fake `ffmpeg` for the network camera stream tests (CAM2 Phase 1b).

Stands in for the real ffmpeg the RTSP driver starts: reads the ffconcat
script from stdin (like the real one) and writes fragmented MP4 to stdout from
tests/fixtures/tiny_h264_frag.mp4 (H.264) or tiny_hevc_frag.mp4, in the same
"init segment then moof+mdat fragments, in arbitrary pipe chunks" shape.
Driven by environment variables only.

FAKE_FMP4_BEHAVIOR (default "normal"):
  normal   - init, then the fragments in a loop until killed
  hevc     - same but with the H.265 fixture
  auth401  - print an RTSP 401 error on stderr, exit 1, no output
  notfound - print a 404 error on stderr, exit 1
  refused  - print "Connection refused" on stderr, exit 1
  exit0    - init + FAKE_FMP4_FRAGMENTS fragments, then exit 0 unprompted (lost camera)
  stall    - init + 2 fragments, then block forever
  garbage  - write bytes that are not a valid MP4 box
FAKE_FMP4_INTERVAL: seconds between fragments (default 0.03)
FAKE_FMP4_CHUNK: stdout write size in bytes (default 41, deliberately not box aligned)
FAKE_FMP4_STDIN_DUMP: if set, the received script is written to this file
FAKE_FMP4_ARGV_DUMP: if set, argv is written to this file
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
behavior = os.environ.get("FAKE_FMP4_BEHAVIOR", "normal")
interval = float(os.environ.get("FAKE_FMP4_INTERVAL", "0.03"))
chunk = int(os.environ.get("FAKE_FMP4_CHUNK", "41"))

if os.environ.get("FAKE_FMP4_ARGV_DUMP"):
    with open(os.environ["FAKE_FMP4_ARGV_DUMP"], "w") as handle:
        handle.write("\n".join(sys.argv))

script = sys.stdin.buffer.read()
if os.environ.get("FAKE_FMP4_STDIN_DUMP"):
    with open(os.environ["FAKE_FMP4_STDIN_DUMP"], "wb") as handle:
        handle.write(script)

if behavior == "auth401":
    sys.stderr.write("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized\n")
    sys.exit(1)
if behavior == "notfound":
    sys.stderr.write("[rtsp @ 0x1] method DESCRIBE failed: 404 Not Found\n")
    sys.exit(1)
if behavior == "refused":
    sys.stderr.write("[tcp @ 0x1] Connection to tcp://192.168.2.50:554 failed: Connection refused\n")
    sys.exit(1)

data = open(os.path.join(HERE, "tiny_hevc_frag.mp4" if behavior == "hevc" else "tiny_h264_frag.mp4"), "rb").read()


def split(blob):
    boxes, pos = [], 0
    while pos + 8 <= len(blob):
        size = int.from_bytes(blob[pos:pos + 4], "big")
        boxes.append((blob[pos + 4:pos + 8], blob[pos:pos + size]))
        pos += size
    return boxes


boxes = split(data)
init = b"".join(b for k, b in boxes if k in (b"ftyp", b"moov"))
fragments, pending = [], b""
for kind, box in boxes:
    if kind in (b"moof", b"mdat"):
        pending += box
        if kind == b"mdat":
            fragments.append(pending)
            pending = b""

out = sys.stdout.buffer


def write(blob):
    for i in range(0, len(blob), chunk):
        out.write(blob[i:i + chunk])
        out.flush()


if behavior == "garbage":
    write(b"\x00\x00\x00\x00this is not an mp4" * 4)
    time.sleep(0.2)
    sys.exit(0)

write(init)
if behavior == "stall":
    write(fragments[0])
    write(fragments[1])
    time.sleep(3600)

limit = int(os.environ.get("FAKE_FMP4_FRAGMENTS", "5")) if behavior == "exit0" else None
sent = 0
while True:
    for fragment in fragments:
        write(fragment)
        sent += 1
        if limit is not None and sent >= limit:
            sys.exit(0)
        time.sleep(interval)
