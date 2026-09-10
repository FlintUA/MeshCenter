"""meshsrv/attachments/relay_http.py

Shared HTTP transport for every Core-side Relay network call, enforcing the
§12 SSRF / DNS-rebinding policy in exactly one place (the design doc's
"one shared testable network policy/transport abstraction for probe,
ConnectivityMonitor, and RelayClient"). MIT-licensed - does not import
`meshtastic` or anything from adapters/meshtastic/.

Why this exists (and why it is one module, not three inline copies):

- The probe worker (Step 1.6A.4 onboarding), `ConnectivityMonitor`, and
  `RelayClient` all talk to an operator-supplied `base_url` over HTTPS. A
  malicious or compromised Relay origin is exactly the kind of attacker-
  controlled input that makes a naive `requests.get(base_url + ...)` an
  SSRF primitive (reaching `http://169.254.169.254`, `http://127.0.0.1`,
  internal RFC1918 hosts, etc.), and DNS rebinding turns a once-validated
  hostname into a moving target between validation and connection.
- The policy below is therefore non-negotiable and identical for all three
  callers: HTTPS only; certificate verification enabled; every A/AAAA
  record resolved and each one checked for global routability; and the
  connection then **pinned** to the validated IP so the TLS SNI and cert
  verification still target the original hostname while the socket itself
  never performs a second, attacker-influenceable DNS lookup.

IP pinning is implemented as a `requests.adapters.HTTPAdapter` whose pool
is keyed by the validated IP, not the hostname (the exact recipe, verified
against requests 2.34.2 / urllib3 2.7.0):

- `_PinnedHTTPSAdapter.get_connection_with_tls_context()` returns a pool
  built with `host=<ip>`, `assert_hostname=<hostname>`, and
  `server_hostname=<hostname>` (SNI). The hostname is *not* re-resolved:
  `HTTPAdapter.request_url()` returns only the URL's path, and urllib3
  connects to the pool's host (the IP).
- `HTTPAdapter.cert_verify()` (which requests calls right after, and which
  *only* sets `cert_reqs`/`ca_certs`/`ca_cert_dir`) never touches
  `assert_hostname`, so the pinned pool's hostname verification survives.
- urllib3 would otherwise emit a `Host` header from the pool's host (the
  IP), so `SecureSession` injects an explicit `Host: <hostname>[:port]`
  header - urllib3 skips its own host header when one is already present,
  preserving the operator-supplied hostname for vhost routing.

Redirects are disabled (`allow_redirects=False`) everywhere: a redirect
from a validated Relay to an internal address would otherwise bypass the
origin checks above after the first hop.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Callable, List, Optional
from urllib.parse import urlsplit

import requests

from meshsrv.attachments.provider_registry import ProviderRegistryError, normalize_origin

DEFAULT_PORT = 443


class RelayNetworkError(RuntimeError):
    """A Relay origin failed the §12 network policy (not a transport
    failure). Carries a stable, snake_case `error_code` a caller can map
    straight to an API error response - `invalid_origin` (not HTTPS / not
    a bare origin / bad port / no host) or `origin_not_routable` (no A/AAAA
    records, or any resolved record is non-globally-routable)."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class RoutableOrigin:
    """The validated result of `validate_origin_routable()` - the original
    (lowercased) hostname and port, plus the single IP every connection for
    this origin is pinned to."""

    hostname: str
    port: int
    pinned_ip: str


def resolve_host_ips(host: str, port: int = DEFAULT_PORT, *, resolver: Callable[..., Any] = socket.getaddrinfo) -> List[str]:
    """Resolve every A/AAAA record for `host`, deduplicated, in resolver
    order. A resolution failure returns `[]` (the caller decides whether
    that means "no route" or a transient error) rather than raising, so the
    policy can classify it uniformly. `resolver` is injectable for tests
    that must not touch real DNS."""
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    ips: List[str] = []
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = sockaddr[0]
        if ip not in ips:
            ips.append(ip)
    return ips


