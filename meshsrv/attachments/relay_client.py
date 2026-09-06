"""meshsrv/attachments/relay_client.py

Core-side HTTP client for the real MCAttach Relay HTTP API (ADR-0005;
`MCAttach_Relay_0.1.0/docs/API.md` and source - `mcattach.elektroniker.help`'s
robots.txt disallows all crawlers, so this contract was confirmed by
reading the deployed project owner's own PHP source, not by fetching the
live site). MIT-licensed - does not import `meshtastic` or anything from
adapters/meshtastic/.

Wire conventions (all confirmed against the real Relay, not assumed):
- `transfer_id` is Base64URL (RFC 4648 section 5, no padding) of 16 raw
  bytes over the wire; this module's public API takes/returns it as raw
  `bytes`, matching `codec.py`'s `OfferFields.transfer_id: bytes(16)`
  convention, and encodes/decodes Base64URL only at the HTTP boundary.
- Digests (`ciphertext_sha256`, `manifest_sha256`, per-chunk `sha256`,
  `receipt_hashes`) are lowercase hex in JSON bodies.
- `receipt_secret` in `POST .../complete` is Base64URL of 32 raw bytes.
- Three-tier bearer auth (ADR-0005): a long-lived `upload_access_token`
  (passed to `RelayClient.__init__`, required only by `create_upload`);
  a per-session `upload_token` (returned by `create_upload`, required by
  `get_upload_status`/`upload_chunk`/`upload_manifest`/`commit`); a
  per-session `revoke_token` (returned by `create_upload`, required only
  by `revoke`). Download endpoints (`get_descriptor`, `get_chunk`,
  `complete`) require no token at all - capability-based on `transfer_id`
  alone (design spec section 11.4).

Retries: only GET requests and the two explicitly-idempotent write
operations (`commit`, `complete` - re-committing an already-committed
upload_id and re-completing an already-recorded receipt are both
idempotent no-ops on the real Relay) are retried on 429/5xx.
`create_upload`, `upload_chunk`, `upload_manifest`, and `revoke` are NOT
retried automatically, because a blind retry after a timeout cannot tell
"the first attempt never arrived" from "the first attempt succeeded and
the retry will now see a 409/401 instead of the transient error it
expected" - surfacing that as a `RelayHTTPError` for the caller's state
machine to reconcile is safer than silently retrying an ambiguous write.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import time
from typing import Any, Dict, List, Optional

import requests
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.2
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str, expected_length: Optional[int] = None) -> bytes:
    padding = "=" * (-len(text) % 4)
    decoded = base64.urlsafe_b64decode(text + padding)
    if expected_length is not None and len(decoded) != expected_length:
        raise RelayError(f"Base64URL value decoded to {len(decoded)} bytes, expected {expected_length}")
    return decoded


class RelayError(RuntimeError):
    """Base class for every error RelayClient raises. A caller that
    catches this must not assume any partial success occurred server-side
    - every write method below either fully succeeds or raises."""


class RelayHTTPError(RelayError):
    """The Relay answered with a non-2xx status that isn't one of the
    transient codes we retry (or retries were exhausted without a config
    that raises RelayUnavailableError instead - see `_request`)."""

    def __init__(self, status_code: int, code: str, message: str, request_id: Optional[str] = None, details: Optional[dict] = None):
        super().__init__(f"Relay returned {status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details or {}


class RelayUnavailableError(RelayError):
    """Raised when every retry attempt against a transient (429/5xx) error
    is exhausted. Distinct from RelayHTTPError so callers can tell "the
    Relay rejected this request" from "the Relay could not be reached right
    now, try the whole operation again later"."""


class RelayVerificationError(RelayError):
    """Raised by `verify_descriptor_signature()` when a descriptor's
    `service_signature` does not verify against the caller-supplied
    (pinned) `service_public_key`. Distinct from `RelayHTTPError` because
    this is never about the HTTP transaction failing - the request
    succeeded and returned a well-formed body that simply is not
    authentic, which is a strictly worse outcome a caller must never
    conflate with "try again"."""


@dataclasses.dataclass(frozen=True)
class RelayLimits:
    max_ciphertext_bytes: int
    max_manifest_bytes: int
    max_chunk_bytes: int
    max_chunks: int
    max_recipients: int
    default_hard_ttl_seconds: int
    max_hard_ttl_seconds: int
    default_download_grace_seconds: int


