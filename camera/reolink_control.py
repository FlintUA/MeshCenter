"""Control side of a saved network camera (CAM2 Phase 2): clients, snapshot sharing, viewer leases.

`ReolinkControlManager` turns a saved source (camera/source_store.py: host, user, the stored password,
and the `control` section) into a `ReolinkClient` (camera/reolink_client.py). There is no second secret
store: the client asks the source store for the password each time it logs in, and a changed source gets
a new client. Nothing here is imported by the RTSP stream code, and every failure is a ReolinkError the
route turns into an API answer, so a dead control API never stops the preview.

Snapshot fallback (browsers without MediaSource, e.g. iPhone before iOS 17.1): the camera makes the JPEG
itself, so nothing is transcoded.

* At most one picture per second per source is fetched, shared by every fallback viewer (1 s cache,
  single flight), and only when a viewer asks: there is no background polling.
* A failure is remembered for a few seconds so six polling viewers of a dead camera do not each wait
  for a connect timeout, one after another.
* Viewers are counted with leases (`SnapshotLeases`): a polling client holds a slot while it keeps asking
  (about 15 s of silence frees it), in the SAME limiter as the fMP4 stream viewers (4 per source, 6 in
  total), and a lease is dropped after 10 minutes so the client is counted and authorised again, like a
  stream's 10 minute lifetime.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from camera import reolink_client as rc
from camera import rtsp_stream
from camera.source_store import SourceNotFound

SNAPSHOT_MIN_INTERVAL_S = 1.0
FAILURE_MEMORY_S = 3.0
LEASE_S = 15.0
LEASE_MAX_AGE_S = 600.0


class SnapshotLeases:
    def __init__(self, limiter: rtsp_stream.ViewerLimiter | None = None, *, lease_s: float = LEASE_S,
                 max_age_s: float = LEASE_MAX_AGE_S, clock: Callable[[], float] = time.monotonic,
                 start_sweeper: bool = True):
        self._limiter = limiter or rtsp_stream.VIEWER_LIMITER
        self._lease_s, self._max_age_s, self._clock = lease_s, max_age_s, clock
        self._leases: dict[tuple[str, str], dict[str, float]] = {}
        self._lock = threading.Lock()
        self._sweeper: threading.Thread | None = None
        self._start_sweeper = start_sweeper

    def touch(self, source_id: str, key: str) -> bool:
        """Keep (or take) a viewer slot for `key`. False = the viewer limit is reached."""
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            lease = self._leases.get((source_id, key))
            if lease is not None and now - lease["start"] > self._max_age_s:
                self._leases.pop((source_id, key))
                self._limiter.release(source_id)  # lifetime over: counted and authorised again below
                lease = None
            if lease is None:
                if not self._limiter.acquire(source_id):
                    return False
                lease = self._leases[(source_id, key)] = {"start": now, "seen": now}
            lease["seen"] = now
            self._ensure_sweeper_locked()
            return True

    def count(self, source_id: str | None = None) -> int:
        with self._lock:
            self._sweep_locked(self._clock())
            return sum(1 for (sid, _key) in self._leases if source_id is None or sid == source_id)

    def clear(self) -> None:
        with self._lock:
            for (sid, _key) in list(self._leases):
                self._limiter.release(sid)
            self._leases.clear()

    def _sweep_locked(self, now: float) -> None:
        for lease_key, lease in list(self._leases.items()):
            if now - lease["seen"] > self._lease_s:
                del self._leases[lease_key]
                self._limiter.release(lease_key[0])

    def _ensure_sweeper_locked(self) -> None:
        if not self._start_sweeper or (self._sweeper is not None and self._sweeper.is_alive()):
            return
        self._sweeper = threading.Thread(target=self._sweep_loop, name="snapshot-lease-sweeper", daemon=True)
        self._sweeper.start()

    def _sweep_loop(self) -> None:
        # frees the slots of clients that went silent even when nobody else calls touch()
        while True:
            time.sleep(3.0)
            with self._lock:
                self._sweep_locked(self._clock())
                if not self._leases:
                    self._sweeper = None
                    return


class ReolinkControlManager:
    def __init__(self, store, *, host_validator=None, client_factory: Callable[..., rc.ReolinkClient] | None = None,
                 clock: Callable[[], float] = time.monotonic, peek: Callable[..., str] | None = None):
        self._store = store
        self._validate = host_validator
        self._factory = client_factory or rc.ReolinkClient
        self._clock = clock
        self._peek = peek or rc.peek_certificate
        self._lock = threading.Lock()
        self._clients: dict[str, tuple[str, rc.ReolinkClient]] = {}
        self._snap: dict[str, dict[str, Any]] = {}

    # -- clients -----------------------------------------------------------

    def _source(self, source_id: str) -> dict:
        try:
            source = self._store.get_source(source_id)
        except SourceNotFound:
            raise rc.ReolinkError(rc.NOT_SUPPORTED, "Unknown camera source.")
        if (source.get("control") or {}).get("type") != "reolink":
            raise rc.ReolinkError(rc.NOT_SUPPORTED, "Camera control is not enabled for this source.")
        return source

    def _client(self, source: dict) -> rc.ReolinkClient:
        control = source["control"]
        signature = repr((source["host"], source.get("username"), source.get("updated_at"), control["port"], control["scheme"],
                          control["channel"], control["allow_http"], control["pinned_sha256"]))
        with self._lock:
            cached = self._clients.get(source["id"])
            if cached and cached[0] == signature:
                return cached[1]
            client = self._factory(
                host=source["host"], port=control["port"], scheme=control["scheme"], channel=control["channel"],
                username=source.get("username") or "", password_provider=lambda sid=source["id"]: self._store.get_password(sid),
                pinned_sha256=control["pinned_sha256"], allow_http=control["allow_http"], host_validator=self._validate,
            )
            self._clients[source["id"]] = (signature, client)
            self._snap.pop(source["id"], None)
            return client

    def forget(self, source_id: str) -> None:
        with self._lock:
            self._clients.pop(source_id, None)
            self._snap.pop(source_id, None)

    # -- user-facing operations ----------------------------------------------

    def probe(self, source_id: str) -> dict:
        source = self._source(source_id)
        client = self._client(source)
        try:
            capabilities = client.probe()
        except rc.ReolinkError as error:
            if error.code == rc.TLS_UNTRUSTED:
                error.fingerprint = self._current_fingerprint(source)  # shown to the user, stored only on confirmation
            raise
        stored = {k: capabilities[k] for k in ("snapshot", "zoom", "focus_read", "zoom_range", "abilities", "denied")}
        stored["probed_at"] = int(time.time())
        self._store.set_control_state(source_id, capabilities=stored)
        return capabilities

    def _current_fingerprint(self, source: dict) -> str:
        validate = self._validate
        if validate is None:
            from camera import address_policy
            validate = address_policy.validate_camera_host
        try:
            addresses = validate(source["host"])
            return self._peek(str(addresses[0]).split("%", 1)[0], source["control"]["port"])
        except Exception:
            return ""

    def trust(self, source_id: str, fingerprint: Any) -> dict:
        """Pin the certificate the camera presents NOW, but only when it is the one the user confirmed."""
        source = self._source(source_id)
        if source["control"]["scheme"] != "https":
            raise rc.ReolinkError(rc.NOT_SUPPORTED, "Certificate pinning only applies to HTTPS.")
        confirmed = rc.normalize_fingerprint(fingerprint)
        if not confirmed:
            raise rc.ReolinkError(rc.BAD_RESPONSE, "Send the SHA-256 fingerprint shown to the user.")
        seen = self._current_fingerprint(source)
        if not seen:
            raise rc.ReolinkError(rc.UNREACHABLE, "The camera could not be reached.")
        if seen != confirmed:
            raise rc.ReolinkError(rc.TLS_PIN_MISMATCH, "The camera now presents a different certificate than the one you confirmed.",
                                  fingerprint=seen)
        self._store.set_control_state(source_id, pinned_sha256=seen)
        self.forget(source_id)
        return {"pinned_sha256": seen}

    def capture(self, source_id: str, small: bool = False) -> bytes:
        """A FRESH picture for the gallery (full resolution unless `small`). Separate from the shared live-view
        cache of snapshot(): a gallery picture must be new and of its own size. Same client and lock, so it also
        waits for any command in flight."""
        return self._client(self._source(source_id)).snapshot(sub=bool(small))

    def zoom_read(self, source_id: str) -> dict:
        return self._client(self._source(source_id)).get_zoom_focus()

    def set_zoom(self, source_id: str, position: Any) -> dict:
        return self._client(self._source(source_id)).set_zoom(position)

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, source_id: str) -> bytes:
        """The shared picture: fresh enough (1 s) is reused; otherwise ONE caller fetches and the rest wait for it."""
        source = self._source(source_id)
        client = self._client(source)
        with self._lock:
            entry = self._snap.setdefault(source_id, {"lock": threading.Lock(), "at": -1e9, "data": b"", "error": None, "error_at": -1e9})
        with entry["lock"]:
            now = self._clock()
            if entry["data"] and now - entry["at"] < SNAPSHOT_MIN_INTERVAL_S:
                return entry["data"]
            if entry["error"] is not None and now - entry["error_at"] < FAILURE_MEMORY_S:
                raise entry["error"]
            started = now
            try:
                data = client.snapshot()
            except rc.ReolinkError as error:
                entry["error"], entry["error_at"] = error, self._clock()
                raise
            # The picture's age counts from when its fetch STARTED: a fetch takes 0.5 to 1 s on a weak link, and
            # counting from its end made the next poll (about a second after the previous one) hit the cache and
            # show the SAME picture again (measured on .104: a new camera fetch only every 2.1 s).
            entry["data"], entry["at"], entry["error"] = data, started, None
            return data
