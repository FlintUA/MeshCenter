"""Tests for camera/usb_driver.py (CAM-1) - the ffmpeg-subprocess-based
USB camera driver that replaces the old linuxpy/v4l2py in-process one.

Process-lifecycle tests (start/stop/failure-detection/reaping) run
against tests/fixtures/fake_ffmpeg.py, a real Python subprocess standing
in for ffmpeg - these exercise real OS pipes, real signals, real process
reaping, not mocks, the same way tests/test_adapter_ipc_client.py does
for the meshtastic adapter's own subprocess.

v4l2-ctl is mocked at the _run_v4l2_ctl() seam instead (real fixture TEXT
is already covered by tests/test_v4l2_ctl_parse.py against real captures
from the Microsoft USB3.0 HD CAMERA - no need for a fake v4l2-ctl binary
too).
"""

import os
import sys
import threading
import time

import pytest

import camera.usb_driver as usb_driver

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FAKE_FFMPEG_PATH = os.path.join(FIXTURES_DIR, "fake_ffmpeg.py")
V4L2_FIXTURES_DIR = os.path.join(FIXTURES_DIR, "v4l2")


def _read_fixture(name: str) -> str:
    with open(os.path.join(V4L2_FIXTURES_DIR, name), encoding="utf-8") as f:
        return f.read()


INFO_TEXT = _read_fixture("microsoft_usb3_hd_camera_video0_info.txt")
FORMATS_TEXT = _read_fixture("microsoft_usb3_hd_camera_list_formats_ext.txt")
CTRLS_TEXT = _read_fixture("microsoft_usb3_hd_camera_list_ctrls.txt")

DEV_PATH = "/dev/video0"


def _fake_run_v4l2_ctl(dev_path, *args, timeout=5.0):
    if "--info" in args:
        return INFO_TEXT
    if "--list-formats-ext" in args:
        return FORMATS_TEXT
    if "--list-ctrls" in args:
        return CTRLS_TEXT
    return ""


@pytest.fixture
def driver(monkeypatch):
    """A UsbCameraDriver wired to the real fake_ffmpeg.py subprocess and
    a mocked v4l2-ctl seam reflecting the real Microsoft USB3.0 HD CAMERA
    (YUYV-only) fixture captures. FAKE_FFMPEG_BEHAVIOR/FPS/etc. are set
    per-test via monkeypatch.setenv - default here is a fast, well-behaved
    stream so tests that don't care about a specific failure mode don't
    need their own setup."""
    monkeypatch.setattr(usb_driver, "FFMPEG_CMD_PREFIX", [sys.executable, FAKE_FFMPEG_PATH])
    monkeypatch.setattr(usb_driver, "V4L2_CTL_PATH", "fake-v4l2-ctl")
    monkeypatch.setattr(usb_driver, "_run_v4l2_ctl", _fake_run_v4l2_ctl)
    monkeypatch.setattr(usb_driver, "_pid_holding_device", lambda dev_path: None)
    monkeypatch.setattr(usb_driver, "_low_power_host", lambda: False)

    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: True if str(p) == DEV_PATH else real_exists(p))

    monkeypatch.setenv("FAKE_FFMPEG_BEHAVIOR", "normal")
    monkeypatch.setenv("FAKE_FFMPEG_FPS", "30")
    monkeypatch.setenv("FAKE_FFMPEG_CHUNK_SIZE", "37")

    d = usb_driver.UsbCameraDriver(dev_path=DEV_PATH, card_name="USB3.0 HD CAMERA")
    yield d
    d.stop()


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------

def test_detect_returns_metadata_when_tools_and_device_present(driver):
    info = driver.detect()
    assert info is not None
    assert info["model"] == "USB3.0 HD CAMERA"
    assert info["dev_path"] == DEV_PATH


