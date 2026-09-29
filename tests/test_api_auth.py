"""Tests for api/api_auth.py's load_auth_state()/is_protected()/needs_setup()
- the bootstrap and lockout-avoidance logic behind the optional password
protection feature added in PR #63, plus later additions that touch this
same bootstrap path: the P1 #5 stabilization follow-up (config.example.py
defaults AUTH_ENABLED=True with no hash), which originally generated a
one-time password and now instead persists {"enabled": True,
"password_hash": ""} and lets the mandatory /setup wizard create one in
the browser (see needs_setup()/setup() and load_auth_state()'s own
comments), and _is_safe_redirect_target()/the login route's `next`
handling (open-redirect fix, see that function's own docstring for the
live-reproduced vulnerability this replaced). No server.py import needed;
this module has no hardware/CLI dependencies of its own.
"""

import json
import os
import threading
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.security import check_password_hash, generate_password_hash

import api.api_auth as api_auth
from api.api_auth import (
    _is_safe_redirect_target,
    _login_throttle_delay,
    is_protected,
    load_auth_state,
    needs_setup,
    register_auth_routes,
)


_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _make_app(tmp_path, password="realpassword123", enabled=True):
    auth_file = tmp_path / "auth.json"
    app = Flask(__name__, template_folder=_TEMPLATE_DIR)
    app.secret_key = "test-secret-key"
    state_lock = threading.RLock()
    auth_state = {"enabled": enabled, "password_hash": generate_password_hash(password)}
    register_auth_routes(app, state_lock, auth_state, str(auth_file), lambda f: f)
    return app, auth_state


def _make_setup_app(tmp_path, auth_state=None):
    """App wired to a caller-supplied auth_state (default: a fresh install -
    enabled, no password yet) plus two dummy routes, so the pass-through
    branch of _enforce_auth() returns 200 instead of a routing 404."""
    auth_file = tmp_path / "auth.json"
    app = Flask(__name__, template_folder=_TEMPLATE_DIR)
    app.secret_key = "test-secret-key"
    if auth_state is None:
        auth_state = {"enabled": True, "password_hash": ""}
    register_auth_routes(app, threading.RLock(), auth_state, str(auth_file), lambda f: f)

    @app.route("/")
    def _index():
        return "index"

    @app.route("/api/ping", methods=["GET", "POST"])
    def _ping():
        return "pong"

    return app, auth_state, auth_file


def test_load_auth_state_first_run_defaults_to_disabled(tmp_path):
    auth_file = tmp_path / "auth.json"  # does not exist yet
    state = load_auth_state(str(auth_file))
    assert state == {"enabled": False, "password_hash": ""}


def test_load_auth_state_no_bootstrap_args_writes_nothing(tmp_path, monkeypatch):
    # AUTH_ENABLED=False in config.py (bootstrap_enabled defaults to
    # False) must not just "not activate" the setup wizard - nothing may
    # be persisted at all. A spy on safe_write_json proves that, not just
    # the resulting state.
    writer = MagicMock()
    monkeypatch.setattr(api_auth, "safe_write_json", writer)

    auth_file = tmp_path / "auth.json"
    state = load_auth_state(str(auth_file))

    assert state == {"enabled": False, "password_hash": ""}
    assert needs_setup(state) is False
    writer.assert_not_called()
    assert not auth_file.exists()
    assert not (tmp_path / "initial_password.txt").exists()


