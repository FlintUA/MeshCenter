import os
import tempfile


def test_safe_screenshot_path_traversal(server_module):
    """Verify safe_screenshot_path rejects path traversal attempts."""
    import camera.camera as camera_module
    assert camera_module.safe_screenshot_path("../etc/passwd") is None
    assert camera_module.safe_screenshot_path("..\\windows\\system32") is None
    assert camera_module.safe_screenshot_path("../../file.jpg") is None


def test_screenshot_exists_directory_rejection(server_module, monkeypatch):
    """Verify screenshot_exists returns False for directories."""
    import camera.camera as camera_module
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(camera_module, "SCREENSHOTS_DIR", tmpdir)

        # Create a directory inside SCREENSHOTS_DIR
        sub_dir = os.path.join(tmpdir, "subdir")
        os.makedirs(sub_dir, exist_ok=True)

        assert camera_module.screenshot_exists("subdir") is False


def test_delete_screenshot_directory_rejection(server_module, monkeypatch):
    """Verify delete_screenshot returns 404/error when given a directory path."""
    import camera.camera as camera_module
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(camera_module, "SCREENSHOTS_DIR", tmpdir)

        # Create a directory inside SCREENSHOTS_DIR
        sub_dir = os.path.join(tmpdir, "subdir")
        os.makedirs(sub_dir, exist_ok=True)

        res, status = camera_module.delete_screenshot("subdir")
        assert status == 404
        assert res["ok"] is False
        assert os.path.exists(sub_dir)
