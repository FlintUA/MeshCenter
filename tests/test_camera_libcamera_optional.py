"""Tests for camera/camera.py's libcamera import guard.

server.py imports camera.camera unconditionally at startup (and
camera/csi_driver.py imports it too, for the driver registry built by
camera_manager.py) - on a device with no libcamera Python bindings at all
(a USB-only setup, or non-Pi hardware like the Droidian node this was
found on), the bare `from libcamera import Transform, controls` used to
crash the whole process before init_camera()'s own, already-existing
graceful "no camera" handling ever got a chance to run. This is a
different failure mode from "camera hardware not detected" (already
handled at runtime) - an ImportError here happens at *module* import
time, before any runtime code executes.

Forces the ImportError with sys.modules["libcamera"] = None (the
standard way to make `import libcamera` raise ImportError regardless of
whether it's actually installed on the machine running this test) -
deliberately the OPPOSITE of conftest.py's _stub_libcamera(), which
fakes libcamera as *present*. Restores sys.modules afterward so it
doesn't leak into other tests.
"""
import sys

import pytest


@pytest.fixture
def libcamera_absent():
    """Forces a fresh import of camera.camera to see libcamera as absent,
    then restores prior sys.modules state (both for "libcamera" and
    "camera.camera") so later tests get their normal, working imports."""
    saved_libcamera = sys.modules.get("libcamera", "__unset__")
    saved_camera_camera = sys.modules.pop("camera.camera", None)

    sys.modules["libcamera"] = None
    try:
        import camera.camera as camera_module
        yield camera_module
    finally:
        del sys.modules["camera.camera"]
        if saved_libcamera == "__unset__":
            sys.modules.pop("libcamera", None)
        else:
            sys.modules["libcamera"] = saved_libcamera
        if saved_camera_camera is not None:
            sys.modules["camera.camera"] = saved_camera_camera
        else:
            # Re-import normally (libcamera stubbed present again by
            # conftest's own _stub_libcamera(), or genuinely absent if
            # this suite never ran that fixture) so later tests see a
            # working module either way.
            import camera.camera  # noqa: F401


def test_import_does_not_raise_when_libcamera_is_absent(libcamera_absent):
    assert libcamera_absent.LIBCAMERA_AVAILABLE is False


def test_transform_and_controls_maps_are_empty_when_libcamera_is_absent(libcamera_absent):
    assert libcamera_absent.Transform is None
    assert libcamera_absent.libcamera_controls is None
    assert libcamera_absent.AWB_MODE_MAP == {}
    assert libcamera_absent.EXPOSURE_MODE_MAP == {}
    assert libcamera_absent.NOISE_REDUCTION_MAP == {}


def test_get_camera_transform_returns_none_when_libcamera_is_absent(libcamera_absent):
    assert libcamera_absent.get_camera_transform() is None


def test_build_camera_controls_returns_empty_dict_when_libcamera_is_absent(libcamera_absent):
    assert libcamera_absent.build_camera_controls() == {}
