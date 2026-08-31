#!/usr/bin/env python3
"""Installation ID generation and validation.

Stage A of the installation-ID rollout (see the external implementation
spec) - generator/validator only. The spec's module also lists
get_confirmed_utc_time()/format_utc_iso8601() as eventually belonging in
this same file; those are deferred to a later stage and deliberately not
present here.

An installation ID identifies a MeshCenter *install*, not a radio - it is
pure entropy (secrets.token_hex()), with no hardware or personal data of
any kind folded in, so it carries no fingerprinting risk and stays valid
across a radio swap or profile switch.
"""

from __future__ import annotations

import re
import secrets

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