@dataclasses.dataclass(frozen=True)
class RelayInfo:
    protocol: str
    relay_version: str
    base_url: str
    provider_id: str  # Base64URL, per ADR-0005
    service_public_key: bytes  # raw 32 bytes (Ed25519)
    limits: RelayLimits
    anonymous_upload: bool
    download_authorization: str


@dataclasses.dataclass(frozen=True)
class ChunkDeclaration:
    size: int
    sha256: bytes  # raw 32 bytes


@dataclasses.dataclass(frozen=True)
class UploadSession:
    upload_id: str
    transfer_id: bytes  # raw 16 bytes
    upload_token: str
    revoke_token: str
    session_expires_at: str
    chunk_count: int


@dataclasses.dataclass(frozen=True)
class ChunkStatus:
    index: int
    uploaded: bool


@dataclasses.dataclass(frozen=True)
class UploadStatus:
    upload_id: str
    transfer_id: bytes
    state: str
    manifest_uploaded: bool
    chunks: List[ChunkStatus]
    session_expires_at: str
    hard_expires_at: Optional[str]


@dataclasses.dataclass(frozen=True)
class DescriptorChunk:
    index: int
    size: int
    sha256: bytes  # raw 32 bytes


@dataclasses.dataclass(frozen=True)
class ObjectDescriptor:
    provider_id: str
    transfer_id: bytes
    total_size: int
    ciphertext_sha256: bytes
    manifest_size: int
    manifest_sha256: bytes
    chunks: List[DescriptorChunk]
    committed_at: str
    hard_expires_at: str
    delete_after: Optional[str]
    encrypted_manifest: bytes
    service_signature: bytes  # raw 64 bytes (Ed25519 detached signature)
    service_public_key: bytes  # raw 32 bytes


@dataclasses.dataclass(frozen=True)
class CompleteResult:
    receipt_recorded: bool
    all_recipients_completed: bool
    delete_after: Optional[str]


