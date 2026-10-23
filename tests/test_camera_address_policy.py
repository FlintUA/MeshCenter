"""CAM2 address policy (SSRF guard): camera/address_policy.py."""
import ipaddress

import pytest

from camera.address_policy import (
    AddressPolicyError,
    check_address,
    container_gateway_addresses,
    container_own_networks,
    validate_camera_host,
)

NO_OWN = frozenset()


def _code(host, **kwargs):
    with pytest.raises(AddressPolicyError) as info:
        validate_camera_host(host, own_addresses=kwargs.pop("own", NO_OWN), **kwargs)
    return info.value.code


@pytest.mark.parametrize("host", [
    "10.0.0.5", "10.255.255.254", "172.16.0.1", "172.31.255.1", "192.168.2.114",
    "169.254.10.20", "fd12:3456::1", "fe80::1", "fe80::1%eth0",
])
def test_lan_literals_are_accepted(host):
    assert validate_camera_host(host, own_addresses=NO_OWN) == [host]


@pytest.mark.parametrize("host,code", [
    ("127.0.0.1", "loopback"), ("127.5.5.5", "loopback"), ("::1", "loopback"),
    ("0.0.0.0", "loopback"), ("::", "loopback"),
    ("224.0.0.1", "multicast"), ("ff02::1", "multicast"), ("255.255.255.255", "multicast"),
    ("169.254.169.254", "metadata_address"), ("169.254.170.2", "metadata_address"),
    ("fd00:ec2::254", "metadata_address"),
    ("8.8.8.8", "not_lan_address"), ("1.1.1.1", "not_lan_address"),
    ("172.32.0.1", "not_lan_address"), ("172.15.255.255", "not_lan_address"),
    ("100.64.0.1", "not_lan_address"), ("192.0.2.1", "not_lan_address"),
    ("2001:db8::1", "not_lan_address"), ("2606:4700::1111", "not_lan_address"),
])
def test_non_lan_literals_are_refused(host, code):
    assert _code(host) == code


def test_ipv4_mapped_ipv6_is_judged_as_the_ipv4_address():
    assert _code("::ffff:127.0.0.1") == "loopback"
    assert _code("::ffff:8.8.8.8") == "not_lan_address"
    assert _code("::ffff:169.254.169.254") == "metadata_address"
    assert validate_camera_host("::ffff:192.168.1.5", own_addresses=NO_OWN)


def test_own_address_is_refused():
    own = frozenset({ipaddress.ip_address("192.168.2.104")})
    assert _code("192.168.2.104", own=own) == "own_address"
    assert validate_camera_host("192.168.2.114", own_addresses=own)


def test_container_gateway_is_refused():
    gateway = frozenset({ipaddress.ip_address("172.17.0.1")})
    assert _code("172.17.0.1", gateway_addresses=gateway) == "gateway_address"
    assert validate_camera_host("172.17.0.2", own_addresses=NO_OWN, gateway_addresses=gateway)


def test_gateway_parsed_from_proc_net_route():
    table = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\x00FFFF0000\t0\t0\t0\n"
    )
    assert container_gateway_addresses(table) == frozenset({ipaddress.ip_address("172.17.0.1")})
    assert container_gateway_addresses("garbage") == frozenset()


@pytest.mark.parametrize("host", [
    "", "   ", "rtsp://192.168.1.5", "192.168.1.5/path", "user@192.168.1.5", "192.168.1.5:554",
    "[::1]", "-evil", "a b", "cam\nera", "x" * 300, "192.168.1.5#x", "cam?x=1",
    "localhost", "foo.localhost", "127.1", "2130706433", "0x7f.1", "1.2.3", "bad_name.lan",
])
def test_malformed_hosts_are_refused(host):
    assert _code(host) in {"invalid_host"}


def test_non_string_is_refused():
    assert _code(None) == "invalid_host"
    assert _code(123) == "invalid_host"


def test_name_resolving_to_lan_is_accepted_and_returns_addresses():
    result = validate_camera_host("cam.lan", own_addresses=NO_OWN, resolver=lambda h: ["192.168.2.114"])
    assert result == ["192.168.2.114"]


def test_every_resolved_address_must_pass():
    resolver = lambda h: ["192.168.2.114", "8.8.8.8"]
    assert _code("cam.example", resolver=resolver) == "not_lan_address"


def test_name_resolving_to_loopback_or_metadata_is_refused():
    assert _code("rebind.example", resolver=lambda h: ["127.0.0.1"]) == "loopback"
    assert _code("meta.example", resolver=lambda h: ["169.254.169.254"]) == "metadata_address"


