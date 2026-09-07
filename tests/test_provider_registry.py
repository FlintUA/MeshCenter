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
import sys
import time
from unittest import mock

import pytest

from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.provider_registry import (
    CLEAR,
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


# ---- ADR-0008 / migration 8: set_default(), upload tokens, update/remove -


from meshsrv.attachments.workspace import MCAWorkspaceManager


@pytest.fixture
def wsm(tmp_path):
    return MCAWorkspaceManager(str(tmp_path / "data"))


def test_register_is_default_true_clears_previous_default(registry):
    first = registry.register(
        display_name="A", base_url="https://a.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024, is_default=True,
    )
    second = registry.register(
        display_name="B", base_url="https://b.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024, is_default=True,
    )
    assert registry.resolve(first.provider_id).is_default is False
    assert registry.resolve(second.provider_id).is_default is True
    assert registry.get_default().provider_id == second.provider_id


def test_set_default_switches_atomically_and_rejects_unknown_id(registry):
    a = registry.register(
        display_name="A", base_url="https://a.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024, is_default=True,
    )
    b = registry.register(
        display_name="B", base_url="https://b.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    registry.set_default(b.provider_id)
    assert registry.resolve(a.provider_id).is_default is False
    assert registry.resolve(b.provider_id).is_default is True

    with pytest.raises(ProviderRegistryError):
        registry.set_default("does-not-exist")
    # a failed set_default() must not have cleared the real default
    assert registry.resolve(b.provider_id).is_default is True


def test_partial_unique_index_enforces_at_most_one_default(conn, registry):
    a = registry.register(
        display_name="A", base_url="https://a.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024, is_default=True,
    )
    registry.register(
        display_name="B", base_url="https://b.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    # bypass ProviderRegistry entirely and try to violate the invariant
    # directly at the schema level - the partial unique index must reject it.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE mca_provider_profiles SET is_default = 1 WHERE workspace_id = 'ws-1' AND provider_id != ?",
            (a.provider_id,),
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits not meaningful on Windows")
def test_upload_token_round_trips_and_is_never_on_the_profile(registry, wsm):
    profile = registry.register(
        display_name="A", base_url="https://a.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    assert profile.upload_token_configured is False
    assert registry.get_upload_token(profile.provider_id, wsm, "a1b2c3d4e5f60718") is None

    registry.set_upload_token(profile.provider_id, wsm, "a1b2c3d4e5f60718", "super-secret-token")

    refreshed = registry.resolve(profile.provider_id)
    assert refreshed.upload_token_configured is True
    # the dataclass never carries the raw token itself under any field name
    assert "super-secret-token" not in repr(refreshed)

    assert registry.get_upload_token(profile.provider_id, wsm, "a1b2c3d4e5f60718") == "super-secret-token"

    token_path = wsm.paths("a1b2c3d4e5f60718").keys / f"relay_upload_token_{profile.provider_id}.secret"
    assert token_path.exists()
    assert (token_path.stat().st_mode & 0o777) == 0o600


def test_update_profile_never_changes_identity_fields(registry):
    profile = registry.register(
        display_name="Old Name", base_url="https://a.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    updated = registry.update_profile(profile.provider_id, display_name="New Name", enabled=False)
    assert updated.display_name == "New Name"
    assert updated.enabled is False
    # provider_id/origin/service_public_key are untouched - update_profile()
    # has no parameters for them at all
    assert updated.provider_id == profile.provider_id
    assert updated.origin == profile.origin
    assert updated.service_public_key == profile.service_public_key


def test_update_profile_clear_sentinel_explicitly_nulls_a_ttl(registry):
    """PR #231 review, section 9: the old COALESCE(?, column) UPDATE could
    never distinguish 'not mentioned' from 'clear it' - both were plain
    None. registry.CLEAR is the fix; a caller that means 'leave alone'
    still just omits the argument."""
    profile = registry.register(
        display_name="Clearable", base_url="https://clear.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024, min_ttl_seconds=60, max_ttl_seconds=3600,
        protocol_version="1.0",
    )
    assert profile.min_ttl_seconds == 60
    assert profile.protocol_version == "1.0"

    # Omitting the argument must leave it untouched...
    unchanged = registry.update_profile(profile.provider_id, display_name="Still Clearable")
    assert unchanged.min_ttl_seconds == 60
    assert unchanged.protocol_version == "1.0"

    # ...while passing CLEAR must actually null it, not leave the old value.
    cleared = registry.update_profile(
        profile.provider_id, min_ttl_seconds=CLEAR, max_ttl_seconds=CLEAR, protocol_version=CLEAR
    )
    assert cleared.min_ttl_seconds is None
    assert cleared.max_ttl_seconds is None
    assert cleared.protocol_version is None


def test_update_profile_rejects_a_ttl_pair_that_would_become_invalid(registry):
    """Validation runs against the *effective* (post-update) values, not
    just whatever this one call happens to pass - lowering min_ttl_seconds
    above the existing max_ttl_seconds must be rejected even though this
    call never touches max_ttl_seconds itself."""
    profile = registry.register(
        display_name="Bounded", base_url="https://bounded.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024, min_ttl_seconds=60, max_ttl_seconds=120,
    )
    with pytest.raises(ProviderRegistryError):
        registry.update_profile(profile.provider_id, min_ttl_seconds=500)
    with pytest.raises(ProviderRegistryError):
        registry.update_profile(profile.provider_id, min_ttl_seconds=-1)
    with pytest.raises(ProviderRegistryError):
        registry.update_profile(profile.provider_id, protocol_version="   ")


def test_register_rejects_malformed_backend_fields(registry):
    """PR #231 review, section 9: kind/display_name/max_ciphertext_bytes/
    the TTL pair/protocol_version must all be validated at registration
    time, not left to fail later, mid-transfer, far from the actual
    mistake."""
    base_kwargs = dict(base_url="https://bad.example.net", service_public_key=SERVICE_KEY)

    with pytest.raises(ProviderRegistryError):
        registry.register(display_name="", max_ciphertext_bytes=1024, **base_kwargs)
    with pytest.raises(ProviderRegistryError):
        registry.register(display_name="X", max_ciphertext_bytes=0, **base_kwargs)
    with pytest.raises(ProviderRegistryError):
        registry.register(display_name="X", max_ciphertext_bytes=1024, kind="bogus", **base_kwargs)
    with pytest.raises(ProviderRegistryError):
        registry.register(
            display_name="X", max_ciphertext_bytes=1024, min_ttl_seconds=100, max_ttl_seconds=50, **base_kwargs
        )
    with pytest.raises(ProviderRegistryError):
        registry.register(display_name="X", max_ciphertext_bytes=1024, protocol_version="  ", **base_kwargs)


def test_set_upload_token_rejects_empty_token(registry, wsm):
    profile = registry.register(
        display_name="Tokened", base_url="https://tokened.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    with pytest.raises(ProviderRegistryError):
        registry.set_upload_token(profile.provider_id, wsm, "a1b2c3d4e5f60718", "")
    with pytest.raises(ProviderRegistryError):
        registry.set_upload_token(profile.provider_id, wsm, "a1b2c3d4e5f60718", "   ")


def test_remove_or_disable_deletes_when_unused_disables_when_referenced(conn, registry, wsm):
    unused = registry.register(
        display_name="Unused", base_url="https://unused.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    in_use = registry.register(
        display_name="InUse", base_url="https://inuse.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    registry.set_upload_token(in_use.provider_id, wsm, "a1b2c3d4e5f60718", "tok")
    conn.execute(
        """
        INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, provider_id, state,
                                  created_at, hard_expires_at, download_grace_seconds)
        VALUES ('att-1', 'ws-1', 'deadbeef', 'sent', 'principal-1', ?, 'DRAFT', 0, 0, 0)
        """,
        (in_use.provider_id,),
    )
    conn.commit()

    assert registry.remove_or_disable(unused.provider_id, wsm, "a1b2c3d4e5f60718") == "deleted"
    assert registry.resolve(unused.provider_id) is None

    assert registry.remove_or_disable(in_use.provider_id, wsm, "a1b2c3d4e5f60718") == "disabled"
    still_there = registry.resolve(in_use.provider_id)
    assert still_there is not None
    assert still_there.enabled is False


def test_get_upload_candidates_requires_enabled_upload_allowed_and_token(registry, wsm):
    no_token = registry.register(
        display_name="NoToken", base_url="https://no-token.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    with_token = registry.register(
        display_name="WithToken", base_url="https://with-token.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024,
    )
    registry.set_upload_token(with_token.provider_id, wsm, "a1b2c3d4e5f60718", "tok")
    download_only = registry.register(
        display_name="DownloadOnly", base_url="https://download-only.example.net", service_public_key=SERVICE_KEY,
        max_ciphertext_bytes=6 * 1024 * 1024, upload_allowed=False,
    )
    registry.set_upload_token(download_only.provider_id, wsm, "a1b2c3d4e5f60718", "tok")

    candidates = {p.provider_id for p in registry.get_upload_candidates()}
    assert candidates == {with_token.provider_id}
