"""Optional whole-app password protection.

On by default as of config.example.py's AUTH_ENABLED=True (P1 #5
stabilization follow-up) - a fresh install with no data/auth.json yet
gets a randomly generated initial password (see load_auth_state()'s
generation branch below), not silent no-protection. Single shared
password, no usernames/roles - a Flask session cookie is set on
successful login and checked in a before_request hook. State (enabled
flag + password hash) lives in its own data/auth.json rather than
settings.json, so it can never be silently wiped by the generic
POST /api/settings merge-and-replace in api_settings.py, and the hash is
never included in a settings.json snapshot handed back to the browser.

The login route's `next` redirect target is validated by
_is_safe_redirect_target() - see that function's own docstring for the
open-redirect vulnerability independently reproduced and fixed there.
"""

import os
import secrets
import time
from urllib.parse import quote, urlsplit

from flask import jsonify, redirect, request, render_template, session
from werkzeug.security import check_password_hash, generate_password_hash

from storage.json_store import safe_read_json, safe_write_json

# P1 #6 stabilization follow-up: raised from 4 to the top of the 10-12
# range the original audit recommended - this app has full access to
# system restart/reboot/Wi-Fi/updates once logged in, so a short password
# is a disproportionately large blast radius for a single shared secret.
MIN_PASSWORD_LENGTH = 12

# Paths that must stay reachable without a session so the login page itself
# (and the assets it needs) can render.
_EXEMPT_PATH_PREFIXES = ("/static/",)
_EXEMPT_PATHS = ("/login",)

# Login throttling (P1 #6): the first few wrong-password attempts from one
# source are free (typos happen), then the lockout window doubles each
# attempt up to a cap. Keyed by request.remote_addr, not by account - this
# app has exactly one shared password, no per-user identity to throttle
# against. In-memory only (no persistence across a process restart) is a
# deliberate choice, not an oversight: this app already runs as a single
# gunicorn worker with all runtime state in one process's memory (see
# CLAUDE.md's "workers = 1" rationale), and restarting the service itself
# requires access (sudoers-gated) far beyond what this throttle defends
# against - persisting the counter to disk would add I/O on every login
# attempt for no realistic security benefit here.
_LOGIN_THROTTLE_FREE_ATTEMPTS = 5
_LOGIN_THROTTLE_BASE_SECONDS = 2
_LOGIN_THROTTLE_MAX_SECONDS = 300
# Stale throttle entries (nothing failed recently) are swept out on access
# rather than kept forever, so the dict doesn't grow unboundedly if probed
# from many different source IPs.
_LOGIN_THROTTLE_ENTRY_TTL_SECONDS = 86400
# fail_count itself has no upper bound - an attacker (or just enough real
# time) can push it arbitrarily high, since a sustained attempt every
# ~_LOGIN_THROTTLE_MAX_SECONDS keeps refreshing last_seen forever, so the
# TTL sweep above never reclaims that entry. The exponent handed to `**`
# must therefore be capped independently of the final delay cap: 9 is the
# smallest exponent where BASE * 2**9 (1024s) already exceeds
# MAX_SECONDS (300s) for the current constants, so anything past it would
# get clamped to the same 300s regardless - capping here avoids computing
# an ever-larger bignum (2 ** over) for every failed attempt an
# indefinitely-persistent attacker sends.
_LOGIN_THROTTLE_MAX_EXPONENT = 9


def _login_throttle_delay(fail_count):
    """Seconds to lock out the *next* attempt after `fail_count` consecutive
    failures have just been recorded. The first _LOGIN_THROTTLE_FREE_ATTEMPTS
    failures set no lockout at all (attempt number FREE_ATTEMPTS+1 still goes
    through unthrottled); starting with the FREE_ATTEMPTS-th failure, a
    lockout is set immediately so attempt FREE_ATTEMPTS+1 onward is blocked,
    doubling each time past that and capped at _LOGIN_THROTTLE_MAX_SECONDS.

    Concretely, with the default of 5 free attempts: failures 1-4 set no
    lockout (so attempt 5 is never throttled), failure 5 sets a lockout that
    blocks attempt 6, failure 6 (once attempt 6 is eventually retried and
    fails) doubles it, and so on. Off-by-one here is easy to get backwards -
    see test_login_throttle_delay_boundary_between_5th_and_6th_failure and
    test_login_5th_wrong_attempt_still_plain_error_6th_is_throttled, which
    pin the exact attempt-6-not-attempt-7 behavior.

    `fail_count` is unbounded input (see _LOGIN_THROTTLE_MAX_EXPONENT above)
    - this function stays cheap and correct for any fail_count, not just
    the small values exercised by the boundary tests; see
    test_login_throttle_delay_stays_capped_for_very_large_fail_counts.
    """
    over = fail_count - _LOGIN_THROTTLE_FREE_ATTEMPTS + 1
    if over <= 0:
        return 0
    exponent = min(over - 1, _LOGIN_THROTTLE_MAX_EXPONENT)
    return min(_LOGIN_THROTTLE_BASE_SECONDS * (2 ** exponent), _LOGIN_THROTTLE_MAX_SECONDS)