def _parse_retry_after(response: Any) -> Optional[float]:
    value = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RelayClient:
    def __init__(
        self,
        base_url: str,
        upload_access_token: Optional[str] = None,
        session: Optional[Any] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        sleep=time.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        self._upload_access_token = upload_access_token
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        data: Optional[bytes] = None,
        bearer_token: Optional[str] = None,
        retryable: bool = True,
    ):
        headers = {}
        if bearer_token is not None:
            headers["Authorization"] = f"Bearer {bearer_token}"
        attempt = 0
        while True:
            attempt += 1
            response = self._session.request(
                method, self._url(path), json=json_body, data=data, headers=headers, timeout=self._timeout
            )
            if retryable and response.status_code in RETRYABLE_STATUS_CODES:
                if attempt > self._max_retries:
                    raise RelayUnavailableError(
                        f"Relay still returning {response.status_code} for {method} {path} "
                        f"after {attempt} attempt(s)"
                    )
                retry_after = _parse_retry_after(response)
                delay = retry_after if retry_after is not None else self._retry_backoff_seconds * (2 ** (attempt - 1))
                self._sleep(delay)
                continue
            return response

    def _raise_for_error(self, response: Any, expected: int) -> None:
        if response.status_code == expected:
            return
        try:
            body = response.json()
            code = body.get("error", "unknown")
            message = body.get("message", response.text)
            request_id = body.get("request_id")
            details = body.get("details")
        except Exception:  # noqa: BLE001 - any parse failure just falls back to raw text
            code, message, request_id, details = "unknown", getattr(response, "text", ""), None, None
        raise RelayHTTPError(response.status_code, code, message, request_id, details)

    # ---- public information (no auth) ----------------------------------

    def get_health(self) -> dict:
        response = self._request("GET", "/health")
        self._raise_for_error(response, expected=200)
        return response.json()

    def get_info(self) -> RelayInfo:
        response = self._request("GET", "/v1/info")
        self._raise_for_error(response, expected=200)
        body = response.json()
        limits = body["limits"]
        capabilities = body["capabilities"]
        return RelayInfo(
            protocol=body["protocol"],
            relay_version=body["relay_version"],
            base_url=body["base_url"],
            provider_id=body["provider_id"],
            service_public_key=_b64url_decode(body["service_key"]["public_key"], 32),
            limits=RelayLimits(
                max_ciphertext_bytes=limits["max_ciphertext_bytes"],
                max_manifest_bytes=limits["max_manifest_bytes"],
                max_chunk_bytes=limits["max_chunk_bytes"],
                max_chunks=limits["max_chunks"],
                max_recipients=limits["max_recipients"],
                default_hard_ttl_seconds=limits["default_hard_ttl_seconds"],
                max_hard_ttl_seconds=limits["max_hard_ttl_seconds"],
                default_download_grace_seconds=limits["default_download_grace_seconds"],
            ),
            anonymous_upload=capabilities["anonymous_upload"],
            download_authorization=capabilities["download_authorization"],
        )

    # ---- upload session lifecycle (upload_access_token / upload_token) --

    def create_upload(
        self,
        *,
        transfer_id: bytes,
        total_size: int,
        ciphertext_sha256: bytes,
        manifest_size: int,
        manifest_sha256: bytes,
        chunks: List[ChunkDeclaration],
        receipt_hashes: List[bytes],
        hard_ttl_seconds: Optional[int] = None,
        download_grace_seconds: Optional[int] = None,
    ) -> UploadSession:
        if self._upload_access_token is None:
            raise RelayError("create_upload requires this client to be constructed with upload_access_token")
        body: Dict[str, Any] = {
            "transfer_id": _b64url_encode(transfer_id),
            "total_size": total_size,
            "ciphertext_sha256": ciphertext_sha256.hex(),
            "manifest_size": manifest_size,
            "manifest_sha256": manifest_sha256.hex(),
            "chunks": [{"size": c.size, "sha256": c.sha256.hex()} for c in chunks],
            "receipt_hashes": [h.hex() for h in receipt_hashes],
        }
        if hard_ttl_seconds is not None:
            body["hard_ttl_seconds"] = hard_ttl_seconds
        if download_grace_seconds is not None:
            body["download_grace_seconds"] = download_grace_seconds
        response = self._request(
            "POST", "/v1/uploads", json_body=body, bearer_token=self._upload_access_token, retryable=False
        )
        self._raise_for_error(response, expected=201)
        result = response.json()
        return UploadSession(
            upload_id=result["upload_id"],
            transfer_id=_b64url_decode(result["transfer_id"], 16),
            upload_token=result["upload_token"],
            revoke_token=result["revoke_token"],
            session_expires_at=result["session_expires_at"],
            chunk_count=result["chunk_count"],
        )

    def get_upload_status(self, upload_id: str, upload_token: str) -> UploadStatus:
        response = self._request("GET", f"/v1/uploads/{upload_id}", bearer_token=upload_token)
        self._raise_for_error(response, expected=200)
        body = response.json()
        return UploadStatus(
            upload_id=body["upload_id"],
            transfer_id=_b64url_decode(body["transfer_id"], 16),
            state=body["state"],
            manifest_uploaded=body["manifest_uploaded"],
            chunks=[ChunkStatus(index=c["index"], uploaded=c["uploaded"]) for c in body["chunks"]],
            session_expires_at=body["session_expires_at"],
            hard_expires_at=body.get("hard_expires_at"),
        )

    def upload_chunk(self, upload_id: str, upload_token: str, index: int, data: bytes) -> None:
        response = self._request(
            "PUT", f"/v1/uploads/{upload_id}/chunks/{index}", data=data, bearer_token=upload_token, retryable=False
        )
        self._raise_for_error(response, expected=200)

    def upload_manifest(self, upload_id: str, upload_token: str, manifest: bytes) -> None:
        response = self._request(
            "PUT", f"/v1/uploads/{upload_id}/manifest", data=manifest, bearer_token=upload_token, retryable=False
        )
        self._raise_for_error(response, expected=200)

    def commit(self, upload_id: str, upload_token: str) -> ObjectDescriptor:
        response = self._request("POST", f"/v1/uploads/{upload_id}/commit", bearer_token=upload_token)
        self._raise_for_error(response, expected=200)
        return self._parse_descriptor(response.json())

    # ---- download (no token - capability-based on transfer_id) ---------

    def get_descriptor(self, transfer_id: bytes) -> ObjectDescriptor:
        response = self._request("GET", f"/v1/objects/{_b64url_encode(transfer_id)}/descriptor")
        self._raise_for_error(response, expected=200)
        return self._parse_descriptor(response.json())

    def get_chunk(self, transfer_id: bytes, index: int) -> bytes:
        response = self._request("GET", f"/v1/objects/{_b64url_encode(transfer_id)}/chunks/{index}")
        self._raise_for_error(response, expected=200)
        return response.content

    def complete(self, transfer_id: bytes, receipt_secret: bytes) -> CompleteResult:
        body = {"receipt_secret": _b64url_encode(receipt_secret)}
        response = self._request("POST", f"/v1/objects/{_b64url_encode(transfer_id)}/complete", json_body=body)
        self._raise_for_error(response, expected=200)
        result = response.json()
        return CompleteResult(
            receipt_recorded=result["receipt_recorded"],
            all_recipients_completed=result["all_recipients_completed"],
            delete_after=result.get("delete_after"),
        )

    def revoke(self, transfer_id: bytes, revoke_token: str) -> None:
        response = self._request(
            "DELETE", f"/v1/objects/{_b64url_encode(transfer_id)}", bearer_token=revoke_token, retryable=False
        )
        self._raise_for_error(response, expected=200)

    # ---- shared parsing --------------------------------------------------

    def _parse_descriptor(self, body: dict) -> ObjectDescriptor:
        descriptor = body["descriptor"]
        chunks = [
            DescriptorChunk(index=c["index"], size=c["size"], sha256=bytes.fromhex(c["sha256"]))
            for c in descriptor["chunks"]
        ]
        return ObjectDescriptor(
            provider_id=descriptor["provider_id"],
            transfer_id=_b64url_decode(descriptor["transfer_id"], 16),
            total_size=descriptor["total_size"],
            ciphertext_sha256=bytes.fromhex(descriptor["ciphertext_sha256"]),
            manifest_size=descriptor["manifest_size"],
            manifest_sha256=bytes.fromhex(descriptor["manifest_sha256"]),
            chunks=chunks,
            committed_at=descriptor["committed_at"],
            hard_expires_at=descriptor["hard_expires_at"],
            delete_after=descriptor.get("delete_after"),
            encrypted_manifest=_b64url_decode(body["encrypted_manifest"]["data"]),
            service_signature=_b64url_decode(body["service_signature"]["signature"], 64),
            service_public_key=_b64url_decode(body["service_signature"]["public_key"], 32),
        )


