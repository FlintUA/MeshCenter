"""meshsrv/attachments/relay/mock_server.py

In-memory mock of the REAL MCAttach Relay HTTP API (ADR-0005;
`MCAttach_Relay_0.1.0` PHP source - `relay.php`, `common.php`,
`storage.php`, `setup.php`). Rewritten in Step 1.1 to replace the Step 0.5
mock, which was built from the design spec's prose alone and diverged from
the deployed Relay in several concrete ways (flat `receipt_hashes` instead
of per-recipient envelope records at session-creation time, Base64URL
`transfer_id`/tokens instead of hex, a three-tier bearer-token model, a
resumable-upload status endpoint, a server-signed descriptor). This is
MIT-licensed Core *test* infrastructure - used by tests/test_relay_mock.py
so RelayClient can be exercised against the real contract without a live
network dependency; it never imports `meshtastic`.

Not modeled (acceptable narrowing for a test double, not a claim of full
parity): per-action rate limiting (RelayClient's generic 429/Retry-After
retry logic is tested separately, in isolation, in
`test_relay_client_retries.py`-style tests using a scripted fake session);
MySQL-specific behavior; the HTML setup/status pages; `Host` header
validation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from flask import Flask, Response, jsonify, request
from nacl.signing import SigningKey

DEFAULT_MAX_CIPHERTEXT_BYTES = 6 * 1024 * 1024  # ADR-0005: 6291456, deliberate margin over 5 MiB plaintext
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024
DEFAULT_MAX_CHUNK_BYTES = 300_000
DEFAULT_MAX_CHUNKS = 64
DEFAULT_MAX_RECEIPTS = 32
DEFAULT_MIN_HARD_TTL_SECONDS = 3600
DEFAULT_DEFAULT_HARD_TTL_SECONDS = 259200
DEFAULT_MAX_HARD_TTL_SECONDS = 259200
DEFAULT_DEFAULT_GRACE_SECONDS = 3600
DEFAULT_MAX_GRACE_SECONDS = 86400
DEFAULT_UPLOAD_SESSION_SECONDS = 21600
DEFAULT_TOMBSTONE_SECONDS = 604800
DEFAULT_MAX_STORAGE_BYTES = 10 * 1024 * 1024 * 1024

DESCRIPTOR_SIGNATURE_DOMAIN = "MCA-RELAY-DESCRIPTOR-V1"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode_checked(text: str, expected_length: int, field_name: str) -> bytes:
    if not text or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in text):
        raise RelayValidationError(f"invalid_{field_name}", f"{field_name} must be Base64URL.", 400)
    padding = "=" * (-len(text) % 4)
    try:
        decoded = base64.urlsafe_b64decode(text + padding)
    except Exception as exc:  # noqa: BLE001
        raise RelayValidationError(f"invalid_{field_name}", f"{field_name} is not valid Base64URL.", 400) from exc
    if len(decoded) != expected_length:
        raise RelayValidationError(f"invalid_{field_name}", f"{field_name} has the wrong decoded length.", 400)
    return decoded


def _hex_hash(value: str, field_name: str) -> bytes:
    if not isinstance(value, str) or len(value) != 64:
        raise RelayValidationError(f"invalid_{field_name}", f"{field_name} must be a lowercase SHA-256 hex digest.", 422)
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise RelayValidationError(f"invalid_{field_name}", f"{field_name} must be a lowercase SHA-256 hex digest.", 422) from exc
    if value != value.lower():
        raise RelayValidationError(f"invalid_{field_name}", f"{field_name} must be lowercase.", 422)
    return raw


def _canonical_json(value) -> str:
    """Mirrors the real Relay's `mca_canonical_json`: recursively sort
    object keys as strings, compact separators, unescaped Unicode/slashes."""

    def normalize(item):
        if isinstance(item, dict):
            return {key: normalize(item[key]) for key in sorted(item.keys())}
        if isinstance(item, list):
            return [normalize(entry) for entry in item]
        return item

    return json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"))


class RelayValidationError(Exception):
    """Raised by MockRelayStore for every rejected operation, carrying the
    real Relay's own error code and HTTP status (ADR-0005 / relay.php)."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class _Chunk:
    expected_size: int
    expected_sha256: bytes
    uploaded: bool = False
    data: bytes = b""


