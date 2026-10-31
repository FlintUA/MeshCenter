"""A test helper: fails the moment any file write carries a plaintext marker (open, io.open, os.write, os.fdopen)."""
import builtins
import contextlib
import io
import os

MARKER = b"PLAINTEXT-MARKER-DO-NOT-WRITE-"


@contextlib.contextmanager
def no_plaintext_writes(marker: bytes = MARKER):
    """Fails the moment any file write (open, io.open, os.write, os.fdopen) carries the plaintext marker."""
    def check(data):
        if isinstance(data, (bytes, bytearray, memoryview)) and marker in bytes(data):
            raise AssertionError("plaintext reached a file write")

    class Spy:
        def __init__(self, handle):
            self._h = handle

        def write(self, data):
            check(data)
            return self._h.write(data)

        def __getattr__(self, name):
            return getattr(self._h, name)

        def __enter__(self):
            self._h.__enter__()
            return self

        def __exit__(self, *a):
            return self._h.__exit__(*a)

        def __iter__(self):
            return iter(self._h)

    real_open, real_io_open, real_os_write, real_fdopen = builtins.open, io.open, os.write, os.fdopen

    def wrap(opener):
        def opened(file, mode="r", *a, **k):
            handle = opener(file, mode, *a, **k)
            return Spy(handle) if any(c in str(mode) for c in "wax+") else handle
        return opened

    def os_write(fd, data):
        check(data)
        return real_os_write(fd, data)

    builtins.open, io.open, os.write, os.fdopen = wrap(real_open), wrap(real_io_open), os_write, wrap(real_fdopen)
    try:
        yield
    finally:
        builtins.open, io.open, os.write, os.fdopen = real_open, real_io_open, real_os_write, real_fdopen


