"""Argon2id for the passphrase wrap, its floor, and calibration to about one second on the actual host
(ADR-0013 decisions 3 and the "KDF calibration" section).

The parameters are stored in the vault header, so they can be raised later and a backup restored on a slower machine
still unlocks (only slower). They are checked on every read: a header with weaker parameters than the floor, or with
absurdly large ones (a memory-exhaustion trick on a 512 MB node), is refused before any derivation runs.
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

import nacl.pwhash.argon2id as _argon2id
from nacl.utils import random as _random

from meshsrv.attachments.vault.errors import VaultError

KEY_BYTES = 32
SALT_BYTES = _argon2id.SALTBYTES                      # 16

MIN_MEMLIMIT = 32 * 1024 * 1024                       # decision 3: never below 32 MiB
MAX_MEMLIMIT = 1024 * 1024 * 1024                     # a tampered header must not be able to exhaust the node
MIN_OPSLIMIT = _argon2id.OPSLIMIT_INTERACTIVE         # 2 passes
MAX_OPSLIMIT = 64
TARGET_SECONDS = 1.0
DEFAULT_CEILING_BYTES = 64 * 1024 * 1024              # used when /proc/meminfo is not readable


@dataclass(frozen=True)
class KdfParams:
    opslimit: int
    memlimit: int
    measured_ms: int = 0          # informational: how long one derivation took when the parameters were chosen


def validate_params(params: KdfParams) -> KdfParams:
    """Refuse weak or absurd parameters. Called on every header read and before every derivation."""
    ops, mem = params.opslimit, params.memlimit
    if not isinstance(ops, int) or not isinstance(mem, int) or isinstance(ops, bool) or isinstance(mem, bool):
        raise VaultError("kdf_params_invalid")
    if ops < MIN_OPSLIMIT or mem < MIN_MEMLIMIT:
        raise VaultError("kdf_params_too_weak")
    if ops > MAX_OPSLIMIT or mem > MAX_MEMLIMIT or mem % 1024:
        raise VaultError("kdf_params_unreasonable")
    return params


def new_salt() -> bytes:
    return _random(SALT_BYTES)


def derive_key(passphrase: bytes, salt: bytes, params: KdfParams) -> bytearray:
    """The 32-byte wrap key. The caller zeroes the returned bytearray after use."""
    validate_params(params)
    if not isinstance(passphrase, (bytes, bytearray)) or not passphrase:
        raise VaultError("passphrase_required")
    if len(salt) != SALT_BYTES:
        raise VaultError("invalid_header")
    return bytearray(_argon2id.kdf(KEY_BYTES, bytes(passphrase), bytes(salt), opslimit=params.opslimit, memlimit=params.memlimit))


def memory_ceiling_bytes(meminfo_path: str = "/proc/meminfo") -> int:
    """A quarter of the currently available RAM (the ceiling of ADR-0013), never below the floor."""
    try:
        with open(meminfo_path, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                    return max(MIN_MEMLIMIT, available // 4)
    except (OSError, ValueError, IndexError):
        pass
    return DEFAULT_CEILING_BYTES


def measure_derivation(opslimit: int, memlimit: int) -> float:
    """Seconds one real derivation takes on this host with these parameters."""
    salt = new_salt()
    started = time.perf_counter()
    _argon2id.kdf(KEY_BYTES, b"calibration-passphrase", salt, opslimit=opslimit, memlimit=memlimit)
    return time.perf_counter() - started


def _power_of_two_at_most(value: int) -> int:
    result = MIN_MEMLIMIT
    while result * 2 <= value:
        result *= 2
    return result


def _best_of(measure: Callable[[int, int], float], ops: int, mem: int, runs: int = 2) -> float:
    """The fastest of a few runs: a first run on a cold node (page faults, a busy neighbour) reads slow and would
    make the chosen parameters too weak."""
    return min(measure(ops, mem) for _ in range(runs))


def calibrate(
    *,
    target_seconds: float = TARGET_SECONDS,
    ceiling_bytes: Optional[int] = None,
    measure: Callable[[int, int], float] = measure_derivation,
) -> KdfParams:
    """Choose parameters so one derivation takes about ``target_seconds`` here.

    Start at the floor. While the derivation is clearly faster than the target, double the memory (up to the
    ceiling, a quarter of the free RAM). Then tune the number of passes towards the target (a few refinement steps,
    never beyond 1.25 times the target). The memory is raised first because it is what makes guessing expensive on
    the attacker's GPU; the passes only fill the remaining time. ``measure`` is injected so the tests are
    deterministic. The floor always wins over the ceiling, and a host that is already slower than the target at the
    floor simply stays at the floor."""
    ceiling = max(MIN_MEMLIMIT, ceiling_bytes if ceiling_bytes is not None else memory_ceiling_bytes())
    memory_top = _power_of_two_at_most(min(ceiling, MAX_MEMLIMIT))
    ops, mem = MIN_OPSLIMIT, MIN_MEMLIMIT
    seconds = _best_of(measure, ops, mem)
    while seconds < target_seconds * 0.6 and mem * 2 <= memory_top:
        mem *= 2
        seconds = _best_of(measure, ops, mem)
    for _ in range(3):
        if seconds >= target_seconds * 0.9 or ops >= MAX_OPSLIMIT:
            break
        wanted = min(MAX_OPSLIMIT, max(ops + 1, int(round(ops * target_seconds / max(seconds, 1e-6)))))
        trial = _best_of(measure, wanted, mem)
        if trial > target_seconds * 1.25 and wanted > ops + 1:
            wanted -= 1
            trial = _best_of(measure, wanted, mem)
        if trial > target_seconds * 1.25:
            break                                   # one more pass would overshoot: keep what we had
        ops, seconds = wanted, trial
    return validate_params(KdfParams(opslimit=ops, memlimit=mem, measured_ms=int(round(seconds * 1000))))
