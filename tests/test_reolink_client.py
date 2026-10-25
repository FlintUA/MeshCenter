"""CAM2 Phase 2: camera/reolink_client.py against the mock Reolink server (tests/fixtures/fake_reolink_server.py)."""
import json
import os
import socket
import ssl
import sys
import urllib.request

import pytest

from camera import address_policy
from camera import reolink_client as rc
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
from fake_reolink_server import FakeReolink, make_self_signed_cert  # noqa: E402

ADMIN, ADMIN_PW = "admin", "pw-admin-test"
GUEST, GUEST_PW = "guest", "pw-guest-test"


def _lan(host):
    return [host]


def make_client(fake, *, user=ADMIN, password=ADMIN_PW, scheme="http", allow_http=True, pin="", validator=_lan, **extra):
    return rc.ReolinkClient(host="127.0.0.1", port=fake.port, scheme=scheme, channel=0, username=user,
                            password_provider=lambda: password, pinned_sha256=pin, allow_http=allow_http,
                            host_validator=validator, timeout=3.0, sleep=lambda _s: None, **extra)


@pytest.fixture
def fake():
    server = FakeReolink().start()
    yield server
    server.stop()


# ---------------------------------------------------------------- login, token, errors

def test_login_uses_a_json_array_and_the_token_goes_in_the_query_not_the_password(fake):
    client = make_client(fake)
    client.get_zoom_focus()
    assert all(entry["array"] for entry in fake.log if entry["command"] != "Snap")
    login, read = fake.log[0], fake.log[1]
    assert login["command"] == "Login" and login["body_has_password"] and not login["token_in_query"]
    assert read["command"] == "GetZoomFocus" and read["token_in_query"] and not read["body_has_password"]


def test_a_single_object_body_is_refused_by_the_camera_so_the_client_must_send_an_array(fake):
    request = urllib.request.Request(f"http://127.0.0.1:{fake.port}/cgi-bin/api.cgi?cmd=Login",
                                     data=json.dumps({"cmd": "Login", "action": 0, "param": {}}).encode(), method="POST")
    answer = json.loads(urllib.request.urlopen(request).read())
    assert answer[0]["code"] == 1 and answer[0]["error"]["rspCode"] == -1
    assert make_client(fake).get_zoom_focus()["zoom_pos"] == 10  # the client's array form works


def test_wrong_password_is_a_login_failure_without_the_password_in_the_error(fake):
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake, password="not-the-password-xyz").get_zoom_focus()
    assert raised.value.code == rc.LOGIN_FAILED
    assert "not-the-password-xyz" not in str(raised.value) and "not-the-password-xyz" not in repr(raised.value.to_dict())


def test_expired_token_relogs_in_once_and_the_command_succeeds(fake):
    client = make_client(fake)
    client.get_zoom_focus()
    fake.expire_tokens()
    assert client.get_zoom_focus()["zoom_pos"] == 10
    assert fake.logins == 2


def test_token_retry_is_bounded_when_the_camera_keeps_refusing_the_token(fake, monkeypatch):
    monkeypatch.setattr(fake, "_login", lambda cmd: [{"cmd": "Login", "code": 0, "value": {"Token": {"leaseTime": 3600, "name": "ghost"}}}])
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake).get_zoom_focus()
    assert raised.value.code == rc.TOKEN_EXPIRED
    assert sum(1 for entry in fake.log if entry["command"] == "GetZoomFocus") == 2  # first try + one retry, no loop


def test_the_token_lease_is_honoured_without_a_failed_call(fake):
    now = [100.0]
    client = make_client(fake, clock=lambda: now[0])
    fake.lease_s = 120
    client.get_zoom_focus()
    now[0] += 200  # past the lease minus the safety margin
    client.get_zoom_focus()
    assert fake.logins == 2  # logged in again BEFORE the lease ran out, not after a failed call


