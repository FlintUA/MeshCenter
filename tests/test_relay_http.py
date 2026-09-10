"""Tests for meshsrv/attachments/relay_http.py - the shared §12 SSRF /
DNS-rebinding network-policy transport used by RelayClient (via
`build_secure_session`), ConnectivityMonitor (via an unbound
`SecureSession`), and the probe worker.

No test here performs real DNS or network I/O: `socket.getaddrinfo` is
always replaced by a deterministic fake, and the `SecureSession` inner
transport is a recording fake, so the whole suite runs offline on any
platform (including CI and a Windows dev box).
"""

import socket

import pytest

from meshsrv.attachments.relay_http import (
    RelayNetworkError,
    SecureSession,
    _PinnedHTTPSAdapter,
    build_secure_session,
    is_globally_routable,
    resolve_host_ips,
    validate_origin_routable,
)

_PUBLIC_V4 = "8.8.8.8"
_PUBLIC_V6 = "2606:4700:4700::1111"


def _resolver(mapping, default=socket.gaierror("no such host")):
    def _resolve(host, port, type=None):
        if host in mapping:
            return mapping[host]
        raise default

    return _resolve


def _addr(family, ip, port=443):
    if family == socket.AF_INET:
        return (family, socket.SOCK_STREAM, 6, "", (ip, port))
    return (family, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))


# ---------------------------------------------------------------------------
# is_globally_routable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr, expected",
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:169.254.169.254", False),
        ("::ffff:8.8.8.8", True),
        ("203.0.113.7", False),  # documentation-only TEST-NET-3
        ("192.0.2.1", False),  # documentation-only TEST-NET-1
        ("not-an-ip", False),
        ("", False),
    ],
)
def test_is_globally_routable(addr, expected):
    assert is_globally_routable(addr) is expected


# ---------------------------------------------------------------------------
# resolve_host_ips
# ---------------------------------------------------------------------------


def test_resolve_host_ips_dedupes_and_covers_both_families():
    fake = _resolver(
        {
            "dual.example": [
                _addr(socket.AF_INET, _PUBLIC_V4),
                _addr(socket.AF_INET6, _PUBLIC_V6),
                _addr(socket.AF_INET, _PUBLIC_V4),  # duplicate
            ]
        }
    )
    assert resolve_host_ips("dual.example", resolver=fake) == [_PUBLIC_V4, _PUBLIC_V6]


def test_resolve_host_ips_gaierror_returns_empty():
    fake = _resolver({})
    assert resolve_host_ips("missing.example", resolver=fake) == []


# ---------------------------------------------------------------------------
# validate_origin_routable
# ---------------------------------------------------------------------------


def test_validate_origin_pins_to_first_resolved_ip():
    fake = _resolver(
        {"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4), _addr(socket.AF_INET6, _PUBLIC_V6)]}
    )
    result = validate_origin_routable("https://ok.example", resolver=fake)
    assert result.hostname == "ok.example"
    assert result.port == 443
    assert result.pinned_ip == _PUBLIC_V4


def test_validate_origin_preserves_explicit_port():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4, port=8443)]})
    result = validate_origin_routable("https://ok.example:8443", resolver=fake)
    assert result.port == 8443


def test_validate_origin_lowercases_hostname():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4)]})
    result = validate_origin_routable("https://OK.Example", resolver=fake)
    assert result.hostname == "ok.example"


@pytest.mark.parametrize(
    "origin",
    [
        "http://ok.example",  # not https
        "https://ok.example/path",  # not a bare origin
        "https://user:pass@ok.example",  # credentials
        "https://ok.example?q=1",  # query
        "https://ok.example#frag",  # fragment
    ],
)
def test_validate_origin_rejects_non_https_bare_origin(origin):
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4)]})
    with pytest.raises(RelayNetworkError) as exc_info:
        validate_origin_routable(origin, resolver=fake)
    assert exc_info.value.error_code == "invalid_origin"


def test_validate_origin_rejects_private_resolution():
    fake = _resolver({"private.example": [_addr(socket.AF_INET, "10.0.0.5")]})
    with pytest.raises(RelayNetworkError) as exc_info:
        validate_origin_routable("https://private.example", resolver=fake)
    assert exc_info.value.error_code == "origin_not_routable"


def test_validate_origin_rejects_loopback_resolution():
    fake = _resolver({"loop.example": [_addr(socket.AF_INET, "127.0.0.1")]})
    with pytest.raises(RelayNetworkError) as exc_info:
        validate_origin_routable("https://loop.example", resolver=fake)
    assert exc_info.value.error_code == "origin_not_routable"


def test_validate_origin_rejects_mixed_public_and_private():
    fake = _resolver(
        {
            "mixed.example": [
                _addr(socket.AF_INET, _PUBLIC_V4),
                _addr(socket.AF_INET, "127.0.0.1"),
            ]
        }
    )
    with pytest.raises(RelayNetworkError) as exc_info:
        validate_origin_routable("https://mixed.example", resolver=fake)
    assert exc_info.value.error_code == "origin_not_routable"


