"""tests/test_mime_allowlist.py

MVP MIME allowlist (Execution Plan Step 0.6 item 4; design spec section
20.2), plus the Finding 8 content-validation helpers: the filename
extension/content normalizer, the incremental full-stream text validator,
and the JSON-document validator.
"""

from __future__ import annotations

import pytest

from meshsrv.attachments.mime_allowlist import (
    TextStreamValidator,
    is_allowed_extension,
    is_allowed_mime_type,
    normalize_file_name_for_mime,
    validate_json_document,
)


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


# ---- Finding 8: filename extension/content normalization ------------------


@pytest.mark.parametrize(
    "file_name,mime_type,expected",
    [
        # already-consistent extensions are preserved, including case and the
        # multi-extension forms (.jpeg, .log).
        ("photo.jpg", "image/jpeg", "photo.jpg"),
        ("photo.jpeg", "image/jpeg", "photo.jpeg"),
        ("PHOTO.JPG", "image/jpeg", "PHOTO.JPG"),
        ("app.log", "text/plain", "app.log"),
        ("data.csv", "text/csv", "data.csv"),
        ("manifest.json", "application/json", "manifest.json"),
        ("diagram.png", "image/png", "diagram.png"),
        ("image.webp", "image/webp", "image.webp"),
        ("doc.pdf", "application/pdf", "doc.pdf"),
        # inconsistent extensions are replaced with the canonical one for the
        # *content* MIME (never the other way around).
        ("report.exe", "text/plain", "report.txt"),
        ("photo.png", "image/jpeg", "photo.jpg"),
        ("notes.md", "text/plain", "notes.txt"),
        ("data.json", "text/csv", "data.csv"),
        # extensionless names get the canonical extension appended.
        ("notes", "text/plain", "notes.txt"),
        ("passwd", "image/jpeg", "passwd.jpg"),
        # a multi-dot name replaces only the final extension.
        ("archive.tar.jpg", "image/png", "archive.tar.png"),
    ],
)
def test_normalize_file_name_for_mime(file_name, mime_type, expected):
    assert normalize_file_name_for_mime(file_name, mime_type) == expected


def test_normalize_file_name_for_unknown_mime_is_identity():
    # An unknown MIME must never be rewritten (the caller should not reach
    # here, but fail-closed to identity rather than mutating the name).
    assert normalize_file_name_for_mime("weird.bin", "application/x-unknown") == "weird.bin"


# ---- Finding 8: incremental full-stream text validation -------------------


def test_text_stream_validator_accepts_clean_utf8_text():
    v = TextStreamValidator()
    v.feed(b"hello world\n")
    v.feed("café — 日本語".encode("utf-8"))
    assert v.finish() is True


def test_text_stream_validator_rejects_nul_in_a_later_chunk():
    v = TextStreamValidator()
    v.feed(b"a" * 512)          # a clean head...
    v.feed(b"tail\x00tail")     # ...then a NUL after byte 512
    assert v.finish() is False


def test_text_stream_validator_rejects_invalid_utf8_in_a_later_chunk():
    v = TextStreamValidator()
    v.feed(b"a" * 512)
    v.feed(b"\xff\xfe")         # invalid UTF-8, not just a split sequence
    assert v.finish() is False


def test_text_stream_validator_handles_multibyte_split_across_chunks():
    v = TextStreamValidator()
    v.feed("café".encode("utf-8")[:3])   # "caf" + the leading byte of é
    v.feed("café".encode("utf-8")[3:])   # the trailing byte of é
    assert v.finish() is True            # split is not a decode error


def test_text_stream_validator_rejects_trailing_incomplete_utf8():
    v = TextStreamValidator()
    v.feed("caf".encode("utf-8"))
    v.feed(b"\xc3")             # a lone continuation-leading byte at EOF
    assert v.finish() is False


def test_text_stream_validator_finish_is_idempotent():
    v = TextStreamValidator()
    v.feed(b"ok")
    assert v.finish() is True
    assert v.finish() is True


# ---- Finding 8: full JSON-document validation ------------------------------


def test_validate_json_document_accepts_complete_object(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"a": [1, 2, 3], "b": null}', encoding="utf-8")
    validate_json_document(p)  # no raise


def test_validate_json_document_accepts_complete_array(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    validate_json_document(p)


@pytest.mark.parametrize("content", [
    '{"broken": ',       # leading {, truncated
    "[1, 2,",            # leading [, truncated
    "{not json}",
    '{"a": 1} trailing', # valid doc followed by trailing garbage
    "",
    "   ",
])
def test_validate_json_document_rejects_invalid(tmp_path, content):
    p = tmp_path / "bad.json"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        validate_json_document(p)
