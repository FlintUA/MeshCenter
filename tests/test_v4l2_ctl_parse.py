"""Tests for camera/v4l2_ctl_parse.py (CAM-1) - the pure-function v4l2-ctl
text parsers that replace linuxpy/v4l2py's structured API. Fixtures under
tests/fixtures/v4l2/ are real `v4l2-ctl` output captured live from the
one camera CAM-1 was verified against (Microsoft USB3.0 HD CAMERA,
045e:8888, YUYV-only, camtest .107) - not synthesized by hand.

The E3500/C170 are not in scope (see CAM-1 task's own scope decision,
2026-09-30) - add fixtures for those if/when that hardware comes back.
"""

import os

import pytest

import camera.v4l2_ctl_parse as v4l2_parse

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "v4l2")


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def video0_info():
    return _read_fixture("microsoft_usb3_hd_camera_video0_info.txt")


@pytest.fixture
def video1_metadata_info():
    return _read_fixture("microsoft_usb3_hd_camera_video1_metadata_info.txt")


@pytest.fixture
def list_formats_ext():
    return _read_fixture("microsoft_usb3_hd_camera_list_formats_ext.txt")


@pytest.fixture
def list_ctrls():
    return _read_fixture("microsoft_usb3_hd_camera_list_ctrls.txt")


@pytest.fixture
def malformed_list_formats_ext():
    return _read_fixture("malformed_truncated_list_formats_ext.txt")


# ---------------------------------------------------------------------------
# parse_device_caps() / parse_card_name()
# ---------------------------------------------------------------------------

def test_parse_device_caps_capture_node_has_video_capture(video0_info):
    caps = v4l2_parse.parse_device_caps(video0_info)
    assert "Video Capture" in caps
    assert "Streaming" in caps
    assert "Extended Pix Format" in caps


def test_parse_device_caps_metadata_node_lacks_video_capture(video1_metadata_info):
    """This is the exact distinction discover_usb_cameras() relies on to
    tell a real capture node (video0) apart from its own paired
    metadata-only node (video1) - both report the same aggregate
    'Capabilities:' value, only 'Device Caps:' differs."""
    caps = v4l2_parse.parse_device_caps(video1_metadata_info)
    assert "Video Capture" not in caps
    assert "Metadata Capture" in caps


def test_parse_device_caps_empty_text_returns_empty_set():
    assert v4l2_parse.parse_device_caps("") == set()


def test_parse_device_caps_garbage_text_returns_empty_set():
    assert v4l2_parse.parse_device_caps("not v4l2-ctl output at all\nrandom\n") == set()


def test_parse_card_name(video0_info):
    assert v4l2_parse.parse_card_name(video0_info) == "USB3.0 HD CAMERA"


def test_parse_card_name_missing_returns_empty_string():
    assert v4l2_parse.parse_card_name("Driver Info:\n\tDriver name: uvcvideo\n") == ""


# ---------------------------------------------------------------------------
# parse_list_formats_ext()
# ---------------------------------------------------------------------------

def test_parse_list_formats_ext_real_camera(list_formats_ext):
    formats = v4l2_parse.parse_list_formats_ext(list_formats_ext)

    assert set(formats.keys()) == {"YUYV"}
    sizes = formats["YUYV"]
    assert {"width": 1920, "height": 1080, "fps": [30.0, 25.0]} in sizes
    assert {"width": 1280, "height": 720, "fps": [60.0, 50.0, 30.0, 25.0]} in sizes
    assert len(sizes) == 2


def test_parse_list_formats_ext_no_mjpeg_for_this_camera(list_formats_ext):
    """Confirms CAM-0's live finding: this exact camera has no MJPEG mode
    at all - the parser must not fabricate one."""
    formats = v4l2_parse.parse_list_formats_ext(list_formats_ext)
    assert "MJPEG" not in formats


def test_parse_list_formats_ext_empty_text_returns_empty_dict():
    assert v4l2_parse.parse_list_formats_ext("") == {}


def test_parse_list_formats_ext_marker_absent_returns_empty_dict():
    assert v4l2_parse.parse_list_formats_ext("ioctl: VIDIOC_ENUM_FMT\n\tType: Video Capture\n") == {}


def test_parse_list_formats_ext_truncated_output_does_not_raise(malformed_list_formats_ext):
    """A radio/camera-equivalent timeout mid-print (cut off mid-line, here
    missing the interval line's closing paren) must degrade gracefully -
    the well-formed size entry survives, the truncated interval is simply
    dropped, never an exception."""
    formats = v4l2_parse.parse_list_formats_ext(malformed_list_formats_ext)
    assert formats == {"YUYV": [{"width": 1920, "height": 1080, "fps": []}]}


# ---------------------------------------------------------------------------
# parse_list_ctrls()
# ---------------------------------------------------------------------------

def test_parse_list_ctrls_real_camera_int_control(list_ctrls):
    controls = v4l2_parse.parse_list_ctrls(list_ctrls)
    assert controls["brightness"] == {
        "type": "int", "min": 1, "max": 15, "step": 1,
        "default": 7, "value": 7, "menu_label": None,
    }


def test_parse_list_ctrls_real_camera_menu_control(list_ctrls):
    controls = v4l2_parse.parse_list_ctrls(list_ctrls)
    assert controls["power_line_frequency"]["type"] == "menu"
    assert controls["power_line_frequency"]["value"] == 1
    assert controls["power_line_frequency"]["menu_label"] == "50 Hz"


def test_parse_list_ctrls_covers_both_sections(list_ctrls):
    """User Controls and Camera Controls section headers are skipped, but
    controls from both sections end up in the one flat namespace."""
    controls = v4l2_parse.parse_list_ctrls(list_ctrls)
    assert "brightness" in controls  # User Controls
    assert "zoom_absolute" in controls  # Camera Controls
    assert len(controls) == 10


def test_parse_list_ctrls_empty_text_returns_empty_dict():
    assert v4l2_parse.parse_list_ctrls("") == {}


def test_parse_list_ctrls_garbage_text_returns_empty_dict():
    assert v4l2_parse.parse_list_ctrls("nothing resembling a control line\n") == {}


# ---------------------------------------------------------------------------
# ffmpeg_and_v4l2ctl_available()
# ---------------------------------------------------------------------------

def test_ffmpeg_and_v4l2ctl_available_true_when_both_found():
    assert v4l2_parse.ffmpeg_and_v4l2ctl_available("/usr/bin/ffmpeg", "/usr/bin/v4l2-ctl") is True


def test_ffmpeg_and_v4l2ctl_available_false_when_ffmpeg_missing():
    assert v4l2_parse.ffmpeg_and_v4l2ctl_available(None, "/usr/bin/v4l2-ctl") is False


def test_ffmpeg_and_v4l2ctl_available_false_when_v4l2ctl_missing():
    assert v4l2_parse.ffmpeg_and_v4l2ctl_available("/usr/bin/ffmpeg", None) is False


def test_ffmpeg_and_v4l2ctl_available_false_when_both_missing():
    assert v4l2_parse.ffmpeg_and_v4l2ctl_available(None, None) is False
