"""Startup warning for a leftover data/initial_password.txt (H3).

v1.8.x wrote the auto-generated first-run password there in plaintext;
v1.9.0 no longer creates it but never removed an existing one. At startup,
if the file is still present we say so once - in the process log, the System
Log and the notification center - and tell the owner what to do, depending on
whether it still matches the current password hash. The password itself is
never logged, and the file is never deleted automatically.
"""

import os

from werkzeug.security import check_password_hash

FILENAME = "initial_password.txt"

MSG_VALID = (
    "A plaintext copy of your current password is stored in data/initial_password.txt "
    "— change your password in Settings → Security, then delete the file."
)
MSG_OBSOLETE = "data/initial_password.txt is an obsolete plaintext password file — delete it."


def _candidates(text):
    """Whole content, each line, and each line's last token (tolerates a
    'Password: xxxx' style label) - all stripped, de-duplicated, non-empty."""
    seen = []
    for chunk in [text] + text.splitlines():
        chunk = chunk.strip()
        for cand in (chunk, chunk.split()[-1] if chunk.split() else ""):
            if cand and cand not in seen:
                seen.append(cand)
    return seen


def password_file_matches(path, password_hash):
    if not password_hash:
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096)
    except OSError:
        return False
    return any(check_password_hash(password_hash, cand) for cand in _candidates(text))


def warn_if_initial_password_file(data_dir, auth_state, log_event, notify, log=print):
    """Returns "valid", "obsolete" or None (file absent). Call once per start."""
    path = os.path.join(data_dir, FILENAME)
    if not os.path.isfile(path):
        return None
    still_valid = password_file_matches(path, str((auth_state or {}).get("password_hash") or ""))
    message = MSG_VALID if still_valid else MSG_OBSOLETE
    log(f"[SECURITY] WARNING: {message}", flush=True)
    log_event("Plaintext password file present", "WARNING", message, source="security")
    notify("warning", "system", "Plaintext password file present", message)
    return "valid" if still_valid else "obsolete"
