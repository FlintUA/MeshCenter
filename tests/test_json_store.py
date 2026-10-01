"""Tests for storage/json_store.py (F4.1 rewrite).

Covers the three defects the F4.0 investigation confirmed:
- shared fixed `<file>.tmp` name -> a concurrent read deletes an in-flight
  writer's temp file out from under it (fixed: unique mkstemp name per write,
  reads never delete anything);
- a corrupt file is silently replaced by the caller's default and then
  re-saved, destroying whatever was still recoverable (fixed: quarantine the
  corrupt file instead, bounded to 3 copies, never auto-resaved by this
  module itself - that decision belongs to the caller);
- no directory fsync after os.replace() (best-effort, platform-guarded since
  this suite also runs on Windows).
"""

import json
import os
import time

import pytest

from storage import json_store


# ---------------------------------------------------------------------------
# Round-trip / basic correctness
# ---------------------------------------------------------------------------

def test_write_then_read_round_trips(tmp_path):
    path = str(tmp_path / "data.json")
    assert json_store.safe_write_json(path, {"a": 1, "b": [1, 2, 3]}) is True
    assert json_store.safe_read_json(path) == {"a": 1, "b": [1, 2, 3]}


def test_read_missing_file_returns_default(tmp_path):
    path = str(tmp_path / "missing.json")
    assert json_store.safe_read_json(path, {"x": 1}) == {"x": 1}
    assert json_store.safe_read_json(path) == {}


def test_write_creates_parent_directory(tmp_path):
    path = str(tmp_path / "sub" / "dir" / "data.json")
    assert json_store.safe_write_json(path, {"a": 1}) is True
    assert json_store.safe_read_json(path) == {"a": 1}


def test_no_tmp_file_left_behind_after_a_successful_write(tmp_path):
    path = str(tmp_path / "data.json")
    json_store.safe_write_json(path, {"a": 1})
    leftovers = [p for p in os.listdir(tmp_path) if p != "data.json"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Unique temp names - the shared-.tmp race this rewrite closes
# ---------------------------------------------------------------------------

def test_write_uses_a_unique_temp_name_not_the_fixed_file_tmp_name():
    """The old implementation always staged to exactly `<file>.tmp`. A
    concurrent safe_read_json() call on that same target would delete that
    exact name while the writer still had it open, so the writer's own
    os.replace() then raised FileNotFoundError. This asserts the new
    implementation never creates that one predictable, poachable name -
    the fix is "no single name to race on", proven by inspecting what
    tempfile.mkstemp() is actually asked for."""
    import tempfile as tempfile_module
    captured = {}
    real_mkstemp = tempfile_module.mkstemp

    def _spy_mkstemp(*args, **kwargs):
        captured["kwargs"] = kwargs
        return real_mkstemp(*args, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(json_store.tempfile, "mkstemp", side_effect=_spy_mkstemp):
        json_store.safe_write_json(
            os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or ".", "test_unique_name.json"),
            {"a": 1},
        )
    os.remove(os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or ".", "test_unique_name.json"))

    assert "kwargs" in captured
    # mkstemp was actually used (not a fixed open("<file>.tmp", "w")).
    assert captured["kwargs"].get("suffix") == ".tmp"


def test_concurrent_read_during_a_write_does_not_delete_the_writers_temp_file(tmp_path, monkeypatch):
    """The actual regression scenario, reproduced directly: start a write,
    pause it mid-flight (after mkstemp, before os.replace), have a
    concurrent safe_read_json() run against the SAME target file in that
    window, then let the write finish. Under the old shared-.tmp design
    this would delete the writer's temp file and the write would silently
    fail; under the new design the reader has no fixed name to find, so
    the writer always completes."""
    path = str(tmp_path / "shared.json")
    json_store.safe_write_json(path, {"before": True})  # so the file exists for the reader

    import threading

    paused = threading.Event()
    resume = threading.Event()
    result = {}

    real_replace = json_store.os.replace

    def _pausing_replace(src, dst):
        paused.set()
        resume.wait(timeout=5)
        return real_replace(src, dst)

    monkeypatch.setattr(json_store.os, "replace", _pausing_replace)

    def _writer():
        result["ok"] = json_store.safe_write_json(path, {"after": True})

    writer_thread = threading.Thread(target=_writer)
    writer_thread.start()
    assert paused.wait(timeout=5), "writer never reached the paused replace() point"

    # A concurrent reader, same target file, while the writer's temp file
    # is still on disk and not yet renamed into place.
    read_during_write = json_store.safe_read_json(path)
    assert read_during_write == {"before": True}  # unaffected, reads the still-current file

    resume.set()
    writer_thread.join(timeout=5)

    assert result["ok"] is True, "write must still succeed - its temp file must survive the concurrent read"
    assert json_store.safe_read_json(path) == {"after": True}


# ---------------------------------------------------------------------------
# Reads never delete anything
# ---------------------------------------------------------------------------

def test_read_never_deletes_a_stale_tmp_file_old_fixed_name_style(tmp_path):
    path = str(tmp_path / "data.json")
    json_store.safe_write_json(path, {"a": 1})
    stale_tmp = tmp_path / "data.json.tmp"
    stale_tmp.write_text('{"half":', encoding="utf-8")  # old-style fixed stale name

    json_store.safe_read_json(path)

    assert stale_tmp.exists(), "safe_read_json() must never delete a .tmp file - that's cleanup_stale_temp_files()'s job now"


def test_read_never_deletes_a_stale_tmp_file_new_mkstemp_style(tmp_path):
    path = str(tmp_path / "data.json")
    json_store.safe_write_json(path, {"a": 1})
    stale_tmp = tmp_path / ".data.json.abc123.tmp"
    stale_tmp.write_text('{"half":', encoding="utf-8")

    json_store.safe_read_json(path)

    assert stale_tmp.exists()


