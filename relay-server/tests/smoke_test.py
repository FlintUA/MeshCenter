#!/usr/bin/env python3
"""Live MCAttach Relay API smoke test using only the Python standard library."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.environ.get("MCA_RELAY_URL", "").rstrip("/")
MASTER_TOKEN = os.environ.get("MCA_UPLOAD_TOKEN", "")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    expected: int = 200,
):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method=method)
    response_headers = None
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read()
            status = response.status
            response_headers = response.headers
    except urllib.error.HTTPError as error:
        payload = error.read()
        status = error.code
        response_headers = error.headers

    if status != expected:
        raise RuntimeError(f"{method} {path}: expected HTTP {expected}, received {status}: {payload[:500]!r}")

    if "application/json" in response_headers.get("Content-Type", ""):
        return json.loads(payload.decode("utf-8"))
    return payload


def main() -> int:
    if not BASE_URL.startswith("https://") or not MASTER_TOKEN:
        print("Set MCA_RELAY_URL and MCA_UPLOAD_TOKEN environment variables.", file=sys.stderr)
        return 2

    print("1/9 public info")
    info = request("GET", "/v1/info")
    assert info["ok"] is True and info["protocol"] == "MCA/1"

    ciphertext = os.urandom(440_000)
    chunks = [ciphertext[:300_000], ciphertext[300_000:]]
    encrypted_manifest = os.urandom(192)
    receipt_secret = os.urandom(32)
    transfer_id = b64url(os.urandom(16))

    create_body = json.dumps(
        {
            "transfer_id": transfer_id,
            "total_size": len(ciphertext),
            "ciphertext_sha256": digest(ciphertext),
            "manifest_size": len(encrypted_manifest),
            "manifest_sha256": digest(encrypted_manifest),
            "chunks": [{"size": len(chunk), "sha256": digest(chunk)} for chunk in chunks],
            "receipt_hashes": [digest(receipt_secret)],
            "hard_ttl_seconds": 3600,
            "download_grace_seconds": 60,
        }
    ).encode("utf-8")

    print("2/9 create upload session")
    created = request(
        "POST",
        "/v1/uploads",
        token=MASTER_TOKEN,
        body=create_body,
        content_type="application/json",
        expected=201,
    )
    upload_id = created["upload_id"]
    upload_token = created["upload_token"]
    revoke_token = created["revoke_token"]

    print("3/9 initial resumable status")
    status = request("GET", f"/v1/uploads/{upload_id}", token=upload_token)
    assert not status["manifest_uploaded"] and not any(part["uploaded"] for part in status["chunks"])

    print("4/9 upload chunks")
    for index, chunk in enumerate(chunks):
        request(
            "PUT",
            f"/v1/uploads/{upload_id}/chunks/{index}",
            token=upload_token,
            body=chunk,
            content_type="application/octet-stream",
        )

    print("5/9 upload encrypted manifest")
    request(
        "PUT",
        f"/v1/uploads/{upload_id}/manifest",
        token=upload_token,
        body=encrypted_manifest,
        content_type="application/octet-stream",
    )
    status = request("GET", f"/v1/uploads/{upload_id}", token=upload_token)
    assert status["manifest_uploaded"] and all(part["uploaded"] for part in status["chunks"])

    print("6/9 commit")
    committed = request("POST", f"/v1/uploads/{upload_id}/commit", token=upload_token, body=b"")
    assert committed["descriptor"]["transfer_id"] == transfer_id

    print("7/9 descriptor and ciphertext download")
    descriptor = request("GET", f"/v1/objects/{transfer_id}/descriptor")
    assert descriptor["service_signature"]["public_key"] == info["service_key"]["public_key"]
    downloaded = b"".join(
        request("GET", f"/v1/objects/{transfer_id}/chunks/{index}")
        for index in range(len(chunks))
    )
    assert downloaded == ciphertext

    print("8/9 completion receipt")
    complete_body = json.dumps({"receipt_secret": b64url(receipt_secret)}).encode("utf-8")
    completed = request(
        "POST",
        f"/v1/objects/{transfer_id}/complete",
        body=complete_body,
        content_type="application/json",
    )
    assert completed["all_recipients_completed"] is True

    print("9/9 sender revoke and tombstone")
    request("DELETE", f"/v1/objects/{transfer_id}", token=revoke_token)
    request("GET", f"/v1/objects/{transfer_id}/descriptor", expected=410)

    print("PASS - Relay API flow completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
