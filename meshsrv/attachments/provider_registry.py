"""meshsrv/attachments/provider_registry.py

Local Provider Registry (design spec section 10.1; Execution Plan Step
0.7; corrected in Step 1.1/ADR-0005 against the real deployed Relay's
source). Backed by the `mca_provider_profiles` table
(`meshsrv.attachments.db.migrations`). MIT-licensed Core code - does not
import `meshtastic`.

The central safety property this module exists to enforce: MeshCenter
never builds a Relay URL from anything carried inside an incoming MCA
message, and never performs a DNS/HTTP lookup for a `provider_id` it does
not already have a locally-registered profile for (design spec section
10.1, "Если provider неизвестен" list; section 20.1, "Произвольный
URL/SSRF" row). `ProviderRegistry.resolve()` is the *only* sanctioned way
to turn a `provider_id` into a usable Relay endpoint, and on a miss it
returns `None` - it does not fall back to guessing, resolving DNS to
"check", or accepting a URL argument at all. See
`tests/test_provider_registry.py::test_unknown_provider_id_never_triggers_network_call`.

MVP trust bootstrap (design spec section 10.1) is one of exactly three
paths, none of which this module performs on its own - it only stores the
result once the caller (UI/admin flow) has already gone through one:
one built-in default provider shipped with a MeshCenter release; manual
admin entry of a base URL with explicit full-fingerprint confirmation; or
import of a signed `.mcaprovider`/QR profile, again with admin fingerprint
confirmation. `register()` assumes that confirmation already happened.

`provider_id`/service-key text encoding: **Base64URL** (RFC 4648 section
5, no padding), confirmed against the real Relay's own
`mca_provider_id()` (ADR-0005) - not hex, which is what this module used
before that ADR. `compute_provider_id` hashes
`origin + "\\n" + raw 32-byte Ed25519 public key`, matching the real
implementation byte-for-byte (including the `"\\n"` separator this module
originally omitted).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import sqlite3
import time
from typing import List, Optional
from urllib.parse import urlsplit


class ProviderRegistryError(ValueError):
    """Raised for a malformed provider registration - never silently
    corrected (e.g. a non-HTTPS base_url is rejected, not upgraded)."""


@dataclasses.dataclass(frozen=True)
class ProviderProfile:
    provider_id: str  # Base64URL, 11 chars (8 bytes)
    display_name: str
    origin: str
    service_public_key: bytes  # raw 32 bytes (Ed25519)
    tls_required: bool
    upload_allowed: bool
    download_allowed: bool
    max_ciphertext_bytes: int
    is_default: bool
    added_at: float


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str, expected_length: Optional[int] = None) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        decoded = base64.urlsafe_b64decode(text + padding)
    except Exception as exc:  # noqa: BLE001 - any malformed base64url is the same error to callers
        raise ProviderRegistryError(f"invalid Base64URL value: {text!r}") from exc
    if expected_length is not None and len(decoded) != expected_length:
        raise ProviderRegistryError(
            f"Base64URL value decoded to {len(decoded)} bytes, expected {expected_length}"
        )
    return decoded


def normalize_origin(base_url: str) -> str:
    """Normalize a Relay base URL to the origin used in the `provider_id`
    formula. Deliberately mirrors the real Relay's own normalization
    exactly (ADR-0005: `strtolower(rtrim($baseUrl, '/'))` on the raw
    string, not a reconstructed URL) after validating it parses as a bare
    HTTPS origin with no path/query/fragment/credentials - this module
    must never derive a different `origin` string than the Relay would for
    the same input, since that would silently produce a different
    `provider_id` than the one the Relay actually reports."""
    raw = base_url.strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise ProviderRegistryError(f"provider base_url must use https: {base_url!r}")
    if not parts.hostname:
        raise ProviderRegistryError(f"provider base_url has no host: {base_url!r}")
    if parts.username or parts.password:
        raise ProviderRegistryError(f"provider base_url must not carry credentials: {base_url!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProviderRegistryError(f"provider base_url must be a bare origin: {base_url!r}")
    return raw.rstrip("/").lower()


def compute_provider_id(origin: str, service_public_key: bytes) -> str:
    """ADR-0005 / real Relay `mca_provider_id()`: Base64URL of the first 8
    bytes of SHA-256(`origin` + `"\\n"` + raw 32-byte Ed25519 public key).

    This is a 64-bit *lookup* value, not proof of trust on its own: a real
    registration is trusted because it went through one of the three
    bootstrap paths in the module docstring, not because its provider_id
    happens to match this formula.
    """
    if len(service_public_key) != 32:
        raise ProviderRegistryError(f"service_public_key must be 32 raw bytes (Ed25519), got {len(service_public_key)}")
    material = origin.encode("utf-8") + b"\n" + service_public_key
    digest = hashlib.sha256(material).digest()
    return _b64url_encode(digest[:8])


def _row_to_profile(row: sqlite3.Row) -> ProviderProfile:
    return ProviderProfile(
        provider_id=row["provider_id"],
        display_name=row["display_name"],
        origin=row["origin"],
        service_public_key=_b64url_decode(row["service_public_key_b64url"], 32),
        tls_required=bool(row["tls_required"]),
        upload_allowed=bool(row["upload_allowed"]),
        download_allowed=bool(row["download_allowed"]),
        max_ciphertext_bytes=row["max_ciphertext_bytes"],
        is_default=bool(row["is_default"]),
        added_at=row["added_at"],
    )


class ProviderRegistry:
    """One workspace's local Provider Registry. `conn` must already be
    migrated to at least schema version 3 (`meshsrv.attachments.db.migrations`)."""

    def __init__(self, conn: sqlite3.Connection, workspace_id: str):
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._workspace_id = workspace_id

    def register(
        self,
        *,
        display_name: str,
        base_url: str,
        service_public_key: bytes,
        max_ciphertext_bytes: int,
        upload_allowed: bool = True,
        download_allowed: bool = True,
        is_default: bool = False,
        now: Optional[float] = None,
    ) -> ProviderProfile:
        """Store a provider profile whose trust was already established by
        the caller through one of the three MVP bootstrap paths (module
        docstring). Registering the same (origin, service key) pair again
        is idempotent - it refreshes the stored fields rather than
        conflicting, since re-importing the same signed `.mcaprovider`
        profile is a normal admin action, not an error."""
        origin = normalize_origin(base_url)
        if len(service_public_key) != 32:
            raise ProviderRegistryError(
                f"service_public_key must be 32 raw bytes (Ed25519), got {len(service_public_key)}"
            )
        provider_id = compute_provider_id(origin, service_public_key)
        now = time.time() if now is None else now
        self._conn.execute(
            """
            INSERT INTO mca_provider_profiles
                (provider_id, workspace_id, origin, service_public_key_b64url,
                 max_ciphertext_bytes, hard_expiry_default_seconds, is_default, added_at,
                 display_name, tls_required, upload_allowed, download_allowed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                display_name = excluded.display_name,
                max_ciphertext_bytes = excluded.max_ciphertext_bytes,
                upload_allowed = excluded.upload_allowed,
                download_allowed = excluded.download_allowed,
                is_default = excluded.is_default
            """,
            (
                provider_id,
                self._workspace_id,
                origin,
                _b64url_encode(service_public_key),
                max_ciphertext_bytes,
                72 * 3600,
                int(is_default),
                now,
                display_name,
                int(upload_allowed),
                int(download_allowed),
            ),
        )
        self._conn.commit()
        return self.resolve(provider_id)  # type: ignore[return-value]

    def resolve(self, provider_id: str) -> Optional[ProviderProfile]:
        """The only sanctioned provider_id -> profile lookup. Returns
        `None` on a miss - never performs DNS/HTTP, never guesses a URL,
        never treats the caller's own claim about the provider (if any) as
        a substitute for a local match. A miss means the caller must show
        `Неизвестное хранилище` / `WAITING_PROVIDER` (design spec section
        10.1), not attempt to reach the network."""
        row = self._conn.execute(
            "SELECT * FROM mca_provider_profiles WHERE provider_id = ? AND workspace_id = ?",
            (provider_id, self._workspace_id),
        ).fetchone()
        if row is None:
            return None
        return _row_to_profile(row)

    def list_providers(self) -> List[ProviderProfile]:
        rows = self._conn.execute(
            "SELECT * FROM mca_provider_profiles WHERE workspace_id = ? ORDER BY added_at",
            (self._workspace_id,),
        ).fetchall()
        return [_row_to_profile(row) for row in rows]

    def get_default(self) -> Optional[ProviderProfile]:
        row = self._conn.execute(
            "SELECT * FROM mca_provider_profiles WHERE workspace_id = ? AND is_default = 1 LIMIT 1",
            (self._workspace_id,),
        ).fetchone()
        return _row_to_profile(row) if row is not None else None