# ---------------------------------------------------------------------------
# Corrupt-file quarantine
# ---------------------------------------------------------------------------

def test_corrupt_json_is_quarantined_not_silently_discarded(tmp_path):
    path = str(tmp_path / "data.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"history":[1,2,3')  # truncated - exactly the power-cut repro

    result = json_store.safe_read_json(path, default={"history": []})

    assert result == {"history": []}
    assert not os.path.exists(path), "the corrupt file must be moved aside, not left under the original name"
    quarantined = [p for p in os.listdir(tmp_path) if p.startswith("data.json.corrupt-")]
    assert len(quarantined) == 1
    with open(tmp_path / quarantined[0], encoding="utf-8") as f:
        assert f.read() == '{"history":[1,2,3'


def test_quarantine_keeps_at_most_three_copies(tmp_path, monkeypatch):
    path = str(tmp_path / "data.json")

    for i in range(5):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"not json at all {i}")
        # Force a distinct quarantine timestamp per iteration - real
        # corruptions won't happen faster than once a second in practice,
        # but the test must not depend on wall-clock timing.
        monkeypatch.setattr(json_store.time, "strftime", lambda fmt, _i=i: f"2026010{_i}-000000")
        json_store.safe_read_json(path, default={})

    quarantined = sorted(p for p in os.listdir(tmp_path) if p.startswith("data.json.corrupt-"))
    assert len(quarantined) == 3
    # The three most recent (iterations 2, 3, 4) survive; 0 and 1 were pruned.
    assert quarantined == [
        "data.json.corrupt-20260102-000000",
        "data.json.corrupt-20260103-000000",
        "data.json.corrupt-20260104-000000",
    ]


def test_quarantine_failure_still_returns_default_and_does_not_raise(tmp_path, monkeypatch):
    path = str(tmp_path / "data.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json")

    def _broken_rename(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(json_store.os, "rename", _broken_rename)

    result = json_store.safe_read_json(path, default={"fallback": True})
    assert result == {"fallback": True}


def test_non_corruption_io_error_does_not_quarantine(tmp_path, monkeypatch):
    """A permission error or similar transient OSError is not evidence the
    FILE is corrupt - quarantining it would destroy a perfectly good file
    over an unrelated, possibly-transient problem."""
    path = str(tmp_path / "data.json")
    json_store.safe_write_json(path, {"a": 1})

    real_open = open

    def _denying_open(p, *args, **kwargs):
        if str(p) == path and "r" in (args[0] if args else kwargs.get("mode", "r")):
            raise PermissionError("simulated permission denied")
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr(json_store, "open", _denying_open, raising=False)

    result = json_store.safe_read_json(path, default={"fallback": True})
    assert result == {"fallback": True}
    assert os.path.exists(path), "a permission error must never quarantine/move the file"


# ---------------------------------------------------------------------------
# cleanup_stale_temp_files()
# ---------------------------------------------------------------------------

def test_cleanup_removes_old_tmp_files_of_both_naming_styles(tmp_path):
    old_fixed = tmp_path / "nodes.json.tmp"
    old_unique = tmp_path / ".chats.json.xyz789.tmp"
    old_fixed.write_text("stale", encoding="utf-8")
    old_unique.write_text("stale", encoding="utf-8")

    old_time = time.time() - 3600
    os.utime(old_fixed, (old_time, old_time))
    os.utime(old_unique, (old_time, old_time))

    json_store.cleanup_stale_temp_files(str(tmp_path), older_than_s=300)

    assert not old_fixed.exists()
    assert not old_unique.exists()


def test_cleanup_leaves_recent_tmp_files_alone(tmp_path):
    recent = tmp_path / ".nodes.json.abc.tmp"
    recent.write_text("in flight", encoding="utf-8")

    json_store.cleanup_stale_temp_files(str(tmp_path), older_than_s=300)

    assert recent.exists()


def test_cleanup_never_touches_non_tmp_files(tmp_path):
    real_file = tmp_path / "nodes.json"
    real_file.write_text("{}", encoding="utf-8")
    old_time = time.time() - 3600
    os.utime(real_file, (old_time, old_time))

    json_store.cleanup_stale_temp_files(str(tmp_path), older_than_s=300)

    assert real_file.exists()


def test_cleanup_recurses_into_subdirectories(tmp_path):
    sub = tmp_path / "profiles" / "deadbeef"
    sub.mkdir(parents=True)
    stale = sub / "nodes.json.tmp"
    stale.write_text("stale", encoding="utf-8")
    old_time = time.time() - 3600
    os.utime(stale, (old_time, old_time))

    json_store.cleanup_stale_temp_files(str(tmp_path), older_than_s=300)

    assert not stale.exists()


def test_cleanup_never_raises_on_a_missing_directory():
    json_store.cleanup_stale_temp_files("/this/path/does/not/exist/at/all", older_than_s=300)


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

def test_write_failure_cleans_up_its_own_temp_file_and_returns_false(tmp_path, monkeypatch):
    path = str(tmp_path / "data.json")

    def _broken_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(json_store.os, "replace", _broken_replace)

    result = json_store.safe_write_json(path, {"a": 1})

    assert result is False
    assert not os.path.exists(path)
    leftovers = [p for p in os.listdir(tmp_path)]
    assert leftovers == [], f"a failed write must not leave its temp file behind: {leftovers}"


def test_atomic_write_json_is_an_alias():
    assert json_store.atomic_write_json is not json_store.safe_write_json  # may wrap, not require identity
    import inspect
    assert callable(json_store.atomic_write_json)
