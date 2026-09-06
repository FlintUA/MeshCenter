"""tests/test_mime_allowlist.py

MVP MIME allowlist (Execution Plan Step 0.6 item 4; design spec section
20.2).
"""

from __future__ import annotations

import pytest

from meshsrv.attachments.mime_allowlist import is_allowed_extension, is_allowed_mime_type


@pytest.mark.parametrize(
    "mime_type",
    ["image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain", "text/csv", "application/json"],
)
def test_allowed_mime_types(mime_type):
    assert is_allowed_mime_type(mime_type) is True


@pytest.mark.parametrize(
    "mime_type",
    [
        "application/x-msdownload",
        "application/zip",
        "video/mp4",
        "audio/mpeg",
        "application/octet-stream",
        "text/html",
        "image/svg+xml",  # SVG can carry script content - deliberately not on the MVP list
    ],
)
def test_disallowed_mime_types(mime_type):
    assert is_allowed_mime_type(mime_type) is False


@pytest.mark.parametrize("file_name", ["photo.jpg", "PHOTO.JPG", "readout.LOG", "data.csv", "manifest.json"])
def test_allowed_extensions(file_name):
    assert is_allowed_extension(file_name) is True


@pytest.mark.parametrize("file_name", ["installer.exe", "archive.zip", "video.mp4", "noextension"])
def test_disallowed_extensions(file_name):
    assert is_allowed_extension(file_name) is False