def test_load_auth_state_bootstrap_enabled_without_hash_needs_setup(tmp_path):
    # config.py's AUTH_ENABLED=True with an empty AUTH_PASSWORD_HASH -
    # config.example.py's default - must persist "enabled, no password yet"
    # so the /setup wizard takes over. Nothing is generated: no password,
    # no plaintext file. (The old "never lock you out with no password set"
    # fallback is now the wizard, see needs_setup().)
    auth_file = tmp_path / "auth.json"
    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    assert needs_setup(state) is True
    assert state == {"enabled": True, "password_hash": ""}

    # Persisted to disk immediately - unlike the plain in-memory bootstrap
    # path, nothing else would ever write auth.json for this case.
    on_disk = json.loads(auth_file.read_text(encoding="utf-8"))
    assert on_disk == state

    # No plaintext credential anywhere: auth.json is the only file created.
    assert not (tmp_path / "initial_password.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["auth.json"]


def test_load_auth_state_second_call_returns_persisted_state_without_rewriting(tmp_path, monkeypatch):
    # Documented recovery path relies on this: after the first run has
    # persisted {"enabled": True, "password_hash": ""}, every later restart
    # (config.py still supplying the same empty bootstrap hash) must hit
    # the "auth.json already exists" early-return branch - same state, no
    # rewrite, nothing re-done.
    auth_file = tmp_path / "auth.json"
    first = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")
    assert first == {"enabled": True, "password_hash": ""}

    writer = MagicMock()
    monkeypatch.setattr(api_auth, "safe_write_json", writer)
    state_again = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    assert state_again == {"enabled": True, "password_hash": ""}
    writer.assert_not_called()
    assert json.loads(auth_file.read_text(encoding="utf-8")) == {"enabled": True, "password_hash": ""}
    assert not (tmp_path / "initial_password.txt").exists()


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


def test_load_auth_state_existing_file_is_never_rewritten(tmp_path, monkeypatch):
    # Same scenario as test_load_auth_state_existing_file_ignores_bootstrap_args,
    # but proving the first-run branch is physically unreachable once
    # auth.json exists - not just "produces the same result as if it had
    # run and then been overridden". bootstrap_enabled=True here
    # specifically to make sure an existing file wins even when the
    # config.py values that WOULD trigger the first-run branch are also
    # present.
    writer = MagicMock()
    monkeypatch.setattr(api_auth, "safe_write_json", writer)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"enabled": True, "password_hash": "stored-hash"}), encoding="utf-8")

    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    assert state == {"enabled": True, "password_hash": "stored-hash"}
    writer.assert_not_called()
    assert not (tmp_path / "initial_password.txt").exists()


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

    # Control characters (ASCII 0-31 and 127) must be rejected to prevent
    # URL manipulation, open redirects, or HTTP header injection.
    assert _is_safe_redirect_target("/\x0b/evil.example") is False
    assert _is_safe_redirect_target("/\x0c/evil.example") is False
    assert _is_safe_redirect_target("/\r\n/evil.example") is False
    assert _is_safe_redirect_target("/\x7f/evil.example") is False
    assert _is_safe_redirect_target("/\x00/evil.example") is False


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


# --- P1 #6: MIN_PASSWORD_LENGTH ---------------------------------------------

def test_min_password_length_is_12_not_the_old_4():
    # Pins the actual constant, not just behavior derived from it - a
    # regression here (e.g. someone "simplifying" back to 4) must fail
    # loudly on this line alone.
    assert api_auth.MIN_PASSWORD_LENGTH == 12


# --- P1 #6: login throttling -------------------------------------------------

def test_login_throttle_delay_boundary_between_5th_and_6th_failure():
    # The exact off-by-one the task called out: with 5 free attempts, the
    # 5th failed *attempt* must still render as a plain error (not 429) -
    # which requires the lockout to already be armed by the time the 5th
    # failure is *recorded*, so it blocks attempt #6. delay(fail_count) is
    # "the delay this many recorded failures impose on the NEXT attempt",
    # not on the attempt that just happened.
    assert _login_throttle_delay(1) == 0
    assert _login_throttle_delay(4) == 0  # attempt 5 still goes through unthrottled
    assert _login_throttle_delay(5) == 2  # ...but now attempt 6 is blocked
    assert _login_throttle_delay(6) == 4
    assert _login_throttle_delay(7) == 8


def test_login_throttle_delay_caps_at_max_seconds():
    assert _login_throttle_delay(100) == api_auth._LOGIN_THROTTLE_MAX_SECONDS


