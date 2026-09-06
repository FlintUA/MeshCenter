"""tests/test_manifest.py — meshsrv/attachments/manifest.py (ADR-0006).

Round-trips the manifest blob format and sealed-envelope sealing/opening,
plus the tamper/wrong-recipient rejection cases the ADR-0006 trust chain
relies on. No Relay, no database - pure encode/decode + crypto.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from nacl.signing import SigningKey

from meshsrv.attachments import crypto, identity, manifest


def _make_recipient():
    sk = SigningKey.generate()
    pub = bytes(sk.verify_key)
    key_id = bytes.fromhex(identity.compute_key_id(pub))
    return sk, pub, key_id


def _make_secret(chunk_count: int = 1) -> manifest.RecipientSecret:
    return manifest.RecipientSecret(
        data_key=crypto.generate_data_key(),
        nonce_prefix=crypto.generate_nonce_prefix(),
        receipt_secret=os.urandom(32),
        chunk_count=chunk_count,
    )


def test_seal_and_open_recipient_secret_round_trip():
    sk, pub, key_id = _make_recipient()
    secret = _make_secret()

    sealed = manifest.seal_recipient_secret(secret, pub)
    opened = manifest.open_recipient_secret(sealed, sk)

    assert opened == secret


def test_open_recipient_secret_rejects_wrong_recipient_key():
    sk, pub, _ = _make_recipient()
    other_sk, _, _ = _make_recipient()
    sealed = manifest.seal_recipient_secret(_make_secret(), pub)

    with pytest.raises(manifest.ManifestError):
        manifest.open_recipient_secret(sealed, other_sk)


def test_open_recipient_secret_rejects_tampered_ciphertext():
    sk, pub, _ = _make_recipient()
    sealed = bytearray(manifest.seal_recipient_secret(_make_secret(), pub))
    sealed[-1] ^= 0xFF

    with pytest.raises(manifest.ManifestError):
        manifest.open_recipient_secret(bytes(sealed), sk)


def test_build_manifest_blob_requires_at_least_one_recipient():
    with pytest.raises(manifest.ManifestError):
        manifest.build_manifest_blob(
            transfer_id=os.urandom(16),
            data_key=crypto.generate_data_key(),
            nonce_prefix=crypto.generate_nonce_prefix(),
            header=manifest.ManifestHeader(
                file_name="a", mime_type="text/plain", plain_size=0,
                plain_sha256=hashlib.sha256(b"").digest(), chunk_count=1,
            ),
            recipients=[],
        )


def test_full_manifest_round_trip_single_recipient():
    sk, pub, key_id = _make_recipient()
    transfer_id = os.urandom(16)
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    plaintext = b"file contents"
    header = manifest.ManifestHeader(
        file_name="notes.txt", mime_type="text/plain", plain_size=len(plaintext),
        plain_sha256=hashlib.sha256(plaintext).digest(), chunk_count=1, comment="hi",
    )
    secret = manifest.RecipientSecret(
        data_key=data_key, nonce_prefix=nonce_prefix, receipt_secret=os.urandom(32), chunk_count=1,
    )
    sealed = manifest.seal_recipient_secret(secret, pub)
    envelope = manifest.RecipientEnvelope(recipient_key_id=key_id, sealed_envelope=sealed)

    blob = manifest.build_manifest_blob(
        transfer_id=transfer_id, data_key=data_key, nonce_prefix=nonce_prefix, header=header, recipients=[envelope],
    )

    # Receiver side: this is exactly the sequence ADR-0006 describes after
    # the blob's own SHA-256 has already been checked against
    # ObjectDescriptor.manifest_sha256 (out of scope for this module).
    parsed = manifest.parse_manifest_blob(blob)
    assert parsed.transfer_id == transfer_id
    found = manifest.find_recipient_envelope(parsed, key_id)
    opened_secret = manifest.open_recipient_secret(found.sealed_envelope, sk)
    assert opened_secret.data_key == data_key
    assert opened_secret.nonce_prefix == nonce_prefix

    decoded_header = manifest.decrypt_manifest_header(
        parsed, data_key=opened_secret.data_key, nonce_prefix=opened_secret.nonce_prefix,
        chunk_count=opened_secret.chunk_count, plain_size=header.plain_size,
    )
    assert decoded_header == header


def test_manifest_round_trip_multiple_recipients_ordered():
    transfer_id = os.urandom(16)
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    header = manifest.ManifestHeader(
        file_name="a", mime_type="text/plain", plain_size=0,
        plain_sha256=hashlib.sha256(b"").digest(), chunk_count=1,
    )
    secret = manifest.RecipientSecret(
        data_key=data_key, nonce_prefix=nonce_prefix, receipt_secret=os.urandom(32), chunk_count=1,
    )

    recipients_material = [_make_recipient() for _ in range(3)]
    envelopes = [
        manifest.RecipientEnvelope(
            recipient_key_id=key_id, sealed_envelope=manifest.seal_recipient_secret(secret, pub)
        )
        for (_, pub, key_id) in recipients_material
    ]

    blob = manifest.build_manifest_blob(
        transfer_id=transfer_id, data_key=data_key, nonce_prefix=nonce_prefix, header=header, recipients=envelopes,
    )
    parsed = manifest.parse_manifest_blob(blob)
    assert [e.recipient_key_id for e in parsed.recipients] == [e.recipient_key_id for e in envelopes]

    # Each recipient can only open their own envelope.
    for (sk, _pub, key_id) in recipients_material:
        found = manifest.find_recipient_envelope(parsed, key_id)
        opened = manifest.open_recipient_secret(found.sealed_envelope, sk)
        assert opened.data_key == data_key


def test_find_recipient_envelope_raises_for_unknown_key_id():
    sk, pub, key_id = _make_recipient()
    secret = _make_secret()
    envelope = manifest.RecipientEnvelope(
        recipient_key_id=key_id, sealed_envelope=manifest.seal_recipient_secret(secret, pub)
    )
    blob = manifest.build_manifest_blob(
        transfer_id=os.urandom(16), data_key=secret.data_key, nonce_prefix=secret.nonce_prefix,
        header=manifest.ManifestHeader(
            file_name="a", mime_type="text/plain", plain_size=0,
            plain_sha256=hashlib.sha256(b"").digest(), chunk_count=1,
        ),
        recipients=[envelope],
    )
    parsed = manifest.parse_manifest_blob(blob)
    with pytest.raises(manifest.ManifestError):
        manifest.find_recipient_envelope(parsed, os.urandom(8))


def test_parse_manifest_blob_rejects_garbage():
    with pytest.raises(manifest.ManifestError):
        manifest.parse_manifest_blob(b"not cbor at all \xff\xff")


def test_parse_manifest_blob_rejects_wrong_version():
    import cbor2

    bad = cbor2.dumps(
        {0: 999, 1: os.urandom(16), 2: {0: os.urandom(24), 1: b"x"}, 3: [{0: os.urandom(8), 1: b"y"}]},
        canonical=True,
    )
    with pytest.raises(manifest.ManifestError):
        manifest.parse_manifest_blob(bad)


def test_parse_manifest_blob_rejects_empty_recipients_list():
    import cbor2

    bad = cbor2.dumps(
        {0: 1, 1: os.urandom(16), 2: {0: os.urandom(24), 1: b"x"}, 3: []},
        canonical=True,
    )
    with pytest.raises(manifest.ManifestError):
        manifest.parse_manifest_blob(bad)


def test_decrypt_manifest_header_rejects_wrong_data_key():
    sk, pub, key_id = _make_recipient()
    transfer_id = os.urandom(16)
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    header = manifest.ManifestHeader(
        file_name="a", mime_type="text/plain", plain_size=0,
        plain_sha256=hashlib.sha256(b"").digest(), chunk_count=1,
    )
    secret = manifest.RecipientSecret(
        data_key=data_key, nonce_prefix=nonce_prefix, receipt_secret=os.urandom(32), chunk_count=1,
    )
    envelope = manifest.RecipientEnvelope(
        recipient_key_id=key_id, sealed_envelope=manifest.seal_recipient_secret(secret, pub)
    )
    blob = manifest.build_manifest_blob(
        transfer_id=transfer_id, data_key=data_key, nonce_prefix=nonce_prefix, header=header, recipients=[envelope],
    )
    parsed = manifest.parse_manifest_blob(blob)

    with pytest.raises(manifest.ManifestError):
        manifest.decrypt_manifest_header(
            parsed, data_key=crypto.generate_data_key(), nonce_prefix=nonce_prefix,
            chunk_count=1, plain_size=header.plain_size,
        )
