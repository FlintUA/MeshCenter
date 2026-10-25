"""AUTH2: per-session revocation (a normal logout ends only that session), "sign out everywhere" (the global
auth_version), old cookies without a session id, and the re-check of long-lived responses.
meshsrv/session_guard.py is exercised on its own, then through the real login routes."""
import json
import os
import threading

import pytest
from flask import Flask, jsonify
from werkzeug.security import generate_password_hash

import meshsrv.session_guard as sg
from api.api_auth import register_auth_routes

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
PASSWORD = "realpassword123"


# ---------------------------------------------------------------- RevokedSessions

def test_a_revoked_id_is_remembered_until_its_expiry():
    now = [1000.0]
    revoked = sg.RevokedSessions(None, lifetime_s=100, clock=lambda: now[0])
    assert revoked.is_revoked("a") is False
    revoked.add("a")
    assert revoked.is_revoked("a") is True and revoked.is_revoked("b") is False
    now[0] += 99
    assert revoked.is_revoked("a") is True
    now[0] += 2  # the cookie itself could not be presented any more
    assert revoked.is_revoked("a") is False and revoked.count() == 0


def test_revoked_ids_survive_a_restart_and_expired_ones_are_dropped(tmp_path):
    path = str(tmp_path / "revoked_sessions.json")
    now = [1000.0]
    first = sg.RevokedSessions(path, lifetime_s=100, clock=lambda: now[0])
    first.add("keep")
    now[0] += 50
    first.add("later")
    now[0] += 60  # "keep" (expires 1100) is over, "later" (expires 1150) is not
    second = sg.RevokedSessions(path, lifetime_s=100, clock=lambda: now[0])
    assert second.is_revoked("later") is True and second.is_revoked("keep") is False
    assert json.loads(open(path, encoding="utf-8").read())["revoked"]


def test_a_failed_write_never_stops_the_logout(tmp_path, monkeypatch):
    revoked = sg.RevokedSessions(str(tmp_path / "r.json"))
    monkeypatch.setattr(sg, "safe_write_json", lambda *_a, **_k: False)
    assert revoked.add("x") is False  # the caller may log it ...
    assert revoked.is_revoked("x") is True  # ... but the revocation stands in memory


def test_the_revoked_list_is_bounded(monkeypatch):
    monkeypatch.setattr(sg, "MAX_REVOKED", 3)
    now = [1.0]
    revoked = sg.RevokedSessions(None, lifetime_s=1000, clock=lambda: now[0])
    for index in range(6):
        now[0] += 1
        revoked.add(f"s{index}")
    assert revoked.count() == 3
    assert revoked.is_revoked("s5") and not revoked.is_revoked("s0")  # the oldest go first


def test_session_ids_are_random_and_long():
    ids = {sg.new_session_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(value) >= 20 for value in ids)


# ---------------------------------------------------------------- guard_stream

class _Body:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self):
        self.closed = True


def test_a_stream_ends_when_the_check_fails_and_the_body_is_closed():
    now = [0.0]
    verdicts = [True, False]

    def check():
        return verdicts.pop(0)

    body = _Body([b"a", b"b", b"c", b"d", b"e", b"f"])
    guarded = sg.guard_stream(body, check, interval_s=3.0, clock=lambda: now[0])
    seen = []
    for chunk in guarded:
        seen.append(chunk)
        now[0] += 1.5  # a check every second chunk
    assert seen == [b"a", b"b", b"c", b"d"] and body.closed is True
    assert verdicts == []


def test_the_end_of_a_stream_is_logged_with_its_age(capsys):
    now = [0.0]
    body = _Body([b"a", b"b", b"c"])
    for _chunk in sg.guard_stream(body, lambda: False, interval_s=3.0, clock=lambda: now[0], label="fMP4 stream"):
        now[0] += 4.0
    out = capsys.readouterr().out
    assert "[AUTH] open fMP4 stream ended after 4 s" in out and "sid" not in out


def test_the_check_is_not_asked_on_every_chunk():
    now = [0.0]
    asked = []
    body = _Body([bytes([i]) for i in range(50)])
    for _chunk in sg.guard_stream(body, lambda: asked.append(1) or True, interval_s=3.0, clock=lambda: now[0]):
        now[0] += 0.1
    assert 1 <= len(asked) <= 2  # 5 s of chunks, one look every 3 s


def test_a_body_that_was_never_iterated_is_still_closed():
    body = _Body([b"a"])
    guarded = sg.guard_stream(body, lambda: True)
    guarded.close()  # what the WSGI server does for a client that left before the first byte
    assert body.closed is True


def test_no_check_means_the_body_itself():
    body = _Body([b"a"])
    assert sg.guard_stream(body, None) is body


# ---------------------------------------------------------------- through the real routes

