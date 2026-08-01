import threading
import time


class RadioConnectionManager:
    """Manage temporary release and reconnect of the Meshtastic serial radio.

    The MeshCenter process keeps running while the listener is paused and the
    serial port is released for use by the official Meshtastic application.
    """

    def __init__(
        self,
        pause_event,
        stop_listener,
        wait_serial_release,
        serial_port,
        log_event=None,
    ):
        self._pause_event = pause_event
        self._stop_listener = stop_listener
        self._wait_serial_release = wait_serial_release
        self._serial_port = serial_port
        self._log_event = log_event
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

    def release(self, timeout=12):
        with self._lock:
            if self._mode == "released":
                return True, self.status(False)
            if self._mode in {"releasing", "reconnecting"}:
                return False, self.status(False)
            self._mode = "releasing"
            self._message = "Stopping the listener and releasing the serial port..."
            self._last_error = ""
            self._updated_at = time.time()

        print("[RADIO MANAGER] Releasing radio", flush=True)
        self._pause_event.set()

        try:
            stopped = bool(self._stop_listener())
            if not stopped:
                raise RuntimeError("The Meshtastic listener could not be stopped")

            released = bool(
                self._wait_serial_release(
                    device=self._serial_port,
                    timeout=timeout,
                )
            )
            if not released:
                raise RuntimeError(
                    f"The serial port is still busy: {self._serial_port}"
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
            self._set_state(
                "error",
                "The radio could not be released.",
                str(error),
            )
            self._log("ERROR", "Radio release failed", str(error))
            print(f"[RADIO MANAGER] Release failed: {error}", flush=True)
            return False, self.status(False)

    def reconnect(self):
        with self._lock:
            if self._mode == "connected":
                return True, self.status(True)
            if self._mode == "releasing":
                return False, self.status(False)
            self._mode = "reconnecting"
            self._message = "Reconnecting MeshCenter to the Meshtastic radio..."
            self._last_error = ""
            self._updated_at = time.time()

        print("[RADIO MANAGER] Reconnect requested", flush=True)
        self._pause_event.clear()
        self._log(
            "ACTION",
            "Radio reconnect requested",
            "Meshtastic listener restart requested after external configuration",
        )
        return True, self.status(False)

    def listener_started(self):
        with self._lock:
            if self._mode in {"reconnecting", "error", "connected"}:
                self._mode = "connected"
                self._message = "The radio is controlled by MeshCenter."
                self._last_error = ""
                self._released_at = None
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
                "serial_port": self._serial_port,
                "updated_at": self._updated_at,
                "released_at": self._released_at,
                "last_error": self._last_error,
            }
