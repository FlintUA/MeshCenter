"""Step 0.2: Ed25519 -> X25519 key conversion, verified against a fixed
test vector so the same Ed25519 seed produces the exact same X25519
public key on every machine this runs on (this repo's dev/prod Pi Zero
2 W hosts included) - not just "some deterministic value", but the one
specific value computed once and pinned here. See
docs/architecture/STEP_0.2_HANDOFF.md and
docs/architecture/ADR-0002-crypto-suite.md for why this conversion (via
libsodium's crypto_sign_ed25519_pk_to_curve25519, exposed through
PyNaCl's nacl.bindings) is the candidate being verified, instead of
carrying a second, independent X25519 key.

libsodium's crypto_sign_ed25519_sk_to_curve25519/_pk_to_curve25519 take
the *full* 64-byte secret key / 32-byte public key in its own wire
format - nacl.signing.SigningKey only exposes the 32-byte seed via
bytes(), so every helper here goes through
nacl.bindings.crypto_sign_seed_keypair(seed) to get the actual 64-byte
secret key libsodium expects, not the bare seed.
"""
import nacl.bindings as sodium
import pytest
from nacl.signing import SigningKey

# Fixed for reproducibility across every machine this test runs on -
# not a real key, never used outside this test file.
FIXED_SEED = bytes([0x11]) * 32

# Computed once (see STEP_0.2_HANDOFF.md's own worked example) and
# pinned here as the expected result. If this ever fails on a real
# machine, that machine's libsodium build produces a different
# Ed25519->X25519 conversion than every other machine this was
# verified on - exactly the risk this test exists to catch before
# Step 1.2 commits to this key-derivation scheme.
EXPECTED_ED25519_PK_HEX = (
    "d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737"
)
EXPECTED_X25519_PK_HEX = (
    "7a46e129fd805047448437e4744f1f1576be8c449fdf57e0c580d36c5cfc6668"
)


def _ed25519_keypair_from_seed(seed: bytes) -> tuple[bytes, bytes]:
    """Returns (full_64_byte_sk, 32_byte_pk) - libsodium's own wire
    format, not PyNaCl's 32-byte SigningKey seed."""
    pk, sk = sodium.crypto_sign_seed_keypair(seed)
    return sk, pk


def test_fixed_vector_ed25519_pk_matches_pinned_value():
    """Sanity check on the fixture itself: if this fails, the pinned
    X25519 expectation below is being compared against the wrong
    Ed25519 input, not a real conversion discrepancy."""
    _, pk = _ed25519_keypair_from_seed(FIXED_SEED)
    assert pk.hex() == EXPECTED_ED25519_PK_HEX


def test_pk_to_curve25519_matches_pinned_test_vector():
    _, pk = _ed25519_keypair_from_seed(FIXED_SEED)
    x25519_pk = sodium.crypto_sign_ed25519_pk_to_curve25519(pk)
    assert x25519_pk.hex() == EXPECTED_X25519_PK_HEX


def test_sk_to_curve25519_is_consistent_with_pk_conversion():
    """The X25519 secret key derived from the Ed25519 secret key must
    scalar-multiply to the exact same X25519 public key that
    crypto_sign_ed25519_pk_to_curve25519 produces from the Ed25519
    public key alone - this is the actual round-trip KEY_ANNOUNCE
    depends on (recipients only ever see the Ed25519 identity key and
    must derive the same X25519 public key the sender's own secret-key
    conversion would produce)."""
    sk, pk = _ed25519_keypair_from_seed(FIXED_SEED)
    x25519_pk_from_pk = sodium.crypto_sign_ed25519_pk_to_curve25519(pk)
    x25519_sk = sodium.crypto_sign_ed25519_sk_to_curve25519(sk)
    x25519_pk_from_sk = sodium.crypto_scalarmult_base(x25519_sk)
    assert x25519_pk_from_pk == x25519_pk_from_sk


def test_conversion_is_deterministic_across_repeated_generation():
    """Same seed, generated twice independently, must convert to the
    identical X25519 public key both times - the property KEY_ANNOUNCE
    actually relies on (every recipient must derive the same value from
    the same announced Ed25519 identity, not just "a valid" value)."""
    _, pk_a = _ed25519_keypair_from_seed(FIXED_SEED)
    _, pk_b = _ed25519_keypair_from_seed(FIXED_SEED)
    x25519_a = sodium.crypto_sign_ed25519_pk_to_curve25519(pk_a)
    x25519_b = sodium.crypto_sign_ed25519_pk_to_curve25519(pk_b)
    assert x25519_a == x25519_b


def test_conversion_works_on_a_freshly_generated_random_keypair():
    """Not just the fixed vector - a real, randomly generated identity
    key must convert without error and produce a 32-byte X25519 key
    consistent with its own secret-key conversion. Guards against the
    fixed-vector tests above passing by coincidence for one specific
    seed while a general keypair fails (e.g. a low-order point edge
    case libsodium is documented to reject)."""
    signing_key = SigningKey.generate()
    seed = bytes(signing_key)
    sk, pk = _ed25519_keypair_from_seed(seed)
    assert pk == bytes(signing_key.verify_key)

    x25519_pk = sodium.crypto_sign_ed25519_pk_to_curve25519(pk)
    x25519_sk = sodium.crypto_sign_ed25519_sk_to_curve25519(sk)
    assert len(x25519_pk) == 32
    assert len(x25519_sk) == 32
    assert sodium.crypto_scalarmult_base(x25519_sk) == x25519_pk


def test_conversion_rejects_the_all_zero_public_key():
    """Documented libsodium behavior: the identity point is not a valid
    Ed25519 public key and must not silently convert to something
    that looks like a usable X25519 key - if this ever starts
    succeeding, that's a libsodium/PyNaCl behavior change worth
    re-reading ADR-0002 over, not something to silently accept."""
    with pytest.raises(Exception):
        sodium.crypto_sign_ed25519_pk_to_curve25519(bytes(32))
