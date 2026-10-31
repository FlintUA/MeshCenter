"""
Schedule action "send_camera_picture" (G3): a fresh picture of a network camera, resized and sent to a trusted
contact through MCAttach.

Pure logic, everything it touches is injected (`PictureDeps`), so the fake-clock tests need no camera, radio or
server. meshsrv/schedule_actions.py builds the real `PictureDeps` from what server.py hands in.

Rules (decided with the owner):
  * at most one picture per 15 minutes per action: refused on save (an interval rule shorter than that) AND at run
    time (`state.last_run_at`, persisted in the rule, so a restart or an edited rule cannot get around it);
  * a missing camera, radio or key never raises: the run is skipped with one System Log line and one notification;
  * the picture comes from the Take picture service (the camera's own full picture), the copy for sending from the
    same code the Media gallery uses (resized to 1280 px by default, location/camera data removed).
"""
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

ACTION_TYPE = "send_camera_picture"
MIN_INTERVAL_S = 15 * 60
# The engine ticks once a minute and compares whole seconds, so a rule that repeats every 15 minutes runs 899.x s after
# the last run about half the time. The run-time guard allows that much slack (found in the live check); anything
# clearly closer than 15 minutes is still refused.
TICK_TOLERANCE_S = 5
RESIZE_CHOICES = ("1280", "1920", "original")
DEFAULT_RESIZE = "1280"
_CONTACT_RE = re.compile(r"^![0-9a-f]{8}$")


class RuleInvalid(ValueError):
    """A schedule rule that must not be saved; `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PictureError(Exception):
    """A step of the run could not be done. `code` is what the System Log and the notification carry."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class PictureDeps:
    clock: Callable[[], float]
    radio_available: Callable[[], bool]
    source_exists: Callable[[str], bool]
    contact_trusted: Callable[[str], bool]
    take_picture: Callable[[str], dict]                 # source id -> {"filename", "display_name"}; raises PictureError
    prepare_copy: Callable[[str, str], bytes]           # (gallery filename, size key) -> JPEG bytes; raises PictureError
    create_transfer: Callable[[str, str, bytes], None]  # (contact id, file name, JPEG); raises PictureError
    log: Callable[[str, str, str], None]                # (level, title, details)
    notify: Callable[[str, str, str], None]             # (level, title, body)


def normalize_params(params: Optional[dict]) -> dict:
    p = dict(params or {})
    p["source_id"] = str(p.get("source_id") or "").strip()
    p["contact_id"] = str(p.get("contact_id") or "").strip().lower()
    resize = str(p.get("resize") or DEFAULT_RESIZE)
    p["resize"] = resize if resize in RESIZE_CHOICES else DEFAULT_RESIZE
    return p


def validate_rule(rule: dict) -> None:
    """Raise RuleInvalid when a send_camera_picture action (or its trigger) cannot be saved."""
    actions = [a for a in rule.get("actions", []) if isinstance(a, dict) and a.get("type") == ACTION_TYPE]
    if not actions:
        return
    trigger = rule.get("trigger") or {}
    for action in actions:
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        source_id = str(params.get("source_id") or "").strip()
        if not source_id or len(source_id) > 64:
            raise RuleInvalid("camera_required", "Choose a network camera.")
        if not _CONTACT_RE.match(str(params.get("contact_id") or "").strip().lower()):
            raise RuleInvalid("contact_required", "Choose a contact.")
        if str(params.get("resize") or DEFAULT_RESIZE) not in RESIZE_CHOICES:
            raise RuleInvalid("invalid_resize", "Choose 1280, 1920 or original.")
    if trigger.get("mode") == "interval":
        try:
            minutes = int(trigger.get("interval_minutes", 60))
        except (TypeError, ValueError):
            minutes = 0
        if minutes * 60 < MIN_INTERVAL_S:
            raise RuleInvalid("interval_too_short",
                              "A picture schedule must repeat at most every %d minutes." % (MIN_INTERVAL_S // 60))


def run(action: dict, rule: dict, deps: PictureDeps) -> str:
    """Run one send_camera_picture action. Returns "sent" or the skip reason (never raises for an expected failure).
    Mutates `action["state"]`; the engine saves the rule afterwards."""
    title = "Schedule: %s" % (rule.get("label") or rule.get("id") or "picture")
    params = normalize_params(action.get("params"))
    state = action.setdefault("state", {})
    now = deps.clock()

    last = state.get("last_run_at") or 0
    if last and now - last < MIN_INTERVAL_S - TICK_TOLERANCE_S:
        return _skip(deps, title, "too_soon", "the last picture run was %d s ago (minimum %d s)" % (now - last, MIN_INTERVAL_S))
    state["last_run_at"] = now

    if not deps.radio_available():
        return _skip(deps, title, "radio_unavailable", "the radio is released or not connected")
    if not deps.contact_trusted(params["contact_id"]):
        return _skip(deps, title, "key_unavailable", "%s has no trusted key" % params["contact_id"])
    if not params["source_id"] or not deps.source_exists(params["source_id"]):
        return _skip(deps, title, "camera_unavailable", "the camera is not configured any more")

    try:
        shot = deps.take_picture(params["source_id"])
        data = deps.prepare_copy(shot["filename"], params["resize"])
        deps.create_transfer(params["contact_id"], shot.get("display_name") or "picture.jpg", data)
    except PictureError as error:
        return _skip(deps, title, error.code, str(error))

    state["last_sent_at"] = now
    deps.log("INFO", title, "Picture sent to %s (%s, %d bytes)" % (params["contact_id"], params["resize"], len(data)))
    return "sent"


def _skip(deps: PictureDeps, title: str, code: str, detail: str) -> str:
    deps.log("WARNING", title, "Picture not sent (%s): %s" % (code, detail))
    deps.notify("warning", title, "Picture not sent (%s)" % code)
    return code


def real_clock() -> float:
    return time.time()
