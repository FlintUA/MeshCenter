"""Pure text parsers for `v4l2-ctl` output - CAM-1 (audit review 2026-09-30,
finding F10): replaces `linuxpy`/`v4l2py` (GPL-3.0-or-later, imported
in-process by the old camera/usb_driver.py) with the same arm's-length
external-program approach already used for the `meshtastic` CLI. No
imports from `camera/` or `server.py` - these functions take a string,
return plain dicts/sets/lists, and never raise: a parse failure (missing
binary, unexpected/future v4l2-ctl output format, truncated capture)
returns an empty result, the same "known-good floor, not a guess" spirit
`usb_driver.py`'s old `CONFIRMED_RESOLUTIONS` fallback already used.

Every regex here was written against real `v4l2-ctl` output captured live
from the one camera CAM-1 was actually verified against (Microsoft
USB3.0 HD CAMERA, 045e:8888, YUYV-only) - see tests/fixtures/v4l2/ for
the exact captures and tests/test_v4l2_ctl_parse.py for the tests built
from them. The output format is v4l2-ctl's own human-readable text, not
a documented stable API (CAM-0 report, section 3's parsing-fragility
note) - a v4l-utils version bump could reformat it, which is why every
caller of these functions must be prepared for an empty result rather
than assuming a match.
"""

from __future__ import annotations

import re


def parse_device_caps(info_text: str) -> set[str]:
    """Capability strings listed under the per-node `Device Caps:` block of
    `v4l2-ctl -d /dev/videoN --info` - e.g. {"Video Capture", "Streaming",
    "Extended Pix Format"}. Deliberately NOT the `Capabilities:` block
    (the deprecated aggregate-across-all-nodes-of-this-device field) -
    same distinction the old usb_driver.py's discover_usb_cameras()
    relied on via linuxpy's `device_capabilities` vs `capabilities`:
    confirmed live that a capture node and its own metadata-only sibling
    node report the *same* aggregate `Capabilities:` value, but different
    `Device Caps:` values (only the capture node's has "Video Capture").
    """
    if not info_text:
        return set()

    lines = info_text.splitlines()
    caps: set[str] = set()
    in_block = False
    for line in lines:
        if re.match(r"\s*Device Caps\s*:", line):
            in_block = True
            continue
        if not in_block:
            continue
        stripped = line.strip()
        # The block ends at the first line that isn't a plain capability
        # name - either a new "Key: value" line (the next top-level field)
        # or a blank line/end of the section this device's block sits in.
        if not stripped or ":" in stripped:
            break
        caps.add(stripped)
    return caps


def parse_card_name(info_text: str) -> str:
    """The `Card type` value from `--info` output, stripped. Empty string
    if the line isn't present or the text doesn't parse."""
    if not info_text:
        return ""
    match = re.search(r"Card type\s*:\s*(.*)", info_text)
    if not match:
        return ""
    return match.group(1).strip()


def parse_list_formats_ext(text: str) -> dict[str, list[dict]]:
    """`v4l2-ctl -d /dev/videoN --list-formats-ext` output into
    {pixel_format_fourcc: [{"width": int, "height": int, "fps": [float, ...]}, ...]}.

    Real sample this was built against (tests/fixtures/v4l2/
    microsoft_usb3_hd_camera_list_formats_ext.txt):

        [0]: 'YUYV' (YUYV 4:2:2)
            Size: Discrete 1920x1080
                Interval: Discrete 0.033s (30.000 fps)
                Interval: Discrete 0.040s (25.000 fps)
            Size: Discrete 1280x720
                Interval: Discrete 0.017s (60.000 fps)
                ...

    Only "Discrete" sizes/intervals are handled - a camera advertising a
    continuous/stepwise range (rare for UVC webcams, not seen on the
    hardware this was verified against) would simply not contribute any
    entries for that block, same "return less than everything rather than
    guess" stance as the rest of this module.
    """
    if not text:
        return {}

    formats: dict[str, list[dict]] = {}
    current_format: str | None = None
    current_size: dict | None = None

    format_re = re.compile(r"^\[\d+\]:\s*'(\w+)'")
    size_re = re.compile(r"^Size:\s*Discrete\s*(\d+)x(\d+)")
    interval_re = re.compile(r"^Interval:\s*Discrete\s*[\d.]+s\s*\(([\d.]+)\s*fps\)")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        format_match = format_re.match(line)
        if format_match:
            current_format = format_match.group(1)
            formats.setdefault(current_format, [])
            current_size = None
            continue

        size_match = size_re.match(line)
        if size_match and current_format is not None:
            current_size = {
                "width": int(size_match.group(1)),
                "height": int(size_match.group(2)),
                "fps": [],
            }
            formats[current_format].append(current_size)
            continue

        interval_match = interval_re.match(line)
        if interval_match and current_size is not None:
            current_size["fps"].append(float(interval_match.group(1)))
            continue

    return formats


def parse_list_ctrls(text: str) -> dict[str, dict]:
    """`v4l2-ctl -d /dev/videoN --list-ctrls` output into
    {control_name: {"type": str, "min": int|None, "max": int|None,
    "step": int|None, "default": int|None, "value": int, "menu_label": str|None}}.

    Real sample this was built against (tests/fixtures/v4l2/
    microsoft_usb3_hd_camera_list_ctrls.txt):

                             brightness 0x00980900 (int)    : min=1 max=15 step=1 default=7 value=7 flags=has-min-max
               power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1 (50 Hz)

    Section headers ("User Controls", "Camera Controls") are skipped -
    the flat name->dict result doesn't distinguish them, matching what
    UsbCameraDriver.get_controls() needs (a single namespace of settable
    controls), not a UI grouping.
    """
    if not text:
        return {}

    controls: dict[str, dict] = {}
    line_re = re.compile(
        r"^(\S+)\s+0x[0-9a-fA-F]+\s*\((\w+)\)\s*:\s*(.*)$"
    )
    kv_re = re.compile(r"(\w+)=(-?\d+)")
    menu_label_re = re.compile(r"\(([^()=]+)\)\s*$")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = line_re.match(line)
        if not match:
            continue

        name, ctrl_type, rest = match.group(1), match.group(2), match.group(3)
        values = {key: int(value) for key, value in kv_re.findall(rest)}

        menu_label = None
        if ctrl_type == "menu":
            label_match = menu_label_re.search(rest)
            if label_match:
                menu_label = label_match.group(1).strip()

        controls[name] = {
            "type": ctrl_type,
            "min": values.get("min"),
            "max": values.get("max"),
            "step": values.get("step"),
            "default": values.get("default"),
            "value": values.get("value"),
            "menu_label": menu_label,
        }

    return controls


def ffmpeg_and_v4l2ctl_available(ffmpeg_path: str | None, v4l2_ctl_path: str | None) -> bool:
    """True only if both resolved paths are non-empty. Pure - callers
    resolve the actual paths once via shutil.which() (see usb_driver.py's
    module-level FFMPEG_PATH/V4L2_CTL_PATH) and pass them in here, rather
    than this function doing its own shutil.which() calls, so it stays a
    plain, deterministically-testable function of its arguments."""
    return bool(ffmpeg_path) and bool(v4l2_ctl_path)
