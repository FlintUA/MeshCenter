import threading
import time

from meshsrv import error_reporting


class RadioConnectionManager:
    """Manage temporary release and reconnect of the Meshtastic radio.

    The MeshCenter process keeps running while the radio is released: for serial
    the listener is paused and the port freed for the official Meshtastic app; for
    TCP (RR1) the session is closed and MeshCenter does not dial again, so the
    radio's single client slot is free for another client.

    A release may carry a duration: `until` is an ABSOLUTE wall-clock time and
    `expired()` tells the owner's timer when to resume (RR1). A TCP release is
    persisted through `store` (per profile) so it survives a restart; a serial
    release stays in memory only.
    """

    def __init__(
        self,
        pause_event,
        stop_listener,
        wait_serial_release,
        serial_port,
        log_event=None,
        on_reconnect=None,
        transport_provider=None,
        tcp_release=None,
        tcp_resume=None,
        store=None,
        clock=time.time,
    ):
        self._pause_event = pause_event
        self._stop_listener = stop_listener
        self._wait_serial_release = wait_serial_release
        # Review round 4 (adapter-port design): `serial_port` may be a
        # zero-arg callable (server.py's current_serial_port, reflecting
        # whatever listener_supervisor last actually verified) instead of
        # a fixed string, read fresh wherever it's displayed/reported -
        # display-only here (wait_serial_release's own `device` arg below
        # is accepted for call-site compatibility but not actually used,
        # see server.py's wait_serial_release() docstring), but a stale
        # value here was still a real, if minor, discrepancy for anyone
        # reading status()'s "serial_port" field after a replug. A plain
        # string (every existing caller/test) is wrapped so nothing else
        # changes for them.
        self._serial_port_provider = serial_port if callable(serial_port) else (lambda: serial_port)
        self._log_event = log_event
        # H2-C Phase 2: called at the start of reconnect() - the radio may
        # have been swapped or reconfigured while released to an external
        # tool (the whole point of the release), so resuming should
        # re-verify identity rather than silently trust whatever was true
        # before the release. Optional/no-op default so this class stays
        # usable standalone (e.g. in tests) without a listener supervisor.
        self._on_reconnect = on_reconnect or (lambda: None)
        self._transport_provider = transport_provider or (lambda: "serial")
        self._tcp_release = tcp_release
        self._tcp_resume = tcp_resume
        self._store = store
        self._clock = clock
        self._until = None          # absolute epoch seconds, or None = until reconnect
        self._kind = "serial"       # which kind of release is (or was last) active
        self._lock = threading.RLock()
        self._mode = "connected"
        self._message = "The radio is controlled by MeshCenter."
        self._updated_at = time.time()
        self._released_at = None
        self._last_error = ""

    def _set_state(self, mode, message, error=""):
        with self._lock:
            self._mode = mode
            self._message = message
            self._last_error = str(error or "")
            self._updated_at = time.time()

    def _log(self, level, title, details):
        if self._log_event is None:
            return
        try:
            self._log_event(level, "radio", title, details)
        except Exception as error:
            print(f"[RADIO MANAGER] Log error: {error}", flush=True)

    def is_released(self):
        with self._lock:
            return self._mode in {"releasing", "released"}

    def commands_allowed(self):
        with self._lock:
            return self._mode not in {"releasing", "released", "reconnecting"}

    def release(self, timeout=12, duration_s=None):
        """Release the radio. `duration_s` None/0 = until Reconnect; otherwise the
        release ends by itself after that many seconds (the owner's timer calls
        reconnect() once `expired()`)."""
        kind = self._transport_provider()
        until = (self._clock() + float(duration_s)) if duration_s else None
        with self._lock:
            if self._mode == "released":
                return True, self.status(False)
            if self._mode in {"releasing", "reconnecting"}:
                return False, self.status(False)
            self._mode = "releasing"
            self._kind = "tcp" if kind == "tcp" else "serial"
            self._until = until
            self._message = "Stopping the listener and releasing the serial port..."
            self._last_error = ""
            self._updated_at = time.time()

        if kind == "tcp":
            return self._release_tcp(until)

        print("[RADIO MANAGER] Releasing radio", flush=True)
        self._pause_event.set()

        try:
            stopped = bool(self._stop_listener())
            if not stopped:
                raise RuntimeError("The Meshtastic listener could not be stopped")

            current_port = self._serial_port_provider()
            released = bool(
                self._wait_serial_release(
                    device=current_port,
                    timeout=timeout,
                )
            )
            if not released:
                raise RuntimeError(
                    f"The serial port is still busy: {current_port}"
                )

            with self._lock:
                self._mode = "released"
                self._message = (
                    "The radio is ready for configuration with the official "
                    "Meshtastic application."
                )
                self._released_at = time.time()
                self._updated_at = time.time()
                self._last_error = ""

            self._log(
                "ACTION",
                "Radio released",
                "USB serial connection released for external configuration",
            )
            print("[RADIO MANAGER] Radio released", flush=True)
            return True, self.status(False)

        except Exception as error:
            self._pause_event.clear()
            with self._lock:
                self._until = None
            if isinstance(error, RuntimeError):
                # Our own, deliberately worded reason ("listener could not be
                # stopped", "serial port is still busy: ...") - safe to show.
                shown = str(error)
            else:
                # Anything else is an unexpected fault: the status shows only a
                # generic text + id, the cause goes to the logs under that id.
                error_id = error_reporting.report_unexpected_error(
                    error, "radio_release", log_event=None, source="radio"
                )
                shown = f"{error_reporting.GENERIC_MESSAGE} (ID: {error_id})"
            self._set_state(
                "error",
                "The radio could not be released.",
                shown,
            )
            self._log("ERROR", "Radio release failed", shown)
            print(f"[RADIO MANAGER] Release failed: {error}", flush=True)
            return False, self.status(False)

    def reconnect(self):
        with self._lock:
            if self._mode == "connected":
                return True, self.status(True)
            if self._mode == "releasing":
                return False, self.status(False)
            kind = self._kind
            self._mode = "reconnecting"
            self._message = "Reconnecting MeshCenter to the Meshtastic radio..."
            self._last_error = ""
            self._until = None
            self._updated_at = time.time()

        if kind == "tcp":
            return self._reconnect_tcp()

        print("[RADIO MANAGER] Reconnect requested", flush=True)
        try:
            self._on_reconnect()
        except Exception as error:
            print(f"[RADIO MANAGER] on_reconnect hook error: {error}", flush=True)
        self._pause_event.clear()
        self._log(
            "ACTION",
            "Radio reconnect requested",
            "Meshtastic listener restart requested after external configuration",
        )
        return True, self.status(False)

    # ------------------------------------------------------------------
    # RR1: TCP release / resume, persistence, timed expiry
    # ------------------------------------------------------------------
    def _release_tcp(self, until):
        """Mode is already "releasing" (so every dialling path already sees the
        release); close the session, then persist and publish "released"."""
        print("[RADIO MANAGER] Releasing TCP radio", flush=True)
        try:
            if self._tcp_release is None:
                raise RuntimeError("TCP release is not available")
            self._tcp_release()
            released_at = self._clock()
            with self._lock:
                self._mode = "released"
                self._message = "The radio is free for another client; MeshCenter is not connecting."
                self._released_at = released_at
                self._updated_at = time.time()
                self._last_error = ""
            if self._store is not None:
                self._store.save({"released": True, "until": until, "since": released_at, "transport": "tcp"})
            self._log(
                "ACTION",
                "Radio released",
                "TCP connection closed - MeshCenter will not reconnect "
                + ("until you press Reconnect" if until is None else "until the chosen time has passed"),
            )
            print("[RADIO MANAGER] TCP radio released", flush=True)
            return True, self.status(False)
        except Exception as error:
            if isinstance(error, RuntimeError):
                shown = str(error)
            else:
                error_id = error_reporting.report_unexpected_error(
                    error, "radio_release", log_event=None, source="radio"
                )
                shown = f"{error_reporting.GENERIC_MESSAGE} (ID: {error_id})"
            with self._lock:
                self._until = None
            self._set_state("error", "The radio could not be released.", shown)
            self._log("ERROR", "Radio release failed", shown)
            print(f"[RADIO MANAGER] TCP release failed: {error}", flush=True)
            return False, self.status(False)

    def _reconnect_tcp(self):
        print("[RADIO MANAGER] TCP reconnect requested", flush=True)
        try:
            if self._store is not None:
                self._store.clear()
            if self._tcp_resume is not None:
                self._tcp_resume()
        except Exception as error:
            print(f"[RADIO MANAGER] TCP resume hook error: {error}", flush=True)
        with self._lock:
            self._mode = "connected"
            self._message = "The radio is controlled by MeshCenter."
            self._released_at = None
            self._until = None
            self._last_error = ""
            self._updated_at = time.time()
        self._log("ACTION", "Radio reconnect requested", "TCP release ended - reconnecting to the radio")
        return True, self.status(False)

    def end_release(self):
        """End a TCP release WITHOUT the resume hook: an explicit connect/reconnect/
        activation is itself the Reconnect and does the connecting. True if there
        was a TCP release to end."""
        with self._lock:
            if self._mode not in {"released", "error"} or self._kind != "tcp":
                return False
        if self._store is not None:
            self._store.clear()
        with self._lock:
            self._mode = "connected"
            self._message = "The radio is controlled by MeshCenter."
            self._released_at = None
            self._until = None
            self._last_error = ""
            self._updated_at = time.time()
        self._log("ACTION", "Radio reconnect requested", "TCP release ended by an explicit connect")
        return True

    def restore_released(self, until, since=None, kind="tcp"):
        """Boot: adopt a persisted release WITHOUT touching the radio."""
        with self._lock:
            self._mode = "released"
            self._kind = kind
            self._until = until
            self._released_at = since or self._clock()
            self._message = "The radio is free for another client; MeshCenter is not connecting."
            self._last_error = ""
            self._updated_at = time.time()

    def load_persisted_release(self):
        """(until, since) of a persisted, still-valid release, else None. An
        expired record is removed (connect normally)."""
        if self._store is None:
            return None
        record = self._store.load()
        if not isinstance(record, dict) or not record.get("released"):
            return None
        until = record.get("until")
        if until is not None:
            try:
                until = float(until)
            except (TypeError, ValueError):
                self._store.clear()
                return None
            if until <= self._clock():
                self._store.clear()
                return None
        return until, record.get("since")

    def expired(self):
        """True when a timed release has run out and the owner should resume."""
        with self._lock:
            return (
                self._mode == "released"
                and self._until is not None
                and self._clock() >= self._until
            )

    def listener_started(self):
        with self._lock:
            if self._mode in {"reconnecting", "error", "connected"}:
                self._mode = "connected"
                self._message = "The radio is controlled by MeshCenter."
                self._last_error = ""
                self._released_at = None
                self._until = None
                self._updated_at = time.time()

    def listener_stopped(self):
        # A manual release owns the stopped state. Unexpected listener failures
        # remain handled by the existing Radio Health / auto-recovery logic.
        return

    def status(self, listener_running=False):
        with self._lock:
            if self._mode == "reconnecting" and listener_running:
                self._mode = "connected"
                self._message = "The radio is controlled by MeshCenter."
                self._released_at = None
                self._last_error = ""
                self._updated_at = time.time()

            return {
                "mode": self._mode,
                "released": self._mode in {"releasing", "released"},
                "commands_allowed": self._mode not in {
                    "releasing", "released", "reconnecting"
                },
                "listener_running": bool(listener_running),
                "message": self._message,
                "serial_port": self._serial_port_provider(),
                "updated_at": self._updated_at,
                "released_at": self._released_at,
                "last_error": self._last_error,
                # RR1: when a timed release ends (absolute epoch seconds; None =
                # until Reconnect) and which kind of radio link is released.
                "until": self._until if self._mode in {"releasing", "released"} else None,
                "remaining_s": (
                    max(0, int(self._until - self._clock()))
                    if self._until is not None and self._mode in {"releasing", "released"}
                    else None
                ),
                "release_kind": self._kind if self._mode in {"releasing", "released"} else None,
            }
