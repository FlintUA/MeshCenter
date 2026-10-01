"""JSON storage helpers for MeshCenter (F4.1 rewrite).

Replaces the original shared-fixed-`<file>.tmp` design, which had two
confirmed defects (F4.0 investigation):

1. Every writer staged to the exact same `<file>.tmp` name. safe_read_json()
   used to unconditionally delete that file if present (meant as "clean up
   after a crashed writer"), but a READ running concurrently with an
   in-flight WRITE to the same file would delete the writer's own temp file
   out from under it, making its later os.replace() raise FileNotFoundError
   - a silently lost write. Fixed here by giving every write a unique
   tempfile.mkstemp() name, and reads never deleting anything at all -
   cleanup_stale_temp_files() below is the only thing that ever removes a
   leftover temp file, run once at startup, well clear of any in-flight write.
2. A corrupt file (a truncated write from a power cut, say) was silently
   replaced by the caller's `default` and - in several callers - immediately
   re-saved, permanently destroying whatever was still in the file before
   the corruption. Fixed here: a JSON decode failure quarantines the file
   (renamed aside, never overwritten) instead of leaving it to be clobbered,
   and callers decide separately whether/when to re-save.
"""

import json
import os
import tempfile
import time


def safe_read_json(filepath, default=None):
    """Reads JSON from `filepath`. Never deletes or modifies any `.tmp`
    file - cleanup_stale_temp_files() is the only thing that does that, and
    only well after a write could plausibly still be in flight.

    A missing file returns `default` silently (the normal "nothing saved
    yet" case). A file that fails to parse as JSON is quarantined (moved
    aside to `<filepath>.corrupt-<timestamp>`, at most 3 such copies kept
    per file) and `default` is returned - the corrupt bytes are preserved
    for inspection, never overwritten by a later save of `default`, and the
    original filename no longer exists so the next write starts clean.
    Any other read failure (permission error, the file vanishing between
    the existence check and open, ...) is NOT evidence of corruption, so
    it is just logged and `default` is returned - the file itself is left
    completely untouched.
    """
    if default is None:
        default = {}

    if not os.path.exists(filepath):
        return default

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[JSON] ERROR: {filepath} is corrupt ({e}) - quarantining and using default", flush=True)
        _quarantine_corrupt_file(filepath)
        return default
    except OSError as e:
        print(f"[JSON] Read error: {e}, using default", flush=True)
        return default


def _quarantine_corrupt_file(filepath, max_copies=3):
    """Best-effort: renames `filepath` to `filepath + '.corrupt-<timestamp>'`
    and prunes older quarantine copies of the same file beyond `max_copies`.
    Never raises - a failure here must not take down the read path that
    called it; the corrupt file is simply left under its original name in
    that case (still not overwritten, since the caller never writes here)."""
    try:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        quarantine_path = f"{filepath}.corrupt-{timestamp}"
        # Collision-avoid within the same second (e.g. two corrupt reads of
        # different files that happen to share a basename never collide
        # since filepath differs, but two reads of the SAME file inside one
        # second otherwise would).
        suffix = 0
        candidate = quarantine_path
        while os.path.exists(candidate):
            suffix += 1
            candidate = f"{quarantine_path}-{suffix}"
        os.rename(filepath, candidate)
    except OSError as e:
        print(f"[JSON] Could not quarantine corrupt file {filepath}: {e}", flush=True)
        return

    try:
        directory = os.path.dirname(filepath) or "."
        basename = os.path.basename(filepath)
        prefix = f"{basename}.corrupt-"
        siblings = sorted(
            name for name in os.listdir(directory) if name.startswith(prefix)
        )
        excess = len(siblings) - max_copies
        for name in siblings[:max(0, excess)]:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
    except OSError:
        pass


def safe_write_json(filepath, data):
    """Writes `data` as JSON to `filepath` atomically: a uniquely-named
    temp file (tempfile.mkstemp(), never the fixed `<file>.tmp` name two
    concurrent writers - or a writer and a reader - could collide on),
    flushed and fsynced before os.replace() swaps it into place, followed
    by a best-effort fsync of the containing directory (POSIX only -
    Windows has no directory file descriptor to fsync, so this step is a
    silent no-op there rather than a platform-specific failure).

    Returns True on success, False on any failure - the temp file is
    removed on a failed attempt, so a failure never leaves a stray partial
    file behind (beyond the original, untouched target)."""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    else:
        directory = "."

    basename = os.path.basename(filepath)
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{basename}.", suffix=".tmp")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None  # fdopen now owns the fd; avoid a double-close below
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, filepath)
        tmp_path = None  # successfully moved - nothing left to clean up

        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # best-effort only (e.g. unsupported on Windows)

        return True
    except Exception as e:
        print(f"[JSON] Write error: {e}", flush=True)
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def atomic_write_json(filepath, data):
    """Backward-compatible alias."""
    return safe_write_json(filepath, data)


def cleanup_stale_temp_files(data_dir, older_than_s=300):
    """Removes leftover `.tmp` files under `data_dir` (recursively) older
    than `older_than_s` seconds - matches both the legacy fixed `<file>.tmp`
    naming and the unique `.{basename}.<random>.tmp` naming safe_write_json()
    uses now. Meant to run once at startup, well after any write from a
    previous process could plausibly still be in flight - reads themselves
    never delete anything any more (see safe_read_json() above).

    Best-effort and silent on a missing/unreadable data_dir or individual
    file errors - this is housekeeping, not something that should ever be
    allowed to block or fail startup."""
    cutoff = time.time() - older_than_s
    try:
        for root, _dirs, files in os.walk(data_dir):
            for name in files:
                if not name.endswith(".tmp"):
                    continue
                path = os.path.join(root, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        print(f"[JSON] Removed stale temp file: {path}", flush=True)
                except OSError:
                    continue
    except OSError:
        pass
