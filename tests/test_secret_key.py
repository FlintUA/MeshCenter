"""Tests for server.py's _load_or_create_secret_key() (F4.1).

Before this fix: open(path, "w") then chmod(path, 0o600) right after -
briefly world-readable between those two calls, and a non-atomic write (a
truncated file from a power cut right after the open() leaves an empty or
partial secret_key.txt, invalidating every session on the next read).
"""

import os
import stat

import pytest


def test_creates_a_new_key_when_no_file_exists(server_module, tmp_path):
    path = str(tmp_path / "secret_key.txt")
    key = server_module._load_or_create_secret_key(path)

    assert key
    with open(path, "r", encoding="utf-8") as f:
        assert f.read().strip() == key


def test_returns_the_existing_key_without_rewriting_it(server_module, tmp_path):
    path = str(tmp_path / "secret_key.txt")
    first = server_module._load_or_create_secret_key(path)
    mtime_before = os.path.getmtime(path)

    second = server_module._load_or_create_secret_key(path)

    assert second == first
    assert os.path.getmtime(path) == mtime_before


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode bits not meaningful on Windows")
def test_new_key_file_is_never_world_or_group_readable(server_module, tmp_path):
    path = str(tmp_path / "secret_key.txt")
    server_module._load_or_create_secret_key(path)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_no_tmp_file_left_behind_after_creation(server_module, tmp_path):
    path = str(tmp_path / "secret_key.txt")
    server_module._load_or_create_secret_key(path)

    leftovers = [p for p in os.listdir(tmp_path) if p != "secret_key.txt"]
    assert leftovers == []


def test_a_leftover_tmp_file_from_a_crashed_previous_attempt_does_not_block_creation(server_module, tmp_path):
    path = str(tmp_path / "secret_key.txt")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("half-written-from-a-crash")

    key = server_module._load_or_create_secret_key(path)

    assert key
    with open(path, "r", encoding="utf-8") as f:
        assert f.read().strip() == key
    assert not os.path.exists(path + ".tmp")


def test_write_failure_returns_a_usable_in_memory_key_without_raising(server_module, tmp_path, monkeypatch):
    path = str(tmp_path / "nested" / "secret_key.txt")  # parent dir doesn't exist -> os.open fails

    key = server_module._load_or_create_secret_key(path)

    assert key  # a usable key for THIS process's lifetime, even though persistence failed
    assert not os.path.exists(path)
