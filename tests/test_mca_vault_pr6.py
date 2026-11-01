"""MCA-VAULT PR 6 (ADR-0013 decision 9 and the memory findings): the unattended unlock file and giving memory back."""
import os

import pytest

from meshsrv import runtime_env
from meshsrv.attachments import mca_runtime
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.vault import header, kdf, passphrase, runtime, trim, unlock_file

FLOOR = kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT)
PASS = "correct horse battery staple"


class Logs:
    def __init__(self):
        self.lines = []

    def __call__(self, level, title, details):
        self.lines.append((level, title, details))

    def text(self):
        return " | ".join("%s %s %s" % line for line in self.lines)


@pytest.fixture
def made(tmp_path):
    created = header.create(passphrase.normalize(PASS), FLOOR)
    (tmp_path / "vault.json").write_text(created.header.to_json(), encoding="utf-8")
    return created, tmp_path


def make_runtime(path, logs=None):
    return runtime.VaultRuntime(path, log=logs or Logs(), free_ram=lambda: 2 * 1024 ** 3)


# ---- giving memory back ---------------------------------------------------------------------------------------------

def test_memory_is_released_after_an_unlock_a_lock_and_both_jobs(made, monkeypatch):
    created, path = made
    calls = []
    monkeypatch.setattr(trim, "release_unused_memory", lambda: calls.append(1) or True)
    rt = make_runtime(path)
    rt.unlock(passphrase=PASS)
    assert len(calls) == 1, "after an unlock"
    rt.lock()
    assert len(calls) == 2, "after a lock"
    rt.lock()
    assert len(calls) == 2, "a lock that was already locked frees nothing new"
    rt.finish_enable(bytearray(b"k" * 32), b"r" * 32)
    assert len(calls) == 3, "after an enable job"
    rt.finish_disable()
    assert len(calls) == 4, "after a disable job"


def test_releasing_memory_never_raises_and_works_without_glibc(monkeypatch):
    monkeypatch.setattr(trim, "_looked_up", True)
    monkeypatch.setattr(trim, "_trim", None)
    assert trim.release_unused_memory() is False, "no malloc_trim (musl, Windows): nothing to do, no error"

    def boom(_):
        raise OSError("no")
    monkeypatch.setattr(trim, "_trim", boom)
    assert trim.release_unused_memory() is False
    seen = []
    monkeypatch.setattr(trim, "_trim", lambda n: seen.append(n) or 1)
    assert trim.release_unused_memory() is True and seen == [0]


# ---- the unlock file ------------------------------------------------------------------------------------------------

def _secret(tmp_path, text, name="secret"):
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return str(path)


def test_the_secret_is_read_as_text_with_only_the_trailing_line_break_removed(tmp_path):
    assert unlock_file.read_secret(_secret(tmp_path, PASS + "\n")) == (PASS, None)
    assert unlock_file.read_secret(_secret(tmp_path, PASS + "\r\n")) == (PASS, None)
    assert unlock_file.read_secret(_secret(tmp_path, " " + PASS + " ")) == (" " + PASS + " ", None), "spaces belong to the passphrase"
    assert unlock_file.read_secret(_secret(tmp_path, "пароль-фраза-123")) == ("пароль-фраза-123", None)


@pytest.mark.parametrize("make, problem", [
    (lambda p: "relative/secret", "path_not_absolute"),
    (lambda p: str(p / "missing"), "not_a_file"),
    (lambda p: str(p), "not_a_file"),                                    # a directory
    (lambda p: _secret(p, "x" * 1025), "too_large"),
    (lambda p: _secret(p, b"\xff\xfe\x00bad"), "not_text"),
    (lambda p: _secret(p, "\n"), "empty"),
    (lambda p: _secret(p, ""), "empty"),
])
def test_a_secret_that_cannot_be_used_is_a_stable_code_never_the_content(tmp_path, make, problem):
    assert unlock_file.read_secret(make(tmp_path)) == (None, problem)


