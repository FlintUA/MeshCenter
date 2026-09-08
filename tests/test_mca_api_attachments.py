"""Tests for api/api_attachments.py (Step 1.6A.2; internal-rest-api.md §7.1).

Covers the ten read-only `GET` endpoints end-to-end through a real Flask
test client, against two backends:

- a **fake facade** (monkeypatched `mca_runtime.get_attachments_facade`) for
  the routing/serialization/validation/no-secret assertions - the bulk of
  the suite, so each endpoint's exact §7.x projection can be pinned without
  standing up the whole worker;
- the **real runtime** (the same `FakeRadioTransport`/`InMemoryEther` shape
  as tests/test_mca_runtime_wiring.py) for the wiring/not-ready/threading
  guarantees.

Together they pin that: a request thread never touches SQLite/filesystem/
network/tick (the endpoints only read facade snapshots); the service is
503-mapped (`mca_not_ready`) before the runtime exists *and* before the
first snapshot publish; every error uses the documented 400/404 codes; and
no secret (upload token, private-key filename, raw public key bytes,
locator/ciphertext/internal fields) reaches the wire.
"""

from __future__ import annotations

import hashlib
import threading
from functools import wraps
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify

from api.api_attachments import register_attachments_routes
from meshsrv.attachments import mca_runtime
from meshsrv.attachments.command_registry import CommandResult, STATUS_SUCCEEDED
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.facade import FacadeNotReady
from meshsrv.attachments.snapshots import (
    AttachmentRecord,
    AttachmentsSnapshot,
    DeliveryRecord,
    TimelineEvent,
)
from meshsrv.connectivity_monitor import (
    ConnectivitySnapshot,
    InternetStatus,
    RelayState,
    RelayStatus,
    UploadDecision,
    UploadReadiness,
    UploadRejectionReason,
)

# The endpoints share one 32-hex id shape (uuid4().hex).
ATTACHMENT_ID = "0" * 32
COMMAND_ID = "f" * 32


# ---- minimal handle_errors (mirrors server.py's behaviour) ----------------

