"""tests/test_provider_registry.py

Provider Registry tests (Execution Plan Step 0.7; corrected in Step 1.1
against ADR-0005's confirmed real-Relay `provider_id` formula). The
central DoD requirement carried over from Step 0.7: resolving an unknown
`provider_id` never performs a network call - MCAttach's only defense
against mesh-carried-URL SSRF (design spec section 20.1, "Произвольный
URL/SSRF").
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import time
from unittest import mock

import pytest

from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.provider_registry import (
    ProviderRegistry,
    ProviderRegistryError,
    compute_provider_id,
    normalize_origin,
)

SERVICE_KEY = b"\xab" * 32  # 32-byte Ed25519 public key, stand-in


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def registry(conn):
    return ProviderRegistry(conn, workspace_id="ws-1")


def test_normalize_origin_matches_real_relay_semantics():
    """ADR-0005: the real Relay normalizes with `strtolower(rtrim(raw,
    '/'))` on the submitted string, not a reconstructed URL - default
    ports are NOT stripped, unlike this module's pre-ADR-0005 behavior."""
    assert normalize_origin("https://mcattach.elektroniker.help") == "https://mcattach.elektroniker.help"
    assert normalize_origin("https://mcattach.elektroniker.help/") == "https://mcattach.elektroniker.help"
    assert normalize_origin("HTTPS://MCAttach.Elektroniker.Help") == "https://mcattach.elektroniker.help"
    # Explicit non-default port is preserved verbatim (lowercased), never stripped.
    assert normalize_origin("https://relay.example.net:8443") == "https://relay.example.net:8443"


def test_normalize_origin_rejects_non_https():
    with pytest.raises(ProviderRegistryError):
        normalize_origin("http://mcattach.elektroniker.help")


def test_normalize_origin_rejects_path_or_query():
    with pytest.raises(ProviderRegistryError):
        normalize_origin("https://mcattach.elektroniker.help/v1")
    with pytest.raises(ProviderRegistryError):
        normalize_origin("https://mcattach.elektroniker.help/?x=1")


def test_normalize_origin_rejects_credentials():
    with pytest.raises(ProviderRegistryError):
        normalize_origin("https://user:pass@relay.example.net")


def test_compute_provider_id_is_base64url_11_chars():
    """ADR-0005: Base64URL(8 bytes) is always 11 characters (no padding) -
    matches the real observed `61G003-kTq8` (11 chars), not hex (which
    would be 16)."""
    provider_id = compute_provider_id("https://relay.example.net", SERVICE_KEY)
    assert len(provider_id) == 11
    # Valid, unpadded Base64URL alphabet only.
    assert all(c.isalnum() or c in "-_" for c in provider_id)
    assert compute_provider_id("https://relay.example.net", SERVICE_KEY) == provider_id


def test_compute_provider_id_matches_reference_formula():
    """Cross-check against a from-scratch reimplementation of ADR-0005's
    formula (origin + "\\n" + raw pubkey, first 8 SHA-256 bytes,
    Base64URL) - catches any accidental drift between this test's
    expectations and the module under test."""
    origin = "https://mcattach.elektroniker.help"
    digest = hashlib.sha256(origin.encode("utf-8") + b"\n" + SERVICE_KEY).digest()
    expected = base64.urlsafe_b64encode(digest[:8]).rstrip(b"=").decode("ascii")
    assert compute_provider_id(origin, SERVICE_KEY) == expected


def test_compute_provider_id_changes_with_origin_or_key():
    base = compute_provider_id("https://relay.example.net", SERVICE_KEY)
    assert compute_provider_id("https://other.example.net", SERVICE_KEY) != base
    assert compute_provider_id("https://relay.example.net", b"\xcd" * 32) != base


def test_compute_provider_id_rejects_wrong_key_length():
    with pytest.raises(ProviderRegistryError):
        compute_provider_id("https://relay.example.net", b"\x00" * 10)


def test_register_and_resolve_round_trip(registry):
    profile = registry.register(
        display_name="Private MCA Relay",
        base_url="https://mcattach.elektroniker.help",
        service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
        is_default=True,
    )
    resolved = registry.resolve(profile.provider_id)
    assert resolved == profile
    assert resolved.origin == "https://mcattach.elektroniker.help"
    assert resolved.service_public_key == SERVICE_KEY
    assert resolved.tls_required is True
    assert resolved.is_default is True


def test_resolve_unknown_provider_id_returns_none(registry):
    assert registry.resolve("AAAAAAAAAAA") is None


def test_unknown_provider_id_never_triggers_network_call(registry):
    """Step 0.7 DoD, verbatim: an unknown provider_id must not cause a
    single network call - not DNS, not HTTP. Patches both `requests`
    entry points (module-level `requests.request` and
    `requests.Session.request`, since RelayClient - meshsrv/attachments/
    relay_client.py - defaults to a Session) and asserts they are never
    invoked while resolving a provider_id this registry has never seen."""
    with mock.patch("requests.api.request") as mocked_request, mock.patch(
        "requests.Session.request"
    ) as mocked_session_request:
        result = registry.resolve("deadbeefAAA")
        assert result is None
        assert mocked_request.call_count == 0
        assert mocked_session_request.call_count == 0


def test_provider_registry_module_does_not_import_networking_libraries():
    """Belt-and-suspenders companion to the mock-based test above: the
    module implementing `resolve()` must not even import an HTTP client,
    so there is no code path inside it that *could* make a network call."""
    import meshsrv.attachments.provider_registry as provider_registry_module

    source = provider_registry_module.__file__
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("import requests", "import httpx", "import socket", "import urllib.request"):
        assert forbidden not in text, f"provider_registry.py must never import networking libraries: {forbidden}"


def test_register_rejects_non_https_base_url(registry):
    with pytest.raises(ProviderRegistryError):
        registry.register(
            display_name="Insecure Relay",
            base_url="http://relay.example.net",
            service_public_key=SERVICE_KEY,
            max_ciphertext_bytes=6 * 1024 * 1024,
        )


def test_register_rejects_wrong_key_length(registry):
    with pytest.raises(ProviderRegistryError):
        registry.register(
            display_name="Bad Key Relay",
            base_url="https://relay.example.net",
            service_public_key=b"\x00" * 10,
            max_ciphertext_bytes=6 * 1024 * 1024,
        )


def test_re_registering_same_provider_is_idempotent_and_updates_fields(registry):
    first = registry.register(
        display_name="Old Name",
        base_url="https://relay.example.net",
        service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=5 * 1024 * 1024,
    )
    second = registry.register(
        display_name="New Name",
        base_url="https://relay.example.net",
        service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    assert first.provider_id == second.provider_id
    assert registry.resolve(first.provider_id).display_name == "New Name"
    assert len(registry.list_providers()) == 1


def test_list_providers_and_get_default(registry):
    registry.register(
        display_name="A",
        base_url="https://a.example.net",
        service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
        is_default=True,
    )
    registry.register(
        display_name="B",
        base_url="https://b.example.net",
        service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    assert len(registry.list_providers()) == 2
    default = registry.get_default()
    assert default is not None
    assert default.display_name == "A"


def test_workspaces_are_isolated(conn):
    registry_a = ProviderRegistry(conn, workspace_id="ws-a")
    registry_b = ProviderRegistry(conn, workspace_id="ws-b")
    profile = registry_a.register(
        display_name="A-only",
        base_url="https://a.example.net",
        service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    assert registry_b.resolve(profile.provider_id) is None
    assert registry_b.list_providers() == []
