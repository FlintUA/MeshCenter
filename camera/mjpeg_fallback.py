"""MJPEG fallback for network cameras (CAM2 Phase 1b), for browsers that cannot play the
fMP4 stream (no MediaSource / ManagedMediaSource, or no H.264 support).

It costs a decode + JPEG encode (measured on a Pi Zero 2W class host: about half a core for the
640x360 sub stream, 220% of a core on a 16 core PC for 4K), so:

* it is OFF on low-power hosts (camera/usb_driver._low_power_host(): <= 4 cores and <= 1 GB RAM)
  and the UI says so instead of silently pegging the CPU;
* it never opens a second camera session: it is fed from the SAME relay the fMP4 viewers use
  (one more reader of the one upstream ffmpeg), through a small local ffmpeg that reads fMP4 on
  stdin and writes MJPEG on stdout;
* it runs only while an MJPEG viewer exists (idle stop) and never for the 4K main stream unless
  the user explicitly asked for main.
"""
from __future__ import annotations

import threading
import time
from typing import Iterator

from camera import rtsp_stream

IDLE_STOP_S = 20.0
FRAME_CAP = 8 * 1024 * 1024
JPEG_QUALITY = "5"  # ffmpeg -q:v scale: 2 best ... 31 worst


def fallback_disabled_reason() -> str | None:
    from camera.usb_driver import _low_power_host

    return "low_power_host" if _low_power_host() else None


def transcode_argv(cmd: list[str]) -> list[str]:
    return [*cmd, "-hide_banner", "-nostats", "-loglevel", "error", "-i", "pipe:0",
            "-an", "-c:v", "mjpeg", "-q:v", JPEG_QUALITY, "-f", "mjpeg", "pipe:1"]


class MjpegFromRelay:
    def __init__(self, upstream: "rtsp_stream.RtspUpstream", *, idle_stop_s: float = IDLE_STOP_S):
        self._upstream = upstream
        self._idle_stop_s = idle_stop_s
        self._lock = threading.Lock()
        self._frame_cond = threading.Condition()
        self._last_frame: bytes | None = None
        self._frame_seq = 0
        self._consumers = 0
        self._last_activity = time.monotonic()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._process = None

    # -- consumers -----------------------------------------------------------

    def frames(self) -> Iterator[bytes]:
        with self._lock:
            self._consumers += 1
            self._last_activity = time.monotonic()
            self._ensure_thread_locked()
        seen = 0
        try:
            while not self._stop.is_set():
                with self._frame_cond:
                    if self._frame_seq == seen:
                        self._frame_cond.wait(1.0)
                    frame, seq = self._last_frame, self._frame_seq
                if frame is not None and seq != seen:
                    seen = seq
                    yield frame
        finally:
            with self._lock:
                self._consumers = max(0, self._consumers - 1)
                self._last_activity = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None:
            rtsp_stream.terminate_process(process)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=rtsp_stream.STOP_JOIN_S)
        self._thread = None
        with self._frame_cond:
            self._last_frame = None
            self._frame_cond.notify_all()

    # -- worker ----------------------------------------------------------------

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ip-camera-mjpeg", daemon=True)
        self._thread.start()

    def _idle(self) -> bool:
        with self._lock:
            return self._consumers == 0 and time.monotonic() - self._last_activity > self._idle_stop_s

    def _run(self) -> None:
        relay = self._upstream.relay
        while not self._stop.is_set() and not self._idle():
            self._upstream.ensure_running()
            self._upstream.note_viewer_activity()
            if relay.wait_init(1.0, abort=self._stop.is_set) is None:
                continue
            cmd = rtsp_stream.ffmpeg_cmd()
            if not cmd:
                time.sleep(1.0)
                continue
            try:
                process = rtsp_stream.spawn_process(rtsp_stream_argv(cmd))
            except Exception:
                time.sleep(1.0)
                continue
            self._process = process
            reader = threading.Thread(target=self._read_frames, args=(process,), daemon=True, name="ip-camera-mjpeg-read")
            reader.start()
            cursor = relay.open_cursor()
            try:
                while not self._stop.is_set() and process.poll() is None:
                    chunks = relay.read(cursor, 1.0)
                    if chunks is None:
                        break  # new upstream session: restart the transcoder with the new init segment
                    self._upstream.note_viewer_activity() if self._consumers else None
                    for chunk in chunks:
                        process.stdin.write(chunk)
                    if self._idle():
                        break
            except Exception:
                pass
            finally:
                try:
                    process.stdin.close()
                except Exception:
                    pass
                rtsp_stream.terminate_process(process)
                self._process = None
                reader.join(timeout=2.0)

    def _read_frames(self, process) -> None:
        buffer = bytearray()
        try:
            while True:
                chunk = process.stdout.read(rtsp_stream.STDOUT_CHUNK)
                if not chunk:
                    return
                buffer.extend(chunk)
                while True:
                    soi = buffer.find(b"\xff\xd8")
                    if soi == -1:
                        if len(buffer) > FRAME_CAP:
                            buffer.clear()
                        break
                    if soi > 0:
                        del buffer[:soi]
                    eoi = buffer.find(b"\xff\xd9", 2)
                    if eoi == -1:
                        if len(buffer) > FRAME_CAP:
                            buffer.clear()
                        break
                    frame = bytes(buffer[:eoi + 2])
                    del buffer[:eoi + 2]
                    with self._frame_cond:
                        self._last_frame = frame
                        self._frame_seq += 1
                        self._frame_cond.notify_all()
        except Exception:
            return


def rtsp_stream_argv(cmd: list[str]) -> list[str]:
    return transcode_argv(cmd)
