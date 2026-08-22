"""Tests for api/api_auth.py's load_auth_state()/is_protected() - the
bootstrap and lockout-avoidance logic behind the optional password
protection feature added in PR #63. No server.py import needed; this module
has no hardware/CLI dependencies of its own.
"""

import json

from werkzeug.security import generate_password_hash

from api.api_auth import _is_safe_next_url, is_protected, load_auth_state


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


def test_is_safe_next_url_valid_relative_paths():
    assert _is_safe_next_url("/") is True
    assert _is_safe_next_url("/map") is True
    assert _is_safe_next_url("/settings?tab=general") is True
    assert _is_safe_next_url("/chat#bottom") is True


def test_is_safe_next_url_rejects_open_redirects():
    assert _is_safe_next_url("https://example.com") is False
    assert _is_safe_next_url("http://example.com") is False
    assert _is_safe_next_url("//example.com") is False
    assert _is_safe_next_url(r"/\\example.com") is False
    assert _is_safe_next_url(r"/\example.com") is False
    assert _is_safe_next_url(r"/\\/example.com") is False
    assert _is_safe_next_url("javascript:alert(1)") is False
    assert _is_safe_next_url("") is False
    assert _is_safe_next_url(None) is False