def test_unresolvable_name_is_refused():
    assert _code("nope.lan", resolver=lambda h: []) == "unresolvable"

    def boom(host):
        raise AddressPolicyError("unresolvable", "x")

    assert _code("nope.lan", resolver=boom) == "unresolvable"


def test_rebinding_second_lookup_is_not_trusted():
    """The validated addresses are returned so the caller connects to THEM; a
    resolver that answers differently the second time cannot change that."""
    answers = iter([["192.168.2.114"], ["127.0.0.1"]])
    resolver = lambda h: next(answers)
    first = validate_camera_host("cam.lan", own_addresses=NO_OWN, resolver=resolver)
    assert first == ["192.168.2.114"]
    assert _code("cam.lan", resolver=resolver) == "loopback"  # re-validation catches it


def test_check_address_direct():
    check_address(ipaddress.ip_address("192.168.0.9"))
    with pytest.raises(AddressPolicyError):
        check_address(ipaddress.ip_address("8.8.4.4"))


# -- container mode (D3): the whole Docker network, not only the gateway ----------

def test_own_networks_are_computed_from_interfaces_and_skip_loopback():
    nets = container_own_networks([
        ("127.0.0.1", "255.0.0.0"), ("172.18.0.5", "255.255.0.0"),
        ("fd00:dead::5", "ffff:ffff:ffff:ffff::"), ("bad", "255.0.0.0"), ("10.1.2.3", "garbage"),
    ])
    assert [str(n) for n in nets] == ["172.18.0.0/16", "fd00:dead::/64"]
    assert container_own_networks([]) == ()


def test_whole_bridge_subnet_is_refused_not_just_the_gateway():
    nets = container_own_networks([("172.18.0.5", "255.255.0.0")])
    gw = frozenset({ipaddress.ip_address("172.18.0.1")})
    for host in ("172.18.0.1", "172.18.0.2", "172.18.7.200", "172.18.255.254"):
        with pytest.raises(AddressPolicyError) as info:
            validate_camera_host(host, own_addresses=NO_OWN, gateway_addresses=gw, blocked_networks=nets)
        assert info.value.code in ("gateway_address", "container_network")
    assert validate_camera_host("192.168.2.114", own_addresses=NO_OWN, gateway_addresses=gw, blocked_networks=nets)
    assert validate_camera_host("172.19.0.9", own_addresses=NO_OWN, gateway_addresses=gw, blocked_networks=nets)


def test_name_resolving_into_the_bridge_subnet_is_refused():
    nets = container_own_networks([("172.17.0.2", "255.255.0.0")])
    with pytest.raises(AddressPolicyError) as info:
        validate_camera_host("nas.lan", own_addresses=NO_OWN, gateway_addresses=frozenset(),
                             blocked_networks=nets, resolver=lambda h: ["172.17.0.1"])
    assert info.value.code == "container_network"


def test_default_validator_applies_container_rules_only_in_a_container(monkeypatch):
    import camera.address_policy as policy

    monkeypatch.setattr(policy, "container_gateway_addresses", lambda: frozenset({ipaddress.ip_address("172.17.0.1")}))
    monkeypatch.setattr(policy, "container_own_networks", lambda: (ipaddress.ip_network("172.17.0.0/16"),))
    monkeypatch.delenv("MESHCENTER_RUNTIME", raising=False)
    assert validate_camera_host("172.17.0.1", own_addresses=NO_OWN) == ["172.17.0.1"]  # native install: a normal LAN address

    monkeypatch.setenv("MESHCENTER_RUNTIME", "container")
    for host in ("172.17.0.1", "172.17.0.9"):
        with pytest.raises(AddressPolicyError):
            validate_camera_host(host, own_addresses=NO_OWN)
    assert validate_camera_host("192.168.2.114", own_addresses=NO_OWN)


def test_host_network_override_allows_own_subnet_but_never_the_gateway(monkeypatch):
    import camera.address_policy as policy

    monkeypatch.setattr(policy, "container_gateway_addresses", lambda: frozenset({ipaddress.ip_address("192.168.2.1")}))
    monkeypatch.setattr(policy, "container_own_networks", lambda: (ipaddress.ip_network("192.168.2.0/24"),))
    monkeypatch.setenv("MESHCENTER_RUNTIME", "container")
    with pytest.raises(AddressPolicyError) as info:
        validate_camera_host("192.168.2.114", own_addresses=NO_OWN)
    assert info.value.code == "container_network"
    monkeypatch.setenv("MESHCENTER_CAMERA_ALLOW_OWN_SUBNET", "1")
    assert validate_camera_host("192.168.2.114", own_addresses=NO_OWN) == ["192.168.2.114"]
    with pytest.raises(AddressPolicyError) as info:
        validate_camera_host("192.168.2.1", own_addresses=NO_OWN)
    assert info.value.code == "gateway_address"
