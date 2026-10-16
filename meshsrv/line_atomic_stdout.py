"""Whole-line, thread-safe stdout (container mode).

`print(text, flush=True)` reaches the stream as two writes - the text, then
"\n" - so under gunicorn's thread pool another thread's line could land
between them and produce `metrics=none[SEND] Queueing message` in `docker
logs`. This wrapper holds each thread's partial line privately and hands the
underlying stream only complete lines, one locked write at a time, so lines
from different threads can interleave but never inside a line.

Stdlib only. `install()` is idempotent and best-effort (a stream that cannot be
wrapped is left alone).
"""
from __future__ import annotations

import sys
import threading


class LineAtomicWriter:
    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._local = threading.local()

    def write(self, text):
        if not isinstance(text, str):
            return self._stream.write(text)
        pending = getattr(self._local, "pending", "") + text
        if "\n" in pending:
            head, _, tail = pending.rpartition("\n")
            with self._lock:
                self._stream.write(head + "\n")
        else:
            tail = pending
        self._local.pending = tail
        return len(text)

    def flush(self):
        # Never push a half line: it would be exactly the interleaving this
        # exists to prevent. Complete lines were already written above.
        with self._lock:
            self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, LineAtomicWriter):
            continue
        try:
            setattr(sys, name, LineAtomicWriter(stream))
        except Exception:
            pass
