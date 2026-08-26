"""Tests for api/api_auth.py's load_auth_state()/is_protected() - the
bootstrap and lockout-avoidance logic behind the optional password
protection feature added in PR #63 - plus _is_safe_redirect_target()/the
login route's `next` handling (open-redirect fix, see that function's own
docstring for the live-reproduced vulnerability this replaced). No
server.py import needed; this module has no hardware/CLI dependencies of
its own.
"""

import json
import threading

from flask import Flask
from werkzeug.security import generate_password_hash

from api.api_auth import _is_safe_redirect_target, is_protected, load_auth_state, register_auth_routes


def test_load_auth_state_first_run_defaults_to_disabled(tmp_path):
    auth_file = tmp_path / "auth.json"  # does not exist yet
    state = load_auth_state(str(auth_file))
    assert state == {"enabled": False, "password_hash": ""}


def test_load_auth_state_bootstrap_enabled_without_hash_stays_disabled(tmp_path):
    # config.py's AUTH_ENABLED=True with an empty AUTH_PASSWORD_HASH must
    # never result in a locked, password-less app - this is the exact
    # scenario config.example.py's own comment warns about.
    auth_file = tmp_path / "auth.json"
    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")
    assert state["enabled"] is False


def test_load_auth_state_bootstrap_enabled_with_hash_is_enabled(tmp_path):
    auth_file = tmp_path / "auth.json"
    password_hash = generate_password_hash("correct horse battery staple")
    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash=password_hash)
    assert state == {"enabled": True, "password_hash": password_hash}


def test_load_auth_state_existing_file_ignores_bootstrap_args(tmp_path):
    # Once auth.json exists (set via the Settings UI), it's the source of
    # truth - config.py's AUTH_ENABLED/AUTH_PASSWORD_HASH must not override
    # a value the user already changed at runtime.
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"enabled": True, "password_hash": "stored-hash"}), encoding="utf-8")

    state = load_auth_state(str(auth_file), bootstrap_enabled=False, bootstrap_password_hash="")
    assert state == {"enabled": True, "password_hash": "stored-hash"}


def test_load_auth_state_tolerates_corrupt_file(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{not valid json", encoding="utf-8")

    # safe_read_json() falls back to {} on a JSON decode error - load_auth_state()
    # must then treat that the same as "no file yet" (bootstrap path), not crash.
    state = load_auth_state(str(auth_file))
    assert state == {"enabled": False, "password_hash": ""}


def test_is_protected_requires_both_enabled_and_a_real_hash():
    assert is_protected({"enabled": True, "password_hash": "somehash"}) is True
    assert is_protected({"enabled": True, "password_hash": ""}) is False
    assert is_protected({"enabled": True, "password_hash": "   "}) is False  # whitespace-only
    assert is_protected({"enabled": False, "password_hash": "somehash"}) is False
    assert is_protected({"enabled": False, "password_hash": ""}) is False


def test_is_protected_tolerates_missing_keys():
    assert is_protected({}) is False
    assert is_protected({"enabled": True}) is False
    assert is_protected({"password_hash": "somehash"}) is False


def test_is_safe_redirect_target_accepts_ordinary_same_origin_paths():
    assert _is_safe_redirect_target("/") is True
    assert _is_safe_redirect_target("/map") is True
    assert _is_safe_redirect_target("/settings?tab=general") is True
    assert _is_safe_redirect_target("/chat#bottom") is True


def test_is_safe_redirect_target_rejects_open_redirects():
    # Already blocked by the old startswith("/")/startswith("//") check.
    assert _is_safe_redirect_target("//evil.example") is False
    assert _is_safe_redirect_target("http://evil.example") is False
    assert _is_safe_redirect_target("https://evil.example") is False
    assert _is_safe_redirect_target("javascript:alert(1)") is False
    assert _is_safe_redirect_target("") is False
    assert _is_safe_redirect_target(None) is False

    # Backslash - independently verified NOT exploitable against this
    # project's Werkzeug version (percent-encoded to %5C before the
    # Location header is sent - see the function's own docstring), but
    # rejected explicitly anyway since urlsplit() alone does not catch it
    # and relying solely on Werkzeug's current encoding behavior would be
    # fragile.
    assert _is_safe_redirect_target("/\\evil.example") is False
    assert _is_safe_redirect_target("/\\/evil.example") is False

    # The actual live vulnerability, independently reproduced via a real
    # Flask test client + real browser navigation against the OLD check
    # (see this module's git history / the investigation report): a tab
    # character is silently stripped while Werkzeug builds the Location
    # header, turning "/\t/evil.example" into "//evil.example" - a
    # protocol-relative absolute URL - *after* a naive prefix check
    # already let it through. Both the raw tab and its percent-encoded
    # form (as it would actually arrive in a query string) must be
    # rejected.
    assert _is_safe_redirect_target("/\t/evil.example") is False


def test_login_redirect_rejects_the_live_reproduced_open_redirect(tmp_path):
    # End-to-end regression test through the real route (not just the
    # helper function in isolation): a real login POST with the tab-
    # stripping payload URL-encoded in the query string exactly as an
    # attacker-supplied link would deliver it, against the actual
    # register_auth_routes() code, not a hand-copied replica.
    auth_file = tmp_path / "auth.json"
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    state_lock = threading.RLock()
    auth_state = {"enabled": True, "password_hash": generate_password_hash("realpassword123")}
    register_auth_routes(app, state_lock, auth_state, str(auth_file), lambda f: f)

    client = app.test_client()
    resp = client.post(
        "/login?next=/%09/evil.example/marker",
        data={"password": "realpassword123"},
    )
    assert resp.status_code == 302
    assert resp.headers.get("Location") == "/"


def test_login_redirect_still_honors_a_legitimate_next_url(tmp_path):
    auth_file = tmp_path / "auth.json"
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    state_lock = threading.RLock()
    auth_state = {"enabled": True, "password_hash": generate_password_hash("realpassword123")}
    register_auth_routes(app, state_lock, auth_state, str(auth_file), lambda f: f)

    client = app.test_client()
    resp = client.post("/login?next=/map", data={"password": "realpassword123"})
    assert resp.status_code == 302
    assert resp.headers.get("Location") == "/map"
