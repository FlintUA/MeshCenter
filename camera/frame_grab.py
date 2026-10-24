"""One picture out of the stream that is already running (CAM2 Phase 3a).

For a network camera without a snapshot API (or when its API cannot be reached while the preview runs): the
init segment and the newest keyframe fragment are copied out of the existing relay (camera/rtsp_stream.py
`FragmentRelay.snapshot_input()`) and handed to a ONE-SHOT ffmpeg on stdin, which decodes one frame and writes
one JPEG. There is no second RTSP session and no connection to the camera at all, so nothing else about the
preview changes; the cost is one decode of one frame (small for the sub stream, noticeable for a 4K main stream
on a Pi Zero, which is why callers rate limit it and the call has a timeout).

The ffmpeg command line carries no secret and no address: its only input is the in-memory bytes.
"""
from __future__ import annotations

import subprocess
from typing import Callable

from camera import rtsp_stream

GRAB_TIMEOUT_S = 15.0
MAX_JPEG_BYTES = 12 * 1024 * 1024

FFMPEG_ARGS = ["-hide_banner", "-loglevel", "error", "-f", "mp4", "-i", "pipe:0",
               "-frames:v", "1", "-an", "-c:v", "mjpeg", "-q:v", "2", "-f", "image2pipe", "pipe:1"]


class GrabError(Exception):
    """code: preview_not_running | ffmpeg_missing | frame_grab_failed"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def grab_frame(relay, *, cmd_provider: Callable[[], list[str]] | None = None, timeout: float = GRAB_TIMEOUT_S) -> bytes:
    data = relay.snapshot_input()
    if data is None:
        raise GrabError("preview_not_running", "Start the preview first: a picture is taken from the running stream.")
    cmd = (cmd_provider or rtsp_stream.ffmpeg_cmd)()
    if not cmd:
        raise GrabError("ffmpeg_missing", "ffmpeg is not installed, so a picture cannot be taken from the stream.")
    try:
        result = subprocess.run(cmd + FFMPEG_ARGS, input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GrabError("frame_grab_failed", "Taking the picture from the stream took too long.")
    except OSError:
        raise GrabError("ffmpeg_missing", "ffmpeg could not be started.")
    jpeg = result.stdout
    if result.returncode != 0 or jpeg[:3] != b"\xff\xd8\xff" or len(jpeg) > MAX_JPEG_BYTES:
        raise GrabError("frame_grab_failed", "A picture could not be taken from the stream.")
    return jpeg
