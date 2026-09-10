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

It also pins the five review corrections to Step 1.6A.2: delivery-adapters
never fabricates `connector_state: READY` (§ UNKNOWN); pagination is strict
(`invalid_pagination`, never clamped); provider ids are canonical-validated
(`invalid_provider_id`); every route has a sanitized exception boundary
(`internal_error`, no `str(e)`/traceback/marker); and `requested_ttl_seconds`
must be a positive ASCII decimal (`invalid_query`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import traceback
from functools import wraps
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify

import api.api_attachments as api_attachments

from api.api_attachments import register_attachments_routes
from api.api_auth import register_auth_routes
from meshsrv.attachments import mca_runtime, receiver, sender
from meshsrv.attachments.command_registry import CommandResult, STATUS_SUCCEEDED
from meshsrv.attachments.commands import CommandQueueFull
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.facade import FacadeNotReady
from meshsrv.attachments.idempotency import IdempotencyEntry, PendingReservations
from meshsrv.attachments.provider_registry import (
    CLEAR,
    MAX_UPLOAD_TOKEN_BYTES,
    ProviderRegistryError,
    decode_provider_id,
    encode_provider_id,
    normalize_origin,
)
from meshsrv.attachments.key_exchange import AddressStatus
from meshsrv.attachments.recipient_snapshot import (
    RecipientBindingSnapshot,
    RecipientSnapshot,
)
from meshsrv.attachments.snapshots import (
    AttachmentRecord,
    AttachmentsSnapshot,
    ContentDescriptor,
    ContentDisposition,
    ContentLocatorError,
    DeliveryRecord,
    TimelineEvent,
    disposition_for_mime_type,
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

# Canonical provider ids, derived from the registry's own encoder (the one
# source of truth for what "canonical Base64URL" means). PROVIDER_ID is
# registered in the fakes; UNREGISTERED_PROVIDER_ID is canonical but never
# registered, so it exercises the 404 / profile_not_found paths.
PROVIDER_ID = encode_provider_id(b"\x00" * 8)
UNREGISTERED_PROVIDER_ID = encode_provider_id(b"\x01" * 8)

# Malformed / non-canonical provider ids that must all 400
# `invalid_provider_id` on both provider routes (padded, wrong-length,
# invalid-alphabet, and non-canonical-trailing-bits spellings).
MALFORMED_PROVIDER_IDS = [
    PROVIDER_ID + "=",   # padded (non-canonical spelling)
    "AAAA",              # wrong length (decodes to 3 bytes, not 8)
    "AAAAAAAAAAAA",      # wrong length (decodes to 9 bytes, not 8)
    "AAAAAAAAAAB",       # non-canonical trailing bits
    "AAAAAAAAAA!",       # invalid alphabet character
]


# ---- minimal handle_errors (mirrors server.py's behaviour) ----------------

def _make_handle_errors(app):
    """Mirror server.py's `handle_errors`, including the debug-mode traceback
    it would emit for an uncaught exception. The local `_mca_error_boundary`
    must catch everything first, so this leaky safety net is never reached."""
    def _handle_errors(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as e:  # pragma: no cover - safety net only
                return jsonify({
                    "ok": False,
                    "error": str(e),
                    "traceback": traceback.format_exc() if app.debug else None,
                }), 500

        return decorated

    return _handle_errors


def _build_client(debug=False):
    app = Flask(__name__)
    app.config["TESTING"] = True
    if debug:
        app.debug = True
    register_attachments_routes(app, _make_handle_errors(app))
    return app.test_client()


def _client(monkeypatch, facade, debug=False):
    """A Flask test client whose per-request facade lookup returns `facade`."""
    monkeypatch.setattr(mca_runtime, "get_attachments_facade", lambda: facade)
    return _build_client(debug=debug)


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
        provider_id=PROVIDER_ID,
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


def _descriptor(attachment_id=ATTACHMENT_ID, *, mime_type="image/jpeg", locator="files/photo.jpg",
                disposition=None, plain_size=1234):
    """A `ContentDescriptor` for content-route tests. `disposition` defaults to
    the real `disposition_for_mime_type(mime_type)` so the inline-vs-attachment
    behaviour under test stays consistent with the production decision."""
    return ContentDescriptor(
        attachment_id=attachment_id,
        locator=locator,
        mime_type=mime_type,
        disposition=disposition if disposition is not None else disposition_for_mime_type(mime_type),
        plain_size=plain_size,
    )


def _trusted_recipient_snapshot(source_address="!aaaaaaaa"):
    """The default recipient snapshot for the fake facade: `!aaaaaaaa` is a
    known, `MCA_READY` binding, so the pre-existing create tests (which all
    post that source_address) pass the Finding 7 synchronous check without
    each having to stand up a binding. The `public_identity` bytes are
    deliberately *not* modelled here - the projection never carries them."""
    return RecipientSnapshot(
        by_address={
            source_address: RecipientBindingSnapshot(
                adapter_id="meshtastic",
                transport_address=source_address,
                key_id="1" * 16,
                status=AddressStatus.MCA_READY,
            )
        }
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
        queue_full=False,
        spool_dir=None,
        committed=None,
        recipient=None,
        probes=None,
        content_files=None,
    ):
        self._snapshot = snapshot or _snapshot([])
        self._providers = providers or {}
        self._connectivity = connectivity or ConnectivitySnapshot(
            internet=InternetStatus.UNKNOWN, relays={}
        )
        self._identity = identity
        self._commands = commands or {}
        self._decisions = decisions or {}
        self._recipient = recipient if recipient is not None else _trusted_recipient_snapshot()
        self.not_ready = False
        self.queue_full = queue_full
        self.submitted = []  # commands the POST endpoints handed to submit()
        self.spool_dir = spool_dir
        # probe_id -> ProbeRecord (or a minimal stand-in exposing the two
        # fields the phase-2 route's synchronous validation reads:
        # service_key_fingerprint + max_ciphertext_bytes).
        self._probes = probes or {}
        # A real reservation store + committed index, so the create endpoint's
        # idempotency paths (fresh/replay_pending/replay_committed/conflict)
        # are exercised against the same logic the facade actually runs.
        self._pending = PendingReservations()
        self._committed = committed or {}
        # locator -> real on-disk Path, the one serve-time resolution the
        # content route performs. An unknown locator raises ContentLocatorError.
        self._content_files = content_files or {}

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

    def get_probe(self, probe_id):
        # The request-thread, non-consuming probe read for phase-2
        # synchronous validation (probe_id_not_found / provider_id_mismatch).
        return self._probes.get(probe_id)

    def submit(self, command):
        # Mirrors the real facade's request-thread write surface: gated on
        # readiness, backpressured on a full queue, otherwise records the
        # command and returns its id (never touches SQLite/FS/network here).
        if self.not_ready:
            raise FacadeNotReady()
        if self.queue_full:
            raise CommandQueueFull()
        self.submitted.append(command)
        return command.command_id

    def spool_outgoing_dir(self):
        return self.spool_dir

    def submit_create(self, command, *, client_request_id, reservation):
        # Mirrors the real facade's §3.6 reservation-aware create enqueue: gated
        # on readiness, backpressured on a full queue, otherwise reserves +
        # records + returns the ReservationOutcome (never touches SQLite/FS).
        if self.not_ready:
            raise FacadeNotReady()
        if self.queue_full:
            raise CommandQueueFull()
        outcome = self._pending.reserve(
            client_request_id, reservation, committed_entries=self._committed
        )
        if outcome.kind != "fresh":
            return outcome
        self.submitted.append(command)
        return outcome

    def provider_snapshot(self):
        return self._providers

    def recipient_snapshot(self):
        return self._recipient

    def connectivity_snapshot(self):
        return self._connectivity

    def evaluate_upload_readiness(self, provider_id, *, ciphertext_bytes=None, requested_ttl_seconds=None):
        if provider_id in self._decisions:
            return self._decisions[provider_id]
        return UploadDecision(ready=False, reason=UploadRejectionReason.PROFILE_NOT_FOUND, detail=None)

    def identity_snapshot(self):
        return self._identity

    def resolve_content_locator(self, locator):
        # The one serve-time re-validation the content route is permitted
        # (§7.14): resolve a descriptor locator to a real path, or raise
        # ContentLocatorError for anything outside the controlled area (here:
        # any locator the test did not deliberately map to a real file).
        if locator in self._content_files:
            return self._content_files[locator]
        raise ContentLocatorError("locator resolves outside the controlled content area")


class _RaisingFacade(_FakeFacade):
    """A fake whose `provider_snapshot()`/`identity_snapshot()` raise with a
    caller-supplied secret marker, to prove the sanitized boundary never lets
    the exception text reach the wire or the log."""

    def __init__(self, marker="SECRET_MARKER_x7k3", **kwargs):
        super().__init__(**kwargs)
        self._marker = marker

    def provider_snapshot(self):
        raise RuntimeError(f"boom {self._marker}")

    def identity_snapshot(self):
        raise ValueError(f"leak {self._marker}")


def _provider(**overrides):
    base = dict(
        provider_id=PROVIDER_ID,
        display_name="Example Relay",
        origin="https://relay.example.net",
        service_public_key=b"K" * 32,
        kind="provider",
        tls_required=True,
        upload_allowed=True,
        download_allowed=True,
        # Enough headroom that the 5 MiB plaintext cap (not the provider's
        # ciphertext limit) is the binding constraint for the success paths:
        # ciphertext_size(5 MiB) = 5 MiB + 20*16 bytes of tag = 5,243,200.
        max_ciphertext_bytes=5 * 1024 * 1024 + 1024,
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
        provider_id=PROVIDER_ID,
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
    f"/api/mca/providers/{PROVIDER_ID}",
    f"/api/mca/providers/{PROVIDER_ID}/upload-readiness",
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
    assert c.get(f"/api/mca/providers/{PROVIDER_ID}/upload-readiness").status_code == 200


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
    assert c.put("/api/attachments").status_code == 405
    assert c.put("/api/mca/identity").status_code == 405
    assert c.delete("/api/mca/identity").status_code == 405


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


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",      # below minimum
        "limit=501",    # above maximum
        "limit=abc",    # non-numeric
        "limit=-1",     # signed
        "limit=5.5",    # fractional
        "limit=+5",     # signed
        "limit=",       # blank (present but empty)
        "offset=-1",    # signed
        "offset=abc",   # non-numeric
        "offset=5.5",   # fractional
        "offset=+1",    # signed
        "offset=",      # blank
    ],
)
def test_list_invalid_pagination(monkeypatch, query):
    # A present-but-malformed or out-of-range limit/offset is a hard 400
    # `invalid_pagination`, never a clamp or a silent default.
    facade = _FakeFacade(snapshot=_snapshot([_attachment(id=f"{i:032x}") for i in range(3)]))
    c = _client(monkeypatch, facade)
    resp = c.get(f"/api/attachments?{query}")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_pagination"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("", 3),                 # defaults (limit=100, offset=0)
        ("limit=1", 1),          # lower bound
        ("limit=500", 3),        # upper bound accepted (only 3 records exist)
        ("offset=0", 3),         # offset lower bound
        ("limit=2&offset=1", 2),  # page 2 of 3
    ],
)
def test_list_pagination_valid_boundaries(monkeypatch, query, expected):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(id=f"{i:032x}") for i in range(3)]))
    c = _client(monkeypatch, facade)
    body = c.get(f"/api/attachments?{query}" if query else "/api/attachments").get_json()
    assert body["total"] == 3
    assert len(body["attachments"]) == expected


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