def test_validate_origin_rejects_ipv4_mapped_private_v6():
    fake = _resolver({"mapped.example": [_addr(socket.AF_INET6, "::ffff:10.0.0.1")]})
    with pytest.raises(RelayNetworkError) as exc_info:
        validate_origin_routable("https://mapped.example", resolver=fake)
    assert exc_info.value.error_code == "origin_not_routable"


def test_validate_origin_rejects_no_resolution():
    fake = _resolver({})
    with pytest.raises(RelayNetworkError) as exc_info:
        validate_origin_routable("https://missing.example", resolver=fake)
    assert exc_info.value.error_code == "origin_not_routable"


# ---------------------------------------------------------------------------
# _PinnedHTTPSAdapter
# ---------------------------------------------------------------------------


class _FakePreparedRequest:
    url = "https://ok.example/v1/info"


def test_pinned_adapter_pools_keyed_by_ip_verify_against_hostname():
    adapter = _PinnedHTTPSAdapter("ok.example", 443, _PUBLIC_V4)
    pool = adapter.get_connection_with_tls_context(_FakePreparedRequest(), True)
    assert pool.scheme == "https"
    assert pool.host == _PUBLIC_V4
    assert pool.assert_hostname == "ok.example"
    assert pool.conn_kw.get("server_hostname") == "ok.example"


# ---------------------------------------------------------------------------
# SecureSession
# ---------------------------------------------------------------------------


class _RecordingSession:
    def __init__(self):
        self.calls = []
        self.mounted = []
        self.closed = False

    def mount(self, prefix, adapter):
        self.mounted.append((prefix, adapter))

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return "response"

    def close(self):
        self.closed = True


def _make_session(origin=None, resolver=None):
    inner = _RecordingSession()
    session = SecureSession(origin, session_factory=lambda: inner, resolver=resolver)
    return inner, session


def test_secure_session_forces_redirects_off_verify_on_and_host_header():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4)]})
    inner, session = _make_session("https://ok.example", resolver=fake)
    response = session.request(
        "GET", "https://ok.example/v1/info", headers={"Authorization": "Bearer x"}, timeout=5
    )
    assert response == "response"
    method, url, kwargs = inner.calls[0]
    assert method == "GET"
    assert url == "https://ok.example/v1/info"
    assert kwargs["allow_redirects"] is False
    assert kwargs["verify"] is True
    assert kwargs["headers"]["Host"] == "ok.example"
    # caller's other headers are preserved, not clobbered
    assert kwargs["headers"]["Authorization"] == "Bearer x"


def test_secure_session_host_header_includes_non_default_port():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4, port=8443)]})
    inner, session = _make_session("https://ok.example:8443", resolver=fake)
    session.request("GET", "https://ok.example:8443/v1/info", timeout=5)
    assert inner.calls[0][2]["headers"]["Host"] == "ok.example:8443"


def test_unbound_secure_session_resolves_per_url():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4)]})
    inner, session = _make_session(origin=None, resolver=fake)
    session.request("GET", "https://ok.example/health", timeout=5)
    assert inner.calls[0][2]["headers"]["Host"] == "ok.example"


def test_unbound_secure_session_rejects_private_url():
    fake = _resolver({"private.example": [_addr(socket.AF_INET, "10.0.0.5")]})
    inner, session = _make_session(origin=None, resolver=fake)
    with pytest.raises(RelayNetworkError):
        session.request("GET", "https://private.example/health", timeout=5)
    assert inner.calls == []


def test_unbound_secure_session_rejects_non_https_url():
    fake = _resolver({})
    inner, session = _make_session(origin=None, resolver=fake)
    with pytest.raises(RelayNetworkError):
        session.request("GET", "http://ok.example/health", timeout=5)
    assert inner.calls == []


def test_secure_session_mounts_a_pinned_adapter():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4)]})
    inner, _ = _make_session("https://ok.example", resolver=fake)
    assert len(inner.mounted) == 1
    prefix, adapter = inner.mounted[0]
    assert prefix == "https://ok.example/"
    assert isinstance(adapter, _PinnedHTTPSAdapter)


def test_secure_session_close_delegates_to_inner():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4)]})
    inner, session = _make_session("https://ok.example", resolver=fake)
    session.close()
    assert inner.closed is True


# ---------------------------------------------------------------------------
# build_secure_session
# ---------------------------------------------------------------------------


def test_build_secure_session_eagerly_validates_origin():
    fake = _resolver({"private.example": [_addr(socket.AF_INET, "10.0.0.5")]})
    with pytest.raises(RelayNetworkError) as exc_info:
        build_secure_session("https://private.example", session_factory=_RecordingSession, resolver=fake)
    assert exc_info.value.error_code == "origin_not_routable"


def test_build_secure_session_returns_bound_secure_session():
    fake = _resolver({"ok.example": [_addr(socket.AF_INET, _PUBLIC_V4)]})
    session = build_secure_session("https://ok.example", session_factory=_RecordingSession, resolver=fake)
    assert isinstance(session, SecureSession)
