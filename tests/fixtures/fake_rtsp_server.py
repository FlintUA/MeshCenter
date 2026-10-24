"""Minimal fake RTSP camera for the CAM2 tests - stdlib only.

Serves one H.264 track over RTSP/TCP (interleaved RTP) from a tiny Annex B
fixture (tests/fixtures/tiny_h264.annexb, 320x180, 15 fps, a keyframe every
15 frames), looping forever. Optional HTTP Basic authentication so a test can
prove a client really sent the credentials. No real camera, no real
credentials: every user/password used with this server is made up by the test.

Not a conformant RTSP server - just enough of OPTIONS / DESCRIBE / SETUP /
PLAY / TEARDOWN for ffmpeg's RTSP demuxer (`-rtsp_transport tcp`).
"""
from __future__ import annotations

import base64
import os
import socket
import struct
import threading
import time

FIXTURE = os.path.join(os.path.dirname(__file__), "tiny_h264.annexb")
FPS = 15
PAYLOAD_TYPE = 96
MTU_PAYLOAD = 1400


def _split_nals(data: bytes) -> list[bytes]:
    nals, i, n = [], 0, len(data)
    starts = []
    while i + 3 <= n:
        if data[i:i + 3] == b"\x00\x00\x01":
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for k, s in enumerate(starts):
        end = (starts[k + 1] - 3) if k + 1 < len(starts) else n
        nal = data[s:end]
        while nal.endswith(b"\x00"):
            nal = nal[:-1]
        if nal:
            nals.append(nal)
    return nals


def load_access_units(path: str = FIXTURE):
    """-> (sps, pps, frames) where frames is a list of lists of NAL units."""
    with open(path, "rb") as handle:
        nals = _split_nals(handle.read())
    sps = next(n for n in nals if n[0] & 0x1F == 7)
    pps = next(n for n in nals if n[0] & 0x1F == 8)
    frames, current = [], []
    for nal in nals:
        kind = nal[0] & 0x1F
        if kind in (7, 8, 6, 9):
            current.append(nal)
        elif kind in (1, 5):
            current.append(nal)
            frames.append(current)
            current = []
    return sps, pps, frames


class FakeRtspServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, user: str | None = None, password: str | None = None):
        self.host, self.user, self.password = host, user, password
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.sps, self.pps, self.frames = load_access_units()
        self.requests: list[tuple[str, bool]] = []  # (method, request carried valid credentials)
        self.sessions_playing = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="fake-rtsp-accept")

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True, name="fake-rtsp-conn").start()

    def _authorized(self, headers: dict) -> bool:
        if self.user is None:
            return True
        value = headers.get("authorization", "")
        if not value.lower().startswith("basic "):
            return False
        try:
            return base64.b64decode(value[6:]).decode() == f"{self.user}:{self.password}"
        except Exception:
            return False

    def _sdp(self) -> bytes:
        b64 = lambda b: base64.b64encode(b).decode()
        return (
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=fake\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
            f"m=video 0 RTP/AVP {PAYLOAD_TYPE}\r\na=rtpmap:{PAYLOAD_TYPE} H264/90000\r\n"
            f"a=fmtp:{PAYLOAD_TYPE} packetization-mode=1;sprop-parameter-sets={b64(self.sps)},{b64(self.pps)}\r\n"
            "a=control:track0\r\n"
        ).encode()

    def _serve(self, conn: socket.socket):
        conn.settimeout(30)
        buf = b""
        playing = False
        try:
            while not self._stop.is_set():
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                head, buf = buf.split(b"\r\n\r\n", 1)
                lines = head.decode(errors="replace").split("\r\n")
                method = lines[0].split(" ", 1)[0]
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                cseq = headers.get("cseq", "0")
                ok = self._authorized(headers)
                self.requests.append((method, ok and self.user is not None))
                base = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
                if method != "OPTIONS" and not ok:
                    conn.sendall(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: Basic realm="fake"\r\n\r\n'.encode())
                    continue
                if method == "OPTIONS":
                    conn.sendall((base + "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n\r\n").encode())
                elif method == "DESCRIBE":
                    sdp = self._sdp()
                    conn.sendall((base + f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n").encode() + sdp)
                elif method == "SETUP":
                    conn.sendall((base + "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: 12345678\r\n\r\n").encode())
                elif method == "PLAY":
                    conn.sendall((base + "Session: 12345678\r\nRange: npt=0.000-\r\n\r\n").encode())
                    playing = True
                    break
                elif method == "TEARDOWN":
                    conn.sendall((base + "Session: 12345678\r\n\r\n").encode())
                    return
                else:
                    conn.sendall(f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n".encode())
            if playing:
                self.sessions_playing += 1
                try:
                    self._play(conn)
                finally:
                    self.sessions_playing -= 1
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _play(self, conn: socket.socket):
        seq, ts, ssrc, t0, n = 0, 0, 0x1234ABCD, time.time(), 0
        conn.settimeout(5)
        while not self._stop.is_set():
            for frame in self.frames:
                target = t0 + n / FPS
                delay = target - time.time()
                if delay > 0:
                    time.sleep(delay)
                packets = []
                for nal in frame:
                    if len(nal) <= MTU_PAYLOAD:
                        packets.append(nal)
                    else:
                        hdr, body = nal[0], nal[1:]
                        for off in range(0, len(body), MTU_PAYLOAD - 2):
                            part = body[off:off + MTU_PAYLOAD - 2]
                            start = 0x80 if off == 0 else 0
                            end = 0x40 if off + MTU_PAYLOAD - 2 >= len(body) else 0
                            fu_ind = (hdr & 0xE0) | 28
                            packets.append(bytes([fu_ind, start | end | (hdr & 0x1F)]) + part)
                for idx, payload in enumerate(packets):
                    marker = 0x80 if idx == len(packets) - 1 else 0
                    rtp = struct.pack("!BBHII", 0x80, marker | PAYLOAD_TYPE, seq & 0xFFFF, (ts * 6000) & 0xFFFFFFFF, ssrc) + payload
                    conn.sendall(b"$\x00" + struct.pack("!H", len(rtp)) + rtp)
                    seq += 1
                ts += 1
                n += 1


if __name__ == "__main__":  # manual use: python fake_rtsp_server.py [user password]
    import sys

    args = sys.argv[1:]
    srv = FakeRtspServer(port=int(os.environ.get("PORT", "0")), user=args[0] if args else None,
                         password=args[1] if len(args) > 1 else None).start()
    print("rtsp://127.0.0.1:%d/live" % srv.port, flush=True)
    while True:
        time.sleep(1)
