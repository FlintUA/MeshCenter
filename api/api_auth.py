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
from urllib.parse import quote, urlsplit

from flask import jsonify, redirect, request, render_template, session
from werkzeug.security import check_password_hash, generate_password_hash

from storage.json_store import safe_read_json, safe_write_json

MIN_PASSWORD_LENGTH = 4

# Paths that must stay reachable without a session so the login page itself
# (and the assets it needs) can render.
_EXEMPT_PATH_PREFIXES = ("/static/",)
_EXEMPT_PATHS = ("/login",)


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
        if request.method == "POST":
            password = request.form.get("password", "")
            if password and check_password_hash(password_hash, password):
                session.clear()
                session.permanent = True
                session["authenticated"] = True
                return redirect(next_url)
            error = "login_error"

        return render_template(
            "login.html",
            error=error,
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