@dataclass
class _Transfer:
    upload_id: str
    transfer_id: bytes
    upload_token_hash: bytes
    revoke_token_hash: bytes
    total_size: int
    ciphertext_sha256: bytes
    manifest_size: int
    manifest_sha256: bytes
    hard_ttl_seconds: int
    grace_seconds: int
    session_expires_at: float
    created_at: float
    chunks: Dict[int, _Chunk]
    receipts: Dict[bytes, Optional[float]]  # receipt_hash -> completed_at
    manifest_uploaded: bool = False
    manifest_data: bytes = b""
    state: str = "staging"  # staging -> committed -> (revoked | expired)
    hard_expires_at: Optional[float] = None
    delete_after: Optional[float] = None
    committed_at: Optional[float] = None


class MockRelayStore:
    """The real Relay's object model and state machine (ADR-0005), held in
    memory. One instance backs one Flask app / one test."""

    def __init__(
        self,
        base_url: str = "https://mock-relay.test",
        max_ciphertext_bytes: int = DEFAULT_MAX_CIPHERTEXT_BYTES,
        max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
        max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        max_receipts: int = DEFAULT_MAX_RECEIPTS,
        min_hard_ttl_seconds: int = DEFAULT_MIN_HARD_TTL_SECONDS,
        default_hard_ttl_seconds: int = DEFAULT_DEFAULT_HARD_TTL_SECONDS,
        max_hard_ttl_seconds: int = DEFAULT_MAX_HARD_TTL_SECONDS,
        default_grace_seconds: int = DEFAULT_DEFAULT_GRACE_SECONDS,
        max_grace_seconds: int = DEFAULT_MAX_GRACE_SECONDS,
        upload_session_seconds: int = DEFAULT_UPLOAD_SESSION_SECONDS,
        tombstone_seconds: int = DEFAULT_TOMBSTONE_SECONDS,
        max_storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES,
        upload_access_token: Optional[str] = None,
        clock=time.time,
    ):
        self.base_url = base_url
        self._limits = dict(
            max_ciphertext_bytes=max_ciphertext_bytes,
            max_manifest_bytes=max_manifest_bytes,
            max_chunk_bytes=max_chunk_bytes,
            max_chunks=max_chunks,
            max_receipts=max_receipts,
            min_hard_ttl_seconds=min_hard_ttl_seconds,
            default_hard_ttl_seconds=default_hard_ttl_seconds,
            max_hard_ttl_seconds=max_hard_ttl_seconds,
            default_grace_seconds=default_grace_seconds,
            max_grace_seconds=max_grace_seconds,
            upload_session_seconds=upload_session_seconds,
            tombstone_seconds=tombstone_seconds,
            max_storage_bytes=max_storage_bytes,
        )
        self._clock = clock
        self._signing_key = SigningKey.generate()
        self.service_public_key = bytes(self._signing_key.verify_key)
        self.upload_access_token = upload_access_token or ("mca_up_" + _b64url_encode(hashlib.sha256(b"mock-upload-token").digest() + b"\x00" * 8))
        self._upload_access_token_hash = hashlib.sha256(self.upload_access_token.encode("ascii")).hexdigest()
        self._transfers_by_upload_id: Dict[str, _Transfer] = {}
        self._transfer_id_to_upload_id: Dict[bytes, str] = {}

    @property
    def provider_id(self) -> str:
        origin = self.base_url.rstrip("/").lower()
        digest = hashlib.sha256(origin.encode("utf-8") + b"\n" + self.service_public_key).digest()
        return _b64url_encode(digest[:8])

    def total_committed_bytes(self) -> int:
        return sum(
            t.total_size + t.manifest_size
            for t in self._transfers_by_upload_id.values()
            if t.state in ("staging", "committed")
        )

    # ---- auth helpers ----------------------------------------------------

    def check_upload_access_token(self, token: Optional[str]) -> None:
        if token is None or hashlib.sha256(token.encode("ascii")).hexdigest() != self._upload_access_token_hash:
            raise RelayValidationError("invalid_token", "The supplied token is not valid.", 401)

    def _check_session_token(self, transfer: _Transfer, token: Optional[str], hash_attr: str) -> None:
        expected = getattr(transfer, hash_attr)
        if token is None or hashlib.sha256(token.encode("ascii")).digest() != expected:
            raise RelayValidationError("invalid_token", "The supplied token is not valid.", 401)

    # ---- session lifecycle ------------------------------------------------

    def create_upload(
        self,
        *,
        transfer_id: bytes,
        total_size: int,
        ciphertext_sha256: bytes,
        manifest_size: int,
        manifest_sha256: bytes,
        chunks: List[dict],
        receipt_hashes: List[bytes],
        hard_ttl_seconds: Optional[int],
        download_grace_seconds: Optional[int],
    ) -> _Transfer:
        if transfer_id in self._transfer_id_to_upload_id:
            raise RelayValidationError("transfer_exists", "This transfer identifier already exists.", 409)
        if total_size < 1 or total_size > self._limits["max_ciphertext_bytes"]:
            raise RelayValidationError("invalid_total_size", "total_size is outside the allowed range.", 422)
        if manifest_size < 1 or manifest_size > self._limits["max_manifest_bytes"]:
            raise RelayValidationError("invalid_manifest_size", "manifest_size is outside the allowed range.", 422)
        if not chunks or len(chunks) > self._limits["max_chunks"]:
            raise RelayValidationError("invalid_chunks", "chunks must be a non-empty bounded array.", 422)

        validated_chunks: Dict[int, _Chunk] = {}
        running_total = 0
        for index, chunk in enumerate(chunks):
            size = chunk["size"]
            if size < 1 or size > self._limits["max_chunk_bytes"]:
                raise RelayValidationError("invalid_size", "size is outside the allowed range.", 422)
            running_total += size
            if running_total > total_size:
                raise RelayValidationError("invalid_chunks", "Declared chunk sizes exceed total_size.", 422)
            validated_chunks[index] = _Chunk(expected_size=size, expected_sha256=chunk["sha256"])
        if running_total != total_size:
            raise RelayValidationError("invalid_chunks", "Declared chunk sizes must equal total_size.", 422)

        if not receipt_hashes or len(receipt_hashes) > self._limits["max_receipts"]:
            raise RelayValidationError(
                "invalid_receipts", "receipt_hashes must contain between 1 and the allowed maximum entries.", 422
            )
        if len(set(receipt_hashes)) != len(receipt_hashes):
            raise RelayValidationError("invalid_receipts", "Duplicate receipt hashes are not allowed.", 422)

        hard_ttl = self._clamp(
            hard_ttl_seconds, self._limits["default_hard_ttl_seconds"],
            self._limits["min_hard_ttl_seconds"], self._limits["max_hard_ttl_seconds"], "hard_ttl_seconds",
        )
        grace = self._clamp(
            download_grace_seconds, self._limits["default_grace_seconds"],
            60, self._limits["max_grace_seconds"], "download_grace_seconds",
        )

        now = self._clock()
        if self.total_committed_bytes() + total_size + manifest_size > self._limits["max_storage_bytes"]:
            raise RelayValidationError("relay_quota_exceeded", "Relay storage quota would be exceeded.", 507)

        upload_id = _b64url_encode(hashlib.sha256(transfer_id + str(now).encode()).digest() + transfer_id[:1])
        upload_token = "mca_us_" + _b64url_encode(hashlib.sha256(upload_id.encode() + b"tok").digest() + b"\x00" * 8)
        revoke_token = "mca_rv_" + _b64url_encode(hashlib.sha256(upload_id.encode() + b"rev").digest() + b"\x00" * 8)

        transfer = _Transfer(
            upload_id=upload_id,
            transfer_id=transfer_id,
            upload_token_hash=hashlib.sha256(upload_token.encode("ascii")).digest(),
            revoke_token_hash=hashlib.sha256(revoke_token.encode("ascii")).digest(),
            total_size=total_size,
            ciphertext_sha256=ciphertext_sha256,
            manifest_size=manifest_size,
            manifest_sha256=manifest_sha256,
            hard_ttl_seconds=hard_ttl,
            grace_seconds=grace,
            session_expires_at=now + self._limits["upload_session_seconds"],
            created_at=now,
            chunks=validated_chunks,
            receipts={h: None for h in receipt_hashes},
        )
        self._transfers_by_upload_id[upload_id] = transfer
        self._transfer_id_to_upload_id[transfer_id] = upload_id
        # Tokens are returned once, here, and never stored in plaintext.
        transfer._plaintext_upload_token = upload_token  # type: ignore[attr-defined]
        transfer._plaintext_revoke_token = revoke_token  # type: ignore[attr-defined]
        return transfer

    @staticmethod
    def _clamp(value, default, minimum, maximum, field_name):
        if value is None:
            return default
        if value < minimum or value > maximum:
            raise RelayValidationError(f"invalid_{field_name}", f"{field_name} is outside the allowed range.", 422)
        return value

    def _get_upload(self, upload_id: str) -> _Transfer:
        transfer = self._transfers_by_upload_id.get(upload_id)
        if transfer is None:
            raise RelayValidationError("upload_not_found", "Upload session was not found.", 404)
        return transfer

    def _require_staging(self, transfer: _Transfer) -> None:
        if transfer.state != "staging":
            raise RelayValidationError("upload_not_staging", "Upload session is no longer writable.", 409)
        if transfer.session_expires_at <= self._clock():
            raise RelayValidationError("upload_expired", "Upload session has expired.", 410)

    def get_upload_status(self, upload_id: str, upload_token: str) -> _Transfer:
        transfer = self._get_upload(upload_id)
        self._check_session_token(transfer, upload_token, "upload_token_hash")
        return transfer

    def upload_chunk(self, upload_id: str, upload_token: str, index: int, data: bytes) -> _Chunk:
        transfer = self._get_upload(upload_id)
        self._check_session_token(transfer, upload_token, "upload_token_hash")
        self._require_staging(transfer)
        chunk = transfer.chunks.get(index)
        if chunk is None:
            raise RelayValidationError("chunk_not_declared", "This chunk index was not declared.", 404)
        if len(data) != chunk.expected_size:
            raise RelayValidationError("size_mismatch", "Uploaded object size does not match the declaration.", 422)
        if hashlib.sha256(data).digest() != chunk.expected_sha256:
            raise RelayValidationError("digest_mismatch", "Uploaded object digest does not match the declaration.", 422)
        chunk.uploaded = True
        chunk.data = bytes(data)
        return chunk

    def upload_manifest(self, upload_id: str, upload_token: str, data: bytes) -> None:
        transfer = self._get_upload(upload_id)
        self._check_session_token(transfer, upload_token, "upload_token_hash")
        self._require_staging(transfer)
        if len(data) != transfer.manifest_size:
            raise RelayValidationError("size_mismatch", "Uploaded object size does not match the declaration.", 422)
        if hashlib.sha256(data).digest() != transfer.manifest_sha256:
            raise RelayValidationError("digest_mismatch", "Uploaded object digest does not match the declaration.", 422)
        transfer.manifest_uploaded = True
        transfer.manifest_data = bytes(data)

    def commit(self, upload_id: str, upload_token: str) -> _Transfer:
        transfer = self._get_upload(upload_id)
        self._check_session_token(transfer, upload_token, "upload_token_hash")
        if transfer.state == "committed":
            return transfer  # idempotent, per the real Relay
        self._require_staging(transfer)
        if not transfer.manifest_uploaded:
            raise RelayValidationError("manifest_missing", "Encrypted manifest has not been uploaded.", 409)
        for chunk in transfer.chunks.values():
            if not chunk.uploaded:
                raise RelayValidationError("chunk_missing", "One or more chunks have not been uploaded.", 409)
        ordered = b"".join(transfer.chunks[i].data for i in sorted(transfer.chunks))
        if len(ordered) != transfer.total_size or hashlib.sha256(ordered).digest() != transfer.ciphertext_sha256:
            raise RelayValidationError("ciphertext_invalid", "Stored ciphertext failed final verification.", 409)
        now = self._clock()
        transfer.state = "committed"
        transfer.committed_at = now
        transfer.hard_expires_at = now + transfer.hard_ttl_seconds
        return transfer

    # ---- post-commit access -------------------------------------------

    def _fetch_object(self, transfer_id: bytes) -> _Transfer:
        upload_id = self._transfer_id_to_upload_id.get(transfer_id)
        transfer = self._transfers_by_upload_id.get(upload_id) if upload_id else None
        if transfer is None or transfer.state == "staging":
            raise RelayValidationError("object_not_found", "Object was not found.", 404)
        if transfer.state in ("expired", "revoked"):
            raise RelayValidationError("object_unavailable", "Object is no longer available.", 410)
        now = self._clock()
        hard_due = transfer.hard_expires_at is not None and transfer.hard_expires_at <= now
        grace_due = transfer.delete_after is not None and transfer.delete_after <= now
        if hard_due or grace_due:
            transfer.state = "expired"
            raise RelayValidationError("object_expired", "Object has expired.", 410)
        return transfer

    def descriptor_payload(self, transfer: _Transfer) -> dict:
        chunks = [
            {"index": index, "size": transfer.chunks[index].expected_size, "sha256": transfer.chunks[index].expected_sha256.hex()}
            for index in sorted(transfer.chunks)
        ]
        signed = {
            "domain": DESCRIPTOR_SIGNATURE_DOMAIN,
            "protocol": "MCA/1",
            "provider_id": self.provider_id,
            "transfer_id": _b64url_encode(transfer.transfer_id),
            "total_size": transfer.total_size,
            "ciphertext_sha256": transfer.ciphertext_sha256.hex(),
            "manifest_size": transfer.manifest_size,
            "manifest_sha256": transfer.manifest_sha256.hex(),
            "chunks": chunks,
            "committed_at": transfer.committed_at,
            "hard_expires_at": transfer.hard_expires_at,
            "delete_after": transfer.delete_after,
        }
        signature = self._signing_key.sign(_canonical_json(signed).encode("utf-8")).signature
        return {
            "ok": True,
            "descriptor": signed,
            "encrypted_manifest": {"encoding": "base64url", "data": _b64url_encode(transfer.manifest_data)},
            "service_signature": {
                "algorithm": "Ed25519",
                "public_key": _b64url_encode(self.service_public_key),
                "signature": _b64url_encode(signature),
            },
        }

    def get_descriptor(self, transfer_id: bytes) -> dict:
        transfer = self._fetch_object(transfer_id)
        return self.descriptor_payload(transfer)

    def get_chunk(self, transfer_id: bytes, index: int) -> bytes:
        transfer = self._fetch_object(transfer_id)
        chunk = transfer.chunks.get(index)
        if chunk is None or not chunk.uploaded:
            raise RelayValidationError("chunk_not_found", "Chunk was not found.", 404)
        return chunk.data

    def complete(self, transfer_id: bytes, receipt_secret: bytes) -> dict:
        transfer = self._fetch_object(transfer_id)
        receipt_hash = hashlib.sha256(receipt_secret).digest()
        if receipt_hash not in transfer.receipts:
            raise RelayValidationError("invalid_receipt", "Receipt proof is not valid for this object.", 403)
        if transfer.receipts[receipt_hash] is None:
            transfer.receipts[receipt_hash] = self._clock()
        all_completed = all(v is not None for v in transfer.receipts.values())
        if all_completed:
            candidate = min(self._clock() + transfer.grace_seconds, transfer.hard_expires_at)
            if transfer.delete_after is None or transfer.delete_after > candidate:
                transfer.delete_after = candidate
        return {
            "ok": True,
            "receipt_recorded": True,
            "all_recipients_completed": all_completed,
            "delete_after": transfer.delete_after,
        }

    def revoke(self, transfer_id: bytes, revoke_token: str) -> None:
        upload_id = self._transfer_id_to_upload_id.get(transfer_id)
        transfer = self._transfers_by_upload_id.get(upload_id) if upload_id else None
        if transfer is None:
            raise RelayValidationError("object_not_found", "Object was not found.", 404)
        self._check_session_token(transfer, revoke_token, "revoke_token_hash")
        if transfer.state != "revoked":
            transfer.state = "revoked"

    def info_payload(self) -> dict:
        return {
            "ok": True,
            "protocol": "MCA/1",
            "relay_version": "mock-0.1.0",
            "base_url": self.base_url,
            "provider_id": self.provider_id,
            "service_key": {"type": "Ed25519", "public_key": _b64url_encode(self.service_public_key)},
            "limits": {
                "max_ciphertext_bytes": self._limits["max_ciphertext_bytes"],
                "max_manifest_bytes": self._limits["max_manifest_bytes"],
                "max_chunk_bytes": self._limits["max_chunk_bytes"],
                "max_chunks": self._limits["max_chunks"],
                "max_recipients": self._limits["max_receipts"],
                "default_hard_ttl_seconds": self._limits["default_hard_ttl_seconds"],
                "max_hard_ttl_seconds": self._limits["max_hard_ttl_seconds"],
                "default_download_grace_seconds": self._limits["default_grace_seconds"],
            },
            "capabilities": {
                "chunked_upload": True,
                "receipt_completion": True,
                "sender_revoke": True,
                "anonymous_upload": False,
                "download_authorization": "transfer_capability",
            },
        }


