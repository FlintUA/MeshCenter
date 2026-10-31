"""Minimal mock of a Reolink camera's HTTP(S) API for the CAM2 Phase 2 tests - stdlib only.

Speaks the documented JSON protocol just far enough for camera/reolink_client.py: POST
/cgi-bin/api.cgi with a JSON ARRAY (a single object is answered with an error, like the real
camera), Login with a token and lease, GetAbility, GetZoomFocus (action 1 returns the range),
StartZoomFocus (op ZoomPos, the lens takes a few reads to arrive), Snap (a JPEG), error code -6
("please login first") for an unknown or expired token and -26 ("ability error") for an account
without the right. Every user and password here is made up by the test; there is no real camera.
Optional TLS with a self-signed certificate so the pinning tests exercise a real handshake.
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

JPEG = os.path.join(os.path.dirname(__file__), "tiny_valid_frame.jpg")


def make_self_signed_cert(directory: str, common_name: str = "camera.test"):
    """-> (cert_path, key_path), or None when neither `cryptography` nor the openssl program is available."""
    cert, key = os.path.join(directory, f"{common_name}.crt"), os.path.join(directory, f"{common_name}.key")
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                       .not_valid_after(now + datetime.timedelta(days=30)).sign(private, hashes.SHA256()))
        with open(cert, "wb") as handle:
            handle.write(certificate.public_bytes(serialization.Encoding.PEM))
        with open(key, "wb") as handle:
            handle.write(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                               serialization.NoEncryption()))
        return cert, key
    except ImportError:
        pass
    if shutil.which("openssl"):
        result = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                                 "-days", "30", "-subj", f"/CN={common_name}"], capture_output=True)
        if result.returncode == 0:
            return cert, key
    return None


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a client that aborts a TLS handshake is part of the tests
        pass


class FakeReolink:
    def __init__(self, *, users=None, tls=None, zoom_range=(0, 32), zoom_pos=10, settle_reads=2, snapshot=True,
                 lease_s=3600, redirect=False, snap_denied=("guest",), zoom_denied=("guest",), login_status=200,
                 keepalive=False, silent_close_after=None):
        self.users = users or {"admin": ("pw-admin-test", "admin"), "guest": ("pw-guest-test", "guest")}
        self.tls = tls  # (cert, key) or None
        self.zoom_range, self.zoom_pos, self.settle_reads = zoom_range, zoom_pos, settle_reads
        self.snapshot, self.lease_s, self.redirect = snapshot, lease_s, redirect
        self.snap_denied, self.zoom_denied = set(snap_denied), set(zoom_denied)
        self.login_status = login_status
        self.keepalive = keepalive  # False: every answer says Connection: close (the old behaviour)
        self.silent_close_after = silent_close_after  # close a connection after N answers WITHOUT saying so (a camera's idle close)
        self.connections = 0  # TCP connections accepted
        self.tokens: dict[str, str] = {}
        self.target = None
        self.reads_left = 0
        self.log: list[dict] = []  # what the client sent: method, command, whether the token was in the query
        self.logins = 0
        self.logouts = 0
        self.snaps = 0
        self.snap_types: list[str] = []
        self.snap_failures = 0  # the next N Snap requests are answered with an unclassified error (rspCode -104)
        self.zoom_commands: list[int] = []
        self._lock = threading.Lock()
        self._server: _QuietServer | None = None
        self._counter = 0
        self._port = 0

    # -- control from the test ---------------------------------------------

    def expire_tokens(self) -> None:
        with self._lock:
            self.tokens.clear()

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> "FakeReolink":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # quiet
                pass

            def setup(self):
                super().setup()
                self._served = 0
                with fake._lock:
                    fake.connections += 1

            def _send(self, status, body: bytes, content_type="application/json", extra=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self._served += 1
                if fake.silent_close_after and self._served >= fake.silent_close_after:
                    self.close_connection = True  # no header: the client finds out on its next request
                elif not fake.keepalive:
                    self.send_header("Connection", "close")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                self._handle("POST", raw)

            def do_GET(self):
                self._handle("GET", b"")

            def _handle(self, method, raw):
                if fake.redirect:
                    return self._send(302, b"", extra={"Location": "http://203.0.113.9/"})
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                token = (query.get("token") or [""])[0]
                if method == "GET" and (query.get("cmd") or [""])[0] == "Snap":
                    return fake._snap(self, token)
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                if not isinstance(body, list) or not body or not isinstance(body[0], dict):
                    fake.log.append({"method": method, "command": "?", "token_in_query": bool(token), "array": False})
                    return self._send(200, json.dumps([{"cmd": "Login", "code": 1, "error": {"detail": "param error", "rspCode": -1}}]).encode())
                command = body[0]
                fake.log.append({"method": method, "command": command.get("cmd"), "token_in_query": bool(token), "array": True,
                                 "body_has_password": "password" in json.dumps(command)})
                if command.get("cmd") == "Login":
                    return self._send(fake.login_status, json.dumps(fake._login(command)).encode())
                with fake._lock:
                    user = fake.tokens.get(token)
                if user is None:
                    return self._send(200, json.dumps([fake._error(command["cmd"], -6, "please login first")]).encode())
                fake._current_token = token
                self._send(200, json.dumps(fake._run(command, user)).encode())

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        if self.tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*self.tls)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._port = self._server.server_address[1]
        threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- protocol ------------------------------------------------------------

    @staticmethod
    def _error(cmd, rsp, detail):
        return {"cmd": cmd, "code": 1, "error": {"detail": detail, "rspCode": rsp}}

    def _login(self, command):
        self.logins += 1
        user = (((command.get("param") or {}).get("User")) or {})
        name, password = user.get("userName"), user.get("password")
        record = self.users.get(name)
        if not record or record[0] != password:
            return [self._error("Login", -7, "login failed")]
        with self._lock:
            self._counter += 1
            token = f"tok{self._counter:04d}x"
            self.tokens[token] = name
        return [{"cmd": "Login", "code": 0, "value": {"Token": {"leaseTime": self.lease_s, "name": token}}}]

    def _zoom_now(self):
        if self.target is not None:
            self.reads_left -= 1
            if self.reads_left <= 0:
                self.zoom_pos, self.target = self.target, None
        return self.zoom_pos

    def _run(self, command, user):
        name = command.get("cmd")
        if name == "GetAbility":
            ability = {"live": {"permit": 7, "ver": 1}, "abilityChn": [{"snap": {"permit": 6 if self.snapshot else 0, "ver": 1},
                                                                         "ptzZoom": {"permit": 6, "ver": 1}}]}
            return [{"cmd": name, "code": 0, "value": {"Ability": ability}}]
        if name == "GetZoomFocus":
            low, high = self.zoom_range
            pos = self._zoom_now()
            return [{"cmd": name, "code": 0,
                     "value": {"ZoomFocus": {"channel": 0, "zoom": {"pos": pos}, "focus": {"pos": 100}}},
                     "range": {"ZoomFocus": {"zoom": {"pos": {"min": low, "max": high}}, "focus": {"pos": {"min": 0, "max": 248}}}}}]
        if name == "StartZoomFocus":
            if user in self.zoom_denied:
                return [self._error(name, -26, "ability error")]
            zoom_focus = ((command.get("param") or {}).get("ZoomFocus")) or {}
            pos = zoom_focus.get("pos")
            if zoom_focus.get("op") != "ZoomPos" or not isinstance(pos, int) or not self.zoom_range[0] <= pos <= self.zoom_range[1]:
                return [self._error(name, -9, "param error")]
            self.zoom_commands.append(pos)
            self.target, self.reads_left = pos, self.settle_reads
            return [{"cmd": name, "code": 0, "value": {"rspCode": 200}}]
        if name == "Logout":
            with self._lock:
                self.logouts += 1
                for token, owner in list(self.tokens.items()):  # the session is really gone
                    if owner == user and token == getattr(self, "_current_token", None):
                        del self.tokens[token]
            return [{"cmd": name, "code": 0, "value": {"rspCode": 200}}]
        return [self._error(name or "?", -2, "not support")]

    def _snap(self, handler, token):
        with self._lock:
            user = self.tokens.get(token)
        self.log.append({"method": "GET", "command": "Snap", "token_in_query": bool(token), "array": False})
        self.snap_types.append((parse_qs(urlparse(handler.path).query).get("snapType") or [""])[0])
        with self._lock:
            failing = self.snap_failures > 0
            if failing:
                self.snap_failures -= 1
        if failing:
            return handler._send(200, json.dumps([self._error("Snap", -104, "unexpected")]).encode())
        if user is None:
            return handler._send(200, json.dumps([self._error("Snap", -6, "please login first")]).encode())
        if user in self.snap_denied or not self.snapshot:
            return handler._send(200, json.dumps([self._error("Snap", -26, "ability error")]).encode())
        self.snaps += 1
        with open(JPEG, "rb") as handle:
            handler._send(200, handle.read(), content_type="image/jpeg")