def test_delivery_adapters_reports_unknown_connector_state(monkeypatch):
    # The read endpoint must NOT fabricate readiness: the real adapter's
    # connector_state derives from the live radio transport's
    # `get_connection_info()` (not request-thread-safe), and there is no
    # immutable snapshot of it. So it reports UNKNOWN, never READY.
    c = _client(monkeypatch, _FakeFacade())
    body = c.get("/api/mca/delivery-adapters").get_json()
    assert body["ok"] is True
    adapter = body["adapters"][0]
    assert adapter["adapter_id"] == "meshtastic"
    assert adapter["connector_profile_id"] == "meshtastic"
    assert adapter["capabilities"]["connector_state"] == "UNKNOWN"
    # The static capability fields are still present and unchanged.
    assert adapter["capabilities"]["wire_formats"] == ["MCA1_TEXT"]
    assert adapter["capabilities"]["max_payload_bytes"] == 180
    assert adapter["capabilities"]["ack_semantics"] == "CONFIRMED"


def test_delivery_adapters_does_not_derive_state_from_connectivity(monkeypatch):
    # Internet/Relay status must never be conflated with the adapter's
    # connector_state: even with the relay ONLINE and internet up, the read
    # endpoint still reports UNKNOWN (no request-thread-safe radio state).
    relay = _relay_status()
    facade = _FakeFacade(
        connectivity=ConnectivitySnapshot(
            internet=InternetStatus.ONLINE, relays={PROVIDER_ID: relay}
        )
    )
    c = _client(monkeypatch, facade)
    adapter = c.get("/api/mca/delivery-adapters").get_json()["adapters"][0]
    assert adapter["capabilities"]["connector_state"] == "UNKNOWN"


# ---- GET /api/mca/providers ----------------------------------------------


def test_providers_empty(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    assert c.get("/api/mca/providers").get_json() == {"ok": True, "providers": []}


def test_providers_list_joins_connectivity_without_raw_key(monkeypatch):
    provider = _provider()
    relay = _relay_status()
    facade = _FakeFacade(
        providers={PROVIDER_ID: provider},
        connectivity=ConnectivitySnapshot(
            internet=InternetStatus.ONLINE, relays={PROVIDER_ID: relay}
        ),
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/providers").get_json()

    assert body["ok"] is True
    assert len(body["providers"]) == 1
    item = body["providers"][0]
    assert item["provider_id"] == PROVIDER_ID
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
    facade = _FakeFacade(providers={PROVIDER_ID: provider})
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
        providers={PROVIDER_ID: provider},
        connectivity=ConnectivitySnapshot(
            internet=InternetStatus.ONLINE, relays={PROVIDER_ID: relay}
        ),
    )
    c = _client(monkeypatch, facade)
    body = c.get(f"/api/mca/providers/{PROVIDER_ID}").get_json()

    assert body["ok"] is True
    item = body["provider"]
    assert item["provider_id"] == PROVIDER_ID
    assert item["state"] == "online"
    assert item["upload_readiness"] == "ready"
    # The detail projection is §7.13 only - no live latency/error join.
    assert "latency_ms" not in item
    assert "error_code" not in item
    assert "service_public_key" not in item


# ---- provider id canonical validation (both provider routes) -------------


@pytest.mark.parametrize("path", [
    f"/api/mca/providers/{PROVIDER_ID}",
    f"/api/mca/providers/{PROVIDER_ID}/upload-readiness",
])
def test_provider_id_canonical_known(monkeypatch, path):
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider()},
        connectivity=ConnectivitySnapshot(internet=InternetStatus.UNKNOWN, relays={}),
        decisions={PROVIDER_ID: UploadDecision(ready=True, reason=None, detail=None)},
    )
    c = _client(monkeypatch, facade)
    assert c.get(path).status_code == 200


@pytest.mark.parametrize(
    "path,status,error_code",
    [
        (f"/api/mca/providers/{UNREGISTERED_PROVIDER_ID}", 404, "provider_not_found"),
        (f"/api/mca/providers/{UNREGISTERED_PROVIDER_ID}/upload-readiness", 200, "profile_not_found"),
    ],
)
def test_provider_id_canonical_unknown(monkeypatch, path, status, error_code):
    # A canonical-but-unregistered id is a well-formed id that resolves to
    # nothing: 404 on detail, and a 200 ready:false profile_not_found on
    # upload-readiness (the readiness eval reports the miss as a rejection).
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get(path)
    assert resp.status_code == status
    if status == 404:
        assert resp.get_json()["error_code"] == error_code
    else:
        body = resp.get_json()
        assert body["ok"] is True
        assert body["ready"] is False
        assert body["reason"] == error_code


@pytest.mark.parametrize("provider_id", MALFORMED_PROVIDER_IDS)
@pytest.mark.parametrize("suffix", ["", "/upload-readiness"])
def test_provider_id_malformed(monkeypatch, provider_id, suffix):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get(f"/api/mca/providers/{provider_id}{suffix}")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_provider_id"


# ---- GET /api/mca/providers/{id}/upload-readiness ------------------------


def test_upload_readiness_ready(monkeypatch):
    facade = _FakeFacade(
        decisions={PROVIDER_ID: UploadDecision(ready=True, reason=None, detail=None)}
    )
    c = _client(monkeypatch, facade)
    body = c.get(f"/api/mca/providers/{PROVIDER_ID}/upload-readiness").get_json()
    assert body == {"ok": True, "ready": True, "reason": None, "detail": None}


def test_upload_readiness_rejected_with_reason(monkeypatch):
    facade = _FakeFacade(
        decisions={
            PROVIDER_ID: UploadDecision(
                ready=False, reason=UploadRejectionReason.CIPHERTEXT_TOO_LARGE, detail=None
            )
        }
    )
    c = _client(monkeypatch, facade)
    body = c.get(f"/api/mca/providers/{PROVIDER_ID}/upload-readiness").get_json()
    assert body["ok"] is True
    assert body["ready"] is False
    assert body["reason"] == "ciphertext_too_large"


@pytest.mark.parametrize(
    "query",
    [
        "requested_ttl_seconds=0",    # zero
        "requested_ttl_seconds=-3",   # negative (signed)
        "requested_ttl_seconds=",     # blank
        "requested_ttl_seconds=abc",  # malformed
        "requested_ttl_seconds=+5",   # signed
        "requested_ttl_seconds=5.5",  # fractional
    ],
)
def test_upload_readiness_rejects_invalid_ttl(monkeypatch, query):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get(f"/api/mca/providers/{PROVIDER_ID}/upload-readiness?{query}")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_query"


@pytest.mark.parametrize(
    "query",
    [
        "ciphertext_bytes=abc",  # malformed
        "ciphertext_bytes=-1",   # negative (signed)
        "ciphertext_bytes=5.5",  # fractional
        "ciphertext_bytes=+5",   # signed
        "ciphertext_bytes=",     # blank
    ],
)
def test_upload_readiness_rejects_invalid_ciphertext(monkeypatch, query):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.get(f"/api/mca/providers/{PROVIDER_ID}/upload-readiness?{query}")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_query"


def test_upload_readiness_accepts_zero_ciphertext_and_positive_ttl(monkeypatch):
    # ciphertext_bytes may be zero-or-greater; requested_ttl_seconds must be
    # a positive integer. Both here are valid and pass through.
    facade = _FakeFacade(
        decisions={PROVIDER_ID: UploadDecision(ready=True, reason=None, detail=None)}
    )
    c = _client(monkeypatch, facade)
    body = c.get(
        f"/api/mca/providers/{PROVIDER_ID}/upload-readiness?ciphertext_bytes=0&requested_ttl_seconds=1"
    ).get_json()
    assert body == {"ok": True, "ready": True, "reason": None, "detail": None}


# ---- GET /api/mca/connectivity -------------------------------------------


