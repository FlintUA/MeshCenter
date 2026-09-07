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
import os
import sqlite3
import time
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from meshsrv.attachments.workspace import MCAWorkspaceManager

_UPLOAD_TOKEN_FILE_MODE = 0o600

_VALID_KINDS = frozenset({"own", "third_party"})


class _ClearSentinel:
    """A distinct sentinel type (not just `object()`) so `repr()` on an
    accidentally-unhandled value in a log line or debugger is still
    legible - see `CLEAR` below."""

    def __repr__(self) -> str:
        return "CLEAR"


# PR #231 review, section 9: `update_profile()`'s optional TTL/
# protocol_version fields used a plain `Optional[int] = None` default,
# which cannot distinguish "caller did not mention this field, leave it
# alone" from "caller wants it explicitly cleared back to NULL" - both
# looked like `None` to the old `COALESCE(?, column)` SQL, so a caller
# could never actually clear a previously-set TTL. Pass this sentinel
# instead of `None` to mean "clear it"; omit the argument (or pass
# `None`) to mean "leave it alone".
CLEAR = _ClearSentinel()


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
    # ADR-0008 / migration 8 (Step 1.6A):
    kind: str = "own"  # "own" | "third_party"
    enabled: bool = True
    min_ttl_seconds: Optional[int] = None
    max_ttl_seconds: Optional[int] = None
    protocol_version: Optional[str] = None
    upload_token_configured: bool = False
    last_checked_at: Optional[float] = None
    last_check_result: Optional[str] = None
    last_latency_ms: Optional[int] = None
    last_error_code: Optional[str] = None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_encode(data: bytes) -> str:
    """Public alias for `_b64url_encode`, for callers outside this module
    (e.g. `connectivity_monitor.py` comparing a Relay's self-reported
    `/v1/info` public key against the pinned profile) that need the exact
    same Base64URL-no-padding encoding this registry uses on disk."""
    return _b64url_encode(data)


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


def encode_provider_id(raw: bytes) -> str:
    """Base64URL-encode an already-known 8-byte provider_id (e.g. from an
    OFFER's wire field, ADR-0001 section 3 key 2 - raw bytes on the wire)
    into the text form `ProviderRegistry` keys rows by. Not to be confused
    with `compute_provider_id()`, which *derives* a provider_id from
    (origin, service_public_key) - use this one when the raw 8 bytes are
    already in hand and only need the matching text key (ADR-0007:
    `receiver.py` uses this for every `provider_registry.resolve()` call,
    since storing/looking up an OFFER's provider_id as hex - a different
    encoding - would never match a real registration)."""
    if len(raw) != 8:
        raise ProviderRegistryError(f"provider_id must be 8 raw bytes, got {len(raw)}")
    return _b64url_encode(raw)


def decode_provider_id(text: str) -> bytes:
    """Inverse of `encode_provider_id()` - Base64URL text (the form
    `ProviderRegistry` keys rows by, and now the sole on-disk encoding of
    `attachments.provider_id` for both 'sent' and 'received' rows -
    Migration 9) back to the raw 8 bytes an OFFER's wire field needs."""
    return _b64url_decode(text, 8)


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


def _validate_display_name(display_name: str) -> None:
    if not display_name or not display_name.strip():
        raise ProviderRegistryError("display_name must not be empty")