def test_a_symlinked_secret_is_followed(tmp_path):
    target = _secret(tmp_path, PASS, "real")
    link = tmp_path / "link"
    try:
        os.symlink(target, str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation needs privileges on this platform")
    assert unlock_file.read_secret(str(link)) == (PASS, None)


def _container(monkeypatch, on=True):
    monkeypatch.setattr(runtime_env, "is_container", lambda: on)


def test_it_is_off_unless_the_variable_is_set_and_ignored_outside_a_container(made, tmp_path, monkeypatch):
    created, path = made
    logs, rt = Logs(), None
    rt = make_runtime(path, logs)
    secret = _secret(tmp_path, PASS)
    _container(monkeypatch)
    assert unlock_file.maybe_unlock(rt, logs, {}) == "off" and rt.is_locked()
    assert unlock_file.maybe_unlock(rt, logs, {unlock_file.ENV: "  "}) == "off" and rt.is_locked()
    _container(monkeypatch, on=False)
    assert unlock_file.maybe_unlock(rt, logs, {unlock_file.ENV: secret}) == "ignored_not_container"
    assert rt.is_locked(), "a native install never unlocks from a file"
    assert any("only used in container mode" in line[2] for line in logs.lines)


def test_the_file_unlocks_the_vault_once_and_the_system_log_says_so(made, tmp_path, monkeypatch):
    created, path = made
    logs = Logs()
    rt = make_runtime(path, logs)
    _container(monkeypatch)
    secret = _secret(tmp_path, PASS + "\n")
    assert unlock_file.maybe_unlock(rt, logs, {unlock_file.ENV: secret}) == "unlocked"
    assert rt.is_locked() is False
    note = [line for line in logs.lines if "unlock file" in line[1]]
    assert note and note[-1][0] == "WARNING" and "next to the data" in note[-1][2]
    assert PASS not in logs.text(), "the passphrase is never logged"
    assert unlock_file.maybe_unlock(rt, logs, {unlock_file.ENV: secret}) == "already_unlocked"


def test_a_wrong_secret_leaves_the_vault_locked_makes_one_attempt_and_logs_no_value(made, tmp_path, monkeypatch):
    created, path = made
    logs = Logs()
    rt = make_runtime(path, logs)
    _container(monkeypatch)
    wrong = "not the right passphrase at all"
    outcome = unlock_file.maybe_unlock(rt, logs, {unlock_file.ENV: _secret(tmp_path, wrong)})
    assert outcome == "failed:unlock_failed" and rt.is_locked()
    assert wrong not in logs.text() and PASS not in logs.text()
    assert rt.status()["failed_attempts"] == 1, "one attempt, counted by the rate limit like a typed one"
    # a problem with the file is also one line, no retry
    assert unlock_file.maybe_unlock(rt, logs, {unlock_file.ENV: str(tmp_path / "gone")}) == "failed:not_a_file"
    assert rt.status()["failed_attempts"] == 1
    assert any("MCAttach stays locked" in line[2] for line in logs.lines)


def test_no_vault_means_nothing_to_unlock(tmp_path, monkeypatch):
    logs = Logs()
    rt = make_runtime(tmp_path, logs)
    _container(monkeypatch)
    assert unlock_file.maybe_unlock(rt, logs, {unlock_file.ENV: _secret(tmp_path, PASS)}) == "no_vault"
    assert unlock_file.maybe_unlock(None, logs, {unlock_file.ENV: _secret(tmp_path, PASS)}) == "no_vault"


def test_the_runtime_asks_for_the_unlock_at_start_and_a_failure_never_stops_mcattach(tmp_path, monkeypatch):
    asked = []

    def maybe(vault, log):
        asked.append(vault)
        raise RuntimeError("a bug in the convenience")

    monkeypatch.setattr(mca_runtime.unlock_file, "maybe_unlock", maybe)
    mca_runtime.reset_state_for_tests()
    try:
        transport = FakeRadioTransport(InMemoryEther(), "!aaaaaaaa")
        mca_runtime.start_attachments_service(str(tmp_path / "d"), transport, control_channel_index=0)
        state = mca_runtime._get_state(str(tmp_path / "d"))  # noqa: SLF001
        assert asked == [state.vault], "asked once, with this workspace's vault"
        assert state.service is not None, "MCAttach started anyway"
    finally:
        mca_runtime.reset_state_for_tests()
