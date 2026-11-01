"""FX1 Phase B (receiver): auto-accept with a per-sender quota, one store (files/), the comment on the received row, and the
magic-byte check on receive."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from meshsrv.attachments import identity, receiver, sender
from meshsrv.attachments.delivery.fakes import FakeTextAdapter, InMemoryEther
from test_receiver import (  # noqa: F401 - the fixtures and helpers of the receiver tests
    ADAPTER_ID, _bind_remote_sender, _build_offer, _raw_provider_id, conn, key_exchange, principal, provider_registry,
    registered_provider, relay_client, remote_sender, store, wsm,
)


def _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, content, *, comment=None, name="incoming.txt"):
    """A real encrypted upload addressed to `principal` (like test_receiver._create_real_transfer, with a comment)."""
    conn2, wsm2, principal2 = remote_sender
    source = tmp_path / ("src-" + uuid.uuid4().hex)
    source.write_bytes(content)
    attachment_id = sender.create_draft(
        conn2, wsm2, principal2, workspace_id="local", source_path=str(source), file_name=name, mime_type="text/plain",
        recipients=[sender.RecipientTarget(public_identity=principal.public_identity, key_id=principal.key_id)],
        adapter_id=ADAPTER_ID, connector_profile_id="default", route_type="DIRECT", route_id="receiver-under-test",
        provider_id=provider_id, comment=comment,
    )
    identities = {principal.key_id: principal.public_identity}
    adapter = FakeTextAdapter(InMemoryEther(), "remote-sender-addr")
    for _ in range(20):
        if sender.get_state(conn2, attachment_id) == sender.SENT:
            break
        sender.run_step(conn2, workspace_manager=wsm2, principal=principal2, recipient_identities=identities,
                        relay_client=relay_client, delivery_adapter=adapter, attachment_id=attachment_id)
    assert sender.get_state(conn2, attachment_id) == sender.SENT
    row = conn2.execute("SELECT transfer_id, hard_expires_at FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    raw = _build_offer(provider_id=provider_id, transfer_id=bytes.fromhex(row[0]), sender_principal=principal2,
                       sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=row[1])
    return raw


def _offer(conn, wsm, principal, provider_registry, key_exchange, raw, *, auto_accept):
    return receiver.handle_offer(conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
                                 key_exchange=key_exchange, raw_offer=raw, network_available=True, auto_accept=auto_accept)


def _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, attachment_id, *, auto_accept=True):
    return receiver.run_step(conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
                             key_exchange=key_exchange, network_available=True, attachment_id=attachment_id,
                             relay_client=relay_client, auto_accept=auto_accept)


def _seed_auto_accepted(conn, principal, sender_principal_id, *, count, plain_size, at, state="AVAILABLE"):
    """Rows that were auto-accepted `at` (epoch seconds), so the quota has something to count."""
    for _ in range(count):
        attachment_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, sender_principal_id, provider_id, state, "
            "created_at, hard_expires_at, download_grace_seconds, plain_size) VALUES (?, ?, ?, 'received', ?, ?, ?, ?, ?, ?, ?, ?)",
            (attachment_id, principal.workspace_id, uuid.uuid4().hex, principal.principal_id, sender_principal_id,
             "AAAAAAAAAAA", state, at, at + 3600, 3600, plain_size),
        )
        conn.execute(
            "INSERT INTO attachment_events (id, attachment_id, occurred_at, event_type, detail_json) VALUES (?, ?, ?, 'auto_accepted', '{}')",
            (uuid.uuid4().hex, attachment_id, at),
        )
    conn.commit()


def _setup(conn, remote_sender, registered_provider):
    _, _, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    return principal2, _raw_provider_id(registered_provider)


def test_auto_accept_downloads_without_a_tap_into_files_with_the_comment(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    _, provider_id = _setup(conn, remote_sender, registered_provider)
    content = os.urandom(5000)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, content, comment="Please read before Friday")
    result = _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True)
    assert result.state == receiver.DOWNLOADING, "a trusted sender's offer goes straight to the download"
    assert conn.execute("SELECT COUNT(*) FROM attachment_events WHERE attachment_id = ? AND event_type = 'auto_accepted'",
                        (result.attachment_id,)).fetchone()[0] == 1

    final = _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, result.attachment_id)
    assert final.state == receiver.AVAILABLE
    row = conn.execute("SELECT saved_path, draft_comment FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()
    saved = Path(row[0])
    assert saved.parent == wsm.paths(principal.principal_id).files, "one store: straight into files/, not the cache"
    assert not any(wsm.paths(principal.principal_id).cache_incoming.iterdir())
    assert saved.read_bytes() == content
    assert row[1] == "Please read before Friday", "the sender's comment is kept on the received row"


def test_without_auto_accept_the_offer_still_waits_for_a_tap(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    _, provider_id = _setup(conn, remote_sender, registered_provider)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, b"abc")
    assert _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=False).state == receiver.WAITING_CONSENT


def test_an_untrusted_sender_is_never_auto_accepted(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    _, _, principal2 = remote_sender
    provider_id = _raw_provider_id(registered_provider)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, b"abc")
    _bind_remote_sender(conn, principal2, confirmed=False)                       # a key nobody confirmed
    assert _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True).state == receiver.WAITING_KEY


def test_twenty_files_in_a_day_from_one_sender_hold_the_next_one_for_a_manual_download(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    principal2, provider_id = _setup(conn, remote_sender, registered_provider)
    _seed_auto_accepted(conn, principal, principal2.principal_id, count=receiver.AUTO_ACCEPT_MAX_FILES, plain_size=10, at=time.time() - 60)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, b"one more")
    result = _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True)
    assert result.state == receiver.WAITING_CONSENT, "above the quota the offer waits; it is not refused"
    assert conn.execute("SELECT error_code FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()[0] == receiver.AUTO_ACCEPT_QUOTA_CODE

    # a person presses Download: the quota no longer applies, the code is cleared
    receiver.begin_download(conn, result.attachment_id)
    final = _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, result.attachment_id)
    assert final.state == receiver.AVAILABLE
    assert conn.execute("SELECT error_code FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()[0] is None


def test_the_quota_is_per_sender_and_per_24_hours(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    principal2, provider_id = _setup(conn, remote_sender, registered_provider)
    # 20 files from ANOTHER sender, and 20 from this one but more than 24 hours ago: neither counts
    _seed_auto_accepted(conn, principal, "someone-else", count=20, plain_size=10, at=time.time() - 60)
    _seed_auto_accepted(conn, principal, principal2.principal_id, count=20, plain_size=10, at=time.time() - 86400 - 120)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, b"fresh")
    assert _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True).state == receiver.DOWNLOADING


def test_fifty_mebibytes_in_a_day_hold_the_next_file_before_any_chunk_is_fetched(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    principal2, provider_id = _setup(conn, remote_sender, registered_provider)
    _seed_auto_accepted(conn, principal, principal2.principal_id, count=1, plain_size=receiver.AUTO_ACCEPT_MAX_BYTES - 100, at=time.time() - 60)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, os.urandom(4000))
    result = _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True)
    assert result.state == receiver.DOWNLOADING, "the size is only known once the relay descriptor is read"
    fetched = []
    original = relay_client.get_chunk
    relay_client.get_chunk = lambda *a, **k: (fetched.append(a), original(*a, **k))[1]
    held = _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, result.attachment_id)
    assert held.state == receiver.WAITING_CONSENT
    assert fetched == [], "no chunk was fetched"
    assert conn.execute("SELECT error_code FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()[0] == receiver.AUTO_ACCEPT_QUOTA_CODE
    assert not list(wsm.paths(principal.principal_id).files.iterdir())

    receiver.begin_download(conn, result.attachment_id)
    assert _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, result.attachment_id).state == receiver.AVAILABLE


def test_an_image_that_is_not_an_image_is_never_stored(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path, monkeypatch
):
    _, provider_id = _setup(conn, remote_sender, registered_provider)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, b"not a picture at all")
    result = _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True)
    # the header says text/plain; the bytes look like a PNG: the sender's claim and the content disagree
    monkeypatch.setattr(receiver.mime_allowlist, "sniff_mime_type", lambda head: "image/png")
    final = _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, result.attachment_id)
    assert final.state == receiver.FAILED
    assert conn.execute("SELECT error_code FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()[0] == "type_mismatch"
    assert not list(wsm.paths(principal.principal_id).files.iterdir()), "nothing was written"


def test_a_text_file_is_not_touched_by_the_image_check(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    _, provider_id = _setup(conn, remote_sender, registered_provider)
    raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, b"plain text")
    result = _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True)
    assert _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, result.attachment_id).state == receiver.AVAILABLE


def test_a_second_file_with_the_same_name_gets_its_own_name_in_files(
    conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path
):
    _, provider_id = _setup(conn, remote_sender, registered_provider)
    names = []
    for index in range(2):
        raw = _transfer(remote_sender, principal, relay_client, provider_id, tmp_path, b"same name %d" % index)
        result = _offer(conn, wsm, principal, provider_registry, key_exchange, raw, auto_accept=True)
        _step(conn, wsm, principal, provider_registry, key_exchange, relay_client, result.attachment_id)
        names.append(Path(conn.execute("SELECT saved_path FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()[0]).name)
    assert names == ["incoming.txt", "incoming (2).txt"]


def test_received_comment_is_cleaned_and_capped():
    assert receiver._clean_received_comment("  hello\x07 there\n second line  ") == "hello there\n second line"
    assert receiver._clean_received_comment("   ") is None and receiver._clean_received_comment(None) is None
    long = "ä" * 2000
    cleaned = receiver._clean_received_comment(long)
    assert len(cleaned.encode("utf-8")) <= receiver.MAX_RECEIVED_COMMENT_BYTES and cleaned.startswith("ä")