def test_login_throttle_delay_stays_capped_for_very_large_fail_counts():
    # fail_count has no upper bound (a sustained attacker keeps refreshing
    # last_seen, so the TTL sweep never reclaims the entry) - correctness
    # must hold far past anything the boundary tests exercise.
    for fail_count in (1_000, 1_000_000, 10 ** 9):
        assert _login_throttle_delay(fail_count) == api_auth._LOGIN_THROTTLE_MAX_SECONDS


def test_login_throttle_max_exponent_constant_is_actually_sufficient():
    # The invariant _LOGIN_THROTTLE_MAX_EXPONENT relies on: that exponent
    # alone must already reach _LOGIN_THROTTLE_MAX_SECONDS, so the function
    # never needs (and the code never computes) a larger one - this is what
    # makes capping the exponent safe rather than just "usually right". If
    # someone tightens MAX_SECONDS or loosens BASE_SECONDS without touching
    # this constant, this test catches the cap becoming insufficient.
    assert (
        api_auth._LOGIN_THROTTLE_BASE_SECONDS * (2 ** api_auth._LOGIN_THROTTLE_MAX_EXPONENT)
        >= api_auth._LOGIN_THROTTLE_MAX_SECONDS
    )


def test_login_5th_wrong_attempt_still_plain_error_6th_is_throttled(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.5"}

    for attempt in range(1, 6):  # attempts 1..5 - all free
        resp = client.post(
            "/login", data={"password": "wrong"}, environ_overrides=overrides
        )
        assert resp.status_code == 200, f"attempt {attempt} should render the plain login form, got {resp.status_code}"
        assert b"auth.login_error" in resp.data or b"Incorrect password" in resp.data
        assert resp.status_code != 429

    # 6th failure - now throttled.
    resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 429
    assert b'id="loginError"' in resp.data  # throttled branch, not the plain login_error one

    # Correct password no longer helps while still locked out - proves the
    # lockout check runs before the password is even checked.
    resp = client.post("/login", data={"password": "realpassword123"}, environ_overrides=overrides)
    assert resp.status_code == 429


def test_login_throttle_lifts_once_the_backoff_window_elapses(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.6"}

    for _ in range(6):  # trip the throttle (6th failure -> 2s lockout)
        client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)

    resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 429

    fake_now[0] += 3  # past the 2s lockout window
    resp = client.post("/login", data={"password": "realpassword123"}, environ_overrides=overrides)
    assert resp.status_code == 302
    assert resp.headers.get("Location") == "/"


def test_login_throttle_is_scoped_per_source_ip(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()

    for _ in range(6):
        client.post("/login", data={"password": "wrong"}, environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    blocked = client.post("/login", data={"password": "wrong"}, environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    assert blocked.status_code == 429

    # A different source IP is unaffected by the first one's failures.
    resp = client.post(
        "/login", data={"password": "realpassword123"}, environ_overrides={"REMOTE_ADDR": "10.0.0.8"}
    )
    assert resp.status_code == 302


def test_login_success_resets_the_failure_counter(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, _ = _make_app(tmp_path)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.9"}

    for _ in range(4):  # under the free-attempt threshold
        client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)

    resp = client.post("/login", data={"password": "realpassword123"}, environ_overrides=overrides)
    assert resp.status_code == 302  # logged in - counter should now be cleared

    # A fresh session for the same IP fails again - if the counter had NOT
    # been reset, this would already be past the free-attempt threshold.
    client2 = app.test_client()
    resp = client2.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 200
    assert b'id="loginError"' not in resp.data


def test_login_throttle_never_engages_while_auth_is_disabled(tmp_path, monkeypatch):
    # Explicit check requested for the interaction with `if not protected`:
    # while auth is disabled, login() returns its early redirect before the
    # throttle code ever runs (see api_auth.py's login(), the `if not
    # protected: return redirect("/")` line precedes the `if request.method
    # == "POST":` throttle block) - so failed attempts against a disabled
    # instance must never pre-warm a lockout for when protection is later
    # turned on from the UI.
    fake_now = [1000.0]
    monkeypatch.setattr(api_auth.time, "monotonic", lambda: fake_now[0])

    app, auth_state = _make_app(tmp_path, enabled=False)
    client = app.test_client()
    overrides = {"REMOTE_ADDR": "10.0.0.10"}

    for _ in range(10):  # far past the free-attempt threshold, if it counted at all
        resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
        assert resp.status_code == 302
        assert resp.headers.get("Location") == "/"

    # Now flip protection on, exactly as api_update_security() would via
    # the Settings UI, and confirm the very first attempt against this same
    # IP is treated as attempt #1, not #11.
    auth_state["enabled"] = True
    resp = client.post("/login", data={"password": "wrong"}, environ_overrides=overrides)
    assert resp.status_code == 200, "must render the ordinary error form, not be pre-throttled"
    assert b'id="loginError"' not in resp.data


# --- Mandatory /setup wizard (replaces the auto-generated first-run password) -

def test_needs_setup_truth_table():
    # enabled + no usable hash -> wizard (the reinterpreted combination
    # is_protected() still calls "open"; see needs_setup()'s docstring).
    assert needs_setup({"enabled": True, "password_hash": ""}) is True
    assert needs_setup({"enabled": True, "password_hash": "   "}) is True  # whitespace-only
    assert needs_setup({"enabled": True, "password_hash": None}) is True
    assert needs_setup({"enabled": True}) is True
    # enabled + real hash -> configured, nothing to set up.
    assert needs_setup({"enabled": True, "password_hash": "somehash"}) is False
    # disabled never needs setup, whatever the hash says.
    assert needs_setup({"enabled": False, "password_hash": ""}) is False
    assert needs_setup({"enabled": False, "password_hash": "somehash"}) is False
    assert needs_setup({}) is False
    assert needs_setup({"password_hash": ""}) is False


def test_needs_setup_and_is_protected_never_both_true():
    # The two states partition "enabled": exactly one of them is true for
    # any enabled state, neither for a disabled one - so _enforce_auth()'s
    # setup check and its protected check can never both apply.
    for enabled in (True, False):
        for password_hash in ("", "   ", "somehash"):
            state = {"enabled": enabled, "password_hash": password_hash}
            assert not (needs_setup(state) and is_protected(state))
            assert (needs_setup(state) or is_protected(state)) is enabled


def test_enforce_auth_redirects_every_page_to_setup_while_setup_needed(tmp_path):
    app, _, _ = _make_setup_app(tmp_path)
    client = app.test_client()

    for path in ("/", "/map", "/some/deep/page", "/login/extra"):
        resp = client.get(path)
        assert resp.status_code == 302, path
        assert resp.headers["Location"] == "/setup", path


def test_enforce_auth_returns_401_setup_required_for_api_paths_while_setup_needed(tmp_path):
    app, _, _ = _make_setup_app(tmp_path)
    client = app.test_client()

    # POST answers 401 too, not the CSRF 403: auth/setup runs first.
    for method, path in (("get", "/api/base_status"), ("get", "/api/ping"), ("post", "/api/ping")):
        resp = getattr(client, method)(path)
        assert resp.status_code == 401, (method, path)
        assert resp.get_json() == {
            "ok": False,
            "error": "Setup required",
            "error_code": "setup_required",
        }, (method, path)


def test_enforce_auth_setup_check_leaves_static_assets_reachable(tmp_path):
    app, _, _ = _make_setup_app(tmp_path)
    resp = app.test_client().get("/static/does-not-exist.css")
    assert resp.status_code == 404  # served (or plainly missing), never redirected to /setup


def test_login_while_setup_needed_ends_up_on_setup(tmp_path):
    app, _, _ = _make_setup_app(tmp_path)
    resp = app.test_client().get("/login", follow_redirects=True)
    assert resp.request.path == "/setup"
    assert resp.status_code == 200


def test_setup_get_renders_the_form_while_setup_needed(tmp_path):
    app, _, _ = _make_setup_app(tmp_path)
    resp = app.test_client().get("/setup")
    assert resp.status_code == 200
    assert b'name="password"' in resp.data
    assert b'name="confirm_password"' in resp.data
    assert b'autocomplete="new-password"' in resp.data
    assert f'data-min-length="{api_auth.MIN_PASSWORD_LENGTH}"'.encode() in resp.data
    # A plain GET renders no error block.
    assert b'class="login-error"' not in resp.data


def test_setup_get_redirects_to_root_once_setup_is_no_longer_needed(tmp_path):
    # Already configured...
    app, _, _ = _make_setup_app(
        tmp_path, {"enabled": True, "password_hash": generate_password_hash("realpassword123")}
    )
    client = app.test_client()
    resp = client.get("/setup")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    # ...and _enforce_auth() then sorts out /login vs pass-through on the next hop.
    resp = client.get("/setup", follow_redirects=True)
    assert resp.request.path == "/login"


def test_setup_post_mismatched_passwords_shows_mismatch_error_and_changes_nothing(tmp_path):
    app, auth_state, auth_file = _make_setup_app(tmp_path)
    client = app.test_client()

    resp = client.post(
        "/setup", data={"password": "a-long-enough-password", "confirm_password": "a-different-password"}
    )
    assert resp.status_code == 200
    assert b"setup_error_mismatch" in resp.data
    assert auth_state == {"enabled": True, "password_hash": ""}
    assert not auth_file.exists()  # nothing persisted
    with client.session_transaction() as sess:
        assert not sess.get("authenticated")


def test_setup_post_too_short_password_shows_too_short_error_and_changes_nothing(tmp_path):
    app, auth_state, auth_file = _make_setup_app(tmp_path)
    client = app.test_client()

    too_short = "x" * (api_auth.MIN_PASSWORD_LENGTH - 1)
    resp = client.post("/setup", data={"password": too_short, "confirm_password": too_short})
    assert resp.status_code == 200
    assert b"setup_error_too_short" in resp.data
    assert auth_state == {"enabled": True, "password_hash": ""}
    assert not auth_file.exists()

    # Both empty is "too short", not a crash and not a silent success.
    resp = client.post("/setup", data={"password": "", "confirm_password": ""})
    assert resp.status_code == 200
    assert b"setup_error_too_short" in resp.data
    assert auth_state["password_hash"] == ""


def test_setup_post_mismatch_is_reported_before_too_short(tmp_path):
    app, _, _ = _make_setup_app(tmp_path)
    resp = app.test_client().post("/setup", data={"password": "short", "confirm_password": "other"})
    assert b"setup_error_mismatch" in resp.data
    assert b'data-i18n="auth.setup_error_too_short"' not in resp.data


def test_setup_post_success_persists_hash_signs_in_and_locks_the_wizard(tmp_path):
    app, auth_state, auth_file = _make_setup_app(tmp_path)
    client = app.test_client()
    password = "x" * api_auth.MIN_PASSWORD_LENGTH  # exactly at the minimum: accepted

    client.get("/setup")  # page render mints a pre-auth CSRF token
    with client.session_transaction() as sess:
        pre_auth_token = sess["csrf_token"]

    # No CSRF header needed: /setup is outside the /api/-only CSRF scope.
    resp = client.post("/setup", data={"password": password, "confirm_password": password})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    # Hash persisted in memory and on disk; the plaintext is stored nowhere.
    assert auth_state["enabled"] is True
    assert check_password_hash(auth_state["password_hash"], password)
    on_disk = json.loads(auth_file.read_text(encoding="utf-8"))
    assert on_disk == auth_state
    assert password not in auth_file.read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["auth.json"]

    # Session granted exactly like a successful login(), CSRF token rotated.
    with client.session_transaction() as sess:
        assert sess["authenticated"] is True
        assert sess.permanent is True
        assert sess["csrf_token"] and sess["csrf_token"] != pre_auth_token

    # Signed in straight away...
    assert client.get("/").status_code == 200
    assert client.get("/api/ping").status_code == 200

    # ...and the wizard is now closed for good.
    resp = client.get("/setup")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    # A different (unauthenticated) browser gets the normal login page now.
    other = app.test_client()
    resp = other.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")
    assert other.get("/api/ping").get_json()["error_code"] == "auth_required"


def test_setup_post_cannot_replace_an_existing_password(tmp_path):
    # Takeover guard: once a password exists, /setup must neither render
    # nor accept a new one - even from an unauthenticated POST.
    original_hash = generate_password_hash("realpassword123")
    app, auth_state, auth_file = _make_setup_app(tmp_path, {"enabled": True, "password_hash": original_hash})
    client = app.test_client()

    resp = client.post(
        "/setup", data={"password": "attacker-chosen-password", "confirm_password": "attacker-chosen-password"}
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    assert auth_state["password_hash"] == original_hash
    assert not auth_file.exists()
    with client.session_transaction() as sess:
        assert not sess.get("authenticated")


def test_setup_end_to_end_with_load_auth_state_survives_a_restart(tmp_path):
    # Fresh install -> first start persists the pending state -> wizard ->
    # "restart" (a second load_auth_state() from the same file) sees a real,
    # protecting hash and no longer needs setup.
    auth_file = tmp_path / "auth.json"
    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")
    assert needs_setup(state) is True

    app = Flask(__name__, template_folder=_TEMPLATE_DIR)
    app.secret_key = "test-secret-key"
    register_auth_routes(app, threading.RLock(), state, str(auth_file), lambda f: f)
    resp = app.test_client().post(
        "/setup", data={"password": "brand new password", "confirm_password": "brand new password"}
    )
    assert resp.status_code == 302

    restarted = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")
    assert needs_setup(restarted) is False
    assert is_protected(restarted) is True
    assert check_password_hash(restarted["password_hash"], "brand new password")


def test_wiped_password_hash_forces_setup_instead_of_open_access(tmp_path):
    # The deliberate behaviour change: an auth.json that says enabled with
    # an empty hash (corrupted/hand-edited/deleted-and-re-bootstrapped) used
    # to mean "access is open" - now it must force /setup, never serve the
    # app unauthenticated.
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"enabled": True, "password_hash": ""}), encoding="utf-8")
    state = load_auth_state(str(auth_file), bootstrap_enabled=True, bootstrap_password_hash="")

    app, _, _ = _make_setup_app(tmp_path, state)
    client = app.test_client()
    assert is_protected(state) is False  # is_protected() itself is unchanged...
    assert client.get("/").headers["Location"] == "/setup"  # ...but that no longer serves the app
    assert client.get("/api/ping").status_code == 401


@pytest.mark.parametrize("password_hash", ["", "   ", "somehash"])
def test_auth_disabled_never_triggers_setup(tmp_path, password_hash):
    # AUTH_ENABLED=False regression: whatever the hash says, a disabled
    # instance is fully open and /setup is never involved.
    app, auth_state, auth_file = _make_setup_app(tmp_path, {"enabled": False, "password_hash": password_hash})
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 200 and resp.data == b"index"
    resp = client.get("/api/ping")
    assert resp.status_code == 200 and resp.data == b"pong"

    resp = client.get("/setup")
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    resp = client.post("/setup", data={"password": "x" * 20, "confirm_password": "x" * 20})
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    assert auth_state == {"enabled": False, "password_hash": password_hash}  # untouched
    assert not auth_file.exists()


def test_auth_disabled_from_config_never_triggers_setup_end_to_end(tmp_path):
    # Same regression through the real bootstrap: config.py's
    # AUTH_ENABLED=False (the default args) -> nothing persisted, no wizard.
    auth_file = tmp_path / "auth.json"
    state = load_auth_state(str(auth_file), bootstrap_enabled=False, bootstrap_password_hash="")

    app, _, _ = _make_setup_app(tmp_path, state)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/ping").status_code == 200
    assert client.get("/setup").headers["Location"] == "/"
    assert not auth_file.exists()
