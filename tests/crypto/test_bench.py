"""Step 0.2 crypto-suite benchmarks (docs/architecture/STEP_0.2_HANDOFF.md).

These are functional tests (they assert real invariants, not just
"ran without crashing") but their real purpose is the printed timing
output - they are NOT a substitute for the mandatory manual run on the
actual dev/prod Pi Zero 2 W hardware before ADR-0002 is closed. A
laptop/CI runner's numbers are not the numbers that matter here.

Marked @pytest.mark.benchmark (registered in pytest.ini) so CI can
still run them (they're cheap and assert correctness, not performance
thresholds - no hard time budget is asserted anywhere in this file,
deliberately, since a loaded CI runner is not a Pi Zero 2 W and a
timing assertion here would only ever produce false-negatives/positives
against the wrong hardware) while still being clearly labelled as the
files whose *numbers* belong in ADR-0002, not their pass/fail alone.

Run manually on each Pi with timing visible:
    python -m pytest tests/crypto/test_bench.py -v -s -m benchmark

Report the printed pk_to_curve25519 conversion time, Ed25519 keypair
generation time, and XChaCha20-Poly1305 256 KiB chunk encryption time
(+ peak RSS) for both dev and prod in ADR-0002, as real measured
numbers - not "fast enough" or "slow".
"""
import os
import time

import nacl.bindings as sodium
import pytest
from nacl.secret import Aead
from nacl.signing import SigningKey

pytestmark = pytest.mark.benchmark

CHUNK_SIZE = 256 * 1024
N_ITERATIONS = 200


def _peak_rss_kb():
    """Peak RSS in KiB. resource.getrusage is POSIX-only (this is what
    dev/prod - both Linux - will actually report); on a non-POSIX dev
    machine (Windows) this returns None and callers must skip the RSS
    line rather than report a meaningless/wrong number."""
    if os.name == "nt":
        return None
    import resource as _resource
    usage_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    # Linux reports ru_maxrss in KiB already (unlike macOS, which uses
    # bytes) - this repo only ever targets Linux (Raspberry Pi OS), so
    # no macOS-vs-Linux unit branch is needed here.
    return usage_kb


def _time_n(fn, n=N_ITERATIONS):
    start = time.perf_counter()
    for _ in range(n):
        fn()
    elapsed = time.perf_counter() - start
    return elapsed / n


def test_bench_ed25519_pk_to_curve25519_conversion():
    seed = bytes([0x22]) * 32
    pk, _sk = sodium.crypto_sign_seed_keypair(seed)

    def _convert():
        result = sodium.crypto_sign_ed25519_pk_to_curve25519(pk)
        assert len(result) == 32

    per_call = _time_n(_convert)
    print(
        f"\n[bench] crypto_sign_ed25519_pk_to_curve25519: "
        f"{per_call * 1e6:.1f} us/call ({N_ITERATIONS} iterations)"
    )


def test_bench_ed25519_keypair_generation():
    def _generate():
        key = SigningKey.generate()
        assert len(bytes(key)) == 32

    per_call = _time_n(_generate)
    print(
        f"\n[bench] Ed25519 keypair generation: "
        f"{per_call * 1e6:.1f} us/call ({N_ITERATIONS} iterations)"
    )


def test_bench_xchacha20poly1305_256kib_chunk_encryption():
    key = os.urandom(Aead.KEY_SIZE)
    aead = Aead(key)
    chunk = os.urandom(CHUNK_SIZE)
    nonce = os.urandom(Aead.NONCE_SIZE)

    ciphertext = aead.encrypt(chunk, nonce=nonce)
    assert aead.decrypt(ciphertext) == chunk

    # Fewer iterations than the conversion/keygen benches - encrypting
    # 256 KiB N times is real work, not microseconds, and the handoff
    # only needs a stable per-chunk figure, not a huge sample.
    n = 50

    def _encrypt():
        aead.encrypt(chunk, nonce=os.urandom(Aead.NONCE_SIZE))

    per_call = _time_n(_encrypt, n=n)
    peak_rss = _peak_rss_kb()
    rss_line = (
        f", peak RSS so far: {peak_rss / 1024:.1f} MiB"
        if peak_rss is not None
        else " (peak RSS not available on this platform - report the "
        "dev/prod Linux number, not this run's)"
    )
    print(
        f"\n[bench] XChaCha20-Poly1305 encrypt, one {CHUNK_SIZE // 1024} "
        f"KiB chunk: {per_call * 1e3:.2f} ms/call ({n} iterations)"
        f"{rss_line}"
    )
    # 5 MiB attachment = 20 chunks of this size (STEP_0.2_HANDOFF.md's
    # own budget framing) - report the extrapolated total too so
    # ADR-0002 doesn't need to redo this arithmetic by hand.
    print(f"[bench] extrapolated 5 MiB (20 chunks): {per_call * 20 * 1e3:.1f} ms")
