"""CAM2 Phase 1b: fMP4 parsing, bounded fan-out, viewer caps, backoff and the upstream supervisor
(camera/fmp4.py, camera/rtsp_stream.py), driven by tests/fixtures/fake_ffmpeg_fmp4.py - no camera, no real ffmpeg."""
import os
import sys
import threading
import time

import pytest

from camera import address_policy, fmp4
from camera import rtsp_stream as rs

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FAKE = os.path.join(FIXTURES, "fake_ffmpeg_fmp4.py")
PASSWORD = "S3cret-pw-for-tests"


def _read(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def _split_all(data, step=37):
    splitter = fmp4.Fmp4Splitter()
    items = []
    for i in range(0, len(data), step):
        items += splitter.feed(data[i:i + step])
    return items


# -- fmp4 ---------------------------------------------------------------------

def test_splitter_reassembles_init_and_fragments_from_arbitrary_chunks():
    for step in (1, 7, 41, 4096, 10**6):
        items = _split_all(_read("tiny_h264_frag.mp4"), step)
        kinds = [k for k, _ in items]
        assert kinds[0] == "init" and kinds.count("init") == 1
        frags = [v for k, v in items if k == "fragment"]
        assert len(frags) == 5 and all(f.data[4:8] == b"moof" for f in frags)


def test_parse_init_h264():
    init = [v for k, v in _split_all(_read("tiny_h264_frag.mp4")) if k == "init"][0]
    info = fmp4.parse_init(init)
    assert (info["width"], info["height"]) == (320, 180)
    assert info["entry"] == "avc1" and info["codec"].startswith("avc1.") and len(info["codec"]) == 11
    assert fmp4.is_h264(info) and not fmp4.is_h265(info)


def test_parse_init_h265_is_recognised():
    init = [v for k, v in _split_all(_read("tiny_hevc_frag.mp4")) if k == "init"][0]
    info = fmp4.parse_init(init)
    assert info["entry"] == "hvc1" and fmp4.is_h265(info) and not fmp4.is_h264(info)
    assert (info["width"], info["height"]) == (160, 90)


def test_fragment_sync_flag_and_timing():
    frags = [v for k, v in _split_all(_read("tiny_h264_frag.mp4")) if k == "fragment"]
    assert frags[0].sync is True
    assert any(f.sync is False for f in frags)
    assert all(f.samples == 6 for f in frags)
    assert all(f.duration_ticks > 0 for f in frags)


def test_corrupt_box_header_raises():
    with pytest.raises(fmp4.Fmp4Error):
        fmp4.Fmp4Splitter().feed(b"\x00\x00\x00\x00" + b"junk" * 10)
    with pytest.raises(fmp4.Fmp4Error):
        fmp4.Fmp4Splitter().feed(b"\x7f\xff\xff\xff" + b"moof" + b"x" * 8)


def test_parse_init_rejects_garbage():
    with pytest.raises(fmp4.Fmp4Error):
        fmp4.parse_init(b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00")


# -- relay --------------------------------------------------------------------

def _frag(sync=True, size=100):
    return fmp4.Fragment(b"m" * size, sync, 6, 6000)


def test_viewer_joins_at_newest_keyframe_with_init_first():
    relay = rs.FragmentRelay()
    relay.begin(b"INIT")
    for sync in (True, False, False, True, False):
        relay.push(_frag(sync))
    cursor = relay.open_cursor()
    out = relay.read(cursor, 0.1)
    assert out[0] == b"INIT" and len(out) == 1 + 2  # newest keyframe fragment + the one after it
    assert relay.read(cursor, 0.01) == []
    relay.push(_frag(False))
    assert len(relay.read(cursor, 0.1)) == 1


def test_viewer_waits_for_a_keyframe_not_mid_gop():
    relay = rs.FragmentRelay()
    relay.begin(b"INIT")
    relay.push(_frag(False))
    cursor = relay.open_cursor()
    assert relay.read(cursor, 0.01) == []
    relay.push(_frag(True))
    assert relay.read(cursor, 0.1)[0] == b"INIT"


def test_slow_viewer_never_grows_memory_and_resyncs_to_a_keyframe():
    relay = rs.FragmentRelay(max_fragments=5, max_bytes=10**9)
    relay.begin(b"INIT")
    relay.push(_frag(True))
    cursor = relay.open_cursor()
    relay.read(cursor, 0.1)
    for i in range(200):  # the viewer reads nothing while 200 fragments arrive
        relay.push(_frag(i % 4 == 0))
        assert relay.stats()["fragments"] <= 5
    out = relay.read(cursor, 0.1)
    assert cursor.dropped > 100
    assert len(out) <= 5 and out  # a bounded, resynchronised batch, not the backlog


def test_byte_cap_bounds_the_buffer():
    relay = rs.FragmentRelay(max_fragments=1000, max_bytes=1000)
    relay.begin(b"INIT")
    for _ in range(50):
        relay.push(_frag(True, size=300))
    assert relay.stats()["bytes"] <= 1000 and relay.stats()["fragments"] >= 1


def test_new_session_ends_old_viewers_and_close_ends_everyone():
    relay = rs.FragmentRelay()
    relay.begin(b"A")
    relay.push(_frag(True))
    cursor = relay.open_cursor()
    assert relay.read(cursor, 0.1)
    relay.begin(b"B")
    assert relay.read(cursor, 0.1) is None
    cursor2 = relay.open_cursor()
    relay.close()
    assert relay.read(cursor2, 0.1) is None


def test_reader_blocks_until_data_or_timeout():
    relay = rs.FragmentRelay()
    relay.begin(b"INIT")
    cursor = relay.open_cursor()
    started = time.monotonic()
    assert relay.read(cursor, 0.2) == []
    assert 0.15 <= time.monotonic() - started < 2
    threading.Timer(0.1, lambda: relay.push(_frag(True))).start()
    assert relay.read(cursor, 3)[0] == b"INIT"


# -- viewer limits (D2) -------------------------------------------------------

def test_viewer_limits_per_source_and_total():
    limiter = rs.ViewerLimiter(per_source=4, total=6)
    assert rs.MAX_VIEWERS_PER_SOURCE == 4 and rs.MAX_VIEWERS_TOTAL == 6
    assert [limiter.acquire("a") for _ in range(5)] == [True, True, True, True, False]
    assert limiter.acquire("b") and limiter.acquire("b")
    assert limiter.acquire("c") is False  # total of 6 reached
    limiter.release("a")
    assert limiter.acquire("c") is True
    assert limiter.count("a") == 3 and limiter.count() == 6
    for _ in range(10):
        limiter.release("zzz")  # releasing the unknown never goes negative
    assert limiter.count("zzz") == 0


# -- backoff ------------------------------------------------------------------

def test_backoff_grows_caps_and_resets_never_fast():
    backoff = rs.Backoff(rand=lambda: 0.5)  # no jitter
    delays = [backoff.next_delay() for _ in range(9)]
    assert delays[:6] == [2, 4, 8, 16, 32, 60] and delays[6:] == [60, 60, 60]
    assert min(delays) >= 2
    backoff.reset()
    assert backoff.next_delay() == 2


def test_backoff_jitter_stays_within_ten_percent():
    for rand in (0.0, 1.0):
        delay = rs.Backoff(rand=lambda r=rand: r).next_delay()
        assert 1.8 <= delay <= 2.2


# -- failure classification ---------------------------------------------------

@pytest.mark.parametrize("lines,got,code", [
    (["method DESCRIBE failed: 401 Unauthorized"], False, "auth_failed"),
    (["method DESCRIBE failed: 404 Not Found"], False, "stream_not_found"),
    (["Connection to tcp://x failed: Connection refused"], False, "unreachable"),
    (["Connection timed out"], False, "unreachable"),
    (["something odd"], True, "stream_error"),
    ([], False, "unreachable"),
])
def test_classify_failure(lines, got, code):
    assert rs.classify_failure(lines, got) == code


# -- upstream supervisor ------------------------------------------------------

def _source(**over):
    src = {"id": "ip:" + "a" * 32, "name": "Cam", "host": "192.168.2.50", "port": 554, "username": "viewer",
           "main_path": "Preview_01_main", "sub_path": "Preview_01_sub", "preferred_profile": "sub",
           "transport": "tcp", "connect_timeout_s": 5}
    src.update(over)
    return src


def _lan(host):
    return [host]


def _upstream(monkeypatch, behavior="normal", limiter=None, **kw):
    monkeypatch.setenv("FAKE_FMP4_BEHAVIOR", behavior)
    defaults = dict(
        host_validator=_lan, limiter=limiter or rs.ViewerLimiter(),
        cmd_provider=lambda: [sys.executable, FAKE],
        backoff=rs.Backoff(steps=(0.05, 0.1, 0.2), jitter=0.0),
        idle_stop_s=30.0, stable_after_s=0.3, stall_after_s=1.0,
    )
    defaults.update(kw)
    up = rs.RtspUpstream(_source(), lambda: PASSWORD, **defaults)
    return up


def _wait(predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def cleanup():
    items = []
    yield items
    for item in items:
        item.stop()


def test_upstream_goes_live_and_reports_the_stream(monkeypatch, cleanup):
    up = _upstream(monkeypatch)
    cleanup.append(up)
    up.ensure_running()
    assert up.wait_ready(15) is True
    assert _wait(lambda: up.state == "live" and up.status()["fps"])
    status = up.status()
    assert status["codec"] == "h264" and status["resolution"] == "320x180"
    assert status["mse_codec"].startswith("avc1.") and status["stream_kind"] == "fmp4"
    assert status["fps"] and status["fps"] > 5
    assert PASSWORD not in repr(status)


def test_ffmpeg_gets_a_secret_free_argv_and_the_script_on_stdin(monkeypatch, tmp_path, cleanup):
    monkeypatch.setenv("FAKE_FMP4_ARGV_DUMP", str(tmp_path / "argv.txt"))
    monkeypatch.setenv("FAKE_FMP4_STDIN_DUMP", str(tmp_path / "stdin.txt"))
    up = _upstream(monkeypatch)
    cleanup.append(up)
    up.ensure_running()
    assert up.wait_ready(15)
    argv = (tmp_path / "argv.txt").read_text()
    script = (tmp_path / "stdin.txt").read_text()
    assert PASSWORD not in argv and "192.168.2.50" not in argv and "rtsp://" not in argv
    assert "-c:v\ncopy" in argv and "pipe:0" in argv
    assert "192.168.2.50:554/Preview_01_sub" in script and PASSWORD in script
    assert script.count("file ") == 1


def test_main_profile_uses_the_main_path_and_switching_reconnects(monkeypatch, tmp_path, cleanup):
    monkeypatch.setenv("FAKE_FMP4_STDIN_DUMP", str(tmp_path / "stdin.txt"))
    up = _upstream(monkeypatch)
    cleanup.append(up)
    up.ensure_running()
    assert up.wait_ready(15)
    assert "Preview_01_sub" in (tmp_path / "stdin.txt").read_text()
    epoch = up.relay.epoch
    up.set_profile("main")
    assert _wait(lambda: up.relay.epoch != epoch and up.state == "live")
    assert _wait(lambda: "Preview_01_main" in (tmp_path / "stdin.txt").read_text())
    assert up.status()["profile"] == "main"
    with pytest.raises(ValueError):
        up.set_profile("huge")


def test_h265_is_detected_and_is_terminal_without_a_retry_loop(monkeypatch, cleanup):
    up = _upstream(monkeypatch, "hevc")
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: up.state == "unsupported_codec")
    status = up.status()
    assert status["error_code"] == "unsupported_codec" and status["codec"] == "h265"
    assert not up.relay.has_init()
    time.sleep(0.6)
    assert up.state == "unsupported_codec" and up.status()["reconnect_attempt"] == 0  # no retries


def test_wrong_password_is_terminal_not_retried(monkeypatch, cleanup):
    calls = []
    up = _upstream(monkeypatch, "auth401", spawn=lambda argv: calls.append(1) or rs.spawn_process(argv))
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: up.state == "auth_failed")
    time.sleep(0.8)
    assert len(calls) == 1 and up.state == "auth_failed"  # one attempt only: no account lockout loop


def test_reconnect_button_retries_after_a_terminal_failure(monkeypatch, cleanup):
    calls = []
    up = _upstream(monkeypatch, "auth401", spawn=lambda argv: calls.append(1) or rs.spawn_process(argv))
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: up.state == "auth_failed")
    monkeypatch.setenv("FAKE_FMP4_BEHAVIOR", "normal")
    up.reconnect()
    assert up.wait_ready(15) and len(calls) == 2