def test_connectivity_separates_internet_and_relays(monkeypatch):
    relay = _relay_status()
    facade = _FakeFacade(
        connectivity=ConnectivitySnapshot(
            internet=InternetStatus.ONLINE, relays={PROVIDER_ID: relay}
        )
    )
    c = _client(monkeypatch, facade)
    body = c.get("/api/mca/connectivity").get_json()
    assert body["ok"] is True
    assert body["internet"] == "online"
    assert body["relays"] == {
        PROVIDER_ID: {
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
        resource_id=PROVIDER_ID,
        result={"provider_id": PROVIDER_ID, "nested": {"items": [1, 2, 3]}},
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
        "resource_id": PROVIDER_ID,
        "result": {"provider_id": PROVIDER_ID, "nested": {"items": [1, 2, 3]}},
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


# ---- sanitized exception boundary (§11 "no secret logging") --------------


def test_unexpected_exception_is_sanitized(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    marker = "SECRET_MARKER_x7k3"
    c = _client(monkeypatch, _RaisingFacade(marker=marker))

    # Two representative facade reads (provider_snapshot + identity_snapshot)
    # each raise an exception carrying the marker.
    resp = c.get("/api/mca/providers")
    resp2 = c.get("/api/mca/identity")

    for r in (resp, resp2):
        assert r.status_code == 500
        assert r.get_json() == {
            "ok": False,
            "error": "Internal server error",
            "error_code": "internal_error",
        }
        assert marker not in r.get_data(as_text=True)
        assert "Traceback" not in r.get_data(as_text=True)

    # The log carries only the safe handler name + exception class.
    assert "MCAttach read endpoint" in caplog.text
    assert marker not in caplog.text
    assert "Traceback" not in caplog.text


def test_unexpected_exception_is_sanitized_even_in_debug_mode(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    marker = "SECRET_MARKER_debug"
    c = _client(monkeypatch, _RaisingFacade(marker=marker), debug=True)

    resp = c.get("/api/mca/providers")
    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": "Internal server error",
        "error_code": "internal_error",
    }
    # Even with app.debug=True, no traceback and no marker leak (the legacy
    # handle_errors traceback path is never reached).
    assert marker not in resp.get_data(as_text=True)
    assert "Traceback" not in resp.get_data(as_text=True)
    assert marker not in caplog.text
    assert "Traceback" not in caplog.text


def test_next_request_succeeds_after_injected_failure(monkeypatch):
    class _FlakyFacade(_FakeFacade):
        def __init__(self):
            super().__init__(identity=_identity())
            self._fail = True

        def identity_snapshot(self):
            if self._fail:
                self._fail = False
                raise RuntimeError("boom SECRET_MARKER_transient")
            return self._identity

    c = _client(monkeypatch, _FlakyFacade())
    assert c.get("/api/mca/identity").status_code == 500
    second = c.get("/api/mca/identity")
    assert second.status_code == 200
    assert second.get_json()["ok"] is True


# ---- Step 1.6A.3A lifecycle mutations (POST) ------------------------------

_LIFECYCLE_PATHS = [
    f"/api/attachments/{ATTACHMENT_ID}/retry",
    f"/api/attachments/{ATTACHMENT_ID}/download",
    f"/api/attachments/{ATTACHMENT_ID}/reject",
    f"/api/attachments/{ATTACHMENT_ID}/cancel",
]


def _assert_202_accepted(body, facade, kind):
    assert body["ok"] is True
    assert set(body) == {"ok", "command_id"}
    # mint_command_id() -> uuid4().hex: 32 lowercase hex chars.
    cid = body["command_id"]
    assert len(cid) == 32 and all(ch in "0123456789abcdef" for ch in cid)
    assert len(facade.submitted) == 1
    assert facade.submitted[0].kind == kind
    assert dict(facade.submitted[0].payload) == {"attachment_id": ATTACHMENT_ID}


def test_post_lifecycle_is_503_when_facade_missing(monkeypatch):
    c = _client(monkeypatch, None)
    for path in _LIFECYCLE_PATHS:
        resp = c.post(path)
        assert resp.status_code == 503, path
        assert resp.get_json()["error_code"] == "mca_not_ready"


def test_post_lifecycle_is_503_when_not_ready(monkeypatch):
    facade = _FakeFacade()
    facade.not_ready = True
    c = _client(monkeypatch, facade)
    for path in _LIFECYCLE_PATHS:
        resp = c.post(path)
        assert resp.status_code == 503, path
        assert resp.get_json()["error_code"] == "mca_not_ready"


def test_post_lifecycle_rejects_a_malformed_id(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    for path in [
        "/api/attachments/not-hex/retry",
        "/api/attachments/ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ/download",
        "/api/attachments/abc/reject",
        "/api/attachments/abc/cancel",
    ]:
        resp = c.post(path)
        assert resp.status_code == 400, path
        assert resp.get_json()["error_code"] == "invalid_attachment_id"


def test_post_lifecycle_unknown_id_is_404(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    for path in _LIFECYCLE_PATHS:
        resp = c.post(path)
        assert resp.status_code == 404, path
        assert resp.get_json()["error_code"] == "attachment_not_found"


@pytest.mark.parametrize("direction,state", [
    ("sent", s) for s in sorted(sender.AUTOMATIC_STATES)
] + [
    ("received", s) for s in sorted(receiver.AUTOMATIC_STATES)
])
def test_retry_accepted_in_the_rows_own_automatic_states(monkeypatch, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/retry")
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, "attachment_retry")


@pytest.mark.parametrize("direction,state", [
    ("sent", sender.SENT),             # awaiting download ACK, not automatic
    ("sent", sender.RECEIVED),
    ("sent", sender.DOWNLOADED),       # terminal - not retryable here
    ("sent", sender.EXPIRED),
    ("sent", sender.REVOKED),
    ("sent", sender.CANCELLED),
    ("sent", sender.FAILED_VALIDATION),
    ("sent", sender.FAILED_UPLOAD),
    ("sent", sender.FAILED_RADIO),
    ("received", receiver.WAITING_CONSENT),  # manual consent, not automatic
    ("received", receiver.OFFER_RECEIVED),
    ("received", receiver.VERIFYING),
    ("received", receiver.AVAILABLE),        # terminal
    ("received", receiver.EXPIRED),
    ("received", receiver.REJECTED),
    ("received", receiver.FAILED),
])
def test_retry_rejected_outside_the_rows_automatic_states(monkeypatch, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/retry")
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "invalid_state_transition"
    assert resp.get_json()["state"] == state  # the 409 carries the exact current state
    assert facade.submitted == []  # nothing was enqueued


@pytest.mark.parametrize("action", ["download", "reject"])
def test_consent_action_accepted_for_a_received_waiting_consent_row(monkeypatch, action):
    facade = _FakeFacade(
        snapshot=_snapshot([_attachment(direction="received", state=receiver.WAITING_CONSENT)])
    )
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/{action}")
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, f"attachment_{action}")


@pytest.mark.parametrize("action", ["download", "reject"])
@pytest.mark.parametrize("direction,state", [
    ("sent", sender.DRAFT),            # wrong direction, regardless of state
    ("sent", sender.READY_TO_SEND),
    ("received", receiver.WAITING_KEY),
    ("received", receiver.DOWNLOADING),
    ("received", receiver.AVAILABLE),
    ("received", receiver.REJECTED),
])
def test_consent_action_rejected_unless_received_and_waiting_consent(monkeypatch, action, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/{action}")
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "invalid_state_transition"
    assert resp.get_json()["state"] == state
    assert facade.submitted == []


@pytest.mark.parametrize("state", sorted(sender.AUTOMATIC_STATES))
def test_cancel_accepted_in_sent_automatic_states(monkeypatch, state):
    # §7.4 cancel: an outgoing attachment in any automatic (pre-SENT) state is
    # cancellable - same sent-side AUTOMATIC_STATES as retry, but *without* a
    # received branch (a received row is never cancellable).
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction="sent", state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/cancel")
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, "attachment_cancel")


@pytest.mark.parametrize("direction,state", [
    ("sent", sender.SENT),              # awaiting download ACK, not automatic
    ("sent", sender.RECEIVED),
    ("sent", sender.DOWNLOADED),        # terminal
    ("sent", sender.EXPIRED),
    ("sent", sender.REVOKED),
    ("sent", sender.CANCELLED),
    ("sent", sender.FAILED_VALIDATION),
    ("sent", sender.FAILED_UPLOAD),
    ("sent", sender.FAILED_RADIO),
    ("received", receiver.WAITING_CONSENT),
    ("received", receiver.DOWNLOADING),  # a received *automatic* state is still not cancellable
    ("received", receiver.AVAILABLE),
    ("received", receiver.REJECTED),
])
def test_cancel_rejected_outside_sent_automatic_states(monkeypatch, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/cancel")
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "invalid_state_transition"
    assert resp.get_json()["state"] == state
    assert facade.submitted == []


@pytest.mark.parametrize("path,direction,state", [
    (f"/api/attachments/{ATTACHMENT_ID}/retry", "sent", sender.DOWNLOADED),   # terminal, not automatic
    (f"/api/attachments/{ATTACHMENT_ID}/download", "sent", sender.DRAFT),     # wrong direction
    (f"/api/attachments/{ATTACHMENT_ID}/reject", "received", receiver.DOWNLOADING),  # not WAITING_CONSENT
    (f"/api/attachments/{ATTACHMENT_ID}/cancel", "received", receiver.WAITING_CONSENT),  # received, not cancellable
])
def test_409_state_transition_envelope_is_exact(monkeypatch, path, direction, state):
    # The synchronous 409 must be exactly {ok, error, error_code, state} - the
    # safe public state value, and nothing else (no direction, ids, paths,
    # comments, filenames, keys, tokens, or exception text).
    facade = _FakeFacade(snapshot=_snapshot([_attachment(
        direction=direction,
        state=state,
        file_name="SECRET_file_name.txt",   # must not leak into the 409 body
        provider_id="SECRET_provider",      # must not leak into the 409 body
    )]))
    c = _client(monkeypatch, facade)
    resp = c.post(path)
    assert resp.status_code == 409
    assert resp.get_json() == {
        "ok": False,
        "error": "invalid state transition",
        "error_code": "invalid_state_transition",
        "state": state,
    }
    assert facade.submitted == []  # the precondition failed before any enqueue


def test_post_lifecycle_queue_full_is_429(monkeypatch):
    facade = _FakeFacade(
        snapshot=_snapshot([_attachment(direction="received", state=receiver.WAITING_CONSENT)]),
        queue_full=True,
    )
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/download")
    assert resp.status_code == 429
    assert resp.get_json()["error_code"] == "command_queue_full"
    assert facade.submitted == []


def test_post_lifecycle_does_not_block_on_the_tick_lock(tmp_path):
    # The request-thread POST path must read only the published snapshot (and
    # submit through the in-memory facade) - never the worker's tick lock. An
    # empty DB means the canonical id is unknown -> 404, not a hang.
    state = _started(tmp_path, "post-thread")
    try:
        c = _build_client()
        state.tick_lock.acquire()
        try:
            resp = c.post(f"/api/attachments/{ATTACHMENT_ID}/retry")
        finally:
            state.tick_lock.release()
        assert resp.status_code == 404
    finally:
        mca_runtime.reset_state_for_tests()


# ---- Step 1.6A.3A: CSRF integration (the global hook, not endpoint-local) --

# The three POST routes are protected by the *already-shipped* project-wide
# CSRF hook (api_auth.register_auth_routes' before_request), exactly like
# every other unsafe /api/ route. api_attachments.py adds no endpoint-local
# CSRF code of its own; these tests prove the global boundary holds for the
# new routes by driving them through a real Flask app that registers BOTH
# register_auth_routes() (for _enforce_csrf) and register_attachments_routes().


def _csrf_client(monkeypatch, facade):
    """A Flask test client running the three POST routes behind the real
    project-wide CSRF hook. Auth is disabled (enabled=False) so _enforce_auth
    passes through and _enforce_csrf is the sole gate - the exact global
    boundary that protects every unsafe /api/ request in production."""
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.config["TESTING"] = True
    state_lock = threading.RLock()
    auth_state = {"enabled": False, "password_hash": ""}
    register_auth_routes(app, state_lock, auth_state, "/nonexistent/auth.json", _make_handle_errors(app))
    register_attachments_routes(app, _make_handle_errors(app))
    monkeypatch.setattr(mca_runtime, "get_attachments_facade", lambda: facade)
    return app.test_client()


def _set_csrf_token(client, token="session-token"):
    with client.session_transaction() as sess:
        sess["csrf_token"] = token


_CSRF_LIFECYCLE_CASES = [
    (f"/api/attachments/{ATTACHMENT_ID}/retry", "sent", sender.DRAFT),
    (f"/api/attachments/{ATTACHMENT_ID}/download", "received", receiver.WAITING_CONSENT),
    (f"/api/attachments/{ATTACHMENT_ID}/reject", "received", receiver.WAITING_CONSENT),
    (f"/api/attachments/{ATTACHMENT_ID}/cancel", "sent", sender.DRAFT),
]


@pytest.mark.parametrize("path,direction,state", _CSRF_LIFECYCLE_CASES)
def test_post_lifecycle_missing_csrf_token_is_403(monkeypatch, path, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _csrf_client(monkeypatch, facade)
    resp = c.post(path)  # no X-CSRF-Token header, no session token
    assert resp.status_code == 403
    assert resp.get_json() == {
        "ok": False,
        "error": "CSRF token missing or invalid",
        "error_code": "csrf_invalid",
    }
    assert facade.submitted == []  # the route never ran, so nothing was submitted


@pytest.mark.parametrize("path,direction,state", _CSRF_LIFECYCLE_CASES)
def test_post_lifecycle_invalid_csrf_token_is_403(monkeypatch, path, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _csrf_client(monkeypatch, facade)
    _set_csrf_token(c, "real-token")
    resp = c.post(path, headers={"X-CSRF-Token": "wrong-token"})
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "csrf_invalid"
    assert facade.submitted == []


@pytest.mark.parametrize("path,direction,state,kind", [
    (f"/api/attachments/{ATTACHMENT_ID}/retry", "sent", sender.DRAFT, "attachment_retry"),
    (f"/api/attachments/{ATTACHMENT_ID}/download", "received", receiver.WAITING_CONSENT, "attachment_download"),
    (f"/api/attachments/{ATTACHMENT_ID}/reject", "received", receiver.WAITING_CONSENT, "attachment_reject"),
])
def test_post_lifecycle_valid_csrf_token_reaches_submission(monkeypatch, path, direction, state, kind):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _csrf_client(monkeypatch, facade)
    _set_csrf_token(c, "session-token")
    resp = c.post(path, headers={"X-CSRF-Token": "session-token"})
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, kind)


# ---- Step 1.6A.3C: contact key request (§7.10) ------------------------------

CONTACT_ID = "!756f9960"


def _recipient_at(address, status):
    """A recipient snapshot with exactly one binding: `address` at `status`
    (`None` status == an empty snapshot, i.e. KEY_UNKNOWN for every address)."""
    if status is None:
        return RecipientSnapshot(by_address={})
    return RecipientSnapshot(
        by_address={
            address: RecipientBindingSnapshot(
                adapter_id="meshtastic",
                transport_address=address,
                key_id="2" * 16,
                status=status,
            )
        }
    )


def _assert_request_key_202(body, facade, contact_id):
    assert body["ok"] is True
    assert set(body) == {"ok", "command_id"}
    cid = body["command_id"]
    assert len(cid) == 32 and all(ch in "0123456789abcdef" for ch in cid)
    assert len(facade.submitted) == 1
    cmd = facade.submitted[0]
    assert cmd.kind == "contact_request_key"
    assert dict(cmd.payload) == {
        "contact_id": contact_id,
        "adapter_id": mca_runtime.ADAPTER_ID,
        "route_id": contact_id,
    }


def test_request_key_is_503_when_facade_missing(monkeypatch):
    c = _client(monkeypatch, None)
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key")
    assert resp.status_code == 503
    assert resp.get_json()["error_code"] == "mca_not_ready"


def test_request_key_is_503_when_not_ready(monkeypatch):
    facade = _FakeFacade()
    facade.not_ready = True
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key")
    assert resp.status_code == 503
    assert resp.get_json()["error_code"] == "mca_not_ready"
    assert facade.submitted == []


@pytest.mark.parametrize("contact_id", [
    "not-hex",       # no leading bang
    "!756f996",      # 7 hex chars
    "!756f99600",    # 9 hex chars
    "!756F9960",     # uppercase hex
    "756f9960",      # missing bang
    "!gggggggg",     # non-hex alphabet
])
def test_request_key_rejects_a_malformed_contact_id(monkeypatch, contact_id):
    c = _client(monkeypatch, _FakeFacade())
    resp = c.post(f"/api/mca/contacts/{contact_id}/request-key")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_contact_id"
    assert resp.get_json()["ok"] is False


@pytest.mark.parametrize("body", [
    {"route": {"adapter_id": "meshtastic", "route_id": "!aaaaaaaa"}},   # route_id != contact_id
    {"route": {"adapter_id": "not-meshtastic", "route_id": CONTACT_ID}},  # wrong adapter
    {"route": "not-a-dict"},
    [1, 2, 3],                                                          # body not an object
])
def test_request_key_rejects_a_bad_route_or_body(monkeypatch, body):
    c = _client(monkeypatch, _FakeFacade(recipient=_recipient_at(CONTACT_ID, None)))
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key", json=body)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_contact_id"


def test_request_key_rejects_an_already_ready_contact(monkeypatch):
    facade = _FakeFacade(recipient=_recipient_at(CONTACT_ID, AddressStatus.MCA_READY))
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key")
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "key_already_known"
    assert facade.submitted == []


@pytest.mark.parametrize("status", [
    None,                             # KEY_UNKNOWN (absent binding)
    AddressStatus.KEY_UNVERIFIED,
    AddressStatus.KEY_CHANGED,
])
def test_request_key_accepted_for_a_non_ready_contact(monkeypatch, status):
    facade = _FakeFacade(recipient=_recipient_at(CONTACT_ID, status))
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key")
    assert resp.status_code == 202
    _assert_request_key_202(resp.get_json(), facade, CONTACT_ID)


def test_request_key_accepts_an_empty_body_and_a_matching_route(monkeypatch):
    # An empty body (DIRECT route default) and an explicit matching DIRECT
    # route are both accepted, and produce the same command payload.
    facade = _FakeFacade(recipient=_recipient_at(CONTACT_ID, None))
    c = _client(monkeypatch, facade)

    empty = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key")
    assert empty.status_code == 202

    matching = c.post(
        f"/api/mca/contacts/{CONTACT_ID}/request-key",
        json={"route": {"adapter_id": "meshtastic", "route_id": CONTACT_ID}},
    )
    assert matching.status_code == 202
    assert len(facade.submitted) == 2
    assert facade.submitted[0].payload == facade.submitted[1].payload


def test_request_key_queue_full_is_429(monkeypatch):
    facade = _FakeFacade(recipient=_recipient_at(CONTACT_ID, None), queue_full=True)
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key")
    assert resp.status_code == 429
    assert resp.get_json()["error_code"] == "command_queue_full"
    assert facade.submitted == []


def test_request_key_missing_csrf_token_is_403(monkeypatch):
    facade = _FakeFacade(recipient=_recipient_at(CONTACT_ID, None))
    c = _csrf_client(monkeypatch, facade)
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key")
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "csrf_invalid"
    assert facade.submitted == []


def test_request_key_valid_csrf_token_reaches_submission(monkeypatch):
    facade = _FakeFacade(recipient=_recipient_at(CONTACT_ID, None))
    c = _csrf_client(monkeypatch, facade)
    _set_csrf_token(c, "session-token")
    resp = c.post(f"/api/mca/contacts/{CONTACT_ID}/request-key", headers={"X-CSRF-Token": "session-token"})
    assert resp.status_code == 202
    _assert_request_key_202(resp.get_json(), facade, CONTACT_ID)


# ---- Step 1.6A.3B: idempotent multipart create (POST /api/attachments) -----

# A minimal JPEG (SOI marker) so `sniff_mime_type` deterministically yields
# `image/jpeg` for the success/fresh paths.
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _make_meta(**overrides):
    meta = {
        "client_request_id": "req-1",
        "recipient": {"source_address": "!aaaaaaaa"},
        "hard_ttl_seconds": 3600,       # within the fake provider's [60, 86400]
        "download_grace_seconds": 3600,
    }
    meta.update(overrides)
    return meta


def _post_create(c, *, data=_JPEG, filename="photo.jpg", meta=None, headers=None):
    return c.post(
        "/api/attachments",
        data={
            "file": (BytesIO(data), filename),
            "metadata": json.dumps(meta if meta is not None else _make_meta()),
        },
        content_type="multipart/form-data",
        headers=headers,
    )


def test_create_503_when_facade_missing(monkeypatch):
    c = _client(monkeypatch, None)
    resp = _post_create(c)
    assert resp.status_code == 503
    assert resp.get_json()["error_code"] == "mca_not_ready"


def test_create_503_when_not_ready(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    facade.not_ready = True
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 503
    assert resp.get_json()["error_code"] == "mca_not_ready"
    assert facade.submitted == []


def test_create_missing_metadata_part_is_invalid_metadata(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = c.post(
        "/api/attachments",
        data={"file": (BytesIO(_JPEG), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_metadata"


@pytest.mark.parametrize("metadata_raw", [
    "not-json",                                              # invalid JSON
    "[1,2,3]",                                               # valid JSON, not an object
    "{}",                                                    # missing client_request_id
    json.dumps({"client_request_id": 123, "recipient": {"source_address": "!aaaaaaaa"}}),
    json.dumps({"client_request_id": "req-1"}),              # missing recipient
    json.dumps({"client_request_id": "req-1", "recipient": {}}),
    json.dumps({"client_request_id": "req-1", "recipient": {"source_address": ""}}),
    json.dumps({"client_request_id": "req-1", "recipient": {"source_address": "!aaaaaaaa"}, "route": []}),
    json.dumps({"client_request_id": "req-1", "recipient": {"source_address": "!aaaaaaaa"}, "route": {"route_type": "INDIRECT"}}),
    json.dumps({"client_request_id": "req-1", "recipient": {"source_address": "!aaaaaaaa"}, "route": {"route_id": "!bbbbbbbb"}}),
    json.dumps({"client_request_id": "req-1", "recipient": {"source_address": "!aaaaaaaa"}, "comment": 5}),
    json.dumps({"client_request_id": "req-1", "recipient": {"source_address": "!aaaaaaaa"}, "hard_ttl_seconds": "3600"}),
])
def test_create_invalid_metadata_is_400(monkeypatch, tmp_path, metadata_raw):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = c.post(
        "/api/attachments",
        data={"file": (BytesIO(_JPEG), "photo.jpg"), "metadata": metadata_raw},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_metadata"
    assert facade.submitted == []


def test_create_file_too_large(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    big = b"\xff\xd8\xff" + b"x" * (5 * 1024 * 1024)  # 3 bytes over the 5 MiB cap
    resp = _post_create(c, data=big)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "file_too_large"
    assert facade.submitted == []


# ---- Finding 4: bounded + atomic multipart staging -------------------------

def test_create_request_body_over_cap_is_413(monkeypatch, tmp_path):
    # The framework-level *total* multipart cap: a body whose content-length
    # exceeds _MAX_REQUEST_BYTES must be rejected with 413 by Werkzeug during
    # form parsing - before any form field is read or any staging I/O runs.
    # The file part stays tiny; the oversized *metadata* part is what pushes the
    # total over the cap, proving the 413 is the request cap, not the 5 MiB
    # file cap (which would be 400 file_too_large).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    meta = _make_meta()
    meta["padding"] = "x" * (6 * 1024 * 1024)
    resp = _post_create(c, meta=meta)
    assert resp.status_code == 413
    assert resp.get_json()["error_code"] == "request_too_large"
    assert facade.submitted == []
    # Rejected before any staging: the spool directory was never even created.
    assert not (tmp_path / "spool").exists()


def test_create_metadata_too_large_is_400(monkeypatch, tmp_path):
    # The metadata part is bounded separately: an oversized (but still
    # parseable) metadata string is rejected with 400 *before* json.loads, and
    # before the file part is even read - no staging I/O happens.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    meta = _make_meta()
    meta["padding"] = "x" * (17 * 1024)  # ~17 KiB, just over the 16 KiB cap
    resp = _post_create(c, meta=meta)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "metadata_too_large"
    assert facade.submitted == []
    assert not (tmp_path / "spool").exists()


def test_create_exact_5mib_file_boundary_is_accepted(monkeypatch, tmp_path):
    # Exactly _MAX_FILE_BYTES plaintext must be accepted (the cap is exclusive:
    # `size > _MAX_FILE_BYTES`, not `>=`).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    exact = b"\xff\xd8\xff\xe0" + b"\x00" * (5 * 1024 * 1024 - 4)
    resp = _post_create(c, data=exact)
    assert resp.status_code == 202
    body = resp.get_json()
    # The staged file is the full 5 MiB plaintext, atomically published to its
    # final server-generated name with no temp file left behind.
    staged = tmp_path / "spool" / body["attachment_id"]
    assert staged.exists()
    assert staged.stat().st_size == 5 * 1024 * 1024
    assert [p.name for p in (tmp_path / "spool").iterdir()] == [body["attachment_id"]]


def test_create_atomic_publish_leaves_no_temp_file(monkeypatch, tmp_path):
    # After a fresh create the spool directory holds exactly the committed
    # file (a bare 32-hex attachment_id) - never a dot-prefixed `.tmp` staging
    # file, because the temp name was atomically renamed to the final name.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    body = _post_create(c).get_json()
    names = [p.name for p in (tmp_path / "spool").iterdir()]
    assert names == [body["attachment_id"]]
    assert not any(n.startswith(".") or n.endswith(".tmp") for n in names)


def test_create_spool_collision_is_500_and_never_overwrites(monkeypatch, tmp_path):
    # The final spool name is a freshly-minted uuid4 hex, but if that name is
    # somehow already present the route must fail closed (500) and never
    # overwrite the existing file, and must remove its own temp file.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    fixed_id = "a" * 32
    collision = spool_dir / fixed_id
    collision.write_bytes(b"pre-existing-do-not-overwrite")
    # Force the minted attachment_id to collide with the pre-created file, but
    # only for the *api* module's uuid lookup (mint_command_id keeps the real
    # uuid module, so its id stays random).
    monkeypatch.setattr(
        api_attachments, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=fixed_id))
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "internal_error"
    assert collision.read_bytes() == b"pre-existing-do-not-overwrite"
    assert facade.submitted == []
    # The temp file was removed; only the pre-existing file remains.
    assert [p.name for p in spool_dir.iterdir()] == [fixed_id]


def test_create_queue_full_discards_the_staged_file(monkeypatch, tmp_path):
    # A queue-full submit (429) must remove the already-staged final spool file.
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool", queue_full=True
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 429
    assert resp.get_json()["error_code"] == "command_queue_full"
    assert facade.submitted == []
    assert list((tmp_path / "spool").iterdir()) == []


def test_create_not_ready_discards_the_staged_file(monkeypatch, tmp_path):
    # A not-ready submit (503) must remove the already-staged final spool file.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    facade.not_ready = True
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 503
    assert resp.get_json()["error_code"] == "mca_not_ready"
    assert facade.submitted == []
    assert list((tmp_path / "spool").iterdir()) == []


def test_create_submit_exception_discards_the_staged_file_and_is_sanitized(
    monkeypatch, tmp_path, caplog
):
    # Finding 2: any other submit failure (not queue-full, not not-ready) must
    # remove the already-staged-and-published spool file and return the
    # sanitized 500 - no exception text/class/path in the response or the log.
    caplog.set_level(logging.ERROR)
    marker = "SECRET_MARKER_submit_x4"

    class _RaisingSubmitFacade(_FakeFacade):
        def submit_create(self, command, *, client_request_id, reservation):
            raise RuntimeError(f"submit-leak {marker}")

    facade = _RaisingSubmitFacade(
        providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool"
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": "Internal server error",
        "error_code": "internal_error",
    }
    assert marker not in resp.get_data(as_text=True)
    assert "Traceback" not in resp.get_data(as_text=True)
    assert marker not in caplog.text
    assert "Traceback" not in caplog.text
    # The staged-and-published spool file was removed, not left orphaned.
    assert list((tmp_path / "spool").iterdir()) == []


def test_create_mime_not_allowed(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=b"\x00\x01\x02\x03\x04")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "mime_not_allowed"
    assert facade.submitted == []


def test_create_explicit_provider_not_found(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, meta=_make_meta(provider_id=UNREGISTERED_PROVIDER_ID))
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "provider_not_found"


def test_create_no_default_provider(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c)  # no provider_id, and no default is configured
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "provider_not_found"


def test_create_ttl_out_of_range(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, meta=_make_meta(hard_ttl_seconds=999999))
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "ttl_out_of_range"


# ---- Finding 6: provider policy checks (request thread) ---------------------


def test_create_provider_disabled(monkeypatch, tmp_path):
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider(enabled=False)}, spool_dir=tmp_path / "spool"
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "provider_disabled"
    assert facade.submitted == []
    # Rejected before any staging I/O.
    assert not (tmp_path / "spool").exists()


def test_create_upload_not_allowed(monkeypatch, tmp_path):
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider(upload_allowed=False)}, spool_dir=tmp_path / "spool"
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "upload_not_allowed"
    assert facade.submitted == []
    assert not (tmp_path / "spool").exists()


def test_create_upload_token_missing(monkeypatch, tmp_path):
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider(upload_token_configured=False)}, spool_dir=tmp_path / "spool"
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "upload_token_missing"
    assert facade.submitted == []
    assert not (tmp_path / "spool").exists()


def test_create_ciphertext_too_large(monkeypatch, tmp_path):
    # The deterministic ciphertext upper bound for the 36-byte _JPEG is
    # 36 + 1*16 = 52 bytes; a provider whose max_ciphertext_bytes is below
    # that must be rejected synchronously, after staging but before publish.
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider(max_ciphertext_bytes=51)}, spool_dir=tmp_path / "spool"
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "ciphertext_too_large"
    assert facade.submitted == []
    # The temp file was discarded; no committed spool file was published.
    assert list((tmp_path / "spool").iterdir()) == []


def test_create_ciphertext_exact_boundary_is_accepted(monkeypatch, tmp_path):
    # The upper bound check is strictly `>`, so exactly 52 == max_ciphertext_bytes
    # is accepted.
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider(max_ciphertext_bytes=52)}, spool_dir=tmp_path / "spool"
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 202
    assert len(facade.submitted) == 1


def test_create_does_not_require_relay_reachability(monkeypatch, tmp_path):
    # Finding 6: Relay/Internet reachability is NOT a create precondition. A
    # provider whose Relay is currently UNREACHABLE (and the fallback internet
    # OFFLINE) must still accept the create and queue it - offline creation
    # stays possible; relay state only gates the later upload, never the draft.
    relay = _relay_status(state=RelayState.UNREACHABLE)
    connectivity = ConnectivitySnapshot(
        internet=InternetStatus.OFFLINE, relays={PROVIDER_ID: relay}
    )
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider()},
        connectivity=connectivity,
        spool_dir=tmp_path / "spool",
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 202
    assert len(facade.submitted) == 1


def test_create_fresh_returns_202_with_minted_ids(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["ok"] is True
    assert set(body) == {"ok", "command_id", "attachment_id"}
    assert len(body["command_id"]) == 32 and all(ch in "0123456789abcdef" for ch in body["command_id"])
    assert len(body["attachment_id"]) == 32 and all(ch in "0123456789abcdef" for ch in body["attachment_id"])

    assert len(facade.submitted) == 1
    cmd = facade.submitted[0]
    assert cmd.kind == "attachment_create"
    payload = dict(cmd.payload)
    assert payload["attachment_id"] == body["attachment_id"]
    assert payload["client_request_id"] == "req-1"
    assert payload["source_address"] == "!aaaaaaaa"
    assert payload["provider_id"] == PROVIDER_ID
    assert payload["mime_type"] == "image/jpeg"
    assert payload["source_name"] == "photo.jpg"
    assert payload["comment"] is None
    assert payload["hard_ttl_seconds"] == 3600
    assert payload["download_grace_seconds"] == 3600
    assert len(payload["canonical_hash"]) == 64
    assert all(ch in "0123456789abcdef" for ch in payload["canonical_hash"])

    # The staged spool file exists for the worker to reference.
    assert (tmp_path / "spool" / body["attachment_id"]).exists()


def test_create_sanitizes_a_path_traversal_filename(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, filename="../../etc/passwd")
    assert resp.status_code == 202
    cmd = facade.submitted[0]
    # basename (never a path), then normalized to a `.jpg` extension consistent
    # with the sniffed image/jpeg content (Finding 8).
    assert cmd.payload["source_name"] == "passwd.jpg"


def test_create_replay_pending_returns_the_same_ids(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    first = _post_create(c).get_json()
    second = _post_create(c).get_json()
    assert first["ok"] is True
    assert second["ok"] is True
    assert second["command_id"] == first["command_id"]
    assert second["attachment_id"] == first["attachment_id"]
    assert second["replayed"] is True
    assert len(facade.submitted) == 1  # only one command was ever enqueued
    # The replay's freshly-staged file was discarded: exactly one spool file.
    assert [p.name for p in (tmp_path / "spool").iterdir()] == [first["attachment_id"]]


def test_create_replay_committed_returns_the_original_row(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    first = _post_create(c).get_json()
    cmd = facade.submitted[0]
    # Simulate the worker committing: clear the pending reservation and publish
    # it into the committed index + snapshot with the same canonical hash.
    facade._pending.remove("req-1")
    facade._committed["req-1"] = IdempotencyEntry(
        attachment_id=first["attachment_id"],
        canonical_hash=cmd.payload["canonical_hash"],
        created_at=0.0,
    )
    facade._snapshot = _snapshot([_attachment(id=first["attachment_id"], state=sender.DRAFT)])

    second = _post_create(c).get_json()
    assert second["ok"] is True
    assert second["attachment_id"] == first["attachment_id"]
    assert second["state"] == sender.DRAFT
    assert "command_id" not in second
    assert len(facade.submitted) == 1  # no second enqueue


def test_create_idempotency_conflict_is_409(monkeypatch, tmp_path):
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider()},
        spool_dir=tmp_path / "spool",
        committed={
            "req-1": IdempotencyEntry(
                attachment_id="c" * 32, canonical_hash="0" * 64, created_at=0.0
            )
        },
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "idempotency_conflict"
    assert facade.submitted == []
    # The freshly-staged file was discarded.
    assert list((tmp_path / "spool").iterdir()) == []


def test_create_queue_full_is_429(monkeypatch, tmp_path):
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool", queue_full=True
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 429
    assert resp.get_json()["error_code"] == "command_queue_full"
    assert facade.submitted == []


# ---- Finding 7: synchronous recipient-trust check --------------------------


def test_create_unknown_recipient_is_400_recipient_not_found(monkeypatch, tmp_path):
    # The recipient check runs *before* provider resolution and staging: with
    # no providers registered either, the unknown recipient is the error that
    # wins, proving the recipient check is a synchronous, pre-staging gate.
    facade = _FakeFacade(
        providers={},
        spool_dir=tmp_path / "spool",
        recipient=RecipientSnapshot(by_address={}),
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "recipient_not_found"
    assert facade.submitted == []
    assert not (tmp_path / "spool").exists()  # nothing was ever staged


@pytest.mark.parametrize("status", [AddressStatus.KEY_UNVERIFIED, AddressStatus.KEY_CHANGED])
def test_create_untrusted_recipient_is_400_recipient_not_trusted(monkeypatch, tmp_path, status):
    facade = _FakeFacade(
        providers={PROVIDER_ID: _provider()},
        spool_dir=tmp_path / "spool",
        recipient=RecipientSnapshot(
            by_address={
                "!aaaaaaaa": RecipientBindingSnapshot(
                    adapter_id="meshtastic",
                    transport_address="!aaaaaaaa",
                    key_id="1" * 16,
                    status=status,
                )
            }
        ),
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "recipient_not_trusted"
    assert facade.submitted == []


def test_create_rejects_unknown_recipient_synchronously_on_the_real_runtime(tmp_path):
    state = _started(tmp_path, "recipient-notfound")
    try:
        c = _build_client()
        resp = _post_create(c)
        # The real runtime has no binding for `!aaaaaaaa`, and the synchronous
        # check reads the worker-published snapshot (not SQLite) - so this is
        # a 400 before provider resolution/staging, on a stopped worker.
        assert resp.status_code == 400
        assert resp.get_json()["error_code"] == "recipient_not_found"
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_missing_csrf_token_is_403(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _csrf_client(monkeypatch, facade)
    resp = _post_create(c)  # no X-CSRF-Token header, no session token
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "csrf_invalid"
    assert facade.submitted == []


def test_create_valid_csrf_token_reaches_submission(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _csrf_client(monkeypatch, facade)
    _set_csrf_token(c, "session-token")
    resp = _post_create(c, headers={"X-CSRF-Token": "session-token"})
    assert resp.status_code == 202
    assert len(facade.submitted) == 1


# ---- Finding 8: MIME + filename validation ---------------------------------


@pytest.mark.parametrize(
    "data,filename,mime_type",
    [
        (_JPEG, "photo.jpg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "diagram.png", "image/png"),
        (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8, "image.webp", "image/webp"),
        (b"%PDF-1.4\n" + b"document", "doc.pdf", "application/pdf"),
        (b"hello world\nline two\n", "notes.txt", "text/plain"),
        (b"2026-01-01 INFO something happened\n", "app.log", "text/plain"),
        (b"a,b,c\n1,2,3\n", "data.csv", "text/csv"),
        (b'{"key": "value"}', "manifest.json", "application/json"),
    ],
)
def test_create_accepts_each_allowed_format(monkeypatch, tmp_path, data, filename, mime_type):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=data, filename=filename)
    assert resp.status_code == 202, resp.get_json()
    cmd = facade.submitted[0]
    assert cmd.payload["mime_type"] == mime_type


def test_create_normalizes_a_mismatched_extension_to_the_sniffed_mime(monkeypatch, tmp_path):
    # Content is a JPEG, but the browser named it `photo.png` - the MIME is
    # established from content, and the extension is rewritten to be consistent
    # (so the worker's later `is_allowed_extension` check cannot fail it).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=_JPEG, filename="photo.png")
    assert resp.status_code == 202
    cmd = facade.submitted[0]
    assert cmd.payload["mime_type"] == "image/jpeg"
    assert cmd.payload["source_name"] == "photo.jpg"


def test_create_ignores_a_misleading_browser_content_type(monkeypatch, tmp_path):
    # The browser claims `image/png` on the part, but the bytes are a JPEG:
    # content wins over the browser's Content-Type and over the extension.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = c.post(
        "/api/attachments",
        data={
            "file": (BytesIO(_JPEG), "photo.png", "image/png"),
            "metadata": json.dumps(_make_meta()),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 202
    cmd = facade.submitted[0]
    assert cmd.payload["mime_type"] == "image/jpeg"
    assert cmd.payload["source_name"] == "photo.jpg"


def test_create_rejects_nul_after_the_sniffed_head(monkeypatch, tmp_path):
    # A text file whose first 512 bytes are clean ASCII but which turns binary
    # after the sniff window must be rejected (full-stream NUL detection).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    data = b"a" * 512 + b"tail\x00tail"
    resp = _post_create(c, data=data, filename="notes.txt")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "mime_not_allowed"
    assert facade.submitted == []


def test_create_rejects_invalid_utf8_after_the_sniffed_head(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    data = b"a" * 512 + b"\xff\xfe"
    resp = _post_create(c, data=data, filename="notes.txt")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "mime_not_allowed"
    assert facade.submitted == []


@pytest.mark.parametrize("data", [b'{"broken": ', b"[1, 2,", b'{"a": 1} trailing'])
def test_create_rejects_invalid_json_document(monkeypatch, tmp_path, data):
    # A leading {/[ sniffs as JSON, but the whole document must parse.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=data, filename="manifest.json")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "mime_not_allowed"
    assert facade.submitted == []


def test_create_rejects_an_empty_file(monkeypatch, tmp_path):
    # Zero-byte policy: no content means no MIME can be established, so an
    # empty file is `mime_not_allowed`, not a silent `text/plain`.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=b"", filename="empty.txt")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "mime_not_allowed"
    assert facade.submitted == []


def test_create_comment_at_exactly_1000_bytes_is_accepted(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, meta=_make_meta(comment="a" * 1000))
    assert resp.status_code == 202
    assert facade.submitted[0].payload["comment"] == "a" * 1000


def test_create_comment_at_1001_bytes_is_rejected(monkeypatch, tmp_path):
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, meta=_make_meta(comment="a" * 1001))
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_metadata"
    assert facade.submitted == []


def test_create_truncates_an_overlong_extensionless_filename_to_255(monkeypatch, tmp_path):
    # The cap is applied to the *final* name (extension included): a 300-char
    # extensionless name is truncated only in its stem, so the result is
    # exactly 255 code points long and still carries the canonical `.txt`
    # (never 259, the pre-fix 255-then-`.txt` outcome).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=b"hello", filename="x" * 300)
    assert resp.status_code == 202
    source_name = facade.submitted[0].payload["source_name"]
    assert source_name == "x" * 251 + ".txt"
    assert len(source_name) == 255


def test_create_255_extensionless_filename_is_shortened_before_ext(monkeypatch, tmp_path):
    # A 255-char extensionless name cannot fit the `.txt` suffix; the stem is
    # shortened to 251 so the final name is 255, not 259.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=b"hello", filename="x" * 255)
    assert resp.status_code == 202
    source_name = facade.submitted[0].payload["source_name"]
    assert source_name == "x" * 251 + ".txt"
    assert len(source_name) == 255


def test_create_overlong_filename_keeps_a_correct_extension(monkeypatch, tmp_path):
    # A 304-char name already ending in the *correct* extension keeps it: the
    # stem is truncated, the `.txt` is retained, and the whole stays ≤ 255.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=b"hello", filename="x" * 300 + ".txt")
    assert resp.status_code == 202
    source_name = facade.submitted[0].payload["source_name"]
    assert source_name == "x" * 251 + ".txt"
    assert len(source_name) == 255


def test_create_filename_cap_is_code_points_not_bytes(monkeypatch, tmp_path):
    # 300 code points of a 2-byte character (600 bytes) are truncated to 251
    # *code points* (502 bytes) — pinning that the cap measures characters,
    # not UTF-8 bytes (a byte-measured cap would stop around 127 characters).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    resp = _post_create(c, data=b"hello", filename="é" * 300)
    assert resp.status_code == 202
    source_name = facade.submitted[0].payload["source_name"]
    assert source_name == "é" * 251 + ".txt"
    assert len(source_name) == 255


# ---- Finding 9: missing regression coverage --------------------------------
#
# The create path's remaining acceptance requirements that Step 1.6A.3B's first
# test pass left untested: the exact 5 MiB + 1 rejection, the chunked/no-whole-
# file-buffer staging property, the two concurrent idempotency races at the HTTP
# layer, the invalid-CSRF token for create (missing/valid already covered), the
# create path's own sanitized exception boundary with an injected secret marker,
# and that a create POST never blocks on the worker tick lock.


class _ChunkTrackingStream:
    """Wraps a byte stream to record how `_stage_spool_file` reads it, proving
    the staging loop streams in fixed-size chunks and never issues an unbounded
    whole-file `read()` (Finding 4: "never whole-file buffered")."""

    def __init__(self, data):
        self._io = BytesIO(data)
        self.requested_sizes = []
        self.saw_unbounded_read = False
        self.total_read = 0

    def read(self, size=-1):
        if size is None or size < 0:
            self.saw_unbounded_read = True
            self.requested_sizes.append(-1)
        else:
            self.requested_sizes.append(size)
        chunk = self._io.read(size)
        self.total_read += len(chunk)
        return chunk


def test_create_file_one_byte_over_cap_is_rejected(monkeypatch, tmp_path):
    # Exactly one byte past the 5 MiB cap (the cap is exclusive: `size > cap`,
    # not `>=`), distinct from the existing 5 MiB + 3 test.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    over = b"\xff\xd8\xff\xe0" + b"\x00" * (api_attachments._MAX_FILE_BYTES - 3)
    resp = _post_create(c, data=over)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "file_too_large"
    assert facade.submitted == []
    assert list((tmp_path / "spool").iterdir()) == []


def test_create_staging_streams_in_bounded_chunks_without_whole_file_buffering(tmp_path):
    # Finding 4: the staging loop reads the file in fixed-size chunks and never
    # calls an unbounded whole-file read (which would buffer it in memory).
    spool_dir = tmp_path / "spool"
    data = b"\xff\xd8\xff\xe0" + b"x" * (2 * 1024 * 1024)
    stream = _ChunkTrackingStream(data)
    path, _digest, head, size, _clean = api_attachments._stage_spool_file(
        SimpleNamespace(stream=stream), spool_dir, "a" * 32
    )
    assert stream.saw_unbounded_read is False
    assert stream.requested_sizes and all(
        s == api_attachments._STAGING_CHUNK_BYTES for s in stream.requested_sizes
    )
    assert stream.total_read == len(data)
    assert size == len(data)
    assert head == b"\xff\xd8\xff\xe0" + b"x" * (api_attachments._SNIFF_HEAD_BYTES - 4)
    path.unlink()  # the successful stage leaves its temp file; clean it up.


def test_create_staging_stops_one_chunk_past_the_cap(tmp_path):
    # Finding 4: an oversized file is rejected after reading at most one chunk
    # beyond the 5 MiB cap - the loop never drains the whole file.
    spool_dir = tmp_path / "spool"
    stream = _ChunkTrackingStream(b"\xff\xd8\xff\xe0" + b"x" * (6 * 1024 * 1024))
    with pytest.raises(api_attachments._FileTooLarge):
        api_attachments._stage_spool_file(SimpleNamespace(stream=stream), spool_dir, "b" * 32)
    assert stream.total_read <= api_attachments._MAX_FILE_BYTES + api_attachments._STAGING_CHUNK_BYTES
    assert stream.total_read < 6 * 1024 * 1024 + 4  # did not drain the whole file
    assert list(spool_dir.iterdir()) == []  # the discarded temp file was removed


def test_create_concurrent_identical_requests_enqueue_exactly_one_command(monkeypatch, tmp_path):
    # Two identical requests racing through the reservation lock: exactly one
    # command is enqueued, and both responses carry the same ids (the second is
    # a replay_pending with `replayed: True`).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    barrier = threading.Barrier(2)
    results = []

    def post():
        barrier.wait()
        results.append(_post_create(c))

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "concurrent create hung on the reservation lock"

    assert sorted(r.status_code for r in results) == [202, 202]
    bodies = [r.get_json() for r in results]
    assert bodies[0]["attachment_id"] == bodies[1]["attachment_id"]
    assert bodies[0]["command_id"] == bodies[1]["command_id"]
    assert len(facade.submitted) == 1


def test_create_concurrent_same_id_different_content_one_fresh_one_conflict(monkeypatch, tmp_path):
    # Same client_request_id, different content: one fresh (202) and one
    # conflict (409) - never two enqueues - and the conflict's staged file is
    # discarded, leaving exactly the fresh request's spool file.
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _client(monkeypatch, facade)
    barrier = threading.Barrier(2)
    results = []

    def post(data):
        barrier.wait()
        results.append(_post_create(c, data=data))

    threads = [
        threading.Thread(target=post, args=(_JPEG,)),
        threading.Thread(target=post, args=(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "concurrent create hung on the reservation lock"

    assert sorted(r.status_code for r in results) == [202, 409]
    conflict = next(r for r in results if r.status_code == 409)
    assert conflict.get_json()["error_code"] == "idempotency_conflict"
    assert len(facade.submitted) == 1
    assert len(list((tmp_path / "spool").iterdir())) == 1


def test_create_invalid_csrf_token_is_403(monkeypatch, tmp_path):
    # Missing and valid tokens are covered; this pins the invalid-token branch
    # for the create endpoint specifically (a wrong X-CSRF-Token never reaches
    # submission or staging).
    facade = _FakeFacade(providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool")
    c = _csrf_client(monkeypatch, facade)
    _set_csrf_token(c, "real-token")
    resp = _post_create(c, headers={"X-CSRF-Token": "wrong-token"})
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "csrf_invalid"
    assert facade.submitted == []
    assert not (tmp_path / "spool").exists()


def test_create_unexpected_exception_is_sanitized(monkeypatch, tmp_path, caplog):
    # The create route has its own sanitized boundary (same `_mca_error_boundary`
    # as the GET routes): an injected facade exception carrying a secret marker
    # must map to the stable 500 and never leak the marker to the wire or log.
    caplog.set_level(logging.ERROR)
    marker = "SECRET_MARKER_create_x9"

    class _RaisingRecipientFacade(_FakeFacade):
        def recipient_snapshot(self):
            raise RuntimeError(f"create-leak {marker}")

    facade = _RaisingRecipientFacade(
        providers={PROVIDER_ID: _provider()}, spool_dir=tmp_path / "spool"
    )
    c = _client(monkeypatch, facade)
    resp = _post_create(c)
    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": "Internal server error",
        "error_code": "internal_error",
    }
    assert marker not in resp.get_data(as_text=True)
    assert "Traceback" not in resp.get_data(as_text=True)
    assert marker not in caplog.text
    assert "Traceback" not in caplog.text
    assert not (tmp_path / "spool").exists()  # the exception fired before staging


def test_create_does_not_block_on_the_tick_lock(tmp_path):
    # A create POST must read only the worker-published snapshot and submit
    # through the in-memory facade - never the worker's tick lock. An empty
    # runtime means no recipient binding -> synchronous 400, not a hang.
    state = _started(tmp_path, "create-thread")
    try:
        c = _build_client()
        state.tick_lock.acquire()
        try:
            result = {}

            def post():
                result["resp"] = _post_create(c)

            poster = threading.Thread(target=post, name="create-request-thread")
            poster.start()
            poster.join(timeout=2.0)
            assert not poster.is_alive(), "create POST blocked on the worker tick lock"
        finally:
            state.tick_lock.release()

        assert result["resp"].status_code == 400
        assert result["resp"].get_json()["error_code"] == "recipient_not_found"
    finally:
        mca_runtime.reset_state_for_tests()


# ---- Correction 1: Flask floor + per-request multipart limit ----------------
#
# The create endpoint sets a per-request `request.max_content_length`, which
# Flask only exposes from 3.1 - so the requirements floor must reject 3.0.
# This test parses the *actual* requirements.txt (never a hardcoded copy), so
# a silent floor regression fails here rather than on a real Pi node.


def test_requirements_pin_flask_floor_above_30():
    from packaging.requirements import Requirement

    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8")
    flask_lines = [
        ln for ln in requirements.splitlines() if ln.strip().startswith("Flask")
    ]
    assert len(flask_lines) == 1, "expected exactly one Flask pin in requirements.txt"
    req = Requirement(flask_lines[0].strip())

    # Flask 3.0 does not expose `request.max_content_length` - it must be
    # excluded by the floor.
    assert not req.specifier.contains("3.0.0")
    assert not req.specifier.contains("3.0.5")
    # The supported range starts at 3.1 (the live-verified version is 3.1.3).
    assert req.specifier.contains("3.1.0")
    assert req.specifier.contains("3.1.3")
    # And it is still bounded below 4.0 (the next, untested major).
    assert not req.specifier.contains("4.0.0")


# ---- Step 1.6A.4: provider mutation routes (§7.11/§7.12) ------------------
#
# The eight mutation endpoints all return 202 + command_id (the worker runs
# them; the client polls the command), except where a *synchronous* pure
# validation error applies (invalid origin, probe not found/fingerprint
# mismatch/policy shape, invalid provider id, upload token too long, queue
# full, or not-ready). The synchronous checks are defense-in-depth only -
# the worker re-checks everything - so these tests pin exactly which
# validation happens *before* the command is enqueued (and thus returns a
# 400, not a 202), and that every accepted request enqueues a frozen
# Command of the right kind with the right payload and never leaks the
# upload token or the raw service key into any response body.


def _probe(*, fingerprint="ab" * 32, max_ciphertext_bytes=5 * 1024 * 1024 + 1024):
    """A minimal probe stand-in for phase-2 synchronous validation: the
    route reads only these two fields (the real `ProbeRecord`'s other
    fields come from the server, never the browser)."""
    return SimpleNamespace(
        service_key_fingerprint=fingerprint,
        max_ciphertext_bytes=max_ciphertext_bytes,
    )


def _submitted(facade, index=-1):
    """The `index`-th command the route handed to `facade.submit()`."""
    return facade.submitted[index]


def test_provider_mutations_are_503_when_facade_missing(monkeypatch):
    c = _client(monkeypatch, None)
    for method, path, body in (
        ("post", "/api/mca/providers/probe", {"base_url": "https://relay.example.net"}),
        ("post", "/api/mca/providers", {"probe_id": "p"}),
        ("patch", f"/api/mca/providers/{PROVIDER_ID}", {"display_name": "x"}),
        ("post", f"/api/mca/providers/{PROVIDER_ID}/default", {}),
        ("delete", f"/api/mca/providers/{PROVIDER_ID}", None),
        ("put", f"/api/mca/providers/{PROVIDER_ID}/upload-token", {"upload_token": "t"}),
        ("delete", f"/api/mca/providers/{PROVIDER_ID}/upload-token", None),
        ("post", f"/api/mca/providers/{PROVIDER_ID}/check", {}),
    ):
        resp = getattr(c, method)(path, json=body)
        assert resp.status_code == 503, (method, path)
        assert resp.get_json()["error_code"] == "mca_not_ready"


def test_probe_normalizes_and_enqueues(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.post("/api/mca/providers/probe", json={"base_url": "https://Relay.Example.Net/"})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["ok"] is True
    assert body["command_id"]
    cmd = _submitted(facade)
    assert cmd.kind == "provider_probe"
    # The worker re-normalizes the origin; the request thread passes the
    # browser's spelling through unchanged (defense-in-depth, not a trust
    # boundary).
    assert cmd.payload == {"base_url": "https://Relay.Example.Net/"}


@pytest.mark.parametrize(
    "base_url",
    [
        None,                    # missing
        "",                      # empty
        "   ",                   # whitespace-only
        123,                     # not a string
        "http://relay.example.net",  # not https
        "ftp://relay.example.net",   # wrong scheme
        "https://relay.example.net/path",  # not a bare origin
        "https://relay.example.net?q=1",   # query
        "https://user:pass@relay.example.net",  # credentials
    ],
)
def test_probe_rejects_non_https_bare_origin_synchronously(monkeypatch, base_url):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    body = {"base_url": base_url} if base_url is not None else {}
    resp = c.post("/api/mca/providers/probe", json=body)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_origin"
    assert facade.submitted == [], "invalid origin must never be enqueued"


def test_register_enqueues_policy_and_fingerprint(monkeypatch):
    facade = _FakeFacade(probes={"probe-1": _probe()})
    c = _client(monkeypatch, facade)
    resp = c.post("/api/mca/providers", json={
        "probe_id": "probe-1",
        "display_name": "My Relay",
        "fingerprint_confirmation": "ab" * 32,
        "policy": {"kind": "third_party", "upload_allowed": False, "download_allowed": True},
    })
    assert resp.status_code == 202
    cmd = _submitted(facade)
    assert cmd.kind == "provider_register"
    # Identity fields come from the probe (server-side), never the browser;
    # the browser only supplies probe_id + confirmation + policy.
    assert cmd.payload == {
        "probe_id": "probe-1",
        "display_name": "My Relay",
        "fingerprint_confirmation": "ab" * 32,
        "policy": {"kind": "third_party", "upload_allowed": False, "download_allowed": True},
    }


def test_register_rejects_unknown_probe_synchronously(monkeypatch):
    facade = _FakeFacade(probes={})
    c = _client(monkeypatch, facade)
    resp = c.post("/api/mca/providers", json={
        "probe_id": "missing",
        "display_name": "My Relay",
        "fingerprint_confirmation": "ab" * 32,
    })
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "probe_id_not_found"
    assert facade.submitted == []


def test_register_rejects_fingerprint_mismatch_synchronously(monkeypatch):
    facade = _FakeFacade(probes={"probe-1": _probe(fingerprint="cd" * 32)})
    c = _client(monkeypatch, facade)
    resp = c.post("/api/mca/providers", json={
        "probe_id": "probe-1",
        "display_name": "My Relay",
        "fingerprint_confirmation": "ab" * 32,
    })
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "provider_id_mismatch"
    assert facade.submitted == []


@pytest.mark.parametrize(
    "policy",
    [
        {"kind": "bogus"},                          # invalid kind
        {"kind": "own", "upload_allowed": "yes"},   # non-bool flag
        {"kind": "own", "download_allowed": 1},     # non-bool flag
        {"kind": "own", "max_ciphertext_bytes": 0},  # non-positive
        {"kind": "own", "max_ciphertext_bytes": 5 * 1024 * 1024 + 1025},  # exceeds probe limit
    ],
)
def test_register_rejects_invalid_policy_synchronously(monkeypatch, policy):
    facade = _FakeFacade(probes={"probe-1": _probe()})
    c = _client(monkeypatch, facade)
    resp = c.post("/api/mca/providers", json={
        "probe_id": "probe-1",
        "display_name": "My Relay",
        "fingerprint_confirmation": "ab" * 32,
        "policy": policy,
    })
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_metadata"
    assert facade.submitted == []


def test_update_enqueues_and_maps_null_to_clear_sentinel(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.patch(f"/api/mca/providers/{PROVIDER_ID}", json={
        "display_name": "Renamed",
        "min_ttl_seconds": None,
        "max_ttl_seconds": 3600,
        "protocol_version": None,
        "enabled": False,
    })
    assert resp.status_code == 202
    cmd = _submitted(facade)
    assert cmd.kind == "provider_update"
    assert cmd.payload["provider_id"] == PROVIDER_ID
    assert cmd.payload["display_name"] == "Renamed"
    assert cmd.payload["enabled"] is False
    assert cmd.payload["max_ttl_seconds"] == 3600
    # JSON `null` + present key -> the CLEAR sentinel (never a bare None,
    # which the worker treats as "leave alone").
    assert cmd.payload["min_ttl_seconds"] is CLEAR
    assert cmd.payload["protocol_version"] is CLEAR


def test_update_rejects_empty_body(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.patch(f"/api/mca/providers/{PROVIDER_ID}", json={})
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_metadata"
    assert facade.submitted == []


def test_update_rejects_malformed_provider_id(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.patch("/api/mca/providers/AAAA", json={"display_name": "x"})
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_provider_id"
    assert facade.submitted == []


def test_set_default_enqueues(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/mca/providers/{PROVIDER_ID}/default", json={})
    assert resp.status_code == 202
    cmd = _submitted(facade)
    assert cmd.kind == "provider_set_default"
    assert cmd.payload == {"provider_id": PROVIDER_ID}


def test_remove_enqueues(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.delete(f"/api/mca/providers/{PROVIDER_ID}")
    assert resp.status_code == 202
    cmd = _submitted(facade)
    assert cmd.kind == "provider_remove"
    assert cmd.payload == {"provider_id": PROVIDER_ID}


def test_set_upload_token_enqueues_without_echoing(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.put(f"/api/mca/providers/{PROVIDER_ID}/upload-token", json={"upload_token": "s3cret"})
    assert resp.status_code == 202
    body = resp.get_json()
    # The 202 body is only ok+command_id - the token never appears anywhere
    # in the response.
    assert "s3cret" not in json.dumps(body)
    cmd = _submitted(facade)
    assert cmd.kind == "provider_set_upload_token"
    assert cmd.payload == {"provider_id": PROVIDER_ID, "upload_token": "s3cret"}


def test_set_upload_token_rejects_overlong_synchronously(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.put(f"/api/mca/providers/{PROVIDER_ID}/upload-token", json={
        "upload_token": "x" * (MAX_UPLOAD_TOKEN_BYTES + 1),
    })
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "upload_token_too_long"
    assert facade.submitted == []


def test_set_upload_token_rejects_empty_synchronously(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.put(f"/api/mca/providers/{PROVIDER_ID}/upload-token", json={"upload_token": ""})
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_metadata"
    assert facade.submitted == []


def test_clear_upload_token_enqueues(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.delete(f"/api/mca/providers/{PROVIDER_ID}/upload-token")
    assert resp.status_code == 202
    cmd = _submitted(facade)
    assert cmd.kind == "provider_clear_upload_token"
    assert cmd.payload == {"provider_id": PROVIDER_ID}


# ---- Step 1.6A.5: GET content (§7.14) --------------------------------------


def test_content_inline_image_serves_mime_and_security_headers(monkeypatch, tmp_path):
    data = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    f = tmp_path / "photo.jpg"
    f.write_bytes(data)
    facade = _FakeFacade(
        snapshot=_snapshot([_attachment(
            direction="received", state="AVAILABLE", file_name="photo.jpg",
            mime_type="image/jpeg", plain_size=len(data),
            descriptor=_descriptor(mime_type="image/jpeg", locator="files/photo.jpg", plain_size=len(data)),
        )]),
        content_files={"files/photo.jpg": f},
    )
    c = _client(monkeypatch, facade)

    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")

    assert resp.status_code == 200
    assert resp.data == data
    assert resp.mimetype == "image/jpeg"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    cd = resp.headers["Content-Disposition"]
    assert "inline" in cd
    assert "photo.jpg" in cd


def test_content_non_inline_is_octet_stream_attachment(monkeypatch, tmp_path):
    # PDF is not in the inline preview set: served as application/octet-stream
    # with an attachment disposition, never sniffed/hinted as PDF.
    data = b"%PDF-1.4 " + b"\x00" * 16
    f = tmp_path / "report.pdf"
    f.write_bytes(data)
    facade = _FakeFacade(
        snapshot=_snapshot([_attachment(
            direction="received", state="AVAILABLE", file_name="report.pdf",
            mime_type="application/pdf", plain_size=len(data),
            descriptor=_descriptor(mime_type="application/pdf", locator="files/report.pdf", plain_size=len(data)),
        )]),
        content_files={"files/report.pdf": f},
    )
    c = _client(monkeypatch, facade)

    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")

    assert resp.status_code == 200
    assert resp.data == data
    assert resp.mimetype == "application/octet-stream"
    cd = resp.headers["Content-Disposition"]
    assert "attachment" in cd
    assert "report.pdf" in cd


def test_content_sanitizes_a_hostile_download_name(monkeypatch, tmp_path):
    data = b"\xff\xd8\xff\xe0" + b"\x00" * 8
    f = tmp_path / "photo.jpg"
    f.write_bytes(data)
    facade = _FakeFacade(
        snapshot=_snapshot([_attachment(
            direction="received", state="AVAILABLE", file_name="../../etc/passwd",
            mime_type="image/jpeg", plain_size=len(data),
            descriptor=_descriptor(mime_type="image/jpeg", locator="files/photo.jpg", plain_size=len(data)),
        )]),
        content_files={"files/photo.jpg": f},
    )
    c = _client(monkeypatch, facade)

    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")

    assert resp.status_code == 200
    cd = resp.headers["Content-Disposition"]
    assert "passwd" in cd
    assert "etc" not in cd and ".." not in cd


def test_content_409_not_available_when_descriptor_is_none(monkeypatch):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(
        direction="received", state="WAITING_CONSENT", descriptor=None,
    )]))
    c = _client(monkeypatch, facade)
    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "not_available"


def test_content_404_content_missing_on_locator_error(monkeypatch):
    # A descriptor whose locator does not resolve to a controlled file (the
    # fake raises ContentLocatorError) is served as 404 content_missing, never
    # the raw exception.
    facade = _FakeFacade(snapshot=_snapshot([_attachment(
        direction="received", state="AVAILABLE",
        descriptor=_descriptor(locator="files/does-not-exist.jpg"),
    )]))
    c = _client(monkeypatch, facade)
    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "content_missing"


def test_content_404_content_missing_when_file_gone(monkeypatch, tmp_path):
    # The locator resolves but the file is no longer on disk -> 404 content_missing.
    missing = tmp_path / "photo.jpg"  # never written
    facade = _FakeFacade(
        snapshot=_snapshot([_attachment(
            direction="received", state="AVAILABLE",
            descriptor=_descriptor(locator="files/photo.jpg"),
        )]),
        content_files={"files/photo.jpg": missing},
    )
    c = _client(monkeypatch, facade)
    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "content_missing"


def test_content_invalid_and_unknown_id(monkeypatch):
    c = _client(monkeypatch, _FakeFacade())
    assert c.get("/api/attachments/not-hex/content").status_code == 400
    assert c.get("/api/attachments/not-hex/content").get_json()["error_code"] == "invalid_attachment_id"
    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "attachment_not_found"


def test_content_is_503_when_facade_missing(monkeypatch):
    c = _client(monkeypatch, None)
    resp = c.get(f"/api/attachments/{ATTACHMENT_ID}/content")
    assert resp.status_code == 503
    assert resp.get_json()["error_code"] == "mca_not_ready"


# ---- Step 1.6A.5: save / revoke / local-content mutations ------------------


_SAVE_PATH = f"/api/attachments/{ATTACHMENT_ID}/save"
_REVOKE_PATH = f"/api/attachments/{ATTACHMENT_ID}/revoke"
_DELETE_LOCAL_PATH = f"/api/attachments/{ATTACHMENT_ID}/local-content"


def test_save_accepted_for_received_available(monkeypatch):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(
        direction="received", state=receiver.AVAILABLE,
    )]))
    c = _client(monkeypatch, facade)
    resp = c.post(_SAVE_PATH)
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, "attachment_save")


@pytest.mark.parametrize("direction,state", [
    ("received", receiver.WAITING_CONSENT),  # not yet AVAILABLE
    ("received", receiver.DOWNLOADING),
    ("received", receiver.REJECTED),
    ("sent", sender.DRAFT),
    ("sent", sender.SENT),
])
def test_save_rejected_unless_received_and_available(monkeypatch, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(_SAVE_PATH)
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "invalid_state_transition"
    assert facade.submitted == []


@pytest.mark.parametrize("state", [sender.SENT, sender.RECEIVED, sender.DOWNLOADED])
def test_revoke_accepted_for_sent_downloadable(monkeypatch, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction="sent", state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(_REVOKE_PATH)
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, "attachment_revoke")


@pytest.mark.parametrize("direction,state", [
    ("sent", sender.DRAFT),            # not yet sent
    ("sent", sender.READY_TO_SEND),
    ("sent", sender.REVOKED),          # terminal
    ("sent", sender.CANCELLED),
    ("sent", sender.EXPIRED),
    ("received", receiver.AVAILABLE),  # wrong direction
])
def test_revoke_rejected_unless_sent_and_downloadable(monkeypatch, direction, state):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(direction=direction, state=state)]))
    c = _client(monkeypatch, facade)
    resp = c.post(_REVOKE_PATH)
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "invalid_state_transition"
    assert facade.submitted == []


def test_delete_local_content_accepted_when_saved(monkeypatch):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(
        direction="received", state=receiver.AVAILABLE, saved=True,
    )]))
    c = _client(monkeypatch, facade)
    resp = c.delete(_DELETE_LOCAL_PATH)
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, "attachment_delete_local_content")


def test_delete_local_content_rejected_when_not_saved(monkeypatch):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(
        direction="received", state=receiver.AVAILABLE, saved=False,
    )]))
    c = _client(monkeypatch, facade)
    resp = c.delete(_DELETE_LOCAL_PATH)
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "not_saved"
    assert facade.submitted == []


@pytest.mark.parametrize("method,path", [
    ("post", _SAVE_PATH),
    ("post", _REVOKE_PATH),
    ("delete", _DELETE_LOCAL_PATH),
])
def test_mutations_reject_malformed_id(monkeypatch, method, path):
    c = _client(monkeypatch, _FakeFacade())
    resp = getattr(c, method)("/api/attachments/not-hex/" + path.rsplit("/", 1)[-1])
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_attachment_id"


@pytest.mark.parametrize("method,path", [
    ("post", _SAVE_PATH),
    ("post", _REVOKE_PATH),
    ("delete", _DELETE_LOCAL_PATH),
])
def test_mutations_unknown_id_is_404(monkeypatch, method, path):
    c = _client(monkeypatch, _FakeFacade())
    resp = getattr(c, method)(path)
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "attachment_not_found"


@pytest.mark.parametrize("method,path,record_kwargs", [
    ("post", _SAVE_PATH, {"direction": "received", "state": receiver.AVAILABLE}),
    ("post", _REVOKE_PATH, {"direction": "sent", "state": sender.SENT}),
    ("delete", _DELETE_LOCAL_PATH, {"direction": "received", "state": receiver.AVAILABLE, "saved": True}),
])
def test_mutations_queue_full_is_429(monkeypatch, method, path, record_kwargs):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(**record_kwargs)]), queue_full=True)
    c = _client(monkeypatch, facade)
    resp = getattr(c, method)(path)
    assert resp.status_code == 429
    assert resp.get_json()["error_code"] == "command_queue_full"
    assert facade.submitted == []


@pytest.mark.parametrize("method,path", [
    ("post", _SAVE_PATH),
    ("post", _REVOKE_PATH),
    ("delete", _DELETE_LOCAL_PATH),
])
def test_mutations_are_503_when_facade_missing(monkeypatch, method, path):
    c = _client(monkeypatch, None)
    resp = getattr(c, method)(path)
    assert resp.status_code == 503
    assert resp.get_json()["error_code"] == "mca_not_ready"


@pytest.mark.parametrize("method,path,record_kwargs,kind", [
    ("post", _SAVE_PATH, {"direction": "received", "state": receiver.AVAILABLE}, "attachment_save"),
    ("post", _REVOKE_PATH, {"direction": "sent", "state": sender.SENT}, "attachment_revoke"),
    ("delete", _DELETE_LOCAL_PATH,
     {"direction": "received", "state": receiver.AVAILABLE, "saved": True},
     "attachment_delete_local_content"),
])
def test_mutations_valid_csrf_token_reaches_submission(monkeypatch, method, path, record_kwargs, kind):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(**record_kwargs)]))
    c = _csrf_client(monkeypatch, facade)
    _set_csrf_token(c, "session-token")
    resp = getattr(c, method)(path, headers={"X-CSRF-Token": "session-token"})
    assert resp.status_code == 202
    _assert_202_accepted(resp.get_json(), facade, kind)


@pytest.mark.parametrize("method,path,record_kwargs", [
    ("post", _SAVE_PATH, {"direction": "received", "state": receiver.AVAILABLE}),
    ("post", _REVOKE_PATH, {"direction": "sent", "state": sender.SENT}),
    ("delete", _DELETE_LOCAL_PATH, {"direction": "received", "state": receiver.AVAILABLE, "saved": True}),
])
def test_mutations_missing_csrf_token_is_403(monkeypatch, method, path, record_kwargs):
    facade = _FakeFacade(snapshot=_snapshot([_attachment(**record_kwargs)]))
    c = _csrf_client(monkeypatch, facade)
    resp = getattr(c, method)(path)  # no X-CSRF-Token header, no session token
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "csrf_invalid"
    assert facade.submitted == []


def test_check_enqueues(monkeypatch):
    facade = _FakeFacade()
    c = _client(monkeypatch, facade)
    resp = c.post(f"/api/mca/providers/{PROVIDER_ID}/check", json={})
    assert resp.status_code == 202
    cmd = _submitted(facade)
    assert cmd.kind == "provider_check"
    assert cmd.payload == {"provider_id": PROVIDER_ID}


def test_provider_mutation_queue_full_is_429(monkeypatch):
    facade = _FakeFacade(queue_full=True)
    c = _client(monkeypatch, facade)
    resp = c.post("/api/mca/providers/probe", json={"base_url": "https://relay.example.net"})
    assert resp.status_code == 429
    assert resp.get_json()["error_code"] == "command_queue_full"
