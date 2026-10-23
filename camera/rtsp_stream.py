"""One upstream connection per network camera, fanned out to many viewers (CAM2 Phase 1b).

    camera --RTSP--> ONE ffmpeg (-c copy, no transcode) --fMP4 on a pipe--> FragmentRelay --> N viewers

Design rules, each one a test:

* ONE ffmpeg / ONE RTSP session per source however many browsers watch
  (cameras limit sessions). It starts on the first viewer and stops
  IDLE_STOP_S after the last one leaves.
* Bounded memory: the relay keeps at most MAX_FRAGMENTS / MAX_BYTES of recent
  fragments. A viewer is only a cursor into that buffer, so a slow client
  cannot grow memory; if it falls behind it is moved to the newest keyframe
  (frames are dropped, never queued).
* Viewer caps (D2): MAX_VIEWERS_PER_SOURCE and MAX_VIEWERS_TOTAL, beyond which
  a new viewer is refused (HTTP 429) - each viewer pins a gunicorn thread.
* Reconnect never loops fast: exponential backoff 2 s ... 60 s with jitter,
  reset only after the stream stayed up STABLE_AFTER_S. Failures that retrying
  cannot fix (wrong password, wrong path, H.265, blocked address) are TERMINAL:
  no retry until the user edits the source or presses Reconnect (repeated bad
  logins can lock a camera account).
* The password never reaches argv (camera/rtsp_input.py), logs or the status:
  every line from ffmpeg is redacted before it is stored or printed.
* The address policy is re-checked on every connect and ffmpeg gets the
  validated IP, not the host name.
* Clean stop: stop() terminates ffmpeg's whole process group; children are also
  armed with PR_SET_PDEATHSIG through the shared spawner thread (F15), and an
  atexit hook stops every upstream on a clean exit.
"""
from __future__ import annotations

import atexit
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from typing import Any, Callable

from camera import address_policy, fmp4, rtsp_input
from meshsrv import subprocess_spawner

MAX_FRAGMENTS = 40
MAX_BYTES = 12 * 1024 * 1024
MAX_VIEWERS_PER_SOURCE = 4
MAX_VIEWERS_TOTAL = 6
IDLE_STOP_S = 60.0
STABLE_AFTER_S = 20.0
STALL_AFTER_S = 8.0
BACKOFF_STEPS = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
FRAG_DURATION_US = 500_000
STDOUT_CHUNK = 65536
STOP_JOIN_S = 6.0

TERMINAL_ERRORS = frozenset({"auth_failed", "stream_not_found", "unsupported_codec", "address_blocked", "ffmpeg_missing"})


def ffmpeg_cmd() -> list[str]:
    """Resolved on every call so tests (and a late apt install) can change it."""
    found = shutil.which("ffmpeg")
    return [found] if found else []


# ---------------------------------------------------------------------------
# bounded fan-out
# ---------------------------------------------------------------------------

class _Stored:
    __slots__ = ("seq", "fragment")

    def __init__(self, seq, fragment):
        self.seq, self.fragment = seq, fragment


class Cursor:
    def __init__(self, epoch: int):
        self.epoch = epoch
        self.last = 0
        self.joined = False
        self.dropped = 0


