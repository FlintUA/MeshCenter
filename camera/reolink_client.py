"""Reolink HTTP(S) API client (CAM2 Phase 2).

Written from the publicly documented JSON protocol (POST /cgi-bin/api.cgi with a
JSON ARRAY of command objects; a single object is rejected by the camera), with
the standard library only: no vendor SDK or DLL, no copied vendor code, no new
dependency. The camera stays Flint's device: the only write this client can
make is lens zoom (`StartZoomFocus`, op ZoomPos), range-checked, rate limited
and read back; it never changes a codec, stream, network or user setting.

Rules this module keeps:

* Credentials are sent once, in the body of the login request. Every later call
  carries the short-lived token in the query string (that is how the camera
  wants it), so the token, like the password, is never logged, never put into an
  exception message and never returned to the browser. Errors are machine codes
  (`ReolinkError.code`) plus a generic message.
* TLS: certificates are verified by default. A camera with a self-signed
  certificate is reached only by an explicit per-source SHA-256 fingerprint pin
  that the user confirmed (the handshake is checked against the pin BEFORE any
  byte, in particular the password, is sent). Verification is never switched off
  globally and a changed fingerprint fails closed. Plain HTTP only when the
  source explicitly opted in.
* The address policy (camera/address_policy.py) is applied on every connection and
  the connection goes to the validated IP, not to a name that could resolve
  elsewhere a moment later. Redirects are never followed.
* One command in flight per client (a lock); zoom has a minimum interval.
* A failure of this API must never affect the RTSP preview: nothing here is
  imported by camera/rtsp_stream.py and every failure is an exception the caller
  turns into an API error.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import random
import socket
import ssl
import threading
import time
from typing import Any, Callable

from camera import address_policy

DEFAULT_TIMEOUT_S = 6.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MIN_ZOOM_INTERVAL_S = 0.5
SETTLE_TIMEOUT_S = 6.0
SETTLE_POLL_S = 0.4
TOKEN_MARGIN_S = 60.0
DEFAULT_LEASE_S = 3600.0
API_PATH = "/cgi-bin/api.cgi"

# machine codes (ReolinkError.code)
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
TLS_UNTRUSTED = "tls_untrusted"
TLS_PIN_MISMATCH = "tls_pin_mismatch"
TLS_ERROR = "tls_error"
HTTP_NOT_ALLOWED = "http_not_allowed"
LOGIN_FAILED = "login_failed"
TOO_MANY_SESSIONS = "too_many_sessions"
PERMISSION_DENIED = "permission_denied"
TOKEN_EXPIRED = "token_expired"
API_ERROR = "api_error"
BAD_RESPONSE = "bad_response"
REDIRECT_REFUSED = "redirect_refused"
RATE_LIMITED = "rate_limited"
ZOOM_OUT_OF_RANGE = "zoom_out_of_range"
NOT_SUPPORTED = "not_supported"
ADDRESS_BLOCKED = "address_blocked"

# The account right a command needs, for "this account has no permission for ..."
RIGHT_FOR_COMMAND = {
    "Snap": "snapshot",
    "StartZoomFocus": "lens_control",
    "GetZoomFocus": "live_view",
    "GetAbility": "live_view",
}

_PERMISSION_WORDS = ("ability error", "permission", "authority", "not allowed", "no right")


class ReolinkError(Exception):
    """A failed control-API call. Carries machine data only: never a URL, token, password or camera text."""

    def __init__(self, code: str, message: str = "", *, command: str = "", right: str = "",
                 fingerprint: str = "", rsp_code: int | None = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.command = command
        self.right = right
        self.fingerprint = fingerprint
        self.rsp_code = rsp_code

    def to_dict(self) -> dict:
        out = {"error_code": self.code, "error": self.message}
        for key in ("command", "right", "fingerprint"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.rsp_code is not None:
            out["rsp_code"] = self.rsp_code
        return out


def fingerprint_of(der_certificate: bytes) -> str:
    """Lower-case hex SHA-256 of the DER certificate (64 characters)."""
    return hashlib.sha256(der_certificate).hexdigest()


def normalize_fingerprint(value: Any) -> str:
    """Accept 'AA:BB:..' or 'aabb..'; '' when it is not a SHA-256 fingerprint."""
    if not isinstance(value, str):
        return ""
    text = value.replace(":", "").replace(" ", "").strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        return ""
    return text


def peek_certificate(address: str, port: int, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """SHA-256 fingerprint of the certificate a camera presents, for the user to confirm before pinning it.

    The handshake only: nothing is sent and the certificate is NOT trusted by this call (it is shown, and
    stored only when the user confirms it)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # peek only: no data is sent, nothing is trusted
    try:
        with socket.create_connection((address, port), timeout=timeout) as raw:
            with context.wrap_socket(raw) as tls:
                return fingerprint_of(tls.getpeercert(binary_form=True) or b"")
    except socket.timeout:
        raise ReolinkError(TIMEOUT, "The camera did not answer in time.")
    except ssl.SSLError:
        raise ReolinkError(TLS_ERROR, "The camera did not complete a TLS handshake.")
    except OSError:
        raise ReolinkError(UNREACHABLE, "The camera could not be reached.")


class ReolinkClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        scheme: str = "https",
        channel: int = 0,
        username: str,
        password_provider: Callable[[], str],
        pinned_sha256: str = "",
        allow_http: bool = False,
        host_validator: Callable[[str], list] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.host, self.port, self.scheme, self.channel = host, int(port), scheme, int(channel)
        self._username = username
        self._password = password_provider
        self._pin = normalize_fingerprint(pinned_sha256)
        self._allow_http = bool(allow_http)
        self._validate = host_validator or address_policy.validate_camera_host
        self._timeout = timeout
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()  # one command in flight per camera
        self._token = ""
        self._token_until = 0.0
        self._zoom_range: tuple[int, int] | None = None
        self._last_zoom_at = -1e9

    # -- transport ---------------------------------------------------------

    def _connect(self) -> http.client.HTTPConnection:
        if self.scheme == "http" and not self._allow_http:
            raise ReolinkError(HTTP_NOT_ALLOWED, "Plain HTTP is not enabled for this camera.")
        try:
            addresses = self._validate(self.host)
        except address_policy.AddressPolicyError as error:
            raise ReolinkError(ADDRESS_BLOCKED, error.message)
        if not addresses:
            raise ReolinkError(UNREACHABLE, "The camera address could not be resolved.")
        address = str(addresses[0])
        try:
            raw = socket.create_connection((address.split("%", 1)[0], self.port), timeout=self._timeout)
        except socket.timeout:
            raise ReolinkError(TIMEOUT, "The camera did not answer in time.")
        except OSError:
            raise ReolinkError(UNREACHABLE, "The camera could not be reached.")
        if self.scheme != "https":
            sock = raw
        else:
            try:
                sock = self._wrap_tls(raw)
            except BaseException:
                raw.close()
                raise
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self._timeout)
        connection.sock = sock
        return connection

    def _wrap_tls(self, raw: socket.socket) -> ssl.SSLSocket:
        if self._pin:
            # A pinned (typically self-signed) camera: the pin IS the trust decision. Checked right after
            # the handshake and before anything is sent.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            try:
                tls = context.wrap_socket(raw)
            except socket.timeout:
                raise ReolinkError(TIMEOUT, "The camera did not answer in time.")
            except (ssl.SSLError, OSError):
                raise ReolinkError(TLS_ERROR, "The camera did not complete a TLS handshake.")
            seen = fingerprint_of(tls.getpeercert(binary_form=True) or b"")
            if not hmac.compare_digest(seen, self._pin):
                tls.close()
                raise ReolinkError(TLS_PIN_MISMATCH, "The camera's certificate changed. Confirm the new fingerprint to continue.",
                                   fingerprint=seen)
            return tls
        context = ssl.create_default_context()
        try:
            return context.wrap_socket(raw, server_hostname=self.host)
        except ssl.SSLCertVerificationError:
            raise ReolinkError(TLS_UNTRUSTED, "The camera's certificate is not trusted.")
        except socket.timeout:
            raise ReolinkError(TIMEOUT, "The camera did not answer in time.")
        except (ssl.SSLError, OSError):
            raise ReolinkError(TLS_ERROR, "The camera did not complete a TLS handshake.")

    def _http(self, method: str, target: str, body: bytes | None = None) -> bytes:
        """One request on a fresh connection. `target` may contain the token: it is never logged or reported."""
        connection = self._connect()
        try:
            headers = {"Connection": "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                connection.request(method, target, body=body, headers=headers)
                response = connection.getresponse()
                if 300 <= response.status < 400:
                    raise ReolinkError(REDIRECT_REFUSED, "The camera answered with a redirect, which is not followed.")
                length = response.getheader("Content-Length")
                if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                    raise ReolinkError(BAD_RESPONSE, "The camera answer is too large.")
                data = response.read(MAX_RESPONSE_BYTES + 1)
            except ReolinkError:
                raise
            except socket.timeout:
                raise ReolinkError(TIMEOUT, "The camera did not answer in time.")
            except (OSError, http.client.HTTPException):
                raise ReolinkError(UNREACHABLE, "The connection to the camera failed.")
            if len(data) > MAX_RESPONSE_BYTES:
                raise ReolinkError(BAD_RESPONSE, "The camera answer is too large.")
            if response.status >= 400:
                raise ReolinkError(BAD_RESPONSE, "The camera answered with an HTTP error.", rsp_code=None)
            return data
        finally:
            connection.close()

    def _post(self, target: str, commands: list) -> list:
        data = self._http("POST", target, json.dumps(commands).encode("utf-8"))
        try:
            parsed = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise ReolinkError(BAD_RESPONSE, "The camera answer is not valid.")
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
            raise ReolinkError(BAD_RESPONSE, "The camera answer is not valid.")
        return parsed

    # -- error mapping -----------------------------------------------------

    @staticmethod
    def _classify(item: dict, command: str) -> ReolinkError:
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        rsp = error.get("rspCode") if isinstance(error.get("rspCode"), int) else None
        detail = str(error.get("detail") or "").lower()
        if command == "Login":
            if "max session" in detail or "too many" in detail:
                return ReolinkError(TOO_MANY_SESSIONS, "The camera has no free login session.", command=command, rsp_code=rsp)
            return ReolinkError(LOGIN_FAILED, "The camera refused the login (check the user name and password).", command=command, rsp_code=rsp)
        if rsp == -6 or "login first" in detail:
            return ReolinkError(TOKEN_EXPIRED, "The camera session expired.", command=command, rsp_code=rsp)
        if rsp == -26 or any(word in detail for word in _PERMISSION_WORDS):
            right = RIGHT_FOR_COMMAND.get(command, "")
            return ReolinkError(PERMISSION_DENIED, "This account has no permission for this camera function.",
                                command=command, right=right, rsp_code=rsp)
        return ReolinkError(API_ERROR, "The camera reported an error.", command=command, rsp_code=rsp)

    # -- session -----------------------------------------------------------

    def login(self) -> None:
        with self._lock:
            body = [{"cmd": "Login", "action": 0, "param": {"User": {
                "Version": "0", "userName": self._username, "password": self._password()}}}]
            items = self._post(API_PATH + "?cmd=Login", body)
            item = items[0]
            if item.get("code") != 0:
                raise self._classify(item, "Login")
            token = ((item.get("value") or {}).get("Token") or {})
            name, lease = token.get("name"), token.get("leaseTime")
            if not isinstance(name, str) or not name:
                raise ReolinkError(BAD_RESPONSE, "The camera answer is not valid.", command="Login")
            lease = float(lease) if isinstance(lease, (int, float)) and lease > 0 else DEFAULT_LEASE_S
            self._token = name
            self._token_until = self._clock() + max(lease - TOKEN_MARGIN_S, 5.0)

    def logout(self) -> None:
        """Best effort; the token is dropped either way."""
        with self._lock:
            token, self._token = self._token, ""
            if not token:
                return
            try:
                self._post(f"{API_PATH}?token={token}", [{"cmd": "Logout", "action": 0, "param": {}}])
            except ReolinkError:
                pass

    def _ensure_token(self) -> str:
        if not self._token or self._clock() >= self._token_until:
            self.login()
        return self._token

    def _command(self, command: str, action: int, param: dict) -> dict:
        """Run one command; log in first, re-login ONCE when the token expired. Returns the response item."""
        with self._lock:
            for attempt in range(2):
                token = self._ensure_token()
                items = self._post(f"{API_PATH}?token={token}", [{"cmd": command, "action": action, "param": param}])
                item = items[0]
                if item.get("code") == 0:
                    return item
                error = self._classify(item, command)
                if error.code == TOKEN_EXPIRED and attempt == 0:
                    self._token = ""
                    continue
                raise error
        raise ReolinkError(API_ERROR, "The camera reported an error.", command=command)  # unreachable

    # -- reads -------------------------------------------------------------

    def get_abilities(self) -> dict:
        """{'names': [...], 'permits': {name: int}} - only what the camera reports, never assumed."""
        item = self._command("GetAbility", 0, {"User": {"userName": self._username}})
        ability = (item.get("value") or {}).get("Ability")
        permits: dict[str, int] = {}

        def collect(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, dict) and isinstance(value.get("permit"), int):
                        permits[str(key)] = value["permit"]

        if isinstance(ability, dict):
            collect(ability)
            channels = ability.get("abilityChn")
            if isinstance(channels, list) and 0 <= self.channel < len(channels):
                collect(channels[self.channel])
        return {"names": sorted(permits), "permits": permits}

    @staticmethod
    def _range(node: Any) -> tuple[int, int] | None:
        pos = node.get("pos") if isinstance(node, dict) else None
        if isinstance(pos, dict) and isinstance(pos.get("min"), int) and isinstance(pos.get("max"), int) and pos["min"] <= pos["max"]:
            return int(pos["min"]), int(pos["max"])
        return None

    def get_zoom_focus(self) -> dict:
        """Current zoom/focus position in DEVICE units, with the valid ranges the camera reports."""
        item = self._command("GetZoomFocus", 1, {"channel": self.channel})
        value = item.get("value") or {}
        zoom_focus = value.get("ZoomFocus") if isinstance(value.get("ZoomFocus"), dict) else {}
        zoom = zoom_focus.get("zoom") if isinstance(zoom_focus.get("zoom"), dict) else {}
        focus = zoom_focus.get("focus") if isinstance(zoom_focus.get("focus"), dict) else {}
        ranges = (item.get("range") or value.get("range") or {})
        ranges = ranges.get("ZoomFocus") if isinstance(ranges.get("ZoomFocus"), dict) else {}
        zoom_range = self._range(ranges.get("zoom"))
        focus_range = self._range(ranges.get("focus"))
        if zoom_range:
            self._zoom_range = zoom_range
        zoom_pos = zoom.get("pos") if isinstance(zoom.get("pos"), int) else None
        focus_pos = focus.get("pos") if isinstance(focus.get("pos"), int) else None
        return {"zoom_pos": zoom_pos, "focus_pos": focus_pos, "zoom_range": zoom_range, "focus_range": focus_range}

    def snapshot(self) -> bytes:
        """One JPEG made by the camera itself (no ffmpeg, no transcode)."""
        with self._lock:
            for attempt in range(2):
                token = self._ensure_token()
                target = f"{API_PATH}?cmd=Snap&channel={self.channel}&rs={random.randrange(10**9)}&token={token}"
                data = self._http("GET", target)
                if data[:3] == b"\xff\xd8\xff":
                    return data
                # a refused snapshot is a small JSON answer, not an image
                try:
                    parsed = json.loads(data.decode("utf-8", "replace"))
                    item = parsed[0] if isinstance(parsed, list) and parsed else parsed
                except ValueError:
                    raise ReolinkError(BAD_RESPONSE, "The camera did not return a picture.", command="Snap")
                if isinstance(item, dict):
                    error = self._classify(item, "Snap")
                    if error.code == TOKEN_EXPIRED and attempt == 0:
                        self._token = ""
                        continue
                    raise error
                raise ReolinkError(BAD_RESPONSE, "The camera did not return a picture.", command="Snap")
        raise ReolinkError(BAD_RESPONSE, "The camera did not return a picture.", command="Snap")  # unreachable

    # -- lens zoom (the only write) -----------------------------------------

    def set_zoom(self, position: Any) -> dict:
        """Move the lens to `position` (device units, validated against the range the camera reports), then
        read it back. Returns {requested, position, settled, zoom_range}."""
        if isinstance(position, bool) or not isinstance(position, int):
            raise ReolinkError(ZOOM_OUT_OF_RANGE, "The zoom position must be a whole number in the camera's own units.")
        with self._lock:
            if self._zoom_range is None:
                if self.get_zoom_focus()["zoom_range"] is None:
                    raise ReolinkError(NOT_SUPPORTED, "The camera does not report a zoom range.", command="GetZoomFocus")
            low, high = self._zoom_range
            if not low <= position <= high:
                raise ReolinkError(ZOOM_OUT_OF_RANGE, f"The zoom position must be between {low} and {high}.")
            now = self._clock()
            if now - self._last_zoom_at < MIN_ZOOM_INTERVAL_S:
                raise ReolinkError(RATE_LIMITED, "Zoom commands are limited; try again in a moment.", command="StartZoomFocus")
            self._last_zoom_at = now
            self._command("StartZoomFocus", 0, {"ZoomFocus": {"channel": self.channel, "pos": position, "op": "ZoomPos"}})
            deadline = self._clock() + SETTLE_TIMEOUT_S
            actual = None
            settled = False
            while True:
                self._sleep(SETTLE_POLL_S)
                actual = self.get_zoom_focus()["zoom_pos"]
                if actual == position:
                    settled = True
                    break
                if self._clock() >= deadline:
                    break
            return {"requested": position, "position": actual, "settled": settled, "zoom_range": [low, high]}

    # -- capability probe ----------------------------------------------------

    def probe(self) -> dict:
        """Login, read the abilities, the zoom/focus position and try one snapshot. Never writes.

        A capability is reported only when the camera really delivered it. A permission error on one part
        is reported for that part (`denied`) and does not hide the others."""
        capabilities: dict[str, Any] = {"login": True, "snapshot": False, "zoom": False, "focus_read": False,
                                        "abilities": [], "denied": [], "zoom_pos": None, "focus_pos": None,
                                        "zoom_range": None}
        self.login()
        for name, action in (("abilities", self.get_abilities), ("zoom_focus", self.get_zoom_focus), ("snapshot", self.snapshot)):
            try:
                result = action()
            except ReolinkError as error:
                if error.code == PERMISSION_DENIED:
                    capabilities["denied"].append({"command": error.command, "right": error.right})
                    continue
                if name == "abilities":
                    continue  # an ability read is optional; the real reads below decide
                if error.code in (API_ERROR, NOT_SUPPORTED, BAD_RESPONSE):
                    continue  # this camera simply does not offer it
                raise
            if name == "abilities":
                capabilities["abilities"] = result["names"]
            elif name == "zoom_focus":
                capabilities["zoom_pos"], capabilities["focus_pos"] = result["zoom_pos"], result["focus_pos"]
                capabilities["zoom_range"] = list(result["zoom_range"]) if result["zoom_range"] else None
                capabilities["zoom"] = result["zoom_pos"] is not None and result["zoom_range"] is not None
                capabilities["focus_read"] = result["focus_pos"] is not None
            else:
                capabilities["snapshot"] = True
        return capabilities
