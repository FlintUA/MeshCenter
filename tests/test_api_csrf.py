"""Tests for the project-wide CSRF mechanism added in Step 1.6A.0.

Covers api/api_auth.py's token lifecycle/validation (the token lives in
the signed session, minted with secrets, exposed via a context processor
to templates, checked in a before_request hook) plus the session-cookie
hardening config server.py sets. No server.py import is needed for the
lifecycle/validation tests - register_auth_routes() is the single seam the
whole mechanism hangs off, so a minimal Flask app with one GET/POST route
exercises it exactly as the real app does. The cookie-config tests use the
session-scoped server_module fixture from tests/conftest.py to assert the
actual app.config values server.py assigns.
"""

import base64
import threading
from unittest.mock import MagicMock

import pytest
from flask import Flask, jsonify, render_template_string, request, session
from werkzeug.security import generate_password_hash

import api.api_auth as api_auth
from api.api_auth import _generate_csrf_token, register_auth_routes


def _make_app(enabled=False, password="realpassword123"):
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    state_lock = threading.RLock()
    auth_state = {
        "enabled": enabled,
        "password_hash": generate_password_hash(password) if enabled else "",
    }
    register_auth_routes(app, state_lock, auth_state, "/nonexistent/auth.json", lambda f: f)

    effects = {"posts": 0}

    @app.route("/page")
    def page():
        # Rendered through register_auth_routes()'s context processor, so
        # {{ csrf_token }} is the token _ensure_csrf_token() just minted.
        return render_template_string("{{ csrf_token }}")

    @app.route("/api/ping", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def api_ping():
        if request.method == "POST":
            effects["posts"] += 1
        return jsonify({"ok": True})

    return app, auth_state, effects


def _get_token(client):
    return client.get("/page").get_data(as_text=True)


# --- Token lifecycle --------------------------------------------------------

def test_generated_token_is_at_least_128_bits_and_unique():
    token = _generate_csrf_token()
    assert token and isinstance(token, str)
    # token_urlsafe() is base64url without padding: decode and check bytes.
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert len(decoded) >= 16, f"{len(decoded)} bytes < 128-bit floor"
    assert _generate_csrf_token() != token


def test_token_issued_for_unauthenticated_session_when_auth_disabled():
    # AUTH_ENABLED=False has no login to rotate from, so the token must be
    # minted on main-page render (§2.3 correction) - via the context
    # processor, not an API endpoint.
    app, _, _ = _make_app(enabled=False)
    client = app.test_client()
    body = client.get("/page").get_data(as_text=True)
    assert body  # non-empty token actually rendered into the page
    with client.session_transaction() as sess:
        assert sess.get("csrf_token") == body


def test_token_is_stable_across_page_refreshes():
    # No rotation on every page load - only login rotates. A refresh must
    # return the same token, or every multi-tab/parallel-fetch flow breaks.
    app, _, _ = _make_app(enabled=False)
    client = app.test_client()
    first = _get_token(client)
    second = _get_token(client)
    assert first and first == second


def test_existing_session_gets_token_lazily():
    # A session that predates CSRF (authenticated but no csrf_token yet)
    # must gain one on the next page render, not 403 everything.
    app, _, _ = _make_app(enabled=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True  # deliberately no csrf_token
    body = client.get("/page").get_data(as_text=True)
    assert body
    with client.session_transaction() as sess:
        assert sess.get("csrf_token") == body


def test_login_rotates_the_token():
    app, _, _ = _make_app(enabled=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "pre-login-token"

    resp = client.post("/login", data={"password": "realpassword123"})
    assert resp.status_code == 302

    with client.session_transaction() as sess:
        assert sess.get("authenticated") is True
        assert sess.get("csrf_token")
        assert sess["csrf_token"] != "pre-login-token"


def test_logout_validates_csrf_then_clears_session():
    app, _, _ = _make_app(enabled=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    token = _get_token(client)

    # Wrong token: rejected, session must NOT be cleared (validate-first).
    resp = client.post("/api/logout", headers={"X-CSRF-Token": "wrong"})
    assert resp.status_code == 403
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is True

    # Correct token: logout proceeds, session (and token) cleared.
    resp = client.post("/api/logout", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is None
        assert sess.get("csrf_token") is None


# --- Validation -------------------------------------------------------------

def test_safe_methods_are_exempt():
    app, _, _ = _make_app(enabled=False)
    client = app.test_client()
    # No token and no prior session at all - GET/HEAD must still pass.
    assert client.get("/api/ping").status_code == 200
    assert client.head("/api/ping").status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_unsafe_methods_without_token_are_403(method):
    app, _, effects = _make_app(enabled=False)
    client = app.test_client()
    client.get("/page")  # session has a token; the request just omits it

    resp = client.open("/api/ping", method=method)
    assert resp.status_code == 403
    data = resp.get_json()
    assert data == {
        "ok": False,
        "error": "CSRF token missing or invalid",
        "error_code": "csrf_invalid",
    }
    # The route handler must never have run (before_request short-circuits).
    assert effects["posts"] == 0


def test_wrong_token_is_403_and_never_echoes_the_real_token():
    app, _, _ = _make_app(enabled=False)
    client = app.test_client()
    token = _get_token(client)

    resp = client.post("/api/ping", headers={"X-CSRF-Token": "wrong-token"})
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "csrf_invalid"
    assert token not in resp.get_data(as_text=True)


def test_correct_token_reaches_the_route():
    app, _, effects = _make_app(enabled=False)
    client = app.test_client()
    token = _get_token(client)

    resp = client.post("/api/ping", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert effects["posts"] == 1


def test_compare_digest_is_used_for_the_comparison(monkeypatch):
    app, _, _ = _make_app(enabled=False)
    real_compare = api_auth.secrets.compare_digest
    spy = MagicMock(wraps=real_compare)
    monkeypatch.setattr(api_auth.secrets, "compare_digest", spy)

    client = app.test_client()
    token = _get_token(client)
    client.post("/api/ping", headers={"X-CSRF-Token": token})

    assert spy.call_count >= 1, "the comparison must go through secrets.compare_digest()"


def test_auth_runs_first_unauthenticated_unsafe_request_is_401():
    # Auth-before-CSRF ordering: an unauthenticated unsafe /api/ request must
    # get 401 auth_required from _enforce_auth, never a 403 from _enforce_csrf.
    app, _, _ = _make_app(enabled=True)
    client = app.test_client()
    resp = client.post("/api/ping")
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_auth_disabled_does_not_disable_csrf():
    # Turning off the password is not a license to skip CSRF - the two are
    # independent: AUTH_ENABLED=False still requires a valid token.
    app, _, _ = _make_app(enabled=False)
    client = app.test_client()
    resp = client.post("/api/ping")
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "csrf_invalid"


# --- Session cookie config (server.py) -------------------------------------

def test_server_sets_session_cookie_config(server_module):
    assert server_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert server_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    # Synthetic config.py in conftest has no SESSION_COOKIE_SECURE, so the
    # globals().get() default (False) applies - HTTP dev default.
    assert server_module.app.config["SESSION_COOKIE_SECURE"] is False


def test_session_cookie_flags_are_emitted_secure_when_configured():
    # Proves the config flags translate to real Set-Cookie attributes:
    # HttpOnly/SameSite always present, Secure present iff configured.
    for secure, expect_secure in ((False, False), (True, True)):
        app = Flask(__name__)
        app.secret_key = "test-secret-key"
        app.config["SESSION_COOKIE_HTTPONLY"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        app.config["SESSION_COOKIE_SECURE"] = secure

        @app.route("/set")
        def set_session():
            session["x"] = "y"
            return "ok"

        cookie = app.test_client().get("/set").headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert ("Secure" in cookie) is expect_secure, f"secure={secure}: {cookie}"
