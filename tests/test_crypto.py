"""tests/test_crypto.py — meshsrv/attachments/crypto.py (ADR-0006).

Zero-I/O unit tests for chunk/header AEAD encryption and nonce
construction. No Relay, no manifest blob, no database.
"""

from __future__ import annotations

import os

import pytest

from meshsrv.attachments import crypto


def _transfer_id() -> bytes:
    return os.urandom(16)


def test_generate_data_key_and_nonce_prefix_are_right_length_and_random():
    a, b = crypto.generate_data_key(), crypto.generate_data_key()
    assert len(a) == crypto.DATA_KEY_BYTES == 32
    assert a != b

    pa, pb = crypto.generate_nonce_prefix(), crypto.generate_nonce_prefix()
    assert len(pa) == crypto.NONCE_PREFIX_BYTES == 16
    assert pa != pb


def test_chunk_plan_single_chunk_for_small_and_zero_size():
    assert crypto.ChunkPlan.for_size(0).chunk_count == 1
    assert crypto.ChunkPlan.for_size(1).chunk_count == 1
    assert crypto.ChunkPlan.for_size(crypto.CHUNK_SIZE_BYTES).chunk_count == 1


def test_chunk_plan_splits_across_boundary():
    plan = crypto.ChunkPlan.for_size(crypto.CHUNK_SIZE_BYTES + 1)
    assert plan.chunk_count == 2
    assert plan.bounds(0) == (0, crypto.CHUNK_SIZE_BYTES)
    assert plan.bounds(1) == (crypto.CHUNK_SIZE_BYTES, crypto.CHUNK_SIZE_BYTES + 1)


def test_chunk_plan_bounds_rejects_out_of_range_index():
    plan = crypto.ChunkPlan.for_size(10)
    with pytest.raises(crypto.CryptoError):
        plan.bounds(1)
    with pytest.raises(crypto.CryptoError):
        plan.bounds(-1)


def test_encrypt_decrypt_chunk_round_trip():
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    transfer_id = _transfer_id()
    plaintext = b"the quick brown fox jumps over the lazy dog"

    ct = crypto.encrypt_chunk(
        data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        index=0, chunk_count=1, plain_size=len(plaintext), plaintext=plaintext,
    )
    assert ct != plaintext
    pt = crypto.decrypt_chunk(
        data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        index=0, chunk_count=1, plain_size=len(plaintext), ciphertext=ct,
    )
    assert pt == plaintext


def test_encrypt_decrypt_header_round_trip():
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    transfer_id = _transfer_id()
    header_plaintext = b'{"file_name": "a.txt"}'

    ct = crypto.encrypt_header(
        data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        chunk_count=3, plain_size=1000, header_plaintext=header_plaintext,
    )
    pt = crypto.decrypt_header(
        data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        chunk_count=3, plain_size=1000, ciphertext=ct,
    )
    assert pt == header_plaintext


def test_header_nonce_never_collides_with_any_chunk_nonce():
    nonce_prefix = crypto.generate_nonce_prefix()
    header_nonce = crypto.header_nonce(nonce_prefix)
    # Exhaustively impractical to check all 2**64 indices; check the
    # boundary values where a collision would most plausibly be introduced
    # by an off-by-one in the suffix construction.
    for index in (0, 1, 2**8 - 1, 2**32, 2**63, 2**64 - 2):
        chunk_nonce = crypto._chunk_nonce(nonce_prefix, index)  # noqa: SLF001 - white-box nonce check
        assert chunk_nonce != header_nonce
    assert header_nonce == nonce_prefix + b"\xff" * 8


