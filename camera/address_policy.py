"""Address policy for network cameras (CAM2, SSRF guard) - stdlib only.

A camera host is something the user types into MeshCenter and MeshCenter then
connects to from the server side, so it must not become a way to reach things
the user never meant to expose: the MeshCenter host itself, cloud metadata
endpoints, other services on loopback, or the public internet.

Allowed: a literal IP address, or a host name whose EVERY resolved address is
in a LAN range: RFC 1918 (10/8, 172.16/12, 192.168/16), IPv4 link-local
(169.254/16), IPv6 unique-local (fc00::/7) and IPv6 link-local (fe80::/10).
Everything else is refused: loopback, unspecified, multicast, broadcast,
public and shared (100.64/10) addresses, the cloud metadata addresses, this
host's own interface addresses, and (in a container) the default gateway,
which is the Docker host / NAS itself, and the container's whole own
network(s): the Docker bridge subnet the container sits on (and any other
network it is attached to) is where the Docker host and other containers
live, never where a camera lives. A container started with host networking
sees the real LAN as "its own network"; there the operator can set
MESHCENTER_CAMERA_ALLOW_OWN_SUBNET=1 (the gateway stays refused).

Validation returns the validated IP addresses, and callers connect to THAT
address, not to the name: a second DNS lookup at connect time could return
something else (DNS rebinding), turning this check into a time-of-check /
time-of-use gap. Re-validate on every (re)connect, not only when saving.

`ipaddress`'s own ``is_private`` is deliberately not used: it is True for
loopback and several other ranges that are not LAN addresses.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Callable, Iterable

_ALLOWED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
))
_ALLOWED_V6 = tuple(ipaddress.ip_network(n) for n in ("fc00::/7", "fe80::/10"))

# Inside an allowed range but never a camera: cloud metadata endpoints.
_METADATA = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "fd00:ec2::254",
))

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_NUMERIC_FORM_RE = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$", re.IGNORECASE)
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")

Resolver = Callable[[str], Iterable[str]]


class AddressPolicyError(ValueError):
    """Raised for a host the policy refuses. ``code`` is machine readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise AddressPolicyError("unresolvable", "The host name could not be resolved.")
    return sorted({info[4][0] for info in infos})


def local_addresses() -> frozenset:
    """This machine's own interface addresses (so a camera cannot be 'this host')."""
    found: set = set()
    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family in (socket.AF_INET, socket.AF_INET6):
                    try:
                        found.add(_normalize(ipaddress.ip_address(addr.address.split("%", 1)[0])))
                    except ValueError:
                        pass
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            try:
                found.add(_normalize(ipaddress.ip_address(info[4][0].split("%", 1)[0])))
            except ValueError:
                pass
    except OSError:
        pass
    return frozenset(found)


ALLOW_OWN_SUBNET_ENV = "MESHCENTER_CAMERA_ALLOW_OWN_SUBNET"