def test_errors_and_requests_never_leak_the_token(fake):
    client = make_client(fake)
    client.get_zoom_focus()
    token = client._token
    fake.expire_tokens()
    fake_error = None
    try:
        client._token_until = 0
        client.set_zoom(99)
    except rc.ReolinkError as error:
        fake_error = error
    assert fake_error is not None and token not in repr(fake_error.to_dict()) and token not in str(fake_error)


# ---------------------------------------------------------------- abilities, reads, zoom

def test_abilities_list_only_what_the_camera_reports(fake):
    abilities = make_client(fake).get_abilities()
    assert "snap" in abilities["names"] and abilities["permits"]["snap"] == 6


def test_zoom_read_returns_device_units_and_the_reported_range(fake):
    state = make_client(fake).get_zoom_focus()
    assert state["zoom_pos"] == 10 and state["zoom_range"] == (0, 32) and state["focus_pos"] == 100 and state["focus_range"] == (0, 248)


def test_zoom_is_validated_against_the_camera_range_before_anything_is_sent(fake):
    client = make_client(fake)
    for bad in (33, -1, 1.5, "7", True, None):
        with pytest.raises(rc.ReolinkError) as raised:
            client.set_zoom(bad)
        assert raised.value.code == rc.ZOOM_OUT_OF_RANGE
    assert fake.zoom_commands == []  # nothing reached the lens


def test_zoom_moves_the_lens_and_reads_the_position_back(fake):
    result = make_client(fake).set_zoom(20)
    timing = result.pop("timing_ms")
    assert result == {"requested": 20, "position": 20, "settled": True, "zoom_range": [0, 32]}
    assert timing["total_ms"] >= 0 and len(timing["polls"]) >= 1 and "command_ms" in timing  # the numbers LENS1 measures
    assert fake.zoom_commands == [20]
    assert [entry["command"] for entry in fake.log if entry["command"] == "StartZoomFocus"] == ["StartZoomFocus"]


def test_zoom_reports_a_lens_that_has_not_arrived(fake):
    fake.settle_reads = 10 ** 6
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    client = rc.ReolinkClient(host="127.0.0.1", port=fake.port, scheme="http", channel=0, username=ADMIN, password_provider=lambda: ADMIN_PW,
                              allow_http=True, host_validator=_lan, timeout=3.0, clock=lambda: now[0], sleep=sleep)
    result = client.set_zoom(20)
    assert result["settled"] is False and result["position"] == 10


def _live_client(fake):
    """Real clock, short real sleeps: for the tests that run two set_zoom calls in two threads."""
    import time as _time
    return rc.ReolinkClient(host="127.0.0.1", port=fake.port, scheme="http", channel=0, username=ADMIN, password_provider=lambda: ADMIN_PW,
                            allow_http=True, host_validator=_lan, timeout=3.0, clock=_time.monotonic, sleep=lambda s: _time.sleep(min(s, 0.02)))


def _wait_until(predicate, seconds=3.0):
    import time as _time
    end = _time.monotonic() + seconds
    while _time.monotonic() < end:
        if predicate():
            return True
        _time.sleep(0.01)
    return False


def test_a_newer_zoom_target_supersedes_the_one_still_waiting_for_the_lens(fake, monkeypatch):
    import threading
    monkeypatch.setattr(rc, "SETTLE_TIMEOUT_S", 5.0)
    client = _live_client(fake)
    client.get_zoom_focus()
    fake.settle_reads = 10 ** 6  # the first target never arrives
    first = {}
    thread = threading.Thread(target=lambda: first.update(client.set_zoom(20)))
    thread.start()
    assert _wait_until(lambda: fake.zoom_commands == [20])
    fake.settle_reads = 2  # the newer target arrives after two reads
    second = client.set_zoom(25)
    thread.join(3.0)
    assert not thread.is_alive(), "the older call must end as soon as a newer one arrives"
    assert first["superseded"] is True and first["settled"] is False and first["requested"] == 20
    assert second["settled"] is True and second["position"] == 25 and "superseded" not in second  # the LAST target is read back
    assert fake.zoom_commands == [20, 25]


