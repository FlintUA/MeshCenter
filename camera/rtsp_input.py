"""Building the ffmpeg input for a network camera without putting the password
on a command line (CAM2 Phase 0 spike, section 9.3 of the camera audit).

`ffmpeg -i rtsp://user:password@host/...` puts the password in the process's
argv, readable by every local user through /proc/<pid>/cmdline (`ps`). ffmpeg
has no option, environment variable or file for RTSP credentials, so instead
ffmpeg is started with a fixed, secret-free argv and reads a two-line
"ffconcat" script on STDIN that names the stream:

    ffconcat version 1.0
    file 'rtsp://user:password@192.168.2.114:554/path'
    option rtsp_transport tcp

Verified live (2026-10-23) against a real RTSP camera with ffmpeg 9.0 (Windows)
and 7.1.5 (Raspberry Pi OS): the stream opens and the ffmpeg process's argv
contains neither the password nor the host. The `option rtsp_transport tcp`
line must come AFTER the `file` line (it applies to the previous file); without
it the inner demuxer falls back to UDP, which many networks and Docker bridge
mode do not pass.

Injection hardening (fail closed, three independent layers):
1. Every user-supplied part (host, user, password, path) is rejected outright
   if it contains a control character (CR, LF, NUL, tab, DEL ...), here and in
   camera/source_store.py. A newline is what would let a value start a new
   ffconcat directive such as `file /etc/passwd`.
2. The parts are percent-encoded, so the URL contains no quote, backslash,
   space or newline; build_concat_script() additionally refuses any URL that
   is not plain printable ASCII without a quote or backslash (no ffconcat
   escaping is needed because nothing that needs escaping can get through).
3. ffmpeg runs with `-protocol_whitelist pipe,fd,rtsp,rtp,tcp,udp` (no file,
   http, crypto ...): even a directive that somehow reached the script cannot
   open a local file or an http URL ("Protocol 'file' not on whitelist").
   The script also pins `option allowed_media_types video` for the stream.

Pure functions, stdlib only, no I/O.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote

# Every protocol the concat wrapper may open: the script itself arrives on a pipe.
PROTOCOL_WHITELIST = "pipe,fd,rtsp,rtp,tcp,udp"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PRINTABLE_URL = re.compile(r"^[\x21-\x7e]+$")
_PATH_ALLOWED = re.compile(r"^[A-Za-z0-9._~!$&()*+,;=:@/%?-]{0,256}$")
_URL_PATH_SAFE = "/:@!$&()*+,;=~%-._"  # '?' and the query part are handled separately; "'" is NOT safe


def valid_stream_path(path: str) -> bool:
    """True for a path/query that is safe to embed: no spaces, quotes, backslashes, control characters, '#' or a scheme."""
    return (
        isinstance(path, str)
        and bool(_PATH_ALLOWED.match(path))
        and "://" not in path
        and not path.startswith("//")
        and ".." not in path.split("?", 1)[0].split("/")
    )


def build_rtsp_url(ip_text: str, port: int, path: str, username: str = "", password: str = "") -> str:
    """rtsp://[user[:password]@]host:port/path - IPv6 in brackets, every part percent-encoded."""
    for part in (ip_text, path, username, password):
        if not isinstance(part, str) or _CONTROL.search(part):
            raise ValueError("control characters are not allowed in a stream URL")
    address = ipaddress.ip_address(ip_text.split("%", 1)[0])
    host = ip_text
    if isinstance(address, ipaddress.IPv6Address):
        host = "[" + ip_text.replace("%", "%25") + "]"
    userinfo = ""
    if username:
        userinfo = quote(username, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    clean = path.lstrip("/")
    base, sep, query = clean.partition("?")
    encoded_path = "/" + quote(base, safe=_URL_PATH_SAFE)
    encoded_query = (sep + quote(query, safe="=&%+,;:@/!$()*~-._")) if sep else ""
    return f"rtsp://{userinfo}{host}:{int(port)}{encoded_path}{encoded_query}"


def build_concat_script(url: str, transport: str = "tcp", timeout_s: int = 10) -> bytes:
    """The stdin script for ffmpeg (see the module docstring)."""
    if not _PRINTABLE_URL.match(url) or "'" in url or chr(92) in url or not url.startswith("rtsp://"):
        raise ValueError("url contains a character that cannot be quoted safely")
    if transport not in ("tcp", "udp"):
        raise ValueError("transport must be tcp or udp")
    timeout_us = max(1, int(timeout_s)) * 1_000_000
    return (
        "ffconcat version 1.0\n"
        f"file '{url}'\n"
        f"option rtsp_transport {transport}\n"
        f"option timeout {timeout_us}\n"
        "option allowed_media_types video\n"
    ).encode("ascii")


def ffmpeg_input_args() -> list[str]:
    """The secret-free argv fragment that reads the script from stdin."""
    return ["-f", "concat", "-safe", "0", "-protocol_whitelist", PROTOCOL_WHITELIST, "-i", "pipe:0"]


def redact(text: str, *secrets: str) -> str:
    """Remove URLs (userinfo included) and any listed secret from text meant for logs or API responses."""
    text = re.sub(r"rtsps?://\S+", "rtsp://<redacted>", text)
    text = re.sub(r"https?://\S+", "http://<redacted>", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
            text = text.replace(quote(secret, safe=""), "<redacted>")
    return text