def test_detect_returns_none_when_device_missing(driver, monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert driver.detect() is None


def test_detect_returns_none_when_ffmpeg_missing(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "FFMPEG_CMD_PREFIX", [])
    assert driver.detect() is None


def test_detect_returns_none_when_v4l2ctl_missing(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "V4L2_CTL_PATH", None)
    assert driver.detect() is None


def test_detect_does_not_raise_when_tools_missing(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "FFMPEG_CMD_PREFIX", [])
    monkeypatch.setattr(usb_driver, "V4L2_CTL_PATH", None)
    driver.detect()  # must not raise


# ---------------------------------------------------------------------------
# Streaming lifecycle - frames, chunk splitting
# ---------------------------------------------------------------------------

def test_start_produces_a_complete_valid_frame(driver, monkeypatch):
    """FAKE_FFMPEG_CHUNK_SIZE=7 forces the canned ~630-byte JPEG to be
    split across many reads, none aligned to the frame boundary - proves
    the reader correctly reassembles it."""
    monkeypatch.setenv("FAKE_FFMPEG_CHUNK_SIZE", "7")
    assert driver.start(resolution="1280x720") is True

    assert _wait_until(lambda: driver._last_frame is not None)
    frame = driver._last_frame
    assert frame[:2] == b"\xff\xd8"
    assert frame[-2:] == b"\xff\xd9"
    assert usb_driver._is_valid_jpeg(frame)


def test_stream_mjpeg_yields_frames(driver):
    assert driver.start(resolution="1280x720") is True
    gen = driver.stream_mjpeg()
    frame = next(gen)
    assert frame[:2] == b"\xff\xd8"
    gen.close()


def test_start_from_a_short_lived_thread_does_not_kill_the_process(driver):
    """PR #314 review finding, live-reproduced on camtest: PR_SET_PDEATHSIG
    is per-THREAD (man 2 prctl) - the kernel delivers the death signal when
    the specific thread that called fork() exits, not when the whole
    parent PROCESS does. Calling start() (and therefore spawning ffmpeg)
    from a short-lived thread used to arm PDEATHSIG against that thread's
    own lifetime: the moment the thread returned, the kernel killed
    ffmpeg - independent of whether Core itself was still running. This
    "worked" under gunicorn's gthread pool purely because its worker
    threads happen to be long-lived; it broke under Werkzeug's dev server
    (`python server.py`), where a request can land on a fresh, short-lived
    thread. Fixed by routing every spawn through one persistent spawner
    thread (see _spawn_ffmpeg()) - this test proves the fix by doing
    exactly what broke it: starting the stream from a thread that then
    immediately exits, and confirming the process and its frames survive
    well past that."""
    started_ok = {}

    def start_and_exit():
        started_ok["result"] = driver.start(resolution="1280x720")

    thread = threading.Thread(target=start_and_exit)
    thread.start()
    thread.join()  # the spawning thread is now gone

    assert started_ok["result"] is True
    process = driver._process
    assert process is not None

    # On Linux this is the actual PR_SET_PDEATHSIG regression check; on
    # other platforms (no PDEATHSIG at all - see _popen_ffmpeg()'s
    # platform guard) this still verifies the queue/future spawn
    # mechanism itself introduces no regression.
    time.sleep(1.0)
    assert process.poll() is None, "ffmpeg was killed after its spawning thread exited"
    assert _wait_until(lambda: driver._last_frame is not None), "no frames after the spawning thread exited"


# ---------------------------------------------------------------------------
# Failure detection - the CAM-0 exit-code finding
# ---------------------------------------------------------------------------

def test_unplug_exit_0_is_detected_as_a_failure_not_a_normal_stop(driver, monkeypatch):
    """CAM-0's own live finding: real ffmpeg exits 0 when the camera is
    unplugged mid-stream - exit code alone must NOT be read as success."""
    monkeypatch.setenv("FAKE_FFMPEG_BEHAVIOR", "exit0")
    monkeypatch.setenv("FAKE_FFMPEG_FRAME_COUNT", "2")
    monkeypatch.setenv("FAKE_FFMPEG_FPS", "30")

    assert driver.start(resolution="1280x720") is True
    assert _wait_until(lambda: driver._available is False, timeout=5.0)
    assert driver._stop_requested is False
    assert driver.get_status()["ok"] is False


def test_sigterm_stop_is_not_logged_as_a_failure(driver, capsys, monkeypatch):
    """A deliberate stop() must never be reported the same way an
    unplug/crash is."""
    monkeypatch.setenv("FAKE_FFMPEG_FPS", "5")  # slow enough that stop() clearly interrupts an active stream

    assert driver.start(resolution="1280x720") is True
    assert _wait_until(lambda: driver._last_frame is not None)

    driver.stop()

    assert driver._available is True
    assert driver._started is False
    out = capsys.readouterr().out
    assert "exited unexpectedly" not in out
    assert "stalled" not in out


def test_stall_is_detected_within_the_configured_timeout(driver, monkeypatch):
    """An unplug can hang instead of exiting on some kernels (CAM-1's own
    design note) - the stall watchdog must catch that even though the
    process never exits on its own."""
    monkeypatch.setattr(usb_driver, "STALL_MIN_SECONDS", 0.5)
    monkeypatch.setattr(usb_driver, "STALL_FRAME_INTERVALS", 1)
    monkeypatch.setenv("FAKE_FFMPEG_BEHAVIOR", "stall")
    monkeypatch.setenv("FAKE_FFMPEG_STALL_FRAMES", "1")
    monkeypatch.setenv("FAKE_FFMPEG_FPS", "30")

    assert driver.start(resolution="1280x720") is True
    assert _wait_until(lambda: driver._last_frame is not None)

    assert _wait_until(lambda: driver._available is False, timeout=5.0)
    # The stalled process must actually be killed, not just marked unavailable.
    process = driver._process
    assert process is None or process.poll() is not None


def test_corrupt_stream_with_no_eoi_triggers_the_buffer_cap_path(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "FRAME_BUFFER_CAP_BYTES", 2048)
    monkeypatch.setenv("FAKE_FFMPEG_BEHAVIOR", "garbage")
    monkeypatch.setenv("FAKE_FFMPEG_CHUNK_SIZE", "512")

    assert driver.start(resolution="1280x720") is True
    assert _wait_until(lambda: driver._available is False, timeout=5.0)


# ---------------------------------------------------------------------------
# Reaping - no zombies
# ---------------------------------------------------------------------------

def test_stop_reaps_the_process_no_zombie(driver):
    assert driver.start(resolution="1280x720") is True
    process = driver._process
    assert process is not None
    assert process.poll() is None  # actually running

    driver.stop()

    assert process.poll() is not None  # reaped, not a zombie


# ---------------------------------------------------------------------------
# Photo capture - spawn counting
# ---------------------------------------------------------------------------

def _install_spawn_counters(monkeypatch):
    """_popen_ffmpeg() is the one real subprocess.Popen() choke point for
    BOTH the streaming and one-shot-photo paths (PR #314 review fix: both
    now go through the persistent spawner thread) - classify each call by
    argv content (`-frames:v` only appears in a one-shot capture) rather
    than patching two different call sites."""
    stream_spawns = []
    oneshot_calls = []
    real_popen = usb_driver._popen_ffmpeg

    def counting_popen(argv):
        (oneshot_calls if "-frames:v" in argv else stream_spawns).append(argv)
        return real_popen(argv)

    monkeypatch.setattr(usb_driver, "_popen_ffmpeg", counting_popen)
    return stream_spawns, oneshot_calls


def test_photo_at_same_resolution_spawns_no_new_process(driver, monkeypatch):
    stream_spawns, oneshot_calls = _install_spawn_counters(monkeypatch)

    assert driver.start(resolution="1280x720") is True
    assert _wait_until(lambda: driver._last_frame is not None)
    assert len(stream_spawns) == 1

    photo = driver.capture_photo(resolution="1280x720")

    assert photo[:2] == b"\xff\xd8"
    assert len(stream_spawns) == 1  # no new stream process
    assert len(oneshot_calls) == 0  # no one-shot process either


def test_photo_at_different_resolution_spawns_one_oneshot_and_one_restart(driver, monkeypatch):
    stream_spawns, oneshot_calls = _install_spawn_counters(monkeypatch)

    assert driver.start(resolution="1280x720") is True
    assert _wait_until(lambda: driver._last_frame is not None)
    assert len(stream_spawns) == 1

    photo = driver.capture_photo(resolution="1920x1080")

    assert photo[:2] == b"\xff\xd8"
    assert usb_driver._is_valid_jpeg(photo)
    assert len(oneshot_calls) == 1
    assert len(stream_spawns) == 2  # original start + the restart after the photo
    assert driver._started is True
    assert driver._resolution == "1280x720"  # restored


def test_photo_cold_start_starts_the_persistent_stream_directly(driver, monkeypatch):
    """No stream running at all yet - must not do a redundant one-shot
    capture, just start the persistent stream at the target resolution."""
    stream_spawns, oneshot_calls = _install_spawn_counters(monkeypatch)

    photo = driver.capture_photo(resolution="1280x720")

    assert photo[:2] == b"\xff\xd8"
    assert len(oneshot_calls) == 0
    assert len(stream_spawns) == 1
    assert driver._started is True
    assert driver._resolution == "1280x720"


# ---------------------------------------------------------------------------
# Idle watchdog
# ---------------------------------------------------------------------------

def test_idle_watchdog_stops_the_process_after_idle_stop_seconds(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "IDLE_STOP_SECONDS", 0.2)
    monkeypatch.setattr(usb_driver, "IDLE_WATCHDOG_POLL_SECONDS", 0.1)

    assert driver.start(resolution="1280x720") is True
    process = driver._process
    assert process is not None

    assert _wait_until(lambda: driver._started is False, timeout=5.0)
    # stop() sets _started=False before the OS-level terminate+reap
    # completes - give that a moment to actually finish.
    assert _wait_until(lambda: process.poll() is not None, timeout=5.0)


def test_active_consumer_prevents_idle_stop(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "IDLE_STOP_SECONDS", 0.2)
    monkeypatch.setattr(usb_driver, "IDLE_WATCHDOG_POLL_SECONDS", 0.1)

    assert driver.start(resolution="1280x720") is True
    with driver._activity_lock:
        driver._active_consumers = 1  # simulate an open stream_mjpeg() viewer

    time.sleep(0.6)
    assert driver._started is True


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

def test_get_controls_reflects_the_real_camera_fixture(driver):
    controls = driver.get_controls()
    assert controls["brightness"] == 7
    assert controls["power_line_frequency"] == 1


def test_set_controls_applies_while_streaming(driver, monkeypatch):
    calls = []
    real_v4l2 = usb_driver._run_v4l2_ctl

    def spying_v4l2(dev_path, *args, timeout=5.0):
        calls.append(args)
        return real_v4l2(dev_path, *args, timeout=timeout)

    monkeypatch.setattr(usb_driver, "_run_v4l2_ctl", spying_v4l2)

    assert driver.start(resolution="1280x720") is True
    ok = driver.set_controls({"brightness": 3})

    assert ok is True
    assert any("--set-ctrl" in call for call in calls)


def test_set_controls_false_when_v4l2ctl_missing(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "V4L2_CTL_PATH", None)
    assert driver.set_controls({"brightness": 3}) is False


# ---------------------------------------------------------------------------
# list_resolutions()
# ---------------------------------------------------------------------------

def test_list_resolutions_reflects_the_real_camera_fixture(driver):
    resolutions = driver.list_resolutions()
    assert "1280x720" in resolutions
    assert "1920x1080" in resolutions


def test_list_resolutions_falls_back_to_confirmed_list_on_probe_failure(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "_run_v4l2_ctl", lambda *a, **k: "")
    assert driver.list_resolutions() == usb_driver.CONFIRMED_RESOLUTIONS


# ---------------------------------------------------------------------------
# Startup sweep - device busy detection
# ---------------------------------------------------------------------------

def test_start_refuses_when_device_is_held_by_another_pid(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "_pid_holding_device", lambda dev_path: 999999)
    assert driver.start(resolution="1280x720") is False
    assert driver._started is False


def test_start_proceeds_when_device_is_held_by_our_own_pid(driver, monkeypatch):
    monkeypatch.setattr(usb_driver, "_pid_holding_device", lambda dev_path: os.getpid())
    assert driver.start(resolution="1280x720") is True


# ---------------------------------------------------------------------------
# discover_usb_cameras()
# ---------------------------------------------------------------------------

def test_discover_usb_cameras_finds_the_capture_node(monkeypatch, tmp_path):
    monkeypatch.setattr(usb_driver, "_run_v4l2_ctl", _fake_run_v4l2_ctl)
    monkeypatch.setattr(usb_driver.glob, "glob", lambda pattern: ["/dev/video0"] if pattern == "/dev/video*" else [])
    monkeypatch.setattr(usb_driver, "_usb_ids_for_video_device", lambda dev_path: ("045e", "8888"))

    found = usb_driver.discover_usb_cameras()

    assert len(found) == 1
    assert found[0]["dev_path"] == "/dev/video0"
    assert found[0]["card"] == "USB3.0 HD CAMERA"
    assert found[0]["vendor_id"] == "045e"
    assert found[0]["formats"] == {"YUYV"}


def test_discover_usb_cameras_skips_metadata_only_nodes(monkeypatch):
    metadata_info = _read_fixture("microsoft_usb3_hd_camera_video1_metadata_info.txt")

    def fake_run(dev_path, *args, timeout=5.0):
        if dev_path == "/dev/video1":
            return metadata_info if "--info" in args else ""
        return _fake_run_v4l2_ctl(dev_path, *args, timeout=timeout)

    monkeypatch.setattr(usb_driver, "_run_v4l2_ctl", fake_run)
    monkeypatch.setattr(usb_driver.glob, "glob", lambda pattern: ["/dev/video0", "/dev/video1"])
    monkeypatch.setattr(usb_driver, "_usb_ids_for_video_device", lambda dev_path: ("045e", "8888"))

    found = usb_driver.discover_usb_cameras()

    assert [f["dev_path"] for f in found] == ["/dev/video0"]


def test_discover_usb_cameras_skips_devices_without_usb_ids(monkeypatch):
    """Excludes platform devices (e.g. bcm2835-isp) that have no real USB
    vendor/product id."""
    monkeypatch.setattr(usb_driver, "_run_v4l2_ctl", _fake_run_v4l2_ctl)
    monkeypatch.setattr(usb_driver.glob, "glob", lambda pattern: ["/dev/video0"])
    monkeypatch.setattr(usb_driver, "_usb_ids_for_video_device", lambda dev_path: ("", ""))

    assert usb_driver.discover_usb_cameras() == []


# ---------------------------------------------------------------------------
# ffmpeg_and_v4l2ctl_available()
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _run_v4l2_ctl() - retry on timeout (live-caught on camtest, 2026-09-30)
# ---------------------------------------------------------------------------

def test_run_v4l2_ctl_retries_once_on_timeout(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise usb_driver.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        return usb_driver.subprocess.CompletedProcess(argv, 0, stdout="ok output", stderr="")

    monkeypatch.setattr(usb_driver, "V4L2_CTL_PATH", "/usr/bin/v4l2-ctl")
    monkeypatch.setattr(usb_driver.subprocess, "run", fake_run)

    result = usb_driver._run_v4l2_ctl("/dev/video0", "--info")

    assert result == "ok output"
    assert len(calls) == 2


def test_run_v4l2_ctl_gives_up_after_second_timeout(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise usb_driver.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(usb_driver, "V4L2_CTL_PATH", "/usr/bin/v4l2-ctl")
    monkeypatch.setattr(usb_driver.subprocess, "run", fake_run)

    result = usb_driver._run_v4l2_ctl("/dev/video0", "--info")

    assert result == ""
    assert len(calls) == 2


def test_ffmpeg_and_v4l2ctl_available_reflects_module_state(driver, monkeypatch):
    assert usb_driver.ffmpeg_and_v4l2ctl_available() is True
    monkeypatch.setattr(usb_driver, "FFMPEG_CMD_PREFIX", [])
    assert usb_driver.ffmpeg_and_v4l2ctl_available() is False