def test_waiting_for_the_lens_does_not_hold_the_camera_lock(fake, monkeypatch):
    import threading
    monkeypatch.setattr(rc, "SETTLE_TIMEOUT_S", 3.0)
    client = _live_client(fake)
    client.get_zoom_focus()
    fake.settle_reads = 10 ** 6
    thread = threading.Thread(target=lambda: client.set_zoom(20))
    thread.start()
    assert _wait_until(lambda: fake.zoom_commands == [20])
    import time as _time
    began = _time.monotonic()
    client.get_zoom_focus()  # a read (or a snapshot) is not queued behind the whole wait any more
    assert _time.monotonic() - began < 1.0
    thread.join(5.0)


def test_a_superseding_call_waits_out_the_minimum_interval_instead_of_failing(fake, monkeypatch):
    import threading
    monkeypatch.setattr(rc, "SETTLE_TIMEOUT_S", 5.0)
    client = _live_client(fake)
    client.get_zoom_focus()
    fake.settle_reads = 10 ** 6
    thread = threading.Thread(target=lambda: client.set_zoom(20))
    thread.start()
    assert _wait_until(lambda: fake.zoom_commands == [20])
    fake.settle_reads = 1
    result = client.set_zoom(21)  # asked within the interval of the first command: no RATE_LIMITED
    thread.join(3.0)
    assert result["settled"] is True and fake.zoom_commands == [20, 21]


def test_zoom_commands_are_rate_limited(fake):
    client = make_client(fake, clock=lambda: 100.0)  # time stands still: the second command is too soon
    client.set_zoom(12)
    with pytest.raises(rc.ReolinkError) as raised:
        client.set_zoom(13)
    assert raised.value.code == rc.RATE_LIMITED
    assert fake.zoom_commands == [12]


def test_an_account_without_lens_rights_gets_a_permission_error_naming_the_right(fake):
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake, user=GUEST, password=GUEST_PW).set_zoom(20)
    assert raised.value.code == rc.PERMISSION_DENIED and raised.value.right == "lens_control" and raised.value.rsp_code == -26
    assert raised.value.command == "StartZoomFocus"


# ---------------------------------------------------------------- snapshot

def test_snapshot_returns_the_cameras_own_jpeg(fake):
    data = make_client(fake).snapshot()
    assert data[:3] == b"\xff\xd8\xff" and fake.snaps == 1
    assert fake.log[-1]["token_in_query"]


def test_the_live_view_snapshot_asks_for_the_small_sub_picture_and_the_full_one_on_request(fake):
    client = make_client(fake)
    client.snapshot()
    client.snapshot(sub=False)
    assert fake.snap_types == ["sub", ""]


def test_snapshot_refused_to_a_guest_names_the_right(fake):
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake, user=GUEST, password=GUEST_PW).snapshot()
    assert raised.value.code == rc.PERMISSION_DENIED and raised.value.right == "snapshot"


def test_snapshot_relogs_in_when_the_token_expired(fake):
    client = make_client(fake)
    client.snapshot()
    fake.expire_tokens()
    assert client.snapshot()[:2] == b"\xff\xd8"
    assert fake.logins == 2


# ---------------------------------------------------------------- probe

def test_probe_reports_only_what_the_camera_delivered(fake):
    caps = make_client(fake).probe()
    assert caps["login"] and caps["snapshot"] and caps["zoom"] and caps["focus_read"]
    assert caps["zoom_range"] == [0, 32] and caps["zoom_pos"] == 10 and "snap" in caps["abilities"] and caps["denied"] == []


def test_probe_with_a_limited_account_keeps_what_works_and_names_what_is_denied(fake):
    caps = make_client(fake, user=GUEST, password=GUEST_PW).probe()
    assert caps["zoom"] is True and caps["snapshot"] is False
    assert caps["denied"] == [{"command": "Snap", "right": "snapshot"}]


def test_probe_never_writes_to_the_lens(fake):
    make_client(fake).probe()
    assert fake.zoom_commands == []


# ---------------------------------------------------------------- transport policy

def test_plain_http_needs_the_explicit_opt_in(fake):
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake, allow_http=False).get_zoom_focus()
    assert raised.value.code == rc.HTTP_NOT_ALLOWED
    assert fake.log == []  # not even a connection


