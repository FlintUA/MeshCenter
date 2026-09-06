"""tests/test_relay_mock.py

Contract/integration tests for the real MCAttach Relay API (ADR-0005) via
RelayClient + the rewritten mock server (Execution Plan Step 1.1,
replacing the Step 0.5 mock built from the design spec's prose alone).
Exercises: the three-tier bearer-token auth model, the atomicity rules of
`commit`, corrupted-chunk detection, idempotent `commit`/`complete`,
`revoke`, and RelayClient's own 429/5xx retry logic in isolation.

RelayClient talks to the mock Relay's real Flask app through
`_FlaskTestClientSession`, a tiny shim that gives Flask's test client the
`.request(method, url, json=, data=, headers=, timeout=)` shape
RelayClient expects - no real socket, no extra test dependency beyond
Flask (already a Core runtime dependency, see requirements.txt), PyNaCl,
and pytest.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time

import pytest

from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.attachments.relay_client import (
    ChunkDeclaration,
    RelayClient,
    RelayHTTPError,
    RelayUnavailableError,
)

BASE_URL = "https://mock-relay.test"


class _ResponseShim:
    """Adapts a Flask test-client response to the small subset of the
    `requests.Response` interface RelayClient actually uses."""

    def __init__(self, flask_response):
        self._flask_response = flask_response
        self.status_code = flask_response.status_code
        self.headers = flask_response.headers
        self.content = flask_response.data
        self.text = flask_response.get_data(as_text=True)

    def json(self):
        return self._flask_response.get_json()


class _FlaskTestClientSession:
    """Adapts a Flask test client to the `requests.Session`-shaped
    interface RelayClient needs, so it can be exercised against the mock
    Relay app in-process."""

    def __init__(self, flask_test_client, base_url: str):
        self._client = flask_test_client
        self._base_url = base_url

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        assert url.startswith(self._base_url), f"unexpected base url in {url!r}"
        path = url[len(self._base_url):]
        flask_response = self._client.open(path, method=method, json=json, data=data, headers=headers or {})
        return _ResponseShim(flask_response)


@pytest.fixture
def store():
    return MockRelayStore(base_url=BASE_URL)


@pytest.fixture
def relay_client(store):
    app = create_mock_relay_app(store)
    session = _FlaskTestClientSession(app.test_client(), BASE_URL)
    return RelayClient(BASE_URL, upload_access_token=store.upload_access_token, session=session, sleep=lambda _s: None)


def _build_plan(chunks, *, num_recipients=1, hard_ttl_seconds=None):
    """Build a realistic, digest-consistent upload plan: real sha256
    digests over real chunk/manifest bytes and real receipt secrets - the
    same values a real sender's crypto layer would compute, not hand-typed
    placeholders."""
    transfer_id = secrets.token_bytes(16)
    ciphertext = b"".join(chunks)
    manifest = b"encrypted-manifest-bytes-stand-in-for-crypto-adr-0002"
    receipt_secrets = [secrets.token_bytes(32) for _ in range(num_recipients)]
    return {
        "transfer_id": transfer_id,
        "chunks": chunks,
        "manifest": manifest,
        "receipt_secrets": receipt_secrets,
        "total_size": len(ciphertext),
        "ciphertext_sha256": hashlib.sha256(ciphertext).digest(),
        "manifest_size": len(manifest),
        "manifest_sha256": hashlib.sha256(manifest).digest(),
        "chunk_declarations": [ChunkDeclaration(size=len(c), sha256=hashlib.sha256(c).digest()) for c in chunks],
        "receipt_hashes": [hashlib.sha256(s).digest() for s in receipt_secrets],
        "hard_ttl_seconds": hard_ttl_seconds,
    }


def _create_session(client, plan):
    return client.create_upload(
        transfer_id=plan["transfer_id"],
        total_size=plan["total_size"],
        ciphertext_sha256=plan["ciphertext_sha256"],
        manifest_size=plan["manifest_size"],
        manifest_sha256=plan["manifest_sha256"],
        chunks=plan["chunk_declarations"],
        receipt_hashes=plan["receipt_hashes"],
        hard_ttl_seconds=plan["hard_ttl_seconds"],
    )


def _upload_and_commit(client, plan):
    session = _create_session(client, plan)
    for index, chunk in enumerate(plan["chunks"]):
        client.upload_chunk(session.upload_id, session.upload_token, index, chunk)
    client.upload_manifest(session.upload_id, session.upload_token, plan["manifest"])
    descriptor = client.commit(session.upload_id, session.upload_token)
    return session, descriptor


def test_get_health_and_info(relay_client, store):
    health = relay_client.get_health()
    assert health["ok"] is True
    info = relay_client.get_info()
    assert info.protocol == "MCA/1"
    assert info.service_public_key == store.service_public_key
    assert info.provider_id == store.provider_id
    assert len(info.provider_id) == 11  # Base64URL(8 bytes) - ADR-0005
    assert info.anonymous_upload is False
    assert info.download_authorization == "transfer_capability"
    assert info.limits.max_ciphertext_bytes == 6 * 1024 * 1024


def test_create_upload_requires_valid_upload_access_token(store):
    app = create_mock_relay_app(store)
    session = _FlaskTestClientSession(app.test_client(), BASE_URL)
    client = RelayClient(BASE_URL, upload_access_token="wrong-token", session=session, sleep=lambda _s: None)
    plan = _build_plan([b"x" * 10])
    with pytest.raises(RelayHTTPError) as exc_info:
        _create_session(client, plan)
    assert exc_info.value.status_code == 401


def test_full_upload_commit_download_complete_round_trip(relay_client):
    plan = _build_plan([b"chunk-zero-", b"chunk-one-longer"])
    session, descriptor = _upload_and_commit(relay_client, plan)
    assert descriptor.transfer_id == plan["transfer_id"]
    assert descriptor.total_size == plan["total_size"]
    assert descriptor.manifest_size == plan["manifest_size"]

    fetched = relay_client.get_descriptor(plan["transfer_id"])
    assert fetched.encrypted_manifest == plan["manifest"]
    assert len(fetched.chunks) == 2
    assert fetched.service_public_key == descriptor.service_public_key

    downloaded = [relay_client.get_chunk(plan["transfer_id"], i) for i in range(2)]
    assert downloaded == plan["chunks"]

    result = relay_client.complete(plan["transfer_id"], plan["receipt_secrets"][0])
    assert result.receipt_recorded is True
    assert result.all_recipients_completed is True
    assert result.delete_after is not None


def test_upload_status_reports_resume_state(relay_client):
    plan = _build_plan([b"aaaa", b"bbbb", b"cccc"])
    session = _create_session(relay_client, plan)
    relay_client.upload_chunk(session.upload_id, session.upload_token, 0, plan["chunks"][0])
    status = relay_client.get_upload_status(session.upload_id, session.upload_token)
    assert status.state == "staging"
    assert status.manifest_uploaded is False
    assert [c.uploaded for c in status.chunks] == [True, False, False]


def test_object_not_downloadable_before_commit(relay_client):
    plan = _build_plan([b"only-chunk"])
    session = _create_session(relay_client, plan)
    relay_client.upload_chunk(session.upload_id, session.upload_token, 0, plan["chunks"][0])
    relay_client.upload_manifest(session.upload_id, session.upload_token, plan["manifest"])
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.get_descriptor(plan["transfer_id"])
    assert exc_info.value.status_code == 404
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.get_chunk(plan["transfer_id"], 0)
    assert exc_info.value.status_code == 404


def test_commit_rejects_incomplete_chunks(relay_client):
    plan = _build_plan([b"chunk-a-", b"chunk-b-"])
    session = _create_session(relay_client, plan)
    relay_client.upload_chunk(session.upload_id, session.upload_token, 0, plan["chunks"][0])
    relay_client.upload_manifest(session.upload_id, session.upload_token, plan["manifest"])
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.commit(session.upload_id, session.upload_token)
    assert exc_info.value.code == "chunk_missing"
    assert exc_info.value.status_code == 409


def test_commit_rejects_missing_manifest(relay_client):
    plan = _build_plan([b"solo-chunk"])
    session = _create_session(relay_client, plan)
    relay_client.upload_chunk(session.upload_id, session.upload_token, 0, plan["chunks"][0])
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.commit(session.upload_id, session.upload_token)
    assert exc_info.value.code == "manifest_missing"


def test_commit_is_idempotent(relay_client):
    plan = _build_plan([b"only-chunk"])
    session, first = _upload_and_commit(relay_client, plan)
    second = relay_client.commit(session.upload_id, session.upload_token)
    assert first.transfer_id == second.transfer_id
    assert first.committed_at == second.committed_at


def test_upload_chunk_rejects_corrupted_chunk(relay_client):
    """Corrupted-chunk detection: uploading same-length bytes that don't
    match the digest declared at session creation is rejected at PUT time
    (422, `digest_mismatch`) - not silently accepted and only caught at
    commit. Same length as the declared chunk is essential here - a
    differently-sized payload would (correctly, matching the real Relay's
    own check order) trip `size_mismatch` first instead."""
    plan = _build_plan([b"correct-bytes-here!"])  # 19 bytes
    session = _create_session(relay_client, plan)
    corrupted = b"corrupted-bytes!!!!"  # also 19 bytes, different content
    assert len(corrupted) == len(plan["chunks"][0])
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.upload_chunk(session.upload_id, session.upload_token, 0, corrupted)
    assert exc_info.value.code == "digest_mismatch"
    assert exc_info.value.status_code == 422


def test_upload_chunk_rejects_size_mismatch(relay_client):
    plan = _build_plan([b"exactly-ten-bytes!!"])  # 19 bytes declared
    session = _create_session(relay_client, plan)
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.upload_chunk(session.upload_id, session.upload_token, 0, b"too-short")  # 9 bytes
    assert exc_info.value.code == "size_mismatch"
    assert exc_info.value.status_code == 422


def test_create_upload_rejects_hard_ttl_outside_range(relay_client):
    plan = _build_plan([b"x"], hard_ttl_seconds=10)  # below the 3600s minimum
    with pytest.raises(RelayHTTPError) as exc_info:
        _create_session(relay_client, plan)
    assert exc_info.value.status_code == 422


def test_create_upload_rejects_duplicate_transfer_id(relay_client):
    plan = _build_plan([b"x" * 5])
    _create_session(relay_client, plan)
    with pytest.raises(RelayHTTPError) as exc_info:
        _create_session(relay_client, plan)
    assert exc_info.value.code == "transfer_exists"
    assert exc_info.value.status_code == 409


def test_commit_rejects_quota_exceeded():
    store = MockRelayStore(base_url=BASE_URL, max_storage_bytes=16)
    app = create_mock_relay_app(store)
    client = RelayClient(
        BASE_URL, upload_access_token=store.upload_access_token,
        session=_FlaskTestClientSession(app.test_client(), BASE_URL), sleep=lambda _s: None,
    )
    plan = _build_plan([b"this-chunk-is-longer-than-16-bytes"])
    with pytest.raises(RelayHTTPError) as exc_info:
        _create_session(client, plan)
    assert exc_info.value.code == "relay_quota_exceeded"
    assert exc_info.value.status_code == 507


def test_complete_is_idempotent(relay_client):
    plan = _build_plan([b"only-chunk"])
    _upload_and_commit(relay_client, plan)
    relay_client.complete(plan["transfer_id"], plan["receipt_secrets"][0])
    relay_client.complete(plan["transfer_id"], plan["receipt_secrets"][0])  # must not raise


def test_complete_requires_all_recipients_before_delete_after_is_set(relay_client):
    plan = _build_plan([b"only-chunk"], num_recipients=2)
    _upload_and_commit(relay_client, plan)
    first = relay_client.complete(plan["transfer_id"], plan["receipt_secrets"][0])
    assert first.all_recipients_completed is False
    assert first.delete_after is None
    second = relay_client.complete(plan["transfer_id"], plan["receipt_secrets"][1])
    assert second.all_recipients_completed is True
    assert second.delete_after is not None


def test_complete_rejects_wrong_receipt_secret(relay_client):
    plan = _build_plan([b"only-chunk"])
    _upload_and_commit(relay_client, plan)
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.complete(plan["transfer_id"], secrets.token_bytes(32))
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "invalid_receipt"


def test_revoke_blocks_further_descriptor_and_chunk_access(relay_client):
    plan = _build_plan([b"only-chunk"])
    session, _descriptor = _upload_and_commit(relay_client, plan)
    relay_client.revoke(plan["transfer_id"], session.revoke_token)
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.get_descriptor(plan["transfer_id"])
    assert exc_info.value.status_code == 410
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.get_chunk(plan["transfer_id"], 0)
    assert exc_info.value.status_code == 410


def test_revoke_rejects_wrong_token(relay_client):
    plan = _build_plan([b"only-chunk"])
    session, _descriptor = _upload_and_commit(relay_client, plan)
    with pytest.raises(RelayHTTPError) as exc_info:
        relay_client.revoke(plan["transfer_id"], "wrong-token")
    assert exc_info.value.status_code == 401
    # And the object must still be perfectly accessible afterwards.
    relay_client.get_descriptor(plan["transfer_id"])


def test_descriptor_service_signature_is_correctly_signed(relay_client, store):
    """The Relay's own Ed25519 signature over the canonical descriptor
    (ADR-0005's "MCA-RELAY-DESCRIPTOR-V1" domain) must actually verify -
    a receiver's future crypto layer (ADR-0002) will rely on this."""
    from nacl.signing import VerifyKey

    plan = _build_plan([b"only-chunk"])
    _session, descriptor = _upload_and_commit(relay_client, plan)
    verify_key = VerifyKey(descriptor.service_public_key)

    transfer_id_b64url = base64.urlsafe_b64encode(plan["transfer_id"]).rstrip(b"=").decode("ascii")
    fetched_response = relay_client._request("GET", f"/v1/objects/{transfer_id_b64url}/descriptor")
    body = fetched_response.json()
    from meshsrv.attachments.relay.mock_server import _canonical_json

    signed_bytes = _canonical_json(body["descriptor"]).encode("utf-8")
    verify_key.verify(signed_bytes, descriptor.service_signature)  # raises if invalid


# ---- retry behaviour (design spec section 23.1, "Network" block; ADR-0005 429s) --


class _FakeResponse:
    def __init__(self, status_code, json_body=None, headers=None):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.headers = headers or {}
        self.content = b""
        self.text = ""

    def json(self):
        return self._json_body


class _ScriptedSession:
    """Returns one canned response per call, in order - used to test
    RelayClient's own retry/backoff logic in isolation from the mock
    Relay, which (being a real Flask app) never returns 429/5xx on its
    own."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        self.calls += 1
        return self._responses.pop(0)


def test_get_info_retries_on_429_then_succeeds():
    responses = [
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _FakeResponse(
            200,
            {
                "ok": True,
                "protocol": "MCA/1",
                "relay_version": "mock-0.1.0",
                "base_url": BASE_URL,
                "provider_id": "AAAAAAAAAAA",
                "service_key": {"type": "Ed25519", "public_key": "A" * 43},
                "limits": {
                    "max_ciphertext_bytes": 6291456,
                    "max_manifest_bytes": 262144,
                    "max_chunk_bytes": 300000,
                    "max_chunks": 64,
                    "max_recipients": 32,
                    "default_hard_ttl_seconds": 259200,
                    "max_hard_ttl_seconds": 259200,
                    "default_download_grace_seconds": 3600,
                },
                "capabilities": {
                    "chunked_upload": True,
                    "receipt_completion": True,
                    "sender_revoke": True,
                    "anonymous_upload": False,
                    "download_authorization": "transfer_capability",
                },
            },
        ),
    ]
    session = _ScriptedSession(responses)
    sleeps = []
    client = RelayClient(BASE_URL, session=session, max_retries=3, sleep=sleeps.append)
    info = client.get_info()
    assert info.relay_version == "mock-0.1.0"
    assert session.calls == 2
    assert sleeps == [0.0]


def test_retries_are_exhausted_and_raise_relay_unavailable():
    responses = [_FakeResponse(503) for _ in range(4)]
    session = _ScriptedSession(responses)
    client = RelayClient(BASE_URL, session=session, max_retries=3, sleep=lambda _s: None)
    with pytest.raises(RelayUnavailableError):
        client.get_info()
    assert session.calls == 4  # initial attempt + 3 retries


def test_create_upload_is_not_retried_on_429():
    """create_upload (and the other non-idempotent writes) must surface a
    transient error immediately rather than silently retrying an ambiguous
    write - see the retry-policy note at the top of relay_client.py."""
    responses = [_FakeResponse(503)]
    session = _ScriptedSession(responses)
    client = RelayClient(BASE_URL, upload_access_token="tok", session=session, max_retries=3, sleep=lambda _s: None)
    plan = _build_plan([b"x"])
    with pytest.raises(RelayHTTPError) as exc_info:
        _create_session(client, plan)
    assert exc_info.value.status_code == 503
    assert session.calls == 1