def _handle_errors(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:  # pragma: no cover - safety net only
            return jsonify({"ok": False, "error": str(e), "traceback": None}), 500

    return decorated


def _build_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_attachments_routes(app, _handle_errors)
    return app.test_client()


def _client(monkeypatch, facade):
    """A Flask test client whose per-request facade lookup returns `facade`."""
    monkeypatch.setattr(mca_runtime, "get_attachments_facade", lambda: facade)
    return _build_client()


# ---- fake facade + record builders ---------------------------------------


def _attachment(id=ATTACHMENT_ID, direction="sent", state="SENT", **overrides):
    base = dict(
        id=id,
        direction=direction,
        state=state,
        file_name="photo.jpg",
        mime_type="image/jpeg",
        plain_size=1234,
        cipher_size=1300,
        created_at=100.0,
        hard_expires_at=200.0,
        download_grace_seconds=3600,
        provider_id="AbCdEfGhIjK",
        saved=False,
        primary_delivery_id=None,
        error_code=None,
        recipients=(),
        deliveries=(),
        descriptor=None,
        timeline=(),
    )
    base.update(overrides)
    return AttachmentRecord(**base)


def _snapshot(records):
    return AttachmentsSnapshot(
        records=tuple(records),
        by_id={r.id: r for r in records},
        idempotency={},
        built_at=0.0,
    )


class _FakeFacade:
    def __init__(
        self,
        *,
        snapshot=None,
        providers=None,
        connectivity=None,
        identity=None,
        commands=None,
        decisions=None,
    ):
        self._snapshot = snapshot or _snapshot([])
        self._providers = providers or {}
        self._connectivity = connectivity or ConnectivitySnapshot(
            internet=InternetStatus.UNKNOWN, relays={}
        )
        self._identity = identity
        self._commands = commands or {}
        self._decisions = decisions or {}
        self.not_ready = False

    def attachments_snapshot(self):
        if self.not_ready:
            raise FacadeNotReady()
        return self._snapshot

    def get_attachment(self, attachment_id):
        if self.not_ready:
            raise FacadeNotReady()
        return self._snapshot.by_id.get(attachment_id)

    def get_command(self, command_id):
        return self._commands.get(command_id)

    def provider_snapshot(self):
        return self._providers

    def connectivity_snapshot(self):
        return self._connectivity

    def evaluate_upload_readiness(self, provider_id, *, ciphertext_bytes=None, requested_ttl_seconds=None):
        if provider_id in self._decisions:
            return self._decisions[provider_id]
        return UploadDecision(ready=False, reason=UploadRejectionReason.PROFILE_NOT_FOUND, detail=None)

    def identity_snapshot(self):
        return self._identity


def _provider(**overrides):
    base = dict(
        provider_id="AbCdEfGhIjK",
        display_name="Example Relay",
        origin="https://relay.example.net",
        service_public_key=b"K" * 32,
        kind="provider",
        tls_required=True,
        upload_allowed=True,
        download_allowed=True,
        max_ciphertext_bytes=5 * 1024 * 1024,
        is_default=True,
        enabled=True,
        min_ttl_seconds=60,
        max_ttl_seconds=86400,
        protocol_version="1",
        upload_token_configured=True,
        last_checked_at=300.0,
        last_check_result="ok",
        last_latency_ms=42,
        last_error_code=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _relay_status(**overrides):
    base = dict(
        provider_id="AbCdEfGhIjK",
        state=RelayState.ONLINE,
        upload_readiness=UploadReadiness.READY,
        checked_at=300.0,
        latency_ms=42,
        error_code=None,
    )
    base.update(overrides)
    return RelayStatus(**base)


def _identity(**overrides):
    base = dict(
        principal_id="princ-1",
        key_id="0123456789abcdef",
        epoch=7,
        public_identity=b"P" * 32,
        status="ACTIVE",
        public_x25519=b"X" * 32,
        private_key_file="id_mca.key",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---- real runtime helpers -------------------------------------------------


class _AlwaysDownSession:
    def request(self, *args, **kwargs):
        import requests

        raise requests.ConnectionError("down for this test")


def _started(tmp_path, tag):
    """Start the real runtime and stop its worker thread for deterministic
    single-threaded driving, re-setting readiness (mirrors the wiring
    test's `_started_state`)."""
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa")
    data_dir = str(tmp_path / tag)
    mca_runtime.start_attachments_service(data_dir, transport)
    state = mca_runtime._get_state(data_dir)  # noqa: SLF001
    assert state.service.stop(), "worker did not stop"
    state.ready_event.set()
    return state


_ALL_PATHS = [
    "/api/attachments",
    f"/api/attachments/{ATTACHMENT_ID}",
    f"/api/attachments/{ATTACHMENT_ID}/deliveries",
    "/api/mca/delivery-adapters",
    "/api/mca/providers",
    "/api/mca/providers/AbCdEfGhIjK",
    "/api/mca/providers/AbCdEfGhIjK/upload-readiness",
    "/api/mca/connectivity",
    "/api/mca/identity",
    f"/api/mca/commands/{COMMAND_ID}",
]


# ---- not-ready / wiring ---------------------------------------------------


def test_every_endpoint_is_503_when_the_facade_does_not_exist(monkeypatch):
    c = _client(monkeypatch, None)
    for path in _ALL_PATHS:
        resp = c.get(path)
        assert resp.status_code == 503, path
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error_code"] == "mca_not_ready"


def test_snapshot_backed_endpoints_are_503_when_not_ready(monkeypatch):
    facade = _FakeFacade()
    facade.not_ready = True
    c = _client(monkeypatch, facade)

    for path in (
        "/api/attachments",
        f"/api/attachments/{ATTACHMENT_ID}",
        f"/api/attachments/{ATTACHMENT_ID}/deliveries",
    ):
        resp = c.get(path)
        assert resp.status_code == 503, path
        assert resp.get_json()["error_code"] == "mca_not_ready"


def test_registry_and_connectivity_endpoints_are_not_readiness_gated(monkeypatch):
    # get_command / provider / connectivity / identity / upload-readiness are
    # independent of snapshot publication - they must NOT 503 even before the
    # first snapshot, matching the facade's own docstring (§3.2).
    facade = _FakeFacade(identity=_identity())
    facade.not_ready = True
    c = _client(monkeypatch, facade)

    assert c.get("/api/mca/identity").status_code == 200
    assert c.get("/api/mca/providers").status_code == 200
    assert c.get("/api/mca/connectivity").status_code == 200
    assert c.get("/api/mca/delivery-adapters").status_code == 200
    assert c.get(f"/api/mca/commands/{COMMAND_ID}").status_code == 404
    assert c.get("/api/mca/providers/AbCdEfGhIjK/upload-readiness").status_code == 200


def test_real_runtime_serves_empty_reads_after_startup(tmp_path):
    state = _started(tmp_path, "wire")
    try:
        c = _build_client()

        resp = c.get("/api/attachments")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["attachments"] == []
        assert body["total"] == 0

        identity = c.get("/api/mca/identity").get_json()
        assert identity["ok"] is True
        assert identity["fingerprint"]
        assert "private_key_file" not in identity
        assert "public_identity" not in identity

        cmd = c.get(f"/api/mca/commands/{COMMAND_ID}")
        assert cmd.status_code == 404
        assert cmd.get_json()["error_code"] == "command_not_found"
    finally:
        mca_runtime.reset_state_for_tests()


def test_endpoint_reads_do_not_block_on_the_tick_lock(tmp_path):
    state = _started(tmp_path, "thread")
    try:
        c = _build_client()
        state.tick_lock.acquire()
        try:
            result = {}

            def hit():
                result["resp"] = c.get("/api/attachments")

            reader = threading.Thread(target=hit, name="request-thread")
            reader.start()
            reader.join(timeout=2.0)
            assert not reader.is_alive(), "endpoint blocked on the worker tick lock"
        finally:
            state.tick_lock.release()

        assert result["resp"].status_code == 200
    finally:
        mca_runtime.reset_state_for_tests()


def test_routes_reject_non_get_methods(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    assert c.post("/api/attachments").status_code == 405
    assert c.put("/api/mca/identity").status_code == 405
    assert c.delete("/api/mca/providers/AbCdEfGhIjK").status_code == 405


# ---- GET /api/attachments -------------------------------------------------


def test_list_empty_snapshot(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    body = c.get("/api/attachments").get_json()
    assert body["ok"] is True
    assert body["attachments"] == []
    assert body["total"] == 0


def test_list_serializes_public_projection_without_timeline(monkeypatch):
    facade = _FakeFacade(snapshot=_snapshot([_attachment()]))
    c = _client(monkeypatch, facade)
    item = c.get("/api/attachments").get_json()["attachments"][0]
    assert item["id"] == ATTACHMENT_ID
    assert item["direction"] == "sent"
    assert item["state"] == "SENT"
    assert item["file_name"] == "photo.jpg"
    assert item["content_available"] is False
    assert "timeline" not in item  # timeline is detail-only (§7.5)
    assert "descriptor" not in item  # locator never leaks
    assert item["recipients"] == []
    assert item["deliveries"] == []


def test_list_direction_filter(monkeypatch):
    facade = _FakeFacade(
        snapshot=_snapshot([
            _attachment(id="a" * 32, direction="sent"),
            _attachment(id="b" * 32, direction="received", state="AVAILABLE"),
        ])
    )
    c = _client(monkeypatch, facade)
    sent = c.get("/api/attachments?direction=sent").get_json()
    assert [a["id"] for a in sent["attachments"]] == ["a" * 32]
    received = c.get("/api/attachments?direction=received").get_json()
    assert [a["id"] for a in received["attachments"]] == ["b" * 32]
    assert c.get("/api/attachments").get_json()["total"] == 2


def test_list_state_filter(monkeypatch):
    facade = _FakeFacade(
        snapshot=_snapshot([
            _attachment(id="a" * 32, state="DRAFT"),
            _attachment(id="b" * 32, state="SENT"),
        ])
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/attachments?state=DRAFT").get_json()
    assert [a["id"] for a in body["attachments"]] == ["a" * 32]


def test_list_filter_errors_uses_failed_states(monkeypatch):
    facade = _FakeFacade(
        snapshot=_snapshot([
            _attachment(id="a" * 32, state="SENT"),
            _attachment(id="b" * 32, state="FAILED_UPLOAD", error_code="relay_unreachable"),
            _attachment(id="c" * 32, state="DRAFT"),
        ])
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/attachments?filter=errors").get_json()
    assert [a["id"] for a in body["attachments"]] == ["b" * 32]


def test_list_filter_pending_uses_non_terminal_states(monkeypatch):
    # "SENT"/"RECEIVED" are not terminal (the sender waits for the download
    # ACK after them); "EXPIRED" is. `pending` = anything not terminal.
    facade = _FakeFacade(
        snapshot=_snapshot([
            _attachment(id="a" * 32, state="EXPIRED"),
            _attachment(id="b" * 32, state="DRAFT"),
        ])
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/attachments?filter=pending").get_json()
    assert [a["id"] for a in body["attachments"]] == ["b" * 32]


def test_list_filter_saved(monkeypatch):
    facade = _FakeFacade(
        snapshot=_snapshot([
            _attachment(id="a" * 32, saved=False),
            _attachment(id="b" * 32, saved=True),
        ])
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/attachments?filter=saved").get_json()
    assert [a["id"] for a in body["attachments"]] == ["b" * 32]


def test_list_total_is_before_pagination(monkeypatch):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(id=f"{i:032x}") for i in range(5)]))
    c = _client(monkeypatch, facade)
    body = c.get("/api/attachments?limit=2&offset=0").get_json()
    assert len(body["attachments"]) == 2
    assert body["total"] == 5  # total is the full filtered count, not the page


def test_list_limit_and_offset_clamping(monkeypatch):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(id=f"{i:032x}") for i in range(3)]))
    c = _client(monkeypatch, facade)
    # limit above 500 clamps to 500; negative offset clamps to 0; garbage
    # falls back to defaults (100) rather than a 400.
    body = c.get("/api/attachments?limit=9999&offset=-5").get_json()
    assert body["total"] == 3
    assert len(body["attachments"]) == 3
    assert c.get("/api/attachments?limit=abc").status_code == 200


@pytest.mark.parametrize(
    "query,code",
    [
        ("direction=up", "invalid_direction"),
        ("state=NOT_A_STATE", "invalid_state"),
        ("filter=weird", "invalid_filter"),
    ],
)
def test_list_invalid_query_params(monkeypatch, query, code):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get(f"/api/attachments?{query}")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == code


# ---- GET /api/attachments/{id} -------------------------------------------


def test_detail_returns_attachment_with_separate_timeline(monkeypatch):
    events = (
        TimelineEvent(event_type="uploaded", detail={"error_code": "relay_unreachable"}, created_at=120.0),
        TimelineEvent(event_type="sent", detail={"secret": "SHOULD-NOT-LEAK"}, created_at=130.0),
    )
    facade = _FakeFacade(snapshot=_snapshot([_attachment(timeline=events)]))
    c = _client(monkeypatch, facade)
    body = c.get(f"/api/attachments/{ATTACHMENT_ID}").get_json()

    assert body["ok"] is True
    assert body["attachment"]["id"] == ATTACHMENT_ID
    assert "timeline" not in body["attachment"]  # timeline is sibling, not nested
    assert [e["event_type"] for e in body["timeline"]] == ["uploaded", "sent"]
    # §11 safe-key allowlist redacts a non-allowlisted detail key.
    assert body["timeline"][0]["detail"] == {"error_code": "relay_unreachable"}
    assert body["timeline"][1]["detail"] == {}


def test_detail_invalid_id(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get("/api/attachments/not-hex")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_attachment_id"


def test_detail_not_found(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}")
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "attachment_not_found"


# ---- GET /api/attachments/{id}/deliveries --------------------------------


def test_deliveries_returns_the_delivery_projection(monkeypatch):
    deliveries = (
        DeliveryRecord(
            id="deliv-1",
            adapter_id="meshtastic",
            connector_profile_id="meshtastic",
            route_type="direct",
            route_id="!aaaaaaaa",
            state="CONFIRMED",
            external_message_id=None,
            sent_at=150.0,
        ),
    )
    facade = _FakeFacade(snapshot=_snapshot([_attachment(deliveries=deliveries)]))
    c = _client(monkeypatch, facade)
    body = c.get(f"/api/attachments/{ATTACHMENT_ID}/deliveries").get_json()

    assert body["ok"] is True
    assert body["deliveries"] == [{
        "id": "deliv-1",
        "adapter_id": "meshtastic",
        "connector_profile_id": "meshtastic",
        "route_type": "direct",
        "route_id": "!aaaaaaaa",
        "state": "CONFIRMED",
        "external_message_id": None,
        "sent_at": 150.0,
    }]


def test_deliveries_invalid_and_missing(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    assert c.get("/api/attachments/not-hex/deliveries").status_code == 400
    assert c.get(f"/api/attachments/{ATTACHMENT_ID}/deliveries").status_code == 404


# ---- GET /api/mca/delivery-adapters --------------------------------------


def test_delivery_adapters_static_shape(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    body = c.get("/api/mca/delivery-adapters").get_json()
    assert body["ok"] is True
    assert body["adapters"] == [{
        "adapter_id": "meshtastic",
        "connector_profile_id": "meshtastic",
        "capabilities": {
            "wire_formats": ["MCA1_TEXT"],
            "max_payload_bytes": 180,
            "supports_direct": True,
            "supports_channel": False,
            "supports_incoming": True,
            "ack_semantics": "CONFIRMED",
            "connector_state": "READY",
        },
    }]


# ---- GET /api/mca/providers ----------------------------------------------


def test_providers_empty(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    assert c.get("/api/mca/providers").get_json() == {"ok": True, "providers": []}


def test_providers_list_joins_connectivity_without_raw_key(monkeypatch):
    provider = _provider()
    relay = _relay_status()
    facade = _FakeFacade(
        providers={"AbCdEfGhIjK": provider},
        connectivity=ConnectivitySnapshot(
            internet=InternetStatus.ONLINE, relays={"AbCdEfGhIjK": relay}
        ),
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/providers").get_json()

    assert body["ok"] is True
    assert len(body["providers"]) == 1
    item = body["providers"][0]
    assert item["provider_id"] == "AbCdEfGhIjK"
    assert item["display_name"] == "Example Relay"
    assert item["service_key_fingerprint"] == hashlib.sha256(b"K" * 32).hexdigest()
    assert item["state"] == "online"
    assert item["upload_readiness"] == "ready"
    assert item["latency_ms"] == 42
    assert item["error_code"] is None
    # No raw key material, no token filename.
    assert "service_public_key" not in item
    assert "upload_token_file" not in item


def test_providers_list_falls_back_when_no_relay_status(monkeypatch):
    # No RelayStatus yet (monitor never refreshed) -> state "unknown" and
    # upload_readiness derived from config only; latency/error are null.
    provider = _provider(upload_token_configured=False)
    facade = _FakeFacade(providers={"AbCdEfGhIjK": provider})
    c = _client(monkeypatch, facade)
    item = c.get("/api/mca/providers").get_json()["providers"][0]
    assert item["state"] == "unknown"
    assert item["upload_readiness"] == "upload_token_missing"
    assert item["latency_ms"] is None
    assert item["error_code"] is None


# ---- GET /api/mca/providers/{id} -----------------------------------------


def test_provider_detail_projection_omits_live_latency_fields(monkeypatch):
    provider = _provider()
    relay = _relay_status()
    facade = _FakeFacade(
        providers={"AbCdEfGhIjK": provider},
        connectivity=ConnectivitySnapshot(
            internet=InternetStatus.ONLINE, relays={"AbCdEfGhIjK": relay}
        ),
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/providers/AbCdEfGhIjK").get_json()

    assert body["ok"] is True
    item = body["provider"]
    assert item["provider_id"] == "AbCdEfGhIjK"
    assert item["state"] == "online"
    assert item["upload_readiness"] == "ready"
    # The detail projection is §7.13 only - no live latency/error join.
    assert "latency_ms" not in item
    assert "error_code" not in item
    assert "service_public_key" not in item


def test_provider_detail_not_found(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get("/api/mca/providers/never-registered")
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "provider_not_found"


# ---- GET /api/mca/providers/{id}/upload-readiness ------------------------


def test_upload_readiness_ready(monkeypatch):
    facade = _FakeFacade(
        decisions={"AbCdEfGhIjK": UploadDecision(ready=True, reason=None, detail=None)}
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/providers/AbCdEfGhIjK/upload-readiness").get_json()
    assert body == {"ok": True, "ready": True, "reason": None, "detail": None}


def test_upload_readiness_rejected_with_reason(monkeypatch):
    facade = _FakeFacade(
        decisions={
            "AbCdEfGhIjK": UploadDecision(
                ready=False, reason=UploadRejectionReason.CIPHERTEXT_TOO_LARGE, detail=None
            )
        }
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/providers/AbCdEfGhIjK/upload-readiness").get_json()
    assert body["ok"] is True
    assert body["ready"] is False
    assert body["reason"] == "ciphertext_too_large"


def test_upload_readiness_malformed_query(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get("/api/mca/providers/AbCdEfGhIjK/upload-readiness?ciphertext_bytes=abc")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_query"
    resp2 = c.get("/api/mca/providers/AbCdEfGhIjK/upload-readiness?requested_ttl_seconds=-3")
    assert resp2.status_code == 400


# ---- GET /api/mca/connectivity -------------------------------------------


def test_connectivity_separates_internet_and_relays(monkeypatch):
    relay = _relay_status()
    facade = _FakeFacade(
        connectivity=ConnectivitySnapshot(
            internet=InternetStatus.ONLINE, relays={"AbCdEfGhIjK": relay}
        )
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/connectivity").get_json()
    assert body["ok"] is True
    assert body["internet"] == "online"
    assert body["relays"] == {
        "AbCdEfGhIjK": {
            "state": "online",
            "upload_readiness": "ready",
            "checked_at": 300.0,
            "latency_ms": 42,
            "error_code": None,
        }
    }


# ---- GET /api/mca/identity -----------------------------------------------


def test_identity_shape_and_no_secret(monkeypatch):
    facade = _FakeFacade(identity=_identity())
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/identity").get_json()

    assert body["ok"] is True
    assert body["principal_id"] == "princ-1"
    assert body["key_id"] == "0123456789abcdef"
    assert body["epoch"] == 7
    assert body["status"] == "ACTIVE"
    assert body["fingerprint"] == hashlib.sha256(b"P" * 32).hexdigest()
    # No raw key material / filename.
    assert "public_identity" not in body
    assert "public_x25519" not in body
    assert "private_key_file" not in body


# ---- GET /api/mca/commands/{id} ------------------------------------------


def test_command_shape_and_frozen_result_unfrozen(monkeypatch):
    result = CommandResult(
        command_id=COMMAND_ID,
        kind="provider_register",
        status=STATUS_SUCCEEDED,
        created_at=0.0,
        updated_at=1.0,
        resource_id="AbCdEfGhIjK",
        result={"provider_id": "AbCdEfGhIjK", "nested": {"items": [1, 2, 3]}},
        error_code=None,
    )
    facade = _FakeFacade(commands={COMMAND_ID: result})
    c = _client(monkeypatch, facade)
    body = c.get(f"/api/mca/commands/{COMMAND_ID}").get_json()

    assert body["ok"] is True
    assert body["command"] == {
        "command_id": COMMAND_ID,
        "type": "provider_register",
        "status": "succeeded",
        "resource_id": "AbCdEfGhIjK",
        "result": {"provider_id": "AbCdEfGhIjK", "nested": {"items": [1, 2, 3]}},
        "error_code": None,
        "created_at": 0.0,
        "updated_at": 1.0,
    }


def test_command_invalid_id(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get("/api/mca/commands/not-hex")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_command_id"


def test_command_not_found(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get(f"/api/mca/commands/{COMMAND_ID}")
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "command_not_found"