_DESCRIPTOR_SIGNATURE_DOMAIN = "MCA-RELAY-DESCRIPTOR-V1"


def _canonical_json(value: Any) -> str:
    """Byte-for-byte the same canonicalization
    `meshsrv/attachments/relay/mock_server.py::_canonical_json()` uses
    (ADR-0007): recursively sort dict keys as strings, compact separators,
    unescaped Unicode/slashes. Duplicated here (not imported from the mock
    server, which is test-only scaffolding) because this is the one place
    production code needs to reproduce the real Relay's own
    `mca_canonical_json` exactly - a signature verification helper must
    never depend on a module that only exists to emulate the Relay for
    tests."""

    def normalize(item):
        if isinstance(item, dict):
            return {key: normalize(item[key]) for key in sorted(item.keys())}
        if isinstance(item, list):
            return [normalize(entry) for entry in item]
        return item

    return json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"))


def verify_descriptor_signature(descriptor: ObjectDescriptor, service_public_key: bytes) -> None:
    """Verify `descriptor.service_signature` against `service_public_key`.

    ADR-0007: `service_public_key` MUST be the pinned key from the local
    Provider Registry (the key confirmed during one of the three MVP trust
    bootstrap paths - see `provider_registry.py`), never
    `descriptor.service_public_key` - that field is attacker-controlled
    data from the very same untrusted response being verified, and using
    it here would make this function verify nothing. Raises
    `RelayVerificationError` on any mismatch; returns `None` on success.
    """

    if len(service_public_key) != 32:
        raise RelayVerificationError(f"service_public_key must be 32 raw bytes, got {len(service_public_key)}")
    signed = {
        "domain": _DESCRIPTOR_SIGNATURE_DOMAIN,
        "protocol": "MCA/1",
        "provider_id": descriptor.provider_id,
        "transfer_id": _b64url_encode(descriptor.transfer_id),
        "total_size": descriptor.total_size,
        "ciphertext_sha256": descriptor.ciphertext_sha256.hex(),
        "manifest_size": descriptor.manifest_size,
        "manifest_sha256": descriptor.manifest_sha256.hex(),
        "chunks": [{"index": c.index, "size": c.size, "sha256": c.sha256.hex()} for c in descriptor.chunks],
        "committed_at": descriptor.committed_at,
        "hard_expires_at": descriptor.hard_expires_at,
        "delete_after": descriptor.delete_after,
    }
    message = _canonical_json(signed).encode("utf-8")
    try:
        VerifyKey(service_public_key).verify(message, descriptor.service_signature)
    except BadSignatureError as exc:
        raise RelayVerificationError("descriptor service_signature failed verification") from exc