class FragmentRelay:
    """Init segment + the most recent fragments; viewers read through Cursors."""

    def __init__(self, max_fragments: int = MAX_FRAGMENTS, max_bytes: int = MAX_BYTES):
        self._cond = threading.Condition()
        self._max_fragments, self._max_bytes = max_fragments, max_bytes
        self._frags: deque = deque()
        self._bytes = 0
        self._seq = 0
        self.epoch = 0
        self.init: bytes | None = None
        self.closed = False

    def begin(self, init: bytes) -> None:
        """A new upstream session: viewers of the old one are ended (they reconnect and get this init)."""
        with self._cond:
            self.epoch += 1
            self.init = init
            self._frags.clear()
            self._bytes = 0
            self.closed = False
            self._cond.notify_all()

    def end_session(self) -> None:
        with self._cond:
            self.epoch += 1
            self.init = None
            self._frags.clear()
            self._bytes = 0
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self.epoch += 1
            self._cond.notify_all()

    def reopen(self) -> None:
        with self._cond:
            self.closed = False

    def push(self, fragment: fmp4.Fragment) -> None:
        with self._cond:
            self._seq += 1
            self._frags.append(_Stored(self._seq, fragment))
            self._bytes += len(fragment.data)
            while len(self._frags) > 1 and (len(self._frags) > self._max_fragments or self._bytes > self._max_bytes):
                self._bytes -= len(self._frags.popleft().fragment.data)
            self._cond.notify_all()

    def has_init(self) -> bool:
        return self.init is not None

    def wait_init(self, timeout: float, abort: Callable[[], bool] = lambda: False) -> bytes | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self.init is None and not self.closed and not abort():
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 0.25))
            return self.init

    def open_cursor(self) -> Cursor:
        with self._cond:
            return Cursor(self.epoch)

    def read(self, cursor: Cursor, timeout: float) -> list | None:
        """Bytes chunks to send next ([] = nothing yet), or None when this viewer's stream is over."""
        with self._cond:
            def ready():
                return (self.closed or cursor.epoch != self.epoch
                        or (self._frags and self._frags[-1].seq > cursor.last and self.init is not None))
            if not ready():
                self._cond.wait(timeout)
            if self.closed or cursor.epoch != self.epoch or self.init is None:
                return None
            if not self._frags or self._frags[-1].seq <= cursor.last:
                return []
            newer = [s for s in self._frags if s.seq > cursor.last]
            out: list = []
            if not cursor.joined:
                sync_at = [i for i, s in enumerate(newer) if s.fragment.sync]
                if not sync_at:
                    return []  # wait for a keyframe; do not start a viewer mid-GOP
                newer = newer[sync_at[-1]:]
                out.append(self.init)
                cursor.joined = True
            elif newer[0].seq > cursor.last + 1 and cursor.last:
                # fell behind the retained window: drop to the newest keyframe, never queue
                cursor.dropped += newer[0].seq - cursor.last - 1
                cursor.last = newer[0].seq - 1  # count each lost fragment once
                sync_at = [i for i, s in enumerate(newer) if s.fragment.sync]
                if not sync_at:
                    return []
                newer = newer[sync_at[-1]:]
            out.extend(s.fragment.data for s in newer)
            cursor.last = newer[-1].seq
            return out

    def stats(self) -> dict:
        with self._cond:
            return {"fragments": len(self._frags), "bytes": self._bytes}


class ViewerLimiter:
    """D2: at most `per_source` viewers per camera and `total` across all of them."""

    def __init__(self, per_source: int = MAX_VIEWERS_PER_SOURCE, total: int = MAX_VIEWERS_TOTAL):
        self._lock = threading.Lock()
        self.per_source, self.total = per_source, total
        self._counts: dict[str, int] = {}

    def acquire(self, source_id: str) -> bool:
        with self._lock:
            if self._counts.get(source_id, 0) >= self.per_source or sum(self._counts.values()) >= self.total:
                return False
            self._counts[source_id] = self._counts.get(source_id, 0) + 1
            return True

    def release(self, source_id: str) -> None:
        with self._lock:
            left = self._counts.get(source_id, 0) - 1
            if left > 0:
                self._counts[source_id] = left
            else:
                self._counts.pop(source_id, None)

    def count(self, source_id: str | None = None) -> int:
        with self._lock:
            return sum(self._counts.values()) if source_id is None else self._counts.get(source_id, 0)


VIEWER_LIMITER = ViewerLimiter()


class Backoff:
    def __init__(self, steps=BACKOFF_STEPS, jitter: float = 0.1, rand: Callable[[], float] = random.random):
        self._steps, self._jitter, self._rand = steps, jitter, rand
        self.attempt = 0

    def next_delay(self) -> float:
        base = self._steps[min(self.attempt, len(self._steps) - 1)]
        self.attempt += 1
        return base * (1.0 + self._jitter * (2.0 * self._rand() - 1.0))

    def reset(self) -> None:
        self.attempt = 0


def classify_failure(lines: list[str], got_data: bool) -> str:
    text = "\n".join(lines).lower()
    if "401" in text or "unauthorized" in text or "authorization failed" in text or "authentication" in text:
        return "auth_failed"
    if "404" in text or "not found" in text:
        return "stream_not_found"
    if any(k in text for k in ("refused", "timed out", "timeout", "no route", "unreachable", "reset by peer", "name or service", "host is down")):
        return "unreachable"
    return "stream_error" if got_data else "unreachable"


