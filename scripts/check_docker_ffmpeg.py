"""Gate for the minimal LGPL ffmpeg in the Docker image (CAM2 Phase 4).

    python scripts/check_docker_ffmpeg.py [image]          # default image: meshcenter:ffmpeg-gate

Runs the image's own ffmpeg with the EXACT argument lists the camera code builds (camera/rtsp_input.py,
camera/frame_grab.py, camera/mjpeg_fallback.py), against the stdlib fake RTSP camera on this machine (Basic auth), and
fails when any of these is not true:

  1. `ffmpeg -L` says LGPL, `-buildconf` has no --enable-gpl / --enable-nonfree / --enable-version3, every library
     resolves (ldd), the version is the pinned one.
  2. The live stream argv (concat script on stdin, -c copy, fragmented mp4 on stdout) produces an init segment that
     camera/fmp4.py reads as H.264 320x180, followed by media fragments.
  3. The one-shot frame grab turns that stream into a JPEG.
  4. The MJPEG fallback transcode turns it into at least two JPEG frames.
  5. An H.265 file still goes through `-c copy` into mp4 (so the code can classify it instead of ffmpeg failing blind).
  6. The ffmpeg part of the image (/opt/ffmpeg plus /usr/share/doc/meshcenter-ffmpeg) is at most 60 MB.

Needs Docker and a free local TCP port; the image only has to contain /usr/local/bin/ffmpeg (the full MeshCenter
image, or the Dockerfile's `ffmpeg-build` stage plus a symlink). Exit code 0 = gate passed.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

from camera import fmp4, frame_grab, mjpeg_fallback, rtsp_input  # noqa: E402
from fake_rtsp_server import FakeRtspServer  # noqa: E402

PINNED_VERSION = "7.1.5"
MAX_FFMPEG_BYTES = 60 * 1024 * 1024
USER, PASSWORD = "gate", "gate-only-password"


def docker(image: str, args: list[str], stdin: bytes | None = None, timeout: float = 60, extra: list[str] | None = None):
    cmd = ["docker", "run", "--rm", "-i", *(extra or []), "--entrypoint", "ffmpeg", image, *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)


def shell(image: str, script: str) -> str:
    out = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", script], capture_output=True, timeout=60)
    return out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")


failures: list[str] = []


def check(ok: bool, what: str, detail: str = ""):
    print(("PASS  " if ok else "FAIL  ") + what + (("  - " + detail) if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(what)


def live_capture(image: str, port: int, seconds: float = 4.0) -> bytes:
    url = f"rtsp://{USER}:{PASSWORD}@host.docker.internal:{port}/live"
    script = rtsp_input.build_concat_script(url, "tcp", 10)
    argv = ["-t", str(int(seconds)), *rtsp_input.ffmpeg_stream_args()]
    result = docker(image, argv, stdin=script, timeout=40, extra=["--add-host=host.docker.internal:host-gateway"])
    if result.returncode not in (0, 255):
        print(result.stderr.decode("utf-8", "replace")[-800:], flush=True)
    return result.stdout


def main() -> int:
    image = sys.argv[1] if len(sys.argv) > 1 else "meshcenter:ffmpeg-gate"

    version = shell(image, "ffmpeg -version | head -1")
    check(f"version {PINNED_VERSION}" in version.replace("ffmpeg version ", "version "), "pinned ffmpeg version", version.strip())
    check("Lesser General Public" in shell(image, "ffmpeg -L"), "ffmpeg -L reports the LGPL")
    buildconf = shell(image, "ffmpeg -buildconf")
    check(re.search(r"enable-(gpl|nonfree|version3)", buildconf) is None, "no gpl/nonfree/version3 in the configure line")
    check("not found" not in shell(image, "ldd /opt/ffmpeg/bin/ffmpeg"), "every shared library resolves")

    server = FakeRtspServer(host="0.0.0.0", user=USER, password=PASSWORD).start()
    try:
        stream = live_capture(image, server.port)
    finally:
        server.stop()
    init_end = stream.find(b"moof")
    check(len(stream) > 2000 and init_end > 0, "live stream: init segment and fragments arrive", f"{len(stream)} bytes")
    info = {}
    if init_end > 0:
        try:
            info = fmp4.parse_init(stream[:init_end - 4])
        except Exception as error:  # noqa: BLE001
            check(False, "live stream: init segment parses", str(error))
    check(fmp4.is_h264(info) and (info.get("width"), info.get("height")) == (320, 180), "live stream is H.264 320x180", str(info))

    grab = docker(image, frame_grab.FFMPEG_ARGS, stdin=stream, timeout=40)
    jpeg = grab.stdout
    check(jpeg.startswith(b"\xff\xd8") and jpeg.rstrip(b"\x00").endswith(b"\xff\xd9") and len(jpeg) > 1000,
          "frame grab: one JPEG", f"{len(jpeg)} bytes; {grab.stderr.decode('utf-8', 'replace')[-300:]}")

    mjpeg = docker(image, mjpeg_fallback.transcode_argv([]), stdin=stream, timeout=40)
    frames = mjpeg.stdout.count(b"\xff\xd8\xff")
    check(frames >= 2, "MJPEG fallback: at least two JPEG frames", f"{frames} frames; {mjpeg.stderr.decode('utf-8', 'replace')[-300:]}")

    hevc_path = os.path.join(ROOT, "tests", "fixtures", "tiny_hevc_frag.mp4")
    with open(hevc_path, "rb") as handle:
        hevc = docker(image, ["-hide_banner", "-loglevel", "error", "-f", "mp4", "-i", "pipe:0", "-c:v", "copy", "-f", "mp4",
                              "-movflags", "empty_moov+default_base_moof", "pipe:1"], stdin=handle.read(), timeout=40)
    check(hevc.returncode == 0 and b"ftyp" in hevc.stdout[:64], "H.265 passes through -c copy",
          hevc.stderr.decode("utf-8", "replace")[-300:])

    sizes = shell(image, "du -sb /opt/ffmpeg /usr/share/doc/meshcenter-ffmpeg 2>/dev/null | awk '{s+=$1} END {print s}'").strip().splitlines()
    total = int(sizes[-1]) if sizes and sizes[-1].isdigit() else -1
    check(0 < total <= MAX_FFMPEG_BYTES, f"ffmpeg part of the image is at most 60 MB", f"{total} bytes")
    print(f"INFO  ffmpeg part of the image: {total / 1048576:.1f} MiB", flush=True)

    print("GATE PASSED" if not failures else "GATE FAILED: " + "; ".join(failures), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