def test_the_address_policy_is_applied_and_nothing_is_sent_to_a_blocked_address(fake):
    def blocked(_host):
        raise address_policy.AddressPolicyError("not_lan_address", "Only local network addresses are allowed.")

    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake, validator=blocked).get_zoom_focus()
    assert raised.value.code == rc.ADDRESS_BLOCKED and fake.log == []


def test_a_redirect_is_never_followed(fake):
    fake.redirect = True
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake).get_zoom_focus()
    assert raised.value.code == rc.REDIRECT_REFUSED


def test_api_down_is_a_clean_error_not_a_hang(fake):
    fake.stop()
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(fake).get_zoom_focus()
    assert raised.value.code == rc.UNREACHABLE


def test_a_camera_that_never_answers_times_out():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        client = rc.ReolinkClient(host="127.0.0.1", port=listener.getsockname()[1], scheme="http", username="u", password_provider=lambda: "p",
                                  allow_http=True, host_validator=_lan, timeout=0.3)
        with pytest.raises(rc.ReolinkError) as raised:
            client.get_zoom_focus()
        assert raised.value.code == rc.TIMEOUT
    finally:
        listener.close()


# ---------------------------------------------------------------- TLS: verify by default, pin by fingerprint

@pytest.fixture
def tls_camera(tmp_path):
    pair = make_self_signed_cert(str(tmp_path))
    if pair is None:
        pytest.skip("needs the cryptography package or the openssl program to make a certificate")
    server = FakeReolink(tls=pair).start()
    with open(pair[0]) as handle:
        der = ssl.PEM_cert_to_DER_cert(handle.read())
    yield server, rc.fingerprint_of(der), tmp_path
    server.stop()


def test_an_untrusted_certificate_is_refused_and_no_credential_is_sent(tls_camera):
    server, _fingerprint, _tmp = tls_camera
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(server, scheme="https", allow_http=False).get_zoom_focus()
    assert raised.value.code == rc.TLS_UNTRUSTED
    assert server.logins == 0 and server.log == []


def test_peek_shows_the_fingerprint_without_trusting_or_sending_anything(tls_camera):
    server, fingerprint, _tmp = tls_camera
    assert rc.peek_certificate("127.0.0.1", server.port, 3.0) == fingerprint
    assert server.log == []


def test_a_pinned_certificate_is_trusted_and_the_login_goes_through(tls_camera):
    server, fingerprint, _tmp = tls_camera
    client = make_client(server, scheme="https", allow_http=False, pin=fingerprint.upper())  # case and colons do not matter
    assert client.get_zoom_focus()["zoom_pos"] == 10


def test_a_changed_fingerprint_fails_closed_before_the_password_is_sent(tls_camera):
    server, fingerprint, _tmp = tls_camera
    wrong = ("0" if fingerprint[0] != "0" else "1") + fingerprint[1:]
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(server, scheme="https", allow_http=False, pin=wrong).get_zoom_focus()
    assert raised.value.code == rc.TLS_PIN_MISMATCH and raised.value.fingerprint == fingerprint
    assert server.logins == 0 and server.log == []  # the pin is checked before any byte of the request


def test_verification_is_not_switched_off_globally(tls_camera):
    server, fingerprint, _tmp = tls_camera
    make_client(server, scheme="https", allow_http=False, pin=fingerprint).get_zoom_focus()
    # a second client without the pin still refuses the same certificate: the pin is per source, not a global switch
    with pytest.raises(rc.ReolinkError) as raised:
        make_client(server, scheme="https", allow_http=False).get_zoom_focus()
    assert raised.value.code == rc.TLS_UNTRUSTED


def test_fingerprint_normalisation():
    assert rc.normalize_fingerprint("AA:" * 31 + "AA") == "aa" * 32
    assert rc.normalize_fingerprint("zz" * 32) == "" and rc.normalize_fingerprint("abc") == "" and rc.normalize_fingerprint(None) == ""