def container_own_networks(nics=None) -> tuple:
    """Container only: the networks this container's interfaces sit on
    (the Docker bridge subnet and any other attached network).

    ``nics`` is a list of (address, netmask) text pairs; by default psutil's
    view of the container's interfaces. Loopback is skipped (it is refused
    separately). Empty when it cannot be read."""
    if nics is None:
        nics = []
        try:
            import psutil

            for addrs in psutil.net_if_addrs().values():
                for addr in addrs:
                    if addr.family in (socket.AF_INET, socket.AF_INET6) and addr.netmask:
                        nics.append((addr.address, addr.netmask))
        except Exception:
            nics = []
    networks = []
    for address, netmask in nics:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if ip.is_loopback:
                continue
            if isinstance(ip, ipaddress.IPv6Address):
                prefix = bin(int(ipaddress.IPv6Address(netmask.split("%", 1)[0]))).count("1")
            else:
                prefix = bin(int(ipaddress.IPv4Address(netmask))).count("1")
            networks.append(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
        except ValueError:
            continue
    return tuple(networks)


def container_gateway_addresses(route_table: str | None = None) -> frozenset:
    """Container only: the default gateway (the Docker host / NAS itself).

    Parsed from /proc/net/route (IPv4, little-endian hex). Empty when it
    cannot be read. ``route_table`` lets a test pass the file contents.
    """
    gateways: set = set()
    try:
        text = route_table if route_table is not None else open("/proc/net/route", encoding="ascii").read()
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "00000000":
                raw = bytes.fromhex(parts[2])[::-1]
                if len(raw) == 4:
                    gateways.add(ipaddress.IPv4Address(raw))
    except (OSError, ValueError):
        pass
    return frozenset(gateways)


def _normalize(address):
    """IPv4-mapped IPv6 (::ffff:127.0.0.1) is judged as the IPv4 address it is."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def check_address(address, *, own_addresses=frozenset(), gateway_addresses=frozenset(), blocked_networks=()) -> None:
    """Raise AddressPolicyError unless ``address`` (an ip_address object) is an allowed camera address."""
    address = _normalize(address)
    if address.is_loopback or address.is_unspecified:
        raise AddressPolicyError("loopback", "This address points at this machine and is not allowed.")
    if address.is_multicast or (isinstance(address, ipaddress.IPv4Address) and address == ipaddress.IPv4Address("255.255.255.255")):
        raise AddressPolicyError("multicast", "Multicast and broadcast addresses are not allowed.")
    if address in _METADATA:
        raise AddressPolicyError("metadata_address", "This address is a cloud metadata endpoint and is not allowed.")
    nets = _ALLOWED_V4 if isinstance(address, ipaddress.IPv4Address) else _ALLOWED_V6
    if not any(address in net for net in nets):
        raise AddressPolicyError("not_lan_address", "Only local network addresses are allowed (10.x, 172.16-31.x, 192.168.x, 169.254.x, fc00::/7, fe80::/10).")
    if address in own_addresses:
        raise AddressPolicyError("own_address", "This address belongs to the MeshCenter host itself.")
    if address in gateway_addresses:
        raise AddressPolicyError("gateway_address", "This address is the container's gateway (the Docker host) and is not allowed.")
    if any(address.version == net.version and address in net for net in blocked_networks):
        raise AddressPolicyError("container_network", "This address is on the container's own (Docker) network and is not allowed.")


def validate_camera_host(
    host: str,
    *,
    resolver: Resolver | None = None,
    own_addresses: frozenset | None = None,
    gateway_addresses: frozenset | None = None,
    blocked_networks: tuple | None = None,
) -> list[str]:
    """Validate a user-supplied camera host; return its validated IP address strings.

    Raises AddressPolicyError. A literal IP is checked directly; a name is
    resolved and every address it resolves to must pass.
    """
    if not isinstance(host, str):
        raise AddressPolicyError("invalid_host", "Enter a host name or IP address.")
    host = host.strip()
    if not host or len(host) > 253 or any(ord(c) < 33 or ord(c) == 127 for c in host):
        raise AddressPolicyError("invalid_host", "Enter a host name or IP address without spaces.")
    if any(c in host for c in "/@?#\\[]") or host.startswith("-"):
        raise AddressPolicyError("invalid_host", "Enter only the host name or IP address, not a URL.")

    own = local_addresses() if own_addresses is None else own_addresses
    in_container = False
    if gateway_addresses is None or blocked_networks is None:
        try:
            from meshsrv import runtime_env

            in_container = runtime_env.is_container()
        except Exception:
            in_container = False
    if gateway_addresses is None:
        gateways = container_gateway_addresses() if in_container else frozenset()
    else:
        gateways = gateway_addresses
    if blocked_networks is None:
        allow_own = os.environ.get(ALLOW_OWN_SUBNET_ENV, "").strip().lower() in ("1", "true", "yes", "on")
        blocked_networks = container_own_networks() if (in_container and not allow_own) else ()

    literal = None
    try:
        base, _, scope = host.partition("%")
        literal = ipaddress.ip_address(base)
        if scope and (not isinstance(literal, ipaddress.IPv6Address) or not _SCOPE_RE.match(scope)):
            raise AddressPolicyError("invalid_host", "Enter a valid IP address.")
    except ValueError:
        literal = None

    if literal is not None:
        check_address(literal, own_addresses=own, gateway_addresses=gateways, blocked_networks=blocked_networks)
        return [str(literal) + (("%" + host.partition("%")[2]) if "%" in host else "")]

    if not _HOSTNAME_RE.match(host) or host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise AddressPolicyError("invalid_host", "Enter a valid host name or IP address.")
    if _NUMERIC_FORM_RE.match(host):
        # '127.1', '2130706433', '0x7f.1' style forms that inet_aton accepts but ip_address rejects.
        raise AddressPolicyError("invalid_host", "Enter the IP address in full dotted form.")

    resolved = list((resolver or _default_resolver)(host))
    if not resolved:
        raise AddressPolicyError("unresolvable", "The host name could not be resolved.")
    result = []
    for text in resolved:
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            raise AddressPolicyError("unresolvable", "The host name could not be resolved.")
        check_address(address, own_addresses=own, gateway_addresses=gateways, blocked_networks=blocked_networks)
        result.append(text)
    return result