def test_unreachable_retries_with_backoff_then_recovers(monkeypatch, cleanup):
    calls = []
    up = _upstream(monkeypatch, "refused", spawn=lambda argv: calls.append(time.monotonic()) or rs.spawn_process(argv))
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: len(calls) >= 3)
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(g >= 0.04 for g in gaps) and gaps[1] >= gaps[0] * 0.9  # grows, never a fast loop
    assert up.state in ("reconnecting", "connecting") and up.status()["error_code"] in ("unreachable", "")
    monkeypatch.setenv("FAKE_FMP4_BEHAVIOR", "normal")
    assert up.wait_ready(20)
    assert _wait(lambda: up.state == "live")


def test_lost_camera_exit0_is_a_failure_and_reconnects(monkeypatch, cleanup):
    """CAM-0 gotcha: ffmpeg exits 0 when the camera disappears. Only our own stop is 'not a failure'."""
    monkeypatch.setenv("FAKE_FMP4_FRAGMENTS", "3")
    up = _upstream(monkeypatch, "exit0")
    cleanup.append(up)
    up.ensure_running()
    assert up.wait_ready(15)
    first_epoch = up.relay.epoch
    assert _wait(lambda: up.relay.epoch > first_epoch + 1, 20)  # it came back at least once on its own


def test_stall_is_detected_and_restarted(monkeypatch, cleanup):
    up = _upstream(monkeypatch, "stall", stall_after_s=0.8)
    cleanup.append(up)
    up.ensure_running()
    assert up.wait_ready(15)
    epoch = up.relay.epoch
    assert _wait(lambda: up.relay.epoch > epoch, 20)


