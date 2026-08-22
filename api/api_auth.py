"""Optional whole-app password protection.

Off by default (see config.example.py's AUTH_ENABLED / AUTH_PASSWORD_HASH).
Single shared password, no usernames/roles - a Flask session cookie is set
on successful login and checked in a before_request hook. State (enabled
flag + password hash) lives in its own data/auth.json rather than
settings.json, so it can never be silently wiped by the generic
POST /api/settings merge-and-replace in api_settings.py, and the hash is
never included in a settings.json snapshot handed back to the browser.
"""

from urllib.parse import quote, urlsplit

from flask import jsonify, redirect, request, render_template, session
from werkzeug.security import check_password_hash, generate_password_hash

from storage.json_store import safe_read_json, safe_write_json

MIN_PASSWORD_LENGTH = 4

# Paths that must stay reachable without a session so the login page itself
# (and the assets it needs) can render.
_EXEMPT_PATH_PREFIXES = ("/static/",)
_EXEMPT_PATHS = ("/login",)


def load_auth_state(auth_file, bootstrap_enabled=False, bootstrap_password_hash=""):
    data = safe_read_json(auth_file, default=None)
    if isinstance(data, dict) and ("password_hash" in data or "enabled" in data):
        return {
            "enabled": bool(data.get("enabled", False)),
            "password_hash": str(data.get("password_hash") or ""),
        }

    # First run: seed from config.py's bootstrap values (both off/empty by
    # default), same pattern as WEATHER_PROVIDER's config->settings.json handoff.
    bootstrap_password_hash = str(bootstrap_password_hash or "")
    return {
        "enabled": bool(bootstrap_enabled) and bool(bootstrap_password_hash.strip()),
        "password_hash": bootstrap_password_hash,
    }


def is_protected(auth_state):
    # An enabled flag with no usable hash never blocks access - avoids a
    # misconfigured/corrupted auth.json permanently locking out the UI.
    return bool(auth_state.get("enabled")) and bool(str(auth_state.get("password_hash") or "").strip())


def sanitize_next_url(next_url):
    """Ensure next_url is a relative path on the same host to prevent open redirects."""
    if not isinstance(next_url, str):
        return "/"

    next_url = next_url.strip()

    # Must start with '/' and not '//' or '/\'
    if not next_url.startswith("/") or next_url.startswith("//") or next_url.startswith("/\\"):
        return "/"

    # Backslashes anywhere in the URL can trigger browser-specific domain resolution bypasses
    if "\\" in next_url:
        return "/"

    try:
        parsed = urlsplit(next_url)
        if parsed.scheme or parsed.netloc:
            return "/"
        if not parsed.path.startswith("/") or parsed.path.startswith("//"):
            return "/"
    except Exception:
        return "/"

    return next_url


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

        next_url = sanitize_next_url(request.values.get("next"))

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
