"""Tests for utils/helpers.py's get_device_model() - added so the system
log's reboot/shutdown labels no longer hardcode "Raspberry Pi" (that string
was wrong on non-Pi hosts, e.g. Droidian phones - see api/api_system.py's
execute_system_action()/api_system_info(), both of which now call this
function instead of reading /proc/device-tree/model themselves).
"""

import builtins

import pytest

import utils.helpers as helpers


@pytest.fixture(autouse=True)
def _reset_cache():
    # get_device_model() caches in a module-level global - reset it around
    # every test so tests don't leak state into each other.
    helpers._device_model_cache = None
    yield
    helpers._device_model_cache = None


def test_reads_and_strips_null_terminator_from_device_tree_model(monkeypatch):
    # /proc/device-tree/model is conventionally null-terminated - this is a
    # regression guard for a real bug found while writing this function:
    # the code this replaced (api/api_system.py's old inline read) searched
    # for the *literal 4-character string* "\x00" (double-escaped in that
    # source) instead of the actual null byte, so a real trailing null was
    # never actually stripped there.
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/device-tree/model":
            class FakeFile:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return "Raspberry Pi 4 Model B Rev 1.4\x00"
            return FakeFile()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert helpers.get_device_model() == "Raspberry Pi 4 Model B Rev 1.4"


def test_falls_back_to_platform_node_when_device_tree_missing(monkeypatch):
    def fake_open(path, *args, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr("platform.node", lambda: "some-hostname")
    assert helpers.get_device_model() == "some-hostname"


def test_returns_empty_string_when_nothing_is_available(monkeypatch):
    def fake_open(path, *args, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr("platform.node", lambda: "")
    assert helpers.get_device_model() == ""


def test_caches_result_across_calls(monkeypatch):
    call_count = 0
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        nonlocal call_count
        if path == "/proc/device-tree/model":
            call_count += 1
            class FakeFile:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return "Some Board\x00"
            return FakeFile()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    first = helpers.get_device_model()
    second = helpers.get_device_model()
    assert first == second == "Some Board"
    assert call_count == 1