def test_garbage_output_fails_and_retries(monkeypatch, cleanup):
    up = _upstream(monkeypatch, "garbage")
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: up.state in ("reconnecting",) or up.status()["reconnect_attempt"] > 0)


def test_blocked_address_is_terminal(monkeypatch, cleanup):
    def blocked(host):
        raise address_policy.AddressPolicyError("not_lan_address", "x")

    up = _upstream(monkeypatch, host_validator=blocked)
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: up.state == "address_blocked")


def test_missing_ffmpeg_is_reported(monkeypatch, cleanup):
    up = _upstream(monkeypatch, cmd_provider=lambda: [])
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: up.state == "ffmpeg_missing")


def test_idle_stop_ends_the_process_after_the_last_viewer(monkeypatch, cleanup):
    limiter = rs.ViewerLimiter()
    up = _upstream(monkeypatch, limiter=limiter, idle_stop_s=0.6)
    cleanup.append(up)
    up.ensure_running()
    assert up.wait_ready(15)
    assert _wait(lambda: up.state == "idle", 15)
    assert up._process is None


def test_a_viewer_keeps_the_upstream_alive(monkeypatch, cleanup):
    limiter = rs.ViewerLimiter()
    up = _upstream(monkeypatch, limiter=limiter, idle_stop_s=0.5)
    cleanup.append(up)
    limiter.acquire(up.id)
    up.ensure_running()
    assert up.wait_ready(15)
    time.sleep(1.5)
    assert up.state == "live"
    limiter.release(up.id)