def _bearer_token() -> Optional[str]:
    header = request.headers.get("Authorization") or request.headers.get("X-MCA-Token") or ""
    header = header.strip()
    if not header.lower().startswith("bearer "):
        return None
    return header[len("bearer "):].strip() or None


def create_mock_relay_app(store: Optional[MockRelayStore] = None) -> Flask:
    if store is None:
        store = MockRelayStore()
    app = Flask(__name__)
    app.config["MCA_RELAY_STORE"] = store

    @app.errorhandler(RelayValidationError)
    def _handle_validation_error(exc: RelayValidationError):
        return jsonify({"ok": False, "error": exc.code, "message": exc.message, "details": {}}), exc.status

    def _iso(value: Optional[float]) -> Optional[str]:
        if value is None:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(value)) + "Z"

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "status": "ready", "version": "mock-0.1.0", "provider_id": store.provider_id}), 200

    @app.get("/v1/info")
    def info():
        return jsonify(store.info_payload()), 200

    @app.post("/v1/uploads")
    def create_upload():
        store.check_upload_access_token(_bearer_token())
        body = request.get_json(force=True, silent=False) or {}
        try:
            transfer_id = _b64url_decode_checked(body.get("transfer_id", ""), 16, "transfer_id")
            chunks_raw = body["chunks"]
            chunks = [{"size": c["size"], "sha256": _hex_hash(c["sha256"], "chunk_sha256")} for c in chunks_raw]
            receipt_hashes = [_hex_hash(h, "receipt_hash") for h in body["receipt_hashes"]]
            transfer = store.create_upload(
                transfer_id=transfer_id,
                total_size=body["total_size"],
                ciphertext_sha256=_hex_hash(body["ciphertext_sha256"], "ciphertext_sha256"),
                manifest_size=body["manifest_size"],
                manifest_sha256=_hex_hash(body["manifest_sha256"], "manifest_sha256"),
                chunks=chunks,
                receipt_hashes=receipt_hashes,
                hard_ttl_seconds=body.get("hard_ttl_seconds"),
                download_grace_seconds=body.get("download_grace_seconds"),
            )
        except KeyError as exc:
            raise RelayValidationError(f"invalid_{exc.args[0]}", f"{exc.args[0]} is required.", 422) from exc
        return (
            jsonify(
                {
                    "ok": True,
                    "upload_id": transfer.upload_id,
                    "transfer_id": _b64url_encode(transfer.transfer_id),
                    "upload_token": transfer._plaintext_upload_token,  # type: ignore[attr-defined]
                    "revoke_token": transfer._plaintext_revoke_token,  # type: ignore[attr-defined]
                    "session_expires_at": _iso(transfer.session_expires_at),
                    "chunk_count": len(transfer.chunks),
                }
            ),
            201,
        )

    @app.get("/v1/uploads/<upload_id>")
    def upload_status(upload_id):
        transfer = store.get_upload_status(upload_id, _bearer_token())
        chunks = [
            {"index": index, "uploaded": transfer.chunks[index].uploaded} for index in sorted(transfer.chunks)
        ]
        return (
            jsonify(
                {
                    "ok": True,
                    "upload_id": transfer.upload_id,
                    "transfer_id": _b64url_encode(transfer.transfer_id),
                    "state": transfer.state,
                    "manifest_uploaded": transfer.manifest_uploaded,
                    "chunks": chunks,
                    "session_expires_at": _iso(transfer.session_expires_at),
                    "hard_expires_at": _iso(transfer.hard_expires_at),
                }
            ),
            200,
        )

    @app.put("/v1/uploads/<upload_id>/chunks/<int:index>")
    def upload_chunk(upload_id, index):
        chunk = store.upload_chunk(upload_id, _bearer_token(), index, request.get_data())
        return jsonify({"ok": True, "chunk_index": index, "size": chunk.expected_size, "sha256": chunk.expected_sha256.hex()}), 200

    @app.put("/v1/uploads/<upload_id>/manifest")
    def upload_manifest(upload_id):
        data = request.get_data()
        store.upload_manifest(upload_id, _bearer_token(), data)
        return jsonify({"ok": True, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}), 200

    @app.post("/v1/uploads/<upload_id>/commit")
    def commit(upload_id):
        transfer = store.commit(upload_id, _bearer_token())
        return jsonify(store.descriptor_payload(transfer)), 200

    @app.get("/v1/objects/<transfer_id_b64>/descriptor")
    def descriptor(transfer_id_b64):
        transfer_id = _b64url_decode_checked(transfer_id_b64, 16, "transfer_id")
        return jsonify(store.get_descriptor(transfer_id)), 200

    @app.get("/v1/objects/<transfer_id_b64>/chunks/<int:index>")
    def download_chunk(transfer_id_b64, index):
        transfer_id = _b64url_decode_checked(transfer_id_b64, 16, "transfer_id")
        data = store.get_chunk(transfer_id, index)
        return Response(data, status=200, mimetype="application/octet-stream")

    @app.post("/v1/objects/<transfer_id_b64>/complete")
    def complete(transfer_id_b64):
        transfer_id = _b64url_decode_checked(transfer_id_b64, 16, "transfer_id")
        body = request.get_json(force=True, silent=False) or {}
        receipt_secret = _b64url_decode_checked(body.get("receipt_secret", ""), 32, "receipt_secret")
        result = store.complete(transfer_id, receipt_secret)
        payload = dict(result)
        payload["delete_after"] = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(result["delete_after"])) + "Z"
            if result["delete_after"] is not None
            else None
        )
        return jsonify(payload), 200

    @app.delete("/v1/objects/<transfer_id_b64>")
    def revoke(transfer_id_b64):
        transfer_id = _b64url_decode_checked(transfer_id_b64, 16, "transfer_id")
        store.revoke(transfer_id, _bearer_token())
        return jsonify({"ok": True, "revoked": True}), 200

    return app
