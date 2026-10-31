"""MCA-VAULT PR 4b: receive and send under the vault. Incoming content goes straight into `<id>.mcv` and the outgoing spool is
read from `<id>.mcv`; no plaintext byte is written to the disk at any point, temp files included."""
from __future__ import annotations

import base64
import builtins
import contextlib
import hashlib
import io
import os
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from _vault_spy import MARKER, no_plaintext_writes
from meshsrv.attachments import codec, identity, receiver, sender
from meshsrv.attachments.db import migrations
from meshsrv.attachments.delivery.fakes import FakeTextAdapter, InMemoryEther
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.attachments.relay_client import RelayClient
from meshsrv.attachments.vault import header, kdf, passphrase
from meshsrv.attachments.vault.codec import VaultCodec, VaultConnection
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.runtime import VaultRuntime
from meshsrv.attachments.workspace import MCAWorkspaceManager

PASS = "correct horse battery staple 42"
FLOOR = kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT)
BASE_URL = "https://mock-relay.test"
ADAPTER_ID = "fake-text"


def make_content(n_marker: int = 12000) -> bytes:
    return MARKER * n_marker                       # 360 KB of an unmistakable plaintext


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class _Shim:
    def __init__(self, r):
        self.status_code, self.headers, self.content = r.status_code, r.headers, r.data
        self._r = r

    @property
    def text(self):
        return self._r.get_data(as_text=True)

    def json(self):
        return self._r.get_json()


class _Session:
    def __init__(self, client):
        self._client = client

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        return _Shim(self._client.open(url[len(BASE_URL):], method=method, json=json, data=data, headers=headers or {}))