def test_stop_terminates_the_process_and_leaves_no_thread(monkeypatch):
    up = _upstream(monkeypatch)
    up.ensure_running()
    assert up.wait_ready(15)
    process = up._process
    thread = up._thread
    up.stop()
    assert process.poll() is not None
    assert thread is None or not thread.is_alive()
    assert up.state == "stopped" and up.relay.closed


def test_restart_after_stop_works(monkeypatch, cleanup):
    up = _upstream(monkeypatch)
    cleanup.append(up)
    up.ensure_running()
    assert up.wait_ready(15)
    up.stop()
    up.ensure_running()
    assert up.wait_ready(15) and up.state in ("connecting", "live")


def test_stop_all_streaming_stops_every_upstream(monkeypatch):
    a, b = _upstream(monkeypatch), _upstream(monkeypatch)
    for up in (a, b):
        up.ensure_running()
        assert up.wait_ready(15)
    rs.stop_all_streaming()
    assert a.state == "stopped" and b.state == "stopped"


def test_logs_and_stderr_never_contain_the_password(monkeypatch, capsys, cleanup):
    up = _upstream(monkeypatch, "refused")
    cleanup.append(up)
    up.ensure_running()
    assert _wait(lambda: up.state in ("reconnecting",))
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert all(PASSWORD not in line for line in up._stderr_tail)
