#!/usr/bin/env python3
"""Installation ID generation and validation, plus confirmed-UTC-time
helpers used to timestamp an ID's assignment.

Stage A of the installation-ID rollout (see the external implementation
spec) added generate_installation_id()/is_valid_installation_id(). Stage C
adds get_confirmed_utc_time()/format_utc_iso8601() per the plan this
module's docstring originally stated.

An installation ID identifies a MeshCenter *install*, not a radio - it is
pure entropy (secrets.token_hex()), with no hardware or personal data of
any kind folded in, so it carries no fingerprinting risk and stays valid
across a radio swap or profile switch.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

_FORMAT_RE = re.compile(r"^MC1(?:-[0-9A-F]{4}){5}$")


def generate_installation_id() -> str:
    """Return a new installation ID: MC1-XXXX-XXXX-XXXX-XXXX-XXXX.

    10 bytes (secrets.token_hex(10)) produce 20 hex characters, grouped into
    five 4-character blocks - exactly matching the five groups in the format
    below.
    """
    hex_value = secrets.token_hex(10).upper()
    groups = [hex_value[i:i + 4] for i in range(0, 20, 4)]
    return "MC1-" + "-".join(groups)


def is_valid_installation_id(value: str) -> bool:
    """Return True if value matches the installation ID format exactly."""
    return bool(_FORMAT_RE.fullmatch(str(value or "")))


def format_utc_iso8601(value: datetime) -> str:
    """Format `value` as an ISO-8601 string with an explicit UTC offset.

    Deliberately does not convert to the local system timezone (unlike
    meshsrv/radio_identity.py's utc_now_iso(), which calls .astimezone()
    despite its name) - an installation's assigned_at is meant to be a
    universal, NTP-confirmed instant, not local wall-clock time.
    """
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def get_confirmed_utc_time() -> str | None:
    """Return the current UTC time as an ISO-8601 string, but only if the
    system's own NTP sync is currently confirmed (via meshsrv.time_service,
    already hardened against timedatectl being flaky/inaccessible) -
    otherwise None, meaning the caller isn't allowed to trust the current
    wall clock yet.

    Lazy import: meshsrv.time_service pulls in subprocess/hardware.* to do
    real NTP probing - keeping that import local to this function, rather
    than at module level, means generate_installation_id()/
    is_valid_installation_id() (and any caller/test of just those two)
    stay free of that heavier import surface.
    """
    from meshsrv.time_service import get_status

    status = get_status()
    if not status.get("synchronized"):
        return None
    return format_utc_iso8601(datetime.now(timezone.utc))