def test_decrypt_chunk_rejects_tampered_ciphertext():
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    transfer_id = _transfer_id()
    ct = bytearray(
        crypto.encrypt_chunk(
            data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
            index=0, chunk_count=1, plain_size=5, plaintext=b"hello",
        )
    )
    ct[0] ^= 0xFF
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_chunk(
            data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
            index=0, chunk_count=1, plain_size=5, ciphertext=bytes(ct),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda transfer_id, index, chunk_count, plain_size: (os.urandom(16), index, chunk_count, plain_size),
        lambda transfer_id, index, chunk_count, plain_size: (transfer_id, index + 1, chunk_count, plain_size),
        lambda transfer_id, index, chunk_count, plain_size: (transfer_id, index, chunk_count + 1, plain_size),
        lambda transfer_id, index, chunk_count, plain_size: (transfer_id, index, chunk_count, plain_size + 1),
    ],
    ids=["wrong_transfer_id", "wrong_index", "wrong_chunk_count", "wrong_plain_size"],
)
def test_decrypt_chunk_rejects_aad_mismatch(mutate):
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    transfer_id = _transfer_id()
    index, chunk_count, plain_size = 0, 2, 100

    ct = crypto.encrypt_chunk(
        data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        index=index, chunk_count=chunk_count, plain_size=plain_size, plaintext=b"x" * plain_size,
    )
    bad_transfer_id, bad_index, bad_chunk_count, bad_plain_size = mutate(transfer_id, index, chunk_count, plain_size)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_chunk(
            data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=bad_transfer_id,
            index=bad_index, chunk_count=bad_chunk_count, plain_size=bad_plain_size, ciphertext=ct,
        )


def test_decrypt_chunk_rejects_wrong_key():
    nonce_prefix = crypto.generate_nonce_prefix()
    transfer_id = _transfer_id()
    ct = crypto.encrypt_chunk(
        data_key=crypto.generate_data_key(), nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        index=0, chunk_count=1, plain_size=3, plaintext=b"abc",
    )
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_chunk(
            data_key=crypto.generate_data_key(), nonce_prefix=nonce_prefix, transfer_id=transfer_id,
            index=0, chunk_count=1, plain_size=3, ciphertext=ct,
        )


def test_chunk_and_header_ciphertexts_for_same_material_differ():
    """A chunk encrypted at index N and a header encrypted with the same
    key/nonce_prefix/transfer_id/chunk_count/plain_size must never be
    interchangeable - the role discriminator in the AAD is what this
    guards, on top of the nonce already differing."""
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    transfer_id = _transfer_id()
    payload = b"same bytes, different role"

    chunk_ct = crypto.encrypt_chunk(
        data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        index=0, chunk_count=1, plain_size=len(payload), plaintext=payload,
    )
    header_ct = crypto.encrypt_header(
        data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
        chunk_count=1, plain_size=len(payload), header_plaintext=payload,
    )
    assert chunk_ct != header_ct

    # And a header-ciphertext must not decrypt as a chunk-0 ciphertext or vice versa,
    # since the header nonce (all-ones suffix) differs from chunk index 0's nonce.
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_chunk(
            data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
            index=0, chunk_count=1, plain_size=len(payload), ciphertext=header_ct,
        )


def test_require_length_rejects_wrong_size_keys_and_nonces():
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_chunk(
            data_key=b"too short", nonce_prefix=crypto.generate_nonce_prefix(), transfer_id=_transfer_id(),
            index=0, chunk_count=1, plain_size=1, plaintext=b"x",
        )
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_chunk(
            data_key=crypto.generate_data_key(), nonce_prefix=b"too short", transfer_id=_transfer_id(),
            index=0, chunk_count=1, plain_size=1, plaintext=b"x",
        )


def test_chunk_index_must_be_non_negative_uint64():
    data_key = crypto.generate_data_key()
    nonce_prefix = crypto.generate_nonce_prefix()
    transfer_id = _transfer_id()
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_chunk(
            data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
            index=-1, chunk_count=1, plain_size=1, plaintext=b"x",
        )
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_chunk(
            data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
            index=2**64, chunk_count=1, plain_size=1, plaintext=b"x",
        )