def _generate_initial_password():
    """16 hex characters, 64 bits of entropy - easy to read and retype
    from a terminal or log line, no ambiguous 0/O or 1/l/I characters
    since hex digits are only 0-9a-f. A separate, patchable function so
    tests can spy on whether generation was ever attempted, not just
    inspect its result."""
    return secrets.token_hex(8)


def _write_initial_password_file(auth_file, plaintext_password):
    """One-time plaintext copy of a freshly generated password, next to
    auth.json. auth.json itself only ever stores the hash (fine to be
    world-readable, same as before this change) - this file is the
    actual credential in recoverable form, so unlike safe_write_json()'s
    default permissions it must be locked down explicitly. Never
    overwritten by anything else - deleting it (or its content no longer
    matching the live password) is the user's own signal that they've
    saved/changed the password elsewhere.
    """
    directory = os.path.dirname(auth_file) or "."
    path = os.path.join(directory, "initial_password.txt")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(plaintext_password + "\n")
        os.chmod(path, 0o600)
        return path
    except OSError as error:
        print(f"[AUTH] Could not write {path}: {error}", flush=True)
        return None


def load_auth_state(auth_file, bootstrap_enabled=False, bootstrap_password_hash=""):
    data = safe_read_json(auth_file, default=None)
    if isinstance(data, dict) and ("password_hash" in data or "enabled" in data):
        return {
            "enabled": bool(data.get("enabled", False)),
            "password_hash": str(data.get("password_hash") or ""),
        }

    # First run: seed from config.py's bootstrap values, same pattern as
    # WEATHER_PROVIDER's config->settings.json handoff.
    bootstrap_password_hash = str(bootstrap_password_hash or "")

    if bool(bootstrap_enabled) and not bootstrap_password_hash.strip():
        # New install with AUTH_ENABLED=True and no hash set - the normal
        # case now that config.example.py defaults to True. Generate a
        # real one-time password and persist it immediately (unlike the
        # plain bootstrap-in-memory below, which nothing ever writes to
        # disk) instead of silently staying unprotected - covers every
        # startup path uniformly (install.sh, meshcenter-firstboot.sh,
        # and CLAUDE.md's own documented manual `cp config.example.py
        # config.py` path, which installer-side generation alone would
        # have missed).
        plaintext_password = _generate_initial_password()
        state = {
            "enabled": True,
            "password_hash": generate_password_hash(plaintext_password),
        }
        safe_write_json(auth_file, state)
        password_file = _write_initial_password_file(auth_file, plaintext_password)

        print(f"[AUTH] No password configured - generated one: {plaintext_password}", flush=True)
        if password_file:
            print(
                f"[AUTH] Saved to {password_file} (readable only by this service's own "
                "user) - log in and change it via Settings -> Security.",
                flush=True,
            )
        return state

    return {
        "enabled": bool(bootstrap_enabled) and bool(bootstrap_password_hash.strip()),
        "password_hash": bootstrap_password_hash,
    }


def _is_safe_redirect_target(target):
    """True only for a same-origin relative path/query/fragment - the
    login route's `next` param must never send a browser off-origin.

    Not just a startswith("/")/startswith("//") check (what this
    replaced): independently reproduced a live open redirect against
    that exact old check via `next=/%09/evil.example` - real browser
    navigation confirmed, not just a suspicious-looking string. Werkzeug
    silently strips the tab character while building the Location header,
    turning `/\t/evil.example` into `//evil.example` (a protocol-relative
    absolute URL) *after* the old prefix check already passed it.
    urlsplit() resolves scheme/netloc correctly for this case (and
    similar ones) because it does real URL parsing, not a guess at one
    specific bypass character.

    Backslash (`/\\evil.example`) is rejected explicitly too, even though
    it was independently verified NOT to be exploitable against the
    Werkzeug version this project currently runs (it percent-encodes `\\`
    to `%5C` before the Location header is ever sent, so a browser never
    sees a raw backslash to normalize) - relying on that as the only
    defense would silently break if Werkzeug's encoding behavior ever
    changes, and urlsplit() alone does not catch it (Python's URL parser
    doesn't treat backslash as a separator the way browsers do).
    """
    if not target or not isinstance(target, str):
        return False
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    parsed = urlsplit(target)
    return not parsed.scheme and not parsed.netloc