def _validate_kind(kind: str) -> None:
    if kind not in _VALID_KINDS:
        raise ProviderRegistryError(f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")


def _validate_max_ciphertext_bytes(max_ciphertext_bytes: int) -> None:
    if max_ciphertext_bytes <= 0:
        raise ProviderRegistryError(f"max_ciphertext_bytes must be positive, got {max_ciphertext_bytes!r}")


def _validate_protocol_version(protocol_version: Optional[str]) -> None:
    if protocol_version is not None and not protocol_version.strip():
        raise ProviderRegistryError("protocol_version must not be an empty string (use CLEAR/None to omit it)")


def _validate_ttl_pair(min_ttl_seconds: Optional[int], max_ttl_seconds: Optional[int]) -> None:
    """PR #231 review, section 9: both TTL bounds must be positive when
    given, and min must never exceed max - a Relay whose min_ttl exceeds
    its own max_ttl can never satisfy any request, a silently-broken
    profile rather than a rejected-at-the-door one."""
    if min_ttl_seconds is not None and min_ttl_seconds <= 0:
        raise ProviderRegistryError(f"min_ttl_seconds must be positive, got {min_ttl_seconds!r}")
    if max_ttl_seconds is not None and max_ttl_seconds <= 0:
        raise ProviderRegistryError(f"max_ttl_seconds must be positive, got {max_ttl_seconds!r}")
    if min_ttl_seconds is not None and max_ttl_seconds is not None and min_ttl_seconds > max_ttl_seconds:
        raise ProviderRegistryError(
            f"min_ttl_seconds ({min_ttl_seconds!r}) must not exceed max_ttl_seconds ({max_ttl_seconds!r})"
        )


def _row_to_profile(row: sqlite3.Row) -> ProviderProfile:
    row_keys = row.keys()
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
        # ADR-0008 columns (migration 8) - guarded with `in row_keys` so
        # this function keeps working against a pre-migration-8 row shape
        # in case any caller still passes one in (defensive only; every
        # real caller migrates through migration 8 as part of the same
        # `migrate()` call).
        kind=(row["kind"] if "kind" in row_keys else "own"),
        enabled=bool(row["enabled"]) if "enabled" in row_keys else True,
        min_ttl_seconds=(row["min_ttl_seconds"] if "min_ttl_seconds" in row_keys else None),
        max_ttl_seconds=(row["max_ttl_seconds"] if "max_ttl_seconds" in row_keys else None),
        protocol_version=(row["protocol_version"] if "protocol_version" in row_keys else None),
        upload_token_configured=(
            row["upload_token_file"] is not None if "upload_token_file" in row_keys else False
        ),
        last_checked_at=(row["last_checked_at"] if "last_checked_at" in row_keys else None),
        last_check_result=(row["last_check_result"] if "last_check_result" in row_keys else None),
        last_latency_ms=(row["last_latency_ms"] if "last_latency_ms" in row_keys else None),
        last_error_code=(row["last_error_code"] if "last_error_code" in row_keys else None),
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
        kind: str = "own",
        min_ttl_seconds: Optional[int] = None,
        max_ttl_seconds: Optional[int] = None,
        protocol_version: Optional[str] = None,
        now: Optional[float] = None,
    ) -> ProviderProfile:
        """Store a provider profile whose trust was already established by
        the caller through one of the three MVP bootstrap paths (module
        docstring). Registering the same (origin, service key) pair again
        is idempotent - it refreshes the stored fields rather than
        conflicting, since re-importing the same signed `.mcaprovider`
        profile is a normal admin action, not an error.

        `is_default=True` is handled by a separate call to `set_default()`
        below rather than folded into this INSERT's own `ON CONFLICT`
        clause (ADR-0008): the old approach only ever updated the single
        upserted row's own `is_default` column, so nothing ever cleared a
        previously-default row when a second one was registered as
        default - two rows could each hold `is_default=1` and
        `get_default()`'s unordered `LIMIT 1` would then pick between them
        non-deterministically. `set_default()` is transactional and is
        the only place that clears the old default, so this method can
        never reproduce that bug again.

        PR #231 review, section 9: validates `kind`/`display_name`/
        `max_ciphertext_bytes`/the TTL pair/`protocol_version` up front -
        a malformed profile used to be accepted silently and only fail
        later, mid-transfer, in a way much harder to trace back to a bad
        registration."""
        origin = normalize_origin(base_url)
        if len(service_public_key) != 32:
            raise ProviderRegistryError(
                f"service_public_key must be 32 raw bytes (Ed25519), got {len(service_public_key)}"
            )
        _validate_display_name(display_name)
        _validate_kind(kind)
        _validate_max_ciphertext_bytes(max_ciphertext_bytes)
        _validate_ttl_pair(min_ttl_seconds, max_ttl_seconds)
        _validate_protocol_version(protocol_version)
        provider_id = compute_provider_id(origin, service_public_key)
        now = time.time() if now is None else now
        self._conn.execute(
            """
            INSERT INTO mca_provider_profiles
                (provider_id, workspace_id, origin, service_public_key_b64url,
                 max_ciphertext_bytes, hard_expiry_default_seconds, is_default, added_at,
                 display_name, tls_required, upload_allowed, download_allowed,
                 kind, min_ttl_seconds, max_ttl_seconds, protocol_version)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                display_name = excluded.display_name,
                max_ciphertext_bytes = excluded.max_ciphertext_bytes,
                upload_allowed = excluded.upload_allowed,
                download_allowed = excluded.download_allowed,
                kind = excluded.kind,
                min_ttl_seconds = excluded.min_ttl_seconds,
                max_ttl_seconds = excluded.max_ttl_seconds,
                protocol_version = excluded.protocol_version
            """,
            (
                provider_id,
                self._workspace_id,
                origin,
                _b64url_encode(service_public_key),
                max_ciphertext_bytes,
                72 * 3600,
                now,
                display_name,
                int(upload_allowed),
                int(download_allowed),
                kind,
                min_ttl_seconds,
                max_ttl_seconds,
                protocol_version,
            ),
        )
        self._conn.commit()
        if is_default:
            return self.set_default(provider_id)
        return self.resolve(provider_id)  # type: ignore[return-value]

    def set_default(self, provider_id: str) -> ProviderProfile:
        """The one sanctioned way to change this workspace's default
        provider (ADR-0008). Transactional: clears `is_default` on every
        other row in this workspace before setting it on `provider_id`, so
        two rows can never simultaneously hold `is_default=1` (also
        enforced at the schema level by migration 8's partial unique
        index `idx_mca_provider_profiles_one_default` - this method just
        means that invariant is never violated in the first place, rather
        than relying on the index to reject a bad write after the fact)."""
        with self._conn:
            self._conn.execute(
                "UPDATE mca_provider_profiles SET is_default = 0 WHERE workspace_id = ? AND is_default = 1",
                (self._workspace_id,),
            )
            cur = self._conn.execute(
                "UPDATE mca_provider_profiles SET is_default = 1 WHERE workspace_id = ? AND provider_id = ?",
                (self._workspace_id, provider_id),
            )
            if cur.rowcount == 0:
                raise ProviderRegistryError(f"no such provider_id in this workspace: {provider_id!r}")
        profile = self.resolve(provider_id)
        assert profile is not None  # just written above, in the same connection
        return profile

    def update_profile(
        self,
        provider_id: str,
        *,
        display_name: Optional[str] = None,
        enabled: Optional[bool] = None,
        upload_allowed: Optional[bool] = None,
        download_allowed: Optional[bool] = None,
        min_ttl_seconds: "Optional[int] | _ClearSentinel" = None,
        max_ttl_seconds: "Optional[int] | _ClearSentinel" = None,
        protocol_version: "Optional[str] | _ClearSentinel" = None,
    ) -> ProviderProfile:
        """Edits the mutable, non-identity-defining fields of an existing
        profile. Deliberately does not accept `service_public_key`/
        `origin`/`base_url` - changing either of those changes what
        `provider_id` even means (`compute_provider_id()`), which is a new
        registration (a fresh trust-bootstrap confirmation), never a
        silent edit of an existing one (ADR-0008).

        PR #231 review, section 9: `min_ttl_seconds`/`max_ttl_seconds`/
        `protocol_version` used a plain `COALESCE(?, column)` UPDATE,
        which can never distinguish "not mentioned, leave alone" from
        "explicitly clear to NULL" - both arrive at the SQL layer as the
        same `None`. Pass `provider_registry.CLEAR` for one of these
        three fields to explicitly clear it; omit the argument (or pass
        `None`) to leave it untouched. A plain int/str value sets it, and
        is validated the same way `register()` validates it - positive,
        min<=max, non-empty."""
        existing = self.resolve(provider_id)
        if existing is None:
            raise ProviderRegistryError(f"no such provider_id in this workspace: {provider_id!r}")
        if display_name is not None:
            _validate_display_name(display_name)

        def _resolve(value, current):
            """None -> leave alone (current value); CLEAR -> NULL; else -> the new value."""
            if value is None:
                return current
            if value is CLEAR:
                return None
            return value

        effective_min_ttl = _resolve(min_ttl_seconds, existing.min_ttl_seconds)
        effective_max_ttl = _resolve(max_ttl_seconds, existing.max_ttl_seconds)
        _validate_ttl_pair(effective_min_ttl, effective_max_ttl)
        effective_protocol_version = _resolve(protocol_version, existing.protocol_version)
        _validate_protocol_version(effective_protocol_version)

        self._conn.execute(
            """
            UPDATE mca_provider_profiles SET
                display_name = COALESCE(?, display_name),
                enabled = COALESCE(?, enabled),
                upload_allowed = COALESCE(?, upload_allowed),
                download_allowed = COALESCE(?, download_allowed),
                min_ttl_seconds = ?,
                max_ttl_seconds = ?,
                protocol_version = ?
            WHERE workspace_id = ? AND provider_id = ?
            """,
            (
                display_name,
                None if enabled is None else int(enabled),
                None if upload_allowed is None else int(upload_allowed),
                None if download_allowed is None else int(download_allowed),
                effective_min_ttl,
                effective_max_ttl,
                effective_protocol_version,
                self._workspace_id,
                provider_id,
            ),
        )
        self._conn.commit()
        return self.resolve(provider_id)  # type: ignore[return-value]

    def record_check_result(
        self,
        provider_id: str,
        *,
        result: str,
        latency_ms: Optional[int] = None,
        error_code: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        """Caches the last health/info probe result (`ConnectivityMonitor`
        is the only intended caller) so `GET /api/mca/providers` (1.6A)
        can serve a cheap cached status without the request itself
        triggering network I/O."""
        now = time.time() if now is None else now
        self._conn.execute(
            """
            UPDATE mca_provider_profiles
            SET last_checked_at = ?, last_check_result = ?, last_latency_ms = ?, last_error_code = ?
            WHERE workspace_id = ? AND provider_id = ?
            """,
            (now, result, latency_ms, error_code, self._workspace_id, provider_id),
        )
        self._conn.commit()

    def remove_or_disable(self, provider_id: str, workspace_manager: "MCAWorkspaceManager", principal_id: str) -> str:
        """Deletes the profile outright (and its upload token file, if
        any) when nothing references it; otherwise disables it and keeps
        history intact (ADR-0008). Returns `"deleted"` or `"disabled"`.
        `resolve()` deliberately keeps returning a disabled profile (it is
        a UI/worker-facing filter, not a trust decision) so an in-flight
        transfer against a since-disabled Relay can still finish or fail
        cleanly instead of hitting a `None` resolve mid-transfer."""
        in_use = self._conn.execute(
            "SELECT 1 FROM attachments WHERE workspace_id = ? AND provider_id = ? LIMIT 1",
            (self._workspace_id, provider_id),
        ).fetchone()
        if in_use is not None:
            self.update_profile(provider_id, enabled=False)
            return "disabled"
        self._delete_upload_token_file(provider_id, workspace_manager, principal_id)
        self._conn.execute(
            "DELETE FROM mca_provider_profiles WHERE workspace_id = ? AND provider_id = ?",
            (self._workspace_id, provider_id),
        )
        self._conn.commit()
        return "deleted"

    def list_enabled(self) -> List[ProviderProfile]:
        return [p for p in self.list_providers() if p.enabled]

    def get_upload_candidates(self) -> List[ProviderProfile]:
        """Profiles a send form may offer as a destination: enabled,
        upload-allowed, and with an upload credential actually configured
        - a Relay can be perfectly `download_allowed` (e.g. a
        received-only or third-party profile) without ever being a valid
        upload target."""
        return [p for p in self.list_enabled() if p.upload_allowed and p.upload_token_configured]

    def get_download_profile(self, provider_id: str) -> Optional[ProviderProfile]:
        """Alias for `resolve()`, for read-path clarity at 1.6A call
        sites that are specifically about the receive/download path -
        behavior is identical, this adds no new restriction."""
        return self.resolve(provider_id)

    # ---- upload token storage (ADR-0008: never a plaintext DB column) --

    def _upload_token_path(self, provider_id: str, workspace_manager: "MCAWorkspaceManager", principal_id: str):
        paths = workspace_manager.ensure_workspace(principal_id)
        return paths.keys / f"relay_upload_token_{provider_id}.secret"

    def set_upload_token(
        self, provider_id: str, workspace_manager: "MCAWorkspaceManager", principal_id: str, token: str
    ) -> None:
        """Writes `token` to a `0600` file under this workspace's `keys/`
        directory (already `0700` - `WorkspacePaths`/`identity.py`'s
        existing convention, not a new trust boundary) and records only
        the filename in `mca_provider_profiles.upload_token_file`. The
        token itself never touches a DB row, an HTTP response, or a log
        line (ADR-0008).

        PR #231 review, section 9: rejects an empty token outright - a
        silently-stored empty upload token file used to read back as a
        real, "configured" token (`upload_token_configured=True`) that
        then failed every real upload attempt with an opaque auth error
        instead of never being marked configured in the first place."""
        if self.resolve(provider_id) is None:
            raise ProviderRegistryError(f"no such provider_id in this workspace: {provider_id!r}")
        if not token or not token.strip():
            raise ProviderRegistryError("upload token must not be empty")
        token_path = self._upload_token_path(provider_id, workspace_manager, principal_id)
        fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), _UPLOAD_TOKEN_FILE_MODE)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(token_path, _UPLOAD_TOKEN_FILE_MODE)
        self._conn.execute(
            "UPDATE mca_provider_profiles SET upload_token_file = ? WHERE workspace_id = ? AND provider_id = ?",
            (token_path.name, self._workspace_id, provider_id),
        )
        self._conn.commit()

    def get_upload_token(
        self, provider_id: str, workspace_manager: "MCAWorkspaceManager", principal_id: str
    ) -> Optional[str]:
        """Reads the raw upload token back off disk. Intended callers:
        `sender.py`'s own `run_step()` path (building a `RelayClient`) -
        never an HTTP handler (ADR-0008: `GET /api/mca/providers` exposes
        only `upload_token_configured: bool` off `ProviderProfile`, never
        this)."""
        row = self._conn.execute(
            "SELECT upload_token_file FROM mca_provider_profiles WHERE workspace_id = ? AND provider_id = ?",
            (self._workspace_id, provider_id),
        ).fetchone()
        if row is None or row["upload_token_file"] is None:
            return None
        paths = workspace_manager.paths(principal_id)
        token_path = paths.keys / row["upload_token_file"]
        if not token_path.exists():
            return None
        return token_path.read_bytes().decode("utf-8")

    def _delete_upload_token_file(self, provider_id: str, workspace_manager: "MCAWorkspaceManager", principal_id: str) -> None:
        row = self._conn.execute(
            "SELECT upload_token_file FROM mca_provider_profiles WHERE workspace_id = ? AND provider_id = ?",
            (self._workspace_id, provider_id),
        ).fetchone()
        if row is None or row["upload_token_file"] is None:
            return
        paths = workspace_manager.paths(principal_id)
        token_path = paths.keys / row["upload_token_file"]
        token_path.unlink(missing_ok=True)

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