def _make_app(tmp_path):
    app = Flask(__name__, template_folder=_TEMPLATE_DIR)
    app.secret_key = "test-secret-key"
    auth_state = {"enabled": True, "password_hash": generate_password_hash(PASSWORD), "auth_version": 1}
    register_auth_routes(app, threading.RLock(), auth_state, str(tmp_path / "auth.json"), lambda f: f,
                         revoked_sessions_file=str(tmp_path / "revoked_sessions.json"))
    holder = {}

    @app.route("/api/ping", methods=["GET", "POST"])
    def _ping():
        return jsonify({"ok": True})

    @app.route("/api/open_stream")
    def _open_stream():
        holder["check"] = app.extensions["mc_session_guard"]()
        return jsonify({"ok": True})

    return app, auth_state, holder


def _login(app):
    client = app.test_client()
    response = client.post("/login", data={"password": PASSWORD})
    assert response.status_code == 302
    return client


def _csrf(client):
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _cookie(client):
    return client.get_cookie("session").value


def _client_with_cookie(app, value):
    client = app.test_client()
    client.set_cookie("session", value)
    return client


def test_a_login_gets_a_session_id(tmp_path):
    app, _state, _holder = _make_app(tmp_path)
    client = _login(app)
    with client.session_transaction() as sess:
        assert isinstance(sess.get("sid"), str) and len(sess["sid"]) >= 20


def test_logout_ends_only_this_session(tmp_path):
    app, _state, _holder = _make_app(tmp_path)
    first, second = _login(app), _login(app)
    copy_of_first = _cookie(first)
    assert first.get("/api/ping").status_code == 200 and second.get("/api/ping").status_code == 200
    assert first.post("/api/logout", headers={"X-CSRF-Token": _csrf(first)}).status_code == 200
    assert first.get("/api/ping").status_code == 401
    assert second.get("/api/ping").status_code == 200, "another browser stays signed in"
    # a copy of the logged-out cookie (a stolen or stale one) is refused too, not just the cleared browser
    assert _client_with_cookie(app, copy_of_first).get("/api/ping").status_code == 401


def test_a_revoked_session_stays_revoked_after_a_restart(tmp_path):
    app, _state, _holder = _make_app(tmp_path)
    client = _login(app)
    cookie = _cookie(client)
    client.post("/api/logout", headers={"X-CSRF-Token": _csrf(client)})
    assert os.path.exists(tmp_path / "revoked_sessions.json")
    restarted, _state2, _holder2 = _make_app(tmp_path)  # same files, a new process
    assert _client_with_cookie(restarted, cookie).get("/api/ping").status_code == 401


def test_a_cookie_without_an_id_stays_valid_and_gets_one(tmp_path):
    app, _state, _holder = _make_app(tmp_path)
    client = _login(app)
    with client.session_transaction() as sess:
        sess.pop("sid")  # a cookie made before AUTH2
    assert client.get("/api/ping").status_code == 200, "no forced re-login on update"
    with client.session_transaction() as sess:
        assert sess.get("sid"), "it is given an id, so a later logout can revoke it"
    client.post("/api/logout", headers={"X-CSRF-Token": _csrf(client)})
    assert client.get("/api/ping").status_code == 401


def test_sign_out_everywhere_ends_every_session(tmp_path):
    app, state, _holder = _make_app(tmp_path)
    first, second = _login(app), _login(app)
    response = first.post("/api/auth/logout_all", headers={"X-CSRF-Token": _csrf(first)})
    assert response.status_code == 200 and state["auth_version"] == 2
    assert first.get("/api/ping").status_code == 401 and second.get("/api/ping").status_code == 401
    assert _login(app).get("/api/ping").status_code == 200, "a new login works"
    persisted = json.loads(open(tmp_path / "auth.json", encoding="utf-8").read())
    assert persisted["auth_version"] == 2


def test_sign_out_everywhere_needs_a_session_and_a_csrf_token(tmp_path):
    app, _state, _holder = _make_app(tmp_path)
    assert app.test_client().post("/api/auth/logout_all").status_code == 401
    client = _login(app)
    assert client.post("/api/auth/logout_all").status_code == 403  # no token


def test_sign_out_everywhere_with_protection_off_is_refused(tmp_path):
    app, state, _holder = _make_app(tmp_path)
    client = _login(app)
    state["enabled"] = False
    response = client.post("/api/auth/logout_all", headers={"X-CSRF-Token": _csrf(client)})
    assert response.status_code == 409 and response.get_json()["error_code"] == "auth_not_enabled"


def test_an_open_stream_sees_a_logout_and_a_global_sign_out(tmp_path):
    app, state, holder = _make_app(tmp_path)
    client = _login(app)
    client.get("/api/open_stream")
    check = holder["check"]
    assert check() is True
    other = _login(app)
    other.get("/api/open_stream")
    other_check = holder["check"]
    client.post("/api/logout", headers={"X-CSRF-Token": _csrf(client)})
    assert check() is False, "a logout ends the stream that session opened"
    assert other_check() is True, "another session's stream goes on"
    state["auth_version"] += 1  # a password change or sign out everywhere
    assert other_check() is False


def test_a_stream_opened_with_protection_off_ends_when_protection_comes_on(tmp_path):
    app, state, holder = _make_app(tmp_path)
    state["enabled"] = False
    client = app.test_client()
    client.get("/api/open_stream")
    check = holder["check"]
    assert check() is True
    state["enabled"] = True
    assert check() is False