def is_protected(auth_state):
    # An enabled flag with no usable hash never blocks access - avoids a
    # misconfigured/corrupted auth.json permanently locking out the UI.
    return bool(auth_state.get("enabled")) and bool(str(auth_state.get("password_hash") or "").strip())


def register_auth_routes(app, state_lock, auth_state, auth_file, handle_errors, resolve_ui_language=None):
    # Closure-local, not module-level: a fresh dict per register_auth_routes()
    # call means each test (and each real app instance) starts with a clean
    # slate, same lifetime as auth_state itself.
    login_throttle = {}

    def _save():
        with state_lock:
            safe_write_json(auth_file, auth_state)

    def _ui_language():
        if resolve_ui_language is None:
            return "en"
        try:
            return resolve_ui_language()
        except Exception:
            return "en"

    def _client_key():
        return request.remote_addr or "unknown"

    def _prune_login_throttle(now):
        stale = [
            key for key, entry in login_throttle.items()
            if now - entry.get("last_seen", 0) > _LOGIN_THROTTLE_ENTRY_TTL_SECONDS
        ]
        for key in stale:
            del login_throttle[key]

    @app.before_request
    def _enforce_auth():
        path = request.path
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PATH_PREFIXES):
            return None

        with state_lock:
            protected = is_protected(auth_state)

        if not protected or session.get("authenticated"):
            return None

        if path.startswith("/api/"):
            return jsonify({
                "ok": False,
                "error": "Authentication required",
                "error_code": "auth_required",
            }), 401

        return redirect("/login?next=" + quote(path, safe=""))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        with state_lock:
            protected = is_protected(auth_state)
            password_hash = str(auth_state.get("password_hash") or "")

        if not protected:
            return redirect("/")

        next_url = request.values.get("next") or "/"
        if not _is_safe_redirect_target(next_url):
            next_url = "/"

        error = None
        retry_after = None
        if request.method == "POST":
            client_key = _client_key()
            now = time.monotonic()

            with state_lock:
                _prune_login_throttle(now)
                entry = login_throttle.get(client_key)
                locked_until = entry["locked_until"] if entry else 0

            if now < locked_until:
                retry_after = int(locked_until - now) + 1
                return render_template(
                    "login.html",
                    error="login_throttled",
                    retry_after=retry_after,
                    next=next_url,
                    ui_language=_ui_language(),
                ), 429

            password = request.form.get("password", "")
            if password and check_password_hash(password_hash, password):
                with state_lock:
                    login_throttle.pop(client_key, None)
                session.clear()
                session.permanent = True
                session["authenticated"] = True
                return redirect(next_url)

            with state_lock:
                entry = login_throttle.setdefault(client_key, {"fail_count": 0, "locked_until": 0})
                entry["fail_count"] += 1
                entry["last_seen"] = now
                entry["locked_until"] = now + _login_throttle_delay(entry["fail_count"])
            error = "login_error"

        return render_template(
            "login.html",
            error=error,
            retry_after=retry_after,
            next=next_url,
            ui_language=_ui_language(),
        )

    @app.route("/api/logout", methods=["POST"])
    @handle_errors
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/security", methods=["GET"])
    @handle_errors
    def api_get_security():
        with state_lock:
            return jsonify({
                "ok": True,
                "enabled": bool(auth_state.get("enabled", False)),
                "password_set": bool(str(auth_state.get("password_hash") or "").strip()),
            })

    @app.route("/api/security", methods=["POST"])
    @handle_errors
    def api_update_security():
        data = request.get_json(force=True) or {}

        with state_lock:
            new_password = data.get("password")
            if new_password:
                new_password = str(new_password)
                if len(new_password) < MIN_PASSWORD_LENGTH:
                    return jsonify({
                        "ok": False,
                        "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
                        "error_code": "password_too_short",
                    }), 400
                auth_state["password_hash"] = generate_password_hash(new_password)

            if "enabled" in data:
                enabled = bool(data["enabled"])
                if enabled and not str(auth_state.get("password_hash") or "").strip():
                    return jsonify({
                        "ok": False,
                        "error": "Set a password before enabling protection",
                        "error_code": "no_password_set",
                    }), 400
                auth_state["enabled"] = enabled
                if enabled:
                    # Whoever just flipped this on did so from an already-open
                    # (until now unauthenticated-because-there-was-nothing-to-
                    # authenticate-against) session - grant it now, otherwise
                    # the very next request from the same browser would
                    # immediately get bounced to /login with no prior chance
                    # to sign in.
                    session.permanent = True
                    session["authenticated"] = True

            _save()
            response = {
                "ok": True,
                "enabled": bool(auth_state.get("enabled", False)),
                "password_set": bool(str(auth_state.get("password_hash") or "").strip()),
            }

        return jsonify(response)
