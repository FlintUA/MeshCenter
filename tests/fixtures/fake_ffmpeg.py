"""Standalone fake ffmpeg for tests/test_usb_camera_driver.py - emits
canned JPEG frames on stdout in the same "raw concatenated MJPEG on a
pipe" shape a real `ffmpeg ... -f mjpeg pipe:1` invocation produces,
without needing a real camera or a real ffmpeg binary. In the style of
tests/fixtures/fake_adapter.py.

Driven entirely by environment variables, never by argv - the driver
builds a full, realistic ffmpeg-shaped argv (see
camera/usb_driver.py's _build_ffmpeg_stream_argv()/_build_ffmpeg_photo_argv()),
and this script only inspects argv for `-frames:v N`, the one place a
real ffmpeg's own behavior (stop after N frames, not only on kill) must
be reproduced for the one-shot photo-capture tests.

FAKE_FFMPEG_BEHAVIOR (default "normal"):
  normal  - emit frames at FAKE_FFMPEG_FPS. With a `-frames:v N` argv
    limit (one-shot photo capture): stop and exit 0 after N frames, same
    as real ffmpeg. With no such limit (streaming): keep emitting until
    killed (SIGTERM/SIGKILL) - Python's default signal handling
    terminates the process immediately with no cleanup, the same
    "killed by signal, no orderly shutdown" shape a real ffmpeg has when
    it responds to SIGTERM, which is exactly what the driver's
    self._stop_requested tracking needs to see as "not a failure."
  exit0   - emit FAKE_FFMPEG_FRAME_COUNT frames, then exit 0 UNPROMPTED
    (no signal involved) - CAM-0's own live-confirmed finding: this is
    exactly what real ffmpeg does when the camera is unplugged mid-stream.
  exit1   - exit immediately, code 1, no frames at all (e.g. a bad device
    path).
  stall   - emit FAKE_FFMPEG_STALL_FRAMES frames (default 1), then block
    forever without exiting and without writing anything further -
    simulates a wedged capture (CAM-1's own design note: an unplug can
    hang instead of exiting on some kernels).
  garbage - write bytes that never contain a JPEG EOI (FFD9) marker at
    all, forever - proves the reader's frame-buffer cap kicks in instead
    of buffering unbounded "frame" data.

FAKE_FFMPEG_FRAME_COUNT (default 1000000): frames to emit before
  exit0/normal-without-a-limit would ever run out on their own (in
  practice, tests kill the process well before this via the driver's own
  stop()/timeout logic).
FAKE_FFMPEG_FPS (default 30): pacing between frames.
FAKE_FFMPEG_CHUNK_SIZE (default 37): stdout is flushed in chunks of this
  many bytes rather than one frame at a time, deliberately not aligned to
  a JPEG frame boundary - proves the reader correctly reassembles a frame
  whose SOI/EOI markers land in different reads.
FAKE_FFMPEG_STALL_FRAMES (default 1): frames emitted before "stall"
  blocks.
"""
import os
import sys
import time

# A real, tiny, PIL-decodable JPEG (not just bytes with the right SOI/EOI
# framing) - the driver's own capture_photo() path validates every
# one-shot frame with PIL (camera/usb_driver.py's _is_valid_jpeg()), so a
# placeholder that merely LOOKS like a JPEG at the byte-marker level
# would fail that check and make these tests indistinguishable from a
# real corrupt-frame bug.
with open(os.path.join(os.path.dirname(__file__), "tiny_valid_frame.jpg"), "rb") as _f:
    _CANNED_JPEG = _f.read()


def _frame_limit_from_argv(argv: list[str]) -> int | None:
    for i, arg in enumerate(argv):
        if arg == "-frames:v" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
    return None


def _write_chunked(stdout, data: bytes, chunk_size: int) -> None:
    for start in range(0, len(data), chunk_size):
        stdout.write(data[start:start + chunk_size])
        stdout.flush()


def main() -> None:
    behavior = os.environ.get("FAKE_FFMPEG_BEHAVIOR", "normal")
    frame_count = int(os.environ.get("FAKE_FFMPEG_FRAME_COUNT", "1000000"))
    fps = float(os.environ.get("FAKE_FFMPEG_FPS", "30"))
    chunk_size = int(os.environ.get("FAKE_FFMPEG_CHUNK_SIZE", "37"))
    stall_frames = int(os.environ.get("FAKE_FFMPEG_STALL_FRAMES", "1"))
    interval = 1.0 / fps if fps > 0 else 0.0

    argv_limit = _frame_limit_from_argv(sys.argv[1:])
    if argv_limit is not None:
        frame_count = argv_limit

    stdout = sys.stdout.buffer

    if behavior == "exit1":
        sys.exit(1)

    if behavior == "garbage":
        chunk = b"\xff\xd8" + (b"\x11" * chunk_size)  # SOI, then never an EOI
        while True:
            stdout.write(chunk)
            stdout.flush()
            time.sleep(interval)
        return

    emitted = 0
    while emitted < frame_count:
        _write_chunked(stdout, _CANNED_JPEG, chunk_size)
        emitted += 1

        if behavior == "stall" and emitted >= stall_frames:
            time.sleep(3600)
            return

        time.sleep(interval)

    if behavior == "exit0":
        sys.exit(0)

    if argv_limit is not None:
        # A one-shot photo capture (-frames:v N) - real ffmpeg exits 0
        # once satisfied.
        sys.exit(0)

    # "normal" streaming with no frame limit - real ffmpeg keeps running
    # until killed; block here the same way.
    time.sleep(3600)


if __name__ == "__main__":
    main()
