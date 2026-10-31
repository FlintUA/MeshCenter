"""A buffer for key material: zeroable, and locked into RAM (so it is not written to swap) where the OS allows it.

Best effort by design (ADR-0013 Decision 6): ``mlock`` needs RLIMIT_MEMLOCK headroom (8192 KiB on our nodes, far
more than a 32-byte key needs) and exists only on POSIX. When it is not available the buffer still works and
``locked`` says so, so the caller can show the warning. PyNaCl does not expose ``sodium_mlock``, hence ctypes on
libc's ``mlock``/``munlock``.
"""
import ctypes
import ctypes.util
import sys


def _libc():
    if not sys.platform.startswith(("linux", "darwin", "freebsd")):
        return None
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    except OSError:
        return None


_LIBC = _libc()


def _address(buf: bytearray) -> int:
    return ctypes.addressof((ctypes.c_char * len(buf)).from_buffer(buf))


class SecretBuffer:
    """Holds secret bytes in a ``bytearray`` that is overwritten by ``zero()`` and by the context manager."""

    def __init__(self, data: bytes = b"", *, lock: bool = True):
        self._buf = bytearray(data)
        self.locked = False
        if lock and len(self._buf) and _LIBC is not None:
            try:
                self.locked = _LIBC.mlock(ctypes.c_void_p(_address(self._buf)), ctypes.c_size_t(len(self._buf))) == 0
            except (OSError, ValueError, TypeError):
                self.locked = False

    def __len__(self):
        return len(self._buf)

    def __bytes__(self):
        return bytes(self._buf)

    def view(self) -> memoryview:
        """The live bytes without a copy (the caller must not keep the view past ``zero()``)."""
        return memoryview(self._buf)

    def zero(self) -> None:
        """Overwrite in place, then unlock. Idempotent."""
        if not len(self._buf):
            return
        for i in range(len(self._buf)):
            self._buf[i] = 0
        if self.locked and _LIBC is not None:
            try:
                _LIBC.munlock(ctypes.c_void_p(_address(self._buf)), ctypes.c_size_t(len(self._buf)))
            except (OSError, ValueError, TypeError):
                pass
            self.locked = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.zero()
        return False


def zero_bytearray(buf: bytearray) -> None:
    """Overwrite a bytearray in place (for the short-lived buffers that are not a SecretBuffer)."""
    for i in range(len(buf)):
        buf[i] = 0