def is_globally_routable(addr: str) -> bool:
    """True iff `addr` is an IP address with global (Internet) scope. IPv4-
    mapped IPv6 addresses (`::ffff:127.0.0.1`, `::ffff:10.0.0.1`) are
    classified by their embedded IPv4 address, so a host that resolves to a
    mapped loopback/RFC1918 address is rejected rather than sneaking past
    `is_global` on the v6 side. Unparseable input (including scoped v6
    like `fe80::1%eth0`) is conservatively non-routable."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    return bool(ip.is_global)


def validate_origin_routable(origin: str, *, resolver: Callable[..., Any] = socket.getaddrinfo) -> RoutableOrigin:
    """Apply the §12 origin policy to `origin` and return the pinned
    target. Reuses `provider_registry.normalize_origin()` so the accepted
    origin string is byte-for-byte the one the Relay itself derives its
    `provider_id` from (ADR-0005) - a different normalization here would
    silently produce a different pin target than the registered origin.
    `resolver` is the low-level `socket.getaddrinfo`-shaped resolver (same
    contract as `resolve_host_ips()`), injectable for tests.

    Raises `RelayNetworkError` with a stable error_code on any violation:
    - `invalid_origin` - not HTTPS, not a bare origin, carries credentials,
      has a path/query/fragment, or an unparseable port.
    - `origin_not_routable` - no A/AAAA records, or *any* resolved record
      is non-globally-routable (the strict reading: a hostname that
      resolves to a mix of public and private addresses is rejected
      outright, not "pinned to the public one").
    """
    try:
        normalized = normalize_origin(origin)
    except ProviderRegistryError as exc:
        raise RelayNetworkError("invalid_origin", str(exc)) from exc

    parts = urlsplit(normalized)
    hostname = parts.hostname
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError as exc:
        raise RelayNetworkError("invalid_origin", f"invalid port in origin: {origin!r}") from exc

    ips = resolve_host_ips(hostname, port, resolver=resolver)
    if not ips:
        raise RelayNetworkError("origin_not_routable", f"no IP addresses resolved for {hostname!r}")
    for ip in ips:
        if not is_globally_routable(ip):
            raise RelayNetworkError(
                "origin_not_routable",
                f"host {hostname!r} resolves to non-globally-routable address {ip!r}",
            )
    return RoutableOrigin(hostname=hostname, port=port, pinned_ip=ips[0])


def _host_header_value(hostname: str, port: int) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port != DEFAULT_PORT:
        return f"{host}:{port}"
    return host


def _mount_prefix(hostname: str, port: int) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port != DEFAULT_PORT:
        return f"https://{host}:{port}/"
    return f"https://{host}/"


def _origin_of_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise RelayNetworkError("invalid_origin", f"Relay URL must use https: {url!r}")
    if not parts.hostname:
        raise RelayNetworkError("invalid_origin", f"Relay URL has no host: {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


class _PinnedHTTPSAdapter(requests.adapters.HTTPAdapter):
    """A `requests` adapter whose connection pool is keyed by the pinned IP
    while TLS still targets the original hostname. See the module docstring
    for the full mechanics; the one non-obvious line is `assert_hostname`
    (cert-hostname verification) + `server_hostname` (SNI) both set to the
    hostname, since urllib3 would otherwise verify the cert against the IP."""

    def __init__(self, hostname: str, port: int, pinned_ip: str):
        super().__init__()
        self._hostname = hostname
        self._port = port
        self._pinned_ip = pinned_ip

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self.poolmanager.connection_from_host(
            host=self._pinned_ip,
            port=self._port,
            scheme="https",
            pool_kwargs={
                "assert_hostname": self._hostname,
                "server_hostname": self._hostname,
            },
        )


class SecureSession:
    """A `requests.Session`-shaped transport that enforces §12 on every
    request. Constructed either bound to one origin (`build_secure_session`)
    - used by `RelayClient`, whose `base_url` is fixed at construction - or
    unbound (`SecureSession()`), which resolves+validates+pins the origin of
    each request URL on first use - used by `ConnectivityMonitor`'s default
    factory, where a fresh session probes a different origin per Relay.

    Only the *request method* is part of the contract callers rely on; it
    matches the two call sites exactly:

      session.request("GET", url, timeout=...)                 # monitor
      session.request(method, url, json=..., data=..., headers=..., timeout=...)  # RelayClient

    Every call forces `allow_redirects=False` and `verify=True` and injects
    the `Host` header regardless of what the caller passed, so no caller
    can accidentally opt back out of the policy by omission."""

    def __init__(
        self,
        origin: Optional[str] = None,
        *,
        session_factory: Callable[[], Any] = requests.Session,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ):
        self._session = session_factory()
        self._resolver = resolver
        self._bound: Optional[RoutableOrigin] = None
        self._pins: dict = {}
        if origin is not None:
            self._bound = validate_origin_routable(origin, resolver=resolver)
            self._install_pin(self._bound)

    def _install_pin(self, pinned: RoutableOrigin) -> None:
        self._session.mount(
            _mount_prefix(pinned.hostname, pinned.port),
            _PinnedHTTPSAdapter(pinned.hostname, pinned.port, pinned.pinned_ip),
        )

    def _pin_for_url(self, url: str) -> RoutableOrigin:
        if self._bound is not None:
            return self._bound
        origin = _origin_of_url(url)
        key = origin.rstrip("/").lower()
        pinned = self._pins.get(key)
        if pinned is None:
            pinned = validate_origin_routable(origin, resolver=self._resolver)
            self._install_pin(pinned)
            self._pins[key] = pinned
        return pinned

    def request(self, method: str, url: str, **kwargs) -> Any:
        pinned = self._pin_for_url(url)
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Host"] = _host_header_value(pinned.hostname, pinned.port)
        kwargs["headers"] = headers
        kwargs["allow_redirects"] = False
        kwargs["verify"] = True
        return self._session.request(method, url, **kwargs)

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()


def build_secure_session(
    origin: str,
    *,
    session_factory: Callable[[], Any] = requests.Session,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> SecureSession:
    """Build a `SecureSession` bound to `origin` (validated up front - a
    non-routable origin raises `RelayNetworkError` here, at construction).
    This is `RelayClient`'s production default transport; the eager
    validation is safe because a Relay's origin was already proven routable
    during onboarding, so a `RelayNetworkError` here is the anomaly, not the
    common case."""
    return SecureSession(origin, session_factory=session_factory, resolver=resolver)
