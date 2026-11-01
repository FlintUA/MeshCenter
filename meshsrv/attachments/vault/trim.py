"""Give memory back after the vault's heavy moments (ADR-0013, PR 6).

An unlock or an enable derives a key with Argon2id: libsodium asks for the whole memory limit at once (64 MiB on a Zero 2 W)
for about a second, then frees it. Measured in an isolated process on the Zero 2 W (docs/MCAttach_Vault.md), the resident memory
goes back down after every derivation: there is nothing retained. What a long-running process with many threads can still keep
is a few free pages in glibc's arenas, so after an unlock, a lock and a finished job this module runs a garbage collection and
asks glibc to return what it holds (`malloc_trim`). Best effort: not glibc (musl, macOS, Windows) means no trim, never an error.
"""
import ctypes
import ctypes.util
import gc
import logging

logger = logging.getLogger(__name__)

_trim = None
_looked_up = False


def _find_trim():
    global _trim, _looked_up
    if _looked_up:
        return _trim
    _looked_up = True
    try:
        name = ctypes.util.find_library("c")
        libc = ctypes.CDLL(name) if name else None
        if libc is not None and hasattr(libc, "malloc_trim"):
            libc.malloc_trim.argtypes = [ctypes.c_size_t]
            libc.malloc_trim.restype = ctypes.c_int
            _trim = libc.malloc_trim
    except (OSError, AttributeError, TypeError):
        _trim = None
    return _trim


def release_unused_memory() -> bool:
    """Collect garbage and ask glibc to return free memory to the system. True when a trim was done. Never raises."""
    try:
        gc.collect()
        trim = _find_trim()
        if trim is None:
            return False
        trim(0)
        return True
    except Exception:  # noqa: BLE001 - this is housekeeping, it must never break an unlock
        logger.debug("release_unused_memory failed", exc_info=True)
        return False
