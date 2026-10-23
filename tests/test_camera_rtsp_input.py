"""CAM2: building the ffmpeg input without the password on a command line (camera/rtsp_input.py).

The pure-function tests run everywhere. The live test needs a real `ffmpeg`
binary (it is skipped when there is none, e.g. a minimal CI image): it starts
ffmpeg against the fake RTSP camera in tests/fixtures/fake_rtsp_server.py with
the secret-free argv, proves the stream really opens with the credentials
(the fake server only accepts the right user/password), and proves the
process's own argv contains neither the password nor the host.
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

from camera import rtsp_input

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
from fake_rtsp_server import FakeRtspServer  # noqa: E402

USER = "viewer"
PASSWORD = "p@ss:w/rd'\"\\ x%1"  # hostile characters, made up


def test_url_percent_encodes_credentials_and_path():
    url = rtsp_input.build_rtsp_url("192.168.2.50", 554, "/Preview_01_sub", USER, PASSWORD)
    assert url.startswith("rtsp://viewer:")
    assert "@192.168.2.50:554/Preview_01_sub" in url
    for bad in ("'", '"', "\\", " ", "\n"):
        assert bad not in url
    assert "p%40ss%3Aw%2Frd%27%22%5C%20x%251" in url


def test_url_without_credentials_and_with_query_and_ipv6():
    assert rtsp_input.build_rtsp_url("10.0.0.2", 8554, "live?channel=1&subtype=1") == "rtsp://10.0.0.2:8554/live?channel=1&subtype=1"
    assert rtsp_input.build_rtsp_url("fd12::5", 554, "x") == "rtsp://[fd12::5]:554/x"
    assert rtsp_input.build_rtsp_url("fe80::1%eth0", 554, "x") == "rtsp://[fe80::1%25eth0]:554/x"
    assert "'" not in rtsp_input.build_rtsp_url("10.0.0.2", 554, "a'b")


def test_concat_script_shape():
    url = rtsp_input.build_rtsp_url("192.168.2.50", 554, "x", USER, PASSWORD)
    script = rtsp_input.build_concat_script(url, "tcp", 7).decode()
    lines = script.splitlines()
    assert lines[0] == "ffconcat version 1.0"
    assert lines[1] == f"file '{url}'"
    # the option lines must come AFTER the file line (they apply to the previous file)
    assert lines[2] == "option rtsp_transport tcp"
    assert lines[3] == "option timeout 7000000"


@pytest.mark.parametrize("url", ["rtsp://h/a'b", "rtsp://h/a\nb", "rtsp://h/a\\b", "rtsp://h/a b", "rtsp://h/\x00"])
def test_concat_script_refuses_unquotable_urls(url):
    with pytest.raises(ValueError):
        rtsp_input.build_concat_script(url)


def test_concat_script_refuses_bad_transport():
    with pytest.raises(ValueError):
        rtsp_input.build_concat_script("rtsp://h/x", "sctp")


def test_argv_fragment_contains_no_url_or_secret():
    argv = rtsp_input.ffmpeg_input_args()
    assert "pipe:0" in argv and "concat" in argv
    assert not any("rtsp://" in a for a in argv)


@pytest.mark.parametrize("path,ok", [
    ("Preview_01_sub", True), ("live/ch0?subtype=1", True), ("", True),
    ("a b", False), ("a'b", False), ("a\\b", False), ("a#b", False), ("//x", False),
    ("rtsp://x", False), ("a/../b", False), ("a\nb", False), ("x" * 300, False),
])
def test_valid_stream_path(path, ok):
    assert rtsp_input.valid_stream_path(path) is ok


def test_redact_removes_urls_and_secrets():
    text = "failed rtsp://viewer:topsecret@192.168.2.50:554/x and http://u:topsecret@h/y plus topsecret again"
    out = rtsp_input.redact(text, "topsecret")
    assert "topsecret" not in out and "192.168.2.50" not in out
    assert rtsp_input.redact("pw is a%40b", "a@b") == "pw is <redacted>"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs a real ffmpeg binary")
@pytest.mark.parametrize("secret", ["plainpw123", PASSWORD])
def test_live_stream_opens_and_argv_has_no_secret(secret):
    server = FakeRtspServer(user=USER, password=secret).start()
    try:
        url = rtsp_input.build_rtsp_url("127.0.0.1", server.port, "live", USER, secret)
        argv = [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                *rtsp_input.ffmpeg_input_args(), "-an", "-c:v", "copy", "-t", "3", "-f", "null", "-"]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdin.write(rtsp_input.build_concat_script(url, "tcp", 8))
        proc.stdin.close()
        time.sleep(1.5)
        if sys.platform.startswith("linux"):
            cmdline = open(f"/proc/{proc.pid}/cmdline", "rb").read().decode(errors="replace")
            assert secret not in cmdline and "127.0.0.1" not in cmdline and "rtsp://" not in cmdline
        assert not any(secret in a or "127.0.0.1" in a for a in argv)
        assert proc.wait(timeout=30) == 0, proc.stderr.read().decode(errors="replace")[:300]
        # the fake camera only completes DESCRIBE/SETUP/PLAY with valid credentials
        assert ("PLAY", True) in server.requests
    finally:
        server.stop()