class Workspace:
    """A workspace + principal + VaultConnection, optionally with a vault header and an unlocked vault."""

    def __init__(self, tmp_path, name, *, vault: bool):
        self.tmp = tmp_path / name
        self.tmp.mkdir()
        self.conn = sqlite3.connect(str(self.tmp / "attachments.db"), factory=VaultConnection, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        migrations.migrate(self.conn)
        self.wsm = MCAWorkspaceManager(str(self.tmp / "data"))
        self.principal = identity.ensure_principal(self.conn, self.wsm, "local")
        self.root = self.wsm.paths(self.principal.principal_id).root
        self.logs = []
        self.vault = None
        if vault:
            created = header.create(passphrase.normalize(PASS), FLOOR)
            (self.root / "vault.json").write_text(created.header.to_json(), encoding="utf-8")
            self.vault = VaultRuntime(self.root, log=lambda *a: self.logs.append(a), clock=_Clock(), free_ram=lambda: 4 * 1024 ** 3)
            self.conn.codec = self.wsm.codec = VaultCodec(self.vault)
            self.vault.unlock(passphrase=PASS)
            # what the migration leaves behind for the identity: the seed as <name>.mcv, no plain file
            seed_path = self.wsm.paths(self.principal.principal_id).keys / self.principal.private_key_file
            self.wsm.codec.write_secret_file(seed_path, seed_path.read_bytes())

    def every_file(self):
        out = {}
        for base, _dirs, files in os.walk(str(self.tmp)):
            for name in files:
                out[str(Path(base, name))] = Path(base, name).read_bytes()
        return out


@pytest.fixture
def store():
    return MockRelayStore(base_url=BASE_URL)


@pytest.fixture
def relay_client(store):
    return RelayClient(BASE_URL, upload_access_token=store.upload_access_token,
                       session=_Session(create_mock_relay_app(store).test_client()), sleep=lambda _s: None)


def _raw_provider_id(profile) -> bytes:
    return base64.urlsafe_b64decode(profile.provider_id + "=" * (-len(profile.provider_id) % 4))


def _bind(receiver_ws, sender_ws):
    now = time.time()
    receiver_ws.conn.execute(
        "INSERT INTO mca_recipient_bindings (id, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity, "
        "key_epoch, bound_at, tofu_confirmed_at) VALUES (?, 'local', ?, 'remote-addr', ?, ?, ?, 0, ?, ?)",
        (uuid.uuid4().hex, ADAPTER_ID, sender_ws.principal.principal_id, sender_ws.principal.key_id,
         sender_ws.principal.public_identity.hex(), now, now))
    receiver_ws.conn.commit()


def _send_to(sender_ws, receiver_ws, relay_client, provider_id, content, *, name="incoming.txt"):
    """Drive a genuine encrypted upload from `sender_ws` to SENT (its source staged through ITS codec) and return the
    transfer id and the signed offer the receiver gets."""
    staged_id = uuid.uuid4().hex
    spool = sender_ws.wsm.paths(sender_ws.principal.principal_id).spool_outgoing
    source = sender_ws.wsm.codec.write_content(spool / staged_id, content, staged_id)
    attachment_id = sender.create_draft(
        sender_ws.conn, sender_ws.wsm, sender_ws.principal, workspace_id="local", attachment_id=staged_id, source_path=str(source),
        file_name=name, mime_type="text/plain",
        recipients=[sender.RecipientTarget(public_identity=receiver_ws.principal.public_identity, key_id=receiver_ws.principal.key_id)],
        adapter_id=ADAPTER_ID, connector_profile_id="default", route_type="DIRECT", route_id="receiver-under-test", provider_id=provider_id)
    identities = {receiver_ws.principal.key_id: receiver_ws.principal.public_identity}
    adapter = FakeTextAdapter(InMemoryEther(), "remote-sender-addr")
    for _ in range(20):
        if sender.get_state(sender_ws.conn, attachment_id) == sender.SENT:
            break
        sender.run_step(sender_ws.conn, workspace_manager=sender_ws.wsm, principal=sender_ws.principal, recipient_identities=identities,
                        relay_client=relay_client, delivery_adapter=adapter, attachment_id=attachment_id)
    assert sender.get_state(sender_ws.conn, attachment_id) == sender.SENT
    row = sender_ws.conn.execute("SELECT transfer_id, hard_expires_at FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    transfer_id = bytes.fromhex(row[0])
    fields = codec.OfferFields(provider_id=provider_id, transfer_id=transfer_id, sender_key_id=bytes.fromhex(sender_ws.principal.key_id), kind=0,
                               size_bucket=1, hard_expires_at=row[1], flags=0)
    signing_key = identity.load_signing_key(sender_ws.wsm, sender_ws.principal)
    return attachment_id, transfer_id, codec.encode_offer(fields, signing_key)


def _receive(receiver_ws, provider_registry, key_exchange, raw_offer, relay_client):
    result = receiver.handle_offer(receiver_ws.conn, workspace_manager=receiver_ws.wsm, principal=receiver_ws.principal,
                                   provider_registry=provider_registry, key_exchange=key_exchange, raw_offer=raw_offer, network_available=True)
    assert result.state == receiver.WAITING_CONSENT
    receiver.begin_download(receiver_ws.conn, result.attachment_id)
    return result.attachment_id


def _step(receiver_ws, provider_registry, key_exchange, attachment_id, relay_client):
    return receiver.run_step(receiver_ws.conn, workspace_manager=receiver_ws.wsm, principal=receiver_ws.principal,
                             provider_registry=provider_registry, key_exchange=key_exchange, network_available=True,
                             attachment_id=attachment_id, relay_client=relay_client)


# ---- the content API of the codec --------------------------------------------------------------------------------------

def test_content_is_written_encrypted_with_no_plain_twin_and_read_back(tmp_path):
    ws = Workspace(tmp_path, "c1", vault=True)
    cache = ws.wsm.paths(ws.principal.principal_id).cache_incoming
    content = make_content()
    with no_plaintext_writes():
        stored = ws.wsm.codec.write_content(cache / ("aa" * 16), content, "aa" * 16)
    assert stored.name == "aa" * 16 + ".mcv" and not (cache / ("aa" * 16)).exists() and not list(cache.glob("*.part"))
    assert MARKER not in stored.read_bytes()
    with ws.wsm.codec.open_content(stored, "aa" * 16) as handle:
        assert handle.read() == content
    assert ws.wsm.codec.content_plain_size(stored) == len(content)
    size, digest = ws.wsm.codec.content_size_and_sha256(stored, "aa" * 16)
    assert (size, digest) == (len(content), hashlib.sha256(content).hexdigest())
    ws.wsm.codec.unlink_content(cache / ("aa" * 16))
    assert not stored.exists()


def test_content_is_bound_to_its_attachment_and_fails_closed(tmp_path):
    ws = Workspace(tmp_path, "c2", vault=True)
    cache = ws.wsm.paths(ws.principal.principal_id).cache_incoming
    stored = ws.wsm.codec.write_content(cache / ("aa" * 16), b"secret content", "aa" * 16)
    with pytest.raises(VaultError) as info:
        ws.wsm.codec.open_content(stored, "bb" * 16)                 # moved to another attachment: does not verify
    assert info.value.code == "vault_corrupt"
    data = bytearray(stored.read_bytes())
    data[-1] ^= 1
    stored.write_bytes(bytes(data))
    with pytest.raises(VaultError) as info:
        ws.wsm.codec.open_content(stored, "aa" * 16)
    assert info.value.code == "vault_corrupt"
    plain = cache / ("cc" * 16)
    plain.write_bytes(b"plain leftover")
    with pytest.raises(VaultError) as info:
        ws.wsm.codec.open_content(plain, "cc" * 16)                  # a plain content file next to an enabled vault
    assert info.value.code == "vault_corrupt" and "PLAIN" not in ws.logs.__repr__()
    ws.vault.lock()
    with pytest.raises(VaultError) as info:
        ws.wsm.codec.write_content(cache / ("dd" * 16), b"x", "dd" * 16)
    assert info.value.code == "vault_locked" and not (cache / ("dd" * 16)).exists() and not (cache / ("dd" * 16 + ".mcv")).exists()


def test_without_a_vault_content_is_exactly_what_it_always_was(tmp_path):
    ws = Workspace(tmp_path, "c3", vault=False)
    cache = ws.wsm.paths(ws.principal.principal_id).cache_incoming
    stored = ws.wsm.codec.write_content(cache / ("aa" * 16), b"plain bytes", "aa" * 16)
    assert stored == cache / ("aa" * 16) and stored.read_bytes() == b"plain bytes"
    with ws.wsm.codec.open_content(stored, "aa" * 16) as handle:
        assert handle.read() == b"plain bytes"
    assert ws.wsm.codec.content_plain_size(stored) == len(b"plain bytes")


# ---- receive under the vault ----------------------------------------------------------------------------------------------

def _world(tmp_path, store, *, receiver_vault, sender_vault):
    rws = Workspace(tmp_path, "receiver", vault=receiver_vault)
    sws = Workspace(tmp_path, "sender", vault=sender_vault)
    registry = ProviderRegistry(rws.conn, "local")
    profile = registry.register(display_name="Mock Relay", base_url=BASE_URL, service_public_key=store.service_public_key,
                                max_ciphertext_bytes=10_000_000)
    exchange = KeyExchangeCoordinator(rws.conn, rws.wsm, rws.principal, ADAPTER_ID)
    _bind(rws, sws)
    return rws, sws, registry, exchange, _raw_provider_id(profile)


def test_an_incoming_file_goes_straight_into_an_mcv_and_no_plaintext_is_ever_written(tmp_path, store, relay_client):
    rws, sws, registry, exchange, provider_id = _world(tmp_path, store, receiver_vault=True, sender_vault=False)
    content = make_content()
    _aid, _tid, raw_offer = _send_to(sws, rws, relay_client, provider_id, content)
    attachment_id = _receive(rws, registry, exchange, raw_offer, relay_client)
    with no_plaintext_writes():
        final = _step(rws, registry, exchange, attachment_id, relay_client)
    assert final.state == receiver.AVAILABLE
    row = rws.conn.execute("SELECT file_name, saved_path, plain_size, plain_sha256 FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    cache = rws.wsm.paths(rws.principal.principal_id).cache_incoming
    assert row[1] == str(cache / (attachment_id + ".mcv")) and row[0].startswith("v1:") and row[2] == len(content)
    assert rws.wsm.codec.open("attachments", "file_name", attachment_id, row[0]) == "incoming.txt"
    with rws.wsm.codec.open_content(Path(row[1]), attachment_id) as handle:
        assert handle.read() == content
    assert sorted(p.name for p in cache.iterdir()) == [attachment_id + ".mcv"], "only the encrypted file: no plain file, no .part"
    for name, data in rws.every_file().items():                     # nothing anywhere on the receiver's disk holds the plaintext
        assert MARKER not in data and b"incoming.txt" not in data, name


def test_a_corrupt_chunk_is_quarantined_encrypted(tmp_path, store, relay_client):
    rws, sws, registry, exchange, provider_id = _world(tmp_path, store, receiver_vault=True, sender_vault=False)
    _aid, transfer_id, raw_offer = _send_to(sws, rws, relay_client, provider_id, make_content())
    obj = store._fetch_object(transfer_id)
    first = sorted(obj.chunks.keys())[0]
    corrupted = bytearray(obj.chunks[first].data)
    corrupted[0] ^= 0xFF
    obj.chunks[first].data = bytes(corrupted)
    attachment_id = _receive(rws, registry, exchange, raw_offer, relay_client)
    final = _step(rws, registry, exchange, attachment_id, relay_client)
    assert final.state == receiver.FAILED
    quarantine = rws.wsm.paths(rws.principal.principal_id).quarantine
    names = sorted(p.name for p in quarantine.iterdir())
    assert names == [transfer_id.hex() + ".bin.mcv"], names
    stored = quarantine / names[0]
    assert bytes(corrupted) not in stored.read_bytes()
    with rws.wsm.codec.open_content(stored, "quarantine/%s.bin" % transfer_id.hex()) as handle:
        assert handle.read() == bytes(corrupted), "the migration's name and associated data: disable can restore it"


def test_a_locked_vault_makes_the_download_wait_not_fail_and_it_finishes_after_unlock(tmp_path, store, relay_client):
    rws, sws, registry, exchange, provider_id = _world(tmp_path, store, receiver_vault=True, sender_vault=False)
    content = make_content()
    _aid, _tid, raw_offer = _send_to(sws, rws, relay_client, provider_id, content)
    attachment_id = _receive(rws, registry, exchange, raw_offer, relay_client)
    before = rws.conn.execute("SELECT state, error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    rws.vault.lock()
    files_before = sorted(rws.every_file())
    for _ in range(3):                                              # repeated steps while locked: same answer each time
        with pytest.raises(VaultError) as info:
            _step(rws, registry, exchange, attachment_id, relay_client)
        assert info.value.code == "vault_locked"
    after = rws.conn.execute("SELECT state, error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert after == before and after[0] != receiver.FAILED, "state unchanged, never FAILED"
    assert sorted(rws.every_file()) == files_before, "nothing was written while locked"
    rws.vault.unlock(passphrase=PASS)
    assert _step(rws, registry, exchange, attachment_id, relay_client).state == receiver.AVAILABLE


# ---- send under the vault ----------------------------------------------------------------------------------------------------

def test_a_file_sent_from_a_vault_workspace_is_read_from_its_mcv_and_arrives_intact(tmp_path, store, relay_client):
    rws, sws, registry, exchange, provider_id = _world(tmp_path, store, receiver_vault=False, sender_vault=True)
    content = make_content()
    with no_plaintext_writes():
        attachment_id, _tid, raw_offer = _send_to(sws, rws, relay_client, provider_id, content, name="quarterly-report.txt")
    spool = sws.wsm.paths(sws.principal.principal_id).spool_outgoing
    assert sorted(p.name for p in spool.iterdir()) == [attachment_id + ".mcv"]
    received = _receive(rws, registry, exchange, raw_offer, relay_client)
    assert _step(rws, registry, exchange, received, relay_client).state == receiver.AVAILABLE
    saved = rws.conn.execute("SELECT saved_path FROM attachments WHERE id = ?", (received,)).fetchone()[0]
    assert Path(saved).read_bytes() == content, "the plain receiver got exactly the plaintext"
    for name, data in sws.every_file().items():
        assert MARKER not in data and b"quarterly-report.txt" not in data, name


def test_sending_from_a_locked_vault_waits_and_changes_nothing(tmp_path, store, relay_client):
    rws, sws, registry, exchange, provider_id = _world(tmp_path, store, receiver_vault=False, sender_vault=True)
    staged_id = uuid.uuid4().hex
    spool = sws.wsm.paths(sws.principal.principal_id).spool_outgoing
    source = sws.wsm.codec.write_content(spool / staged_id, make_content(), staged_id)
    attachment_id = sender.create_draft(
        sws.conn, sws.wsm, sws.principal, workspace_id="local", attachment_id=staged_id, source_path=str(source), file_name="a.txt",
        mime_type="text/plain", recipients=[sender.RecipientTarget(public_identity=rws.principal.public_identity, key_id=rws.principal.key_id)],
        adapter_id=ADAPTER_ID, connector_profile_id="default", route_type="DIRECT", route_id="r", provider_id=provider_id)
    identities = {rws.principal.key_id: rws.principal.public_identity}
    sender.run_step(sws.conn, workspace_manager=sws.wsm, principal=sws.principal, recipient_identities=identities,
                    relay_client=relay_client, delivery_adapter=FakeTextAdapter(InMemoryEther(), "x"), attachment_id=attachment_id)
    assert sender.get_state(sws.conn, attachment_id) == sender.VALIDATING      # the next step reads the content
    sws.vault.lock()
    before = sws.conn.execute("SELECT state, error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    for _ in range(3):
        with pytest.raises(VaultError) as info:
            sender.run_step(sws.conn, workspace_manager=sws.wsm, principal=sws.principal, recipient_identities={},
                            relay_client=relay_client, delivery_adapter=FakeTextAdapter(InMemoryEther(), "x"), attachment_id=attachment_id)
        assert info.value.code == "vault_locked"
    assert sws.conn.execute("SELECT state, error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone() == before
    sws.vault.unlock(passphrase=PASS)
    for _ in range(20):
        if sender.get_state(sws.conn, attachment_id) == sender.SENT:
            break
        sender.run_step(sws.conn, workspace_manager=sws.wsm, principal=sws.principal, recipient_identities=identities,
                        relay_client=relay_client, delivery_adapter=FakeTextAdapter(InMemoryEther(), "x"), attachment_id=attachment_id)
    assert sender.get_state(sws.conn, attachment_id) == sender.SENT
