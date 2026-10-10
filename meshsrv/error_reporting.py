"""Unexpected-error handling for HTTP responses.

An unexpected exception must never reach the client as `str(e)` or a
traceback (paths, library internals, command lines). Instead the response
carries a generic message, a stable `error_code` ("internal_error") and a
short random `error_id`; the full exception and traceback are logged
server-side under the SAME id (process log + one System Log ERROR entry), so a
user can quote the id and the cause can be found.

Domain errors - validation, "radio busy", storage failures, CSRF, identity /
transport refusals - are NOT unexpected: they keep their own specific message
and code. Code that wants to surface a message from deep inside a handler
raises `UserFacingError` (its message is, by definition, safe to show).

Request bodies are never logged here: only the exception type, its message
(truncated), the route/context name and the traceback.
"""

import secrets
import traceback

GENERIC_MESSAGE = "Internal server error"
ERROR_CODE = "internal_error"


class UserFacingError(Exception):
    """An error whose message is safe to show to the user as-is."""

    def __init__(self, message, status=400, error_code=None):
        super().__init__(message)
        self.message = str(message)
        self.status = int(status)
        self.error_code = error_code


def new_error_id():
    """8 hex characters - short enough to read out, plenty to find in a log."""
    return secrets.token_hex(4)


def _default_log_event():
    """system_log.log_event, imported lazily: system_log reads config.py at
    import time, which modules importing this one (camera, tests) may not have."""
    try:
        from system_log import log_system_event
        return log_system_event
    except Exception:
        return None


def report_unexpected_error(exc, where, log_event="auto", log_print=print, source="server"):
    """Log `exc` (full traceback in the process log, one ERROR entry in the
    System Log) under a fresh error id and return that id.

    `where` names the route/function (never user input). Never raises: logging
    problems must not turn a handled error into a second one."""
    error_id = new_error_id()
    try:
        log_print(f"[ERROR {error_id}] {where}: {type(exc).__name__}: {exc}", flush=True)
        log_print(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            flush=True,
        )
    except Exception:
        pass
    if log_event == "auto":
        log_event = _default_log_event()
    if log_event is not None:
        try:
            log_event(
                f"Unexpected error in {where}",
                "ERROR",
                f"[{error_id}] {type(exc).__name__}: {str(exc)[:300]}",
                source=source,
            )
        except Exception:
            pass
    return error_id


def internal_error_payload(error_id):
    """The JSON body for an unexpected error (HTTP 500)."""
    return {
        "ok": False,
        "error": GENERIC_MESSAGE,
        "error_code": ERROR_CODE,
        "error_id": error_id,
    }