# ---------------------------------------------------------------------------
# process plumbing (same fork-safety rules as camera/usb_driver.py)
# ---------------------------------------------------------------------------

def _popen(argv: list[str]) -> subprocess.Popen:
    kwargs: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0}
    if sys.platform == "linux":
        from meshsrv.adapter_ipc_client import _set_pdeathsig_to_sigkill

        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _set_pdeathsig_to_sigkill
    elif os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(argv, **kwargs)


def spawn_process(argv: list[str]) -> subprocess.Popen:
    # Popen must run on the one long-lived spawner thread: PDEATHSIG is per-THREAD (see meshsrv/subprocess_spawner.py)
    return subprocess_spawner.spawn(lambda: _popen(argv), timeout=10.0)


def terminate_process(process: subprocess.Popen) -> None:
    from camera.usb_driver import _terminate_process

    _terminate_process(process)


_ALL: "weakref.WeakSet[RtspUpstream]" = weakref.WeakSet()


def stop_all_streaming() -> None:
    for upstream in list(_ALL):
        try:
            upstream.stop()
        except Exception:
            pass


atexit.register(stop_all_streaming)


# ---------------------------------------------------------------------------
# the upstream
# ---------------------------------------------------------------------------

class RtspUpstream:
    def __init__(
        self,
        source: dict,
        password_getter: Callable[[], str],
        *,
        host_validator: Callable[[str], list] | None = None,
        limiter: ViewerLimiter | None = None,
        spawn: Callable[[list[str]], Any] | None = None,
        cmd_provider: Callable[[], list[str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        backoff: Backoff | None = None,
        idle_stop_s: float = IDLE_STOP_S,
        stable_after_s: float = STABLE_AFTER_S,
        stall_after_s: float = STALL_AFTER_S,
    ):
        self.source = dict(source)
        self.id = str(source["id"])
        self._password = password_getter
        self._validate = host_validator or address_policy.validate_camera_host
        self.limiter = limiter or VIEWER_LIMITER
        self._spawn = spawn or spawn_process
        self._cmd = cmd_provider or ffmpeg_cmd
        self._clock = clock
        self._backoff = backoff or Backoff()
        self._idle_stop_s, self._stable_after_s, self._stall_after_s = idle_stop_s, stable_after_s, stall_after_s
        self.relay = FragmentRelay()

        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._process = None
        self._stop_requested = False
        self._wake = False
        self._profile = str(source.get("preferred_profile") or "sub")
        self._generation = 0
        self._last_viewer_at = clock()

        self.state = "stopped"
        self.error_code = ""
        self.info: dict = {}
        self._fps_window: deque = deque(maxlen=20)  # (arrival, samples, duration_s, bytes)
        self._next_retry_at: float | None = None
        self._stderr_tail: deque = deque(maxlen=30)
        _ALL.add(self)

    # -- public ------------------------------------------------------------

    def ensure_running(self) -> None:
        with self._cond:
            self._last_viewer_at = self._clock()
            if self._thread is not None and self._thread.is_alive():
                if self.state in ("idle", "stopped"):
                    self._wake = True
                    self._cond.notify_all()
                return
            self._stop_requested = False
            self.relay.reopen()
            self.state = "connecting"
            self.error_code = ""
            self._thread = threading.Thread(target=self._run, name=f"ip-camera-{self.id[3:11]}", daemon=True)
            self._thread.start()

    def note_viewer_activity(self) -> None:
        with self._cond:
            self._last_viewer_at = self._clock()

    def stop(self) -> None:
        with self._cond:
            self._stop_requested = True
            self._wake = True
            self._cond.notify_all()
            process, thread = self._process, self._thread
        if process is not None:
            terminate_process(process)
        self.relay.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STOP_JOIN_S)
        with self._cond:
            self._thread = None
            self.state = "stopped"

    def reconnect(self) -> None:
        """User pressed Reconnect: clears a terminal failure and the backoff and tries now."""
        with self._cond:
            self._backoff.reset()
            self._wake = True
            self.error_code = ""
            if self.state in TERMINAL_ERRORS or self.state in ("reconnecting", "idle"):
                self.state = "connecting"
            self._cond.notify_all()
        self.ensure_running()

    def set_profile(self, profile: str) -> None:
        if profile not in ("sub", "main"):
            raise ValueError("profile must be sub or main")
        with self._cond:
            if profile == self._profile:
                return
            self._profile = profile
            self._generation += 1
            self._backoff.reset()
            self._wake = True
            self.error_code = ""
            if self.state in TERMINAL_ERRORS or self.state == "reconnecting":
                self.state = "connecting"
            process = self._process
            self._cond.notify_all()
        if process is not None:
            terminate_process(process)  # the supervisor loop reconnects with the new path

    @property
    def profile(self) -> str:
        return self._profile

    def wait_ready(self, timeout: float) -> bool:
        """True once an init segment exists; False on timeout or a terminal state."""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if self.relay.has_init():
                return True
            if self.state in ("unsupported_codec", "auth_failed", "stream_not_found", "address_blocked", "ffmpeg_missing", "stopped"):
                return False
            self.relay.wait_init(0.25, abort=lambda: self.state in TERMINAL_ERRORS or self.state == "stopped")
        return self.relay.has_init()

    def status(self) -> dict:
        with self._cond:
            now = self._clock()
            retry_in = None if self._next_retry_at is None else max(0.0, self._next_retry_at - now)
            info = dict(self.info)
            window = list(self._fps_window)
        fps = bitrate = None
        if len(window) >= 3:
            # Measured over wall-clock arrival (what a viewer actually receives), skipping the first
            # fragment (it carries the time before the window started).
            span = window[-1][0] - window[0][0]
            if span > 0:
                fps = round(sum(w[1] for w in window[1:]) / span, 1)
                bitrate = round(sum(w[3] for w in window[1:]) * 8 / span / 1000)
        width, height = info.get("width"), info.get("height")
        return {
            "state": self.state,
            "error_code": self.error_code,
            "profile": self._profile,
            "stream_kind": "fmp4",
            "codec": info.get("family"),
            "mse_codec": info.get("codec") or None,
            "width": width, "height": height,
            "resolution": f"{width}x{height}" if width and height else None,
            "fps": fps, "bitrate_kbps": bitrate,
            "reconnect_attempt": self._backoff.attempt if self.state == "reconnecting" else 0,
            "reconnect_in_s": None if retry_in is None else round(retry_in, 1),
            "viewers": self.limiter.count(self.id),
        }

    # -- supervisor ----------------------------------------------------------

    def _log(self, message: str) -> None:
        print(f"[IP CAMERA {self.id[3:11]}] {rtsp_input.redact(message, self._password())}", flush=True)

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stop_requested:
                    return
                self._wake = False
                generation = self._generation
            outcome = self._connect_once(generation)
            if outcome == "stopped":
                return
            if outcome == "idle":
                with self._cond:
                    self.state = "idle"
                    self._next_retry_at = None
                self.relay.end_session()
                return
            if outcome in TERMINAL_ERRORS:
                with self._cond:
                    self.state, self.error_code = outcome, outcome
                    self._next_retry_at = None
                    self.relay.end_session()
                    self._cond.wait_for(lambda: self._wake or self._stop_requested, timeout=self._idle_stop_s)
                    if self._stop_requested:
                        return
                    if not self._wake:  # nobody cares any more
                        self.state = "idle"
                        return
                continue
            if outcome == "restart":  # profile switch
                continue
            # retryable: back off, interruptible by stop / reconnect / profile switch / idleness
            delay = self._backoff.next_delay()
            with self._cond:
                self.state, self.error_code = "reconnecting", outcome
                self._next_retry_at = self._clock() + delay
                self.relay.end_session()
                end = self._clock() + delay
                while not (self._wake or self._stop_requested):
                    left = end - self._clock()
                    if left <= 0:
                        break
                    if self.limiter.count(self.id) == 0 and self._clock() - self._last_viewer_at > self._idle_stop_s:
                        self.state = "idle"
                        self._next_retry_at = None
                        return
                    self._cond.wait(min(left, 1.0))
                self._next_retry_at = None

    def _idle_expired(self) -> bool:
        return self.limiter.count(self.id) == 0 and (self._clock() - self._last_viewer_at) > self._idle_stop_s

    def _connect_once(self, generation: int) -> str:
        """One connect attempt. Returns 'stopped', 'idle', 'restart', a terminal/retryable error code."""
        cmd = self._cmd()
        if not cmd:
            return "ffmpeg_missing"
        try:
            addresses = self._validate(str(self.source.get("host") or ""))
        except address_policy.AddressPolicyError:
            return "address_blocked"
        profile = self._profile
        path = str(self.source.get("main_path" if profile == "main" else "sub_path") or "")
        if not path:
            return "stream_not_found"
        password = self._password()
        try:
            url = rtsp_input.build_rtsp_url(addresses[0], int(self.source.get("port") or 554), path,
                                            str(self.source.get("username") or ""), password)
            script = rtsp_input.build_concat_script(url, str(self.source.get("transport") or "tcp"),
                                                    int(self.source.get("connect_timeout_s") or 10))
        except ValueError:
            return "stream_error"
        argv = [*cmd, *rtsp_input.ffmpeg_stream_args()]

        with self._cond:
            self.state, self.error_code = "connecting", ""
            self.info = {}
            self._fps_window.clear()
            self._stderr_tail.clear()
        try:
            process = self._spawn(argv)
        except Exception as error:
            self._log(f"could not start ffmpeg: {type(error).__name__}")
            return "stream_error"
        with self._cond:
            self._process = process
        killed_for: list[str] = []
        got = {"init": False, "fragment": False, "at": self._clock(), "stable_at": None}

        def watchdog():
            started = self._clock()
            first_deadline = int(self.source.get("connect_timeout_s") or 10) + 10
            while process.poll() is None:
                time.sleep(0.25)
                now = self._clock()
                with self._cond:
                    if self._stop_requested:
                        return
                    if generation != self._generation:
                        killed_for.append("restart")
                    elif self._idle_expired():
                        killed_for.append("idle")
                    elif not got["fragment"] and now - started > first_deadline:
                        killed_for.append("timeout")
                    elif got["fragment"] and now - got["at"] > self._stall_after_s:
                        killed_for.append("stall")
                if killed_for:
                    terminate_process(process)
                    return

        threading.Thread(target=watchdog, daemon=True, name="ip-camera-watchdog").start()

        def drain_stderr():
            try:
                for raw in iter(process.stderr.readline, b""):
                    line = rtsp_input.redact(raw.decode(errors="replace").rstrip(), password)
                    if line:
                        self._stderr_tail.append(line)
            except Exception:
                pass

        threading.Thread(target=drain_stderr, daemon=True, name="ip-camera-stderr").start()
        try:
            process.stdin.write(script)
            process.stdin.close()
        except Exception:
            pass

        splitter = fmp4.Fmp4Splitter()
        terminal: str | None = None
        try:
            while True:
                chunk = process.stdout.read(STDOUT_CHUNK)
                if not chunk:
                    break
                for kind, item in splitter.feed(chunk):
                    if kind == "init":
                        info = fmp4.parse_init(item)
                        family = "h264" if fmp4.is_h264(info) else ("h265" if fmp4.is_h265(info) else (info.get("entry") or "unknown"))
                        info["family"] = family
                        with self._cond:
                            self.info = info
                        if family != "h264":
                            terminal = "unsupported_codec"
                            terminate_process(process)
                            break
                        got["init"] = True
                        self.relay.begin(item)
                    else:
                        now = self._clock()
                        if not got["fragment"]:
                            got["fragment"] = True
                            got["stable_at"] = now
                            with self._cond:
                                self.state = "live"
                        got["at"] = now
                        scale = self.info.get("timescale") or 90000
                        with self._cond:
                            self._fps_window.append((now, item.samples, item.duration_ticks / scale, len(item.data)))
                        self.relay.push(item)
                        if got["stable_at"] is not None and now - got["stable_at"] >= self._stable_after_s:
                            self._backoff.reset()
                if terminal:
                    break
        except fmp4.Fmp4Error:
            self._log("corrupt stream from ffmpeg")
            terminate_process(process)
        except Exception as error:
            self._log(f"reader error: {type(error).__name__}")
        try:
            process.wait(timeout=3)
        except Exception:
            terminate_process(process)
        with self._cond:
            self._process = None
            stop = self._stop_requested
        if stop:
            return "stopped"
        if terminal:
            self._log("stream codec is not H.264 (not playable in a browser without a transcode)")
            return terminal
        if killed_for:
            reason = killed_for[0]
            if reason == "idle":
                return "idle"
            if reason == "restart":
                return "restart"
            self._log(f"upstream {reason}")
            return "unreachable"
        code = classify_failure(list(self._stderr_tail), got["fragment"])
        for line in list(self._stderr_tail)[-3:]:
            self._log("ffmpeg: " + line[:200])
        return code
