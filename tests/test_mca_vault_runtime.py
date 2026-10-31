"""MCA-VAULT PR 2 (ADR-0013): the vault runtime state machine, with a vault made by the core library."""
import threading

import pytest

from meshsrv.attachments.vault import header, kdf, passphrase, recovery, runtime, swapcheck
from meshsrv.attachments.vault.errors import VaultError

FLOOR = kdf.KdfParams(kdf.MIN_OPSLIMIT, kdf.MIN_MEMLIMIT)
PASS = "correct horse battery staple"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Logs:
    def __init__(self):
        self.lines = []

    def __call__(self, level, title, details):
        self.lines.append((level, title, details))

    def text(self):
        return " | ".join("%s %s %s" % l for l in self.lines)


@pytest.fixture
def made(tmp_path):
    created = header.create(passphrase.normalize(PASS), FLOOR)
    (tmp_path / "vault.json").write_text(created.header.to_json(), encoding="utf-8")
    return created, tmp_path


def make_runtime(path, **kw):
    kw.setdefault("log", Logs())
    kw.setdefault("clock", Clock())
    kw.setdefault("free_ram", lambda: 2 * 1024 ** 3)
    return runtime.VaultRuntime(path, **kw)


def test_no_vault_files_means_disabled_and_nothing_is_locked(tmp_path):
    rt = make_runtime(tmp_path)
    assert rt.state() == "disabled" and rt.is_enabled() is False and rt.is_locked() is False
    assert rt.storage_key() is None and rt.status() == {"state": "disabled", "enabled": False, "locked": False}
    with pytest.raises(VaultError) as info:
        rt.unlock(passphrase=PASS)
    assert info.value.code == "vault_not_enabled"


def test_a_vault_is_locked_after_every_start_then_unlocks_and_locks(made):
    created, path = made
    logs = Logs()
    rt = make_runtime(path, log=logs)
    assert rt.state() == "locked" and rt.is_locked() is True and rt.storage_key() is None and rt.subkey("files") is None
    status = rt.unlock(passphrase=PASS)
    assert status["state"] == "unlocked" and status["locked"] is False
    assert rt.storage_key() == bytes(created.storage_key) and len(rt.subkey("files")) == 32
    generation = rt.generation
    rt.lock()
    assert rt.state() == "locked" and rt.storage_key() is None and rt.generation == generation + 1
    assert [l[1] for l in logs.lines if l[0] == "INFO"] == ["MCAttach vault unlocked", "MCAttach vault locked"]


def test_unlock_with_the_recovery_key_and_idempotent_unlock(made):
    created, path = made
    rt = make_runtime(path)
    rt.unlock(recovery_key=recovery.encode(created.recovery_key))
    assert rt.storage_key() == bytes(created.storage_key)
    generation = rt.generation
    assert rt.unlock(passphrase="anything else")["state"] == "unlocked", "already unlocked: nothing is derived again"
    assert rt.generation == generation


def test_lock_zeroes_the_key_in_place(made):
    _, path = made
    rt = make_runtime(path)
    rt.unlock(passphrase=PASS)
    live = rt._key.view()
    rt.lock()
    assert bytes(live) == b"\x00" * 32


def test_wrong_attempts_are_rate_limited_1_2_4_4_then_60_and_never_a_lockout(made):
    created, path = made
    clock, logs = Clock(), Logs()
    rt = make_runtime(path, clock=clock, log=logs)
    waits = []
    for _ in range(6):
        with pytest.raises(VaultError) as info:
            rt.unlock(passphrase="the wrong passphrase 1")
        assert info.value.code == "unlock_failed"
        waits.append(rt.status()["retry_in_s"])
        clock.now += 61                                            # wait it out before the next try
    assert waits == [1, 2, 4, 4, 60, 60]
    assert [runtime.retry_delay_after(n) for n in (0, 1, 2, 3, 4, 5, 9)] == [0, 1, 2, 4, 4, 60, 60]
    # in a delay window the attempt is refused without being counted and without deriving anything
    with pytest.raises(VaultError) as info:
        rt.unlock(passphrase="the wrong passphrase 1")
    assert info.value.code == "unlock_failed"
    with pytest.raises(VaultError) as info:
        rt.unlock(passphrase=PASS)                                  # even the right one waits out the delay
    assert info.value.code == "unlock_rate_limited" and info.value.retry_in_s >= 1
    assert rt.status()["failed_attempts"] == 7
    clock.now += 61
    assert rt.unlock(passphrase=PASS)["state"] == "unlocked", "no permanent lockout"
    assert rt.status()["failed_attempts"] == 0
    assert "wrong passphrase" not in logs.text() and PASS not in logs.text()


def test_a_wrong_recovery_key_counts_as_a_failure_too(made):
    _, path = made
    clock = Clock()
    rt = make_runtime(path, clock=clock)
    with pytest.raises(VaultError):
        rt.unlock(recovery_key=recovery.encode(b"\x01" * 32))
    assert rt.status()["failed_attempts"] == 1
    clock.now += 5
    with pytest.raises(VaultError) as info:
        rt.unlock(recovery_key="not a key")
    assert info.value.code == "invalid_recovery_key"


def test_unlock_is_refused_with_a_clear_message_when_free_ram_is_too_low(made):
    _, path = made
    logs = Logs()
    free = {"bytes": 40 * 1024 * 1024}                              # a Zero 2 W with little free: 32 MiB + 16 MiB margin needed
    rt = make_runtime(path, log=logs, free_ram=lambda: free["bytes"])
    with pytest.raises(VaultError) as info:
        rt.unlock(passphrase=PASS)
    assert info.value.code == "insufficient_memory"
    assert "48 MiB needed" in info.value.message and "40 MiB free" in info.value.message
    assert rt.state() == "locked" and rt.status()["failed_attempts"] == 0, "a memory refusal is not a wrong attempt"
    assert any(l[1] == "MCAttach vault unlock refused" for l in logs.lines)
    free["bytes"] = 60 * 1024 * 1024
    assert rt.unlock(passphrase=PASS)["state"] == "unlocked"


def test_unreadable_meminfo_does_not_block_the_unlock(made):
    _, path = made
    rt = make_runtime(path, free_ram=lambda: None)
    assert rt.unlock(passphrase=PASS)["state"] == "unlocked"


def test_free_ram_bytes_reads_memavailable(tmp_path):
    info = tmp_path / "meminfo"
    info.write_text("MemTotal: 425172 kB\nMemAvailable: 121720 kB\n")
    assert runtime.free_ram_bytes(str(info)) == 121720 * 1024
    assert runtime.free_ram_bytes(str(tmp_path / "missing")) is None


def test_unlock_waits_for_the_service_tick_lock_and_reports_busy_when_it_cannot_get_it(made, monkeypatch):
    _, path = made
    monkeypatch.setattr(runtime, "LOCK_WAIT_SECONDS", 0.2)
    tick = threading.Lock()
    rt = make_runtime(path, serialize_with=tick)
    tick.acquire()                                                  # the worker is in the middle of a tick
    with pytest.raises(VaultError) as info:
        rt.unlock(passphrase=PASS)
    assert info.value.code == "vault_busy" and rt.state() == "locked"
    tick.release()
    assert rt.unlock(passphrase=PASS)["state"] == "unlocked"
    assert tick.acquire(timeout=0.1), "the tick lock is released after the derivation"
    tick.release()


def test_only_one_unlock_runs_at_a_time(made, monkeypatch):
    _, path = made
    rt = make_runtime(path)
    started, release = threading.Event(), threading.Event()
    real = rt._derive

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(rt, "_derive", slow)
    monkeypatch.setattr(runtime, "LOCK_WAIT_SECONDS", 0.2)
    first = threading.Thread(target=lambda: rt.unlock(passphrase=PASS))
    first.start()
    assert started.wait(5)
    with pytest.raises(VaultError) as info:
        rt.unlock(passphrase=PASS)
    assert info.value.code == "vault_busy"
    release.set()
    first.join(10)
    assert rt.state() == "unlocked"


def test_a_migrating_vault_counts_as_enabled_and_locked(tmp_path):
    (tmp_path / "vault-migration.json").write_text("{}")
    rt = make_runtime(tmp_path)
    assert rt.state() == "migrating" and rt.is_enabled() and rt.is_locked()


def test_header_problems_are_reported_so_a_build_that_cannot_read_it_fails_closed(made):
    created, path = made
    rt = make_runtime(path)
    assert rt.header_problem() is None
    (path / "vault.json").write_text('{"v": 2}')
    assert rt.header_problem() == "unsupported_header_version"
    (path / "vault.json").write_text("garbage")
    assert rt.header_problem() == "invalid_header"
    (path / "vault.json").unlink()
    assert rt.header_problem() is None


def test_the_header_backup_is_removed_by_the_first_successful_unlock(made):
    _, path = made
    (path / "vault.json.bak").write_text("old header")
    rt = make_runtime(path)
    with pytest.raises(VaultError):
        rt.unlock(passphrase="not the passphrase 123")
    assert (path / "vault.json.bak").exists(), "a failed unlock keeps it"
    rt._clock.now += 5                                              # the 1 s delay of the failed attempt has passed
    rt.unlock(passphrase=PASS)
    assert not (path / "vault.json.bak").exists()


def test_status_reports_kdf_memory_need_and_swap_without_any_secret(made):
    created, path = made
    rt = make_runtime(path, swap_inspector=lambda: {"disk_swap": True, "known": True, "devices": [{"name": "/var/swap"}]})
    status = rt.status()
    assert status["state"] == "locked" and status["kdf"] == {"memlimit_mib": 32, "opslimit": 2, "measured_ms": 0}
    assert status["min_free_ram_mib"] == 48 and status["swap"] == {"disk_swap": True, "known": True}
    assert status["key_locked_in_ram"] is None
    text = repr(status)
    assert bytes(created.storage_key).hex() not in text and "/var/swap" not in text


def test_a_key_that_cannot_be_locked_in_ram_is_logged_once(made, monkeypatch):
    _, path = made
    logs = Logs()
    monkeypatch.setattr(runtime.memory, "_LIBC", None)             # no mlock available
    rt = make_runtime(path, log=logs)
    rt.unlock(passphrase=PASS)
    assert rt.status()["key_locked_in_ram"] is False
    assert [l[1] for l in logs.lines].count("MCAttach vault key not locked in RAM") == 1


def test_postponed_work_is_logged_once_per_lock_period(made):
    _, path = made
    logs = Logs()
    rt = make_runtime(path, log=logs)
    for _ in range(3):
        rt.note_postponed("Key requests")
    assert [l[1] for l in logs.lines] == ["MCAttach locked"]
    rt.unlock(passphrase=PASS)
    rt.lock()
    rt.note_postponed("Key requests")
    assert [l[1] for l in logs.lines].count("MCAttach locked") == 2


# ---- swap check --------------------------------------------------------------------------------------------------

def test_swap_inspection_tells_zram_from_a_disk_swap(tmp_path):
    zram = tmp_path / "zram"
    zram.write_text("Filename\t\t\t\tType\t\tSize\tUsed\tPriority\n/dev/zram0                              partition\t424956\t59000\t100\n")
    assert swapcheck.inspect_swaps(str(zram)) == {"devices": [{"name": "/dev/zram0", "type": "partition", "zram": True}], "disk_swap": False, "known": True}
    disk = tmp_path / "disk"
    disk.write_text("Filename\tType\tSize\tUsed\tPriority\n/var/swap\tfile\t102400\t0\t-2\n/dev/zram0\tpartition\t1\t0\t100\n")
    found = swapcheck.inspect_swaps(str(disk))
    assert found["disk_swap"] is True and [d["zram"] for d in found["devices"]] == [False, True]
    empty = tmp_path / "empty"
    empty.write_text("Filename\tType\tSize\tUsed\tPriority\n")
    assert swapcheck.inspect_swaps(str(empty)) == {"devices": [], "disk_swap": False, "known": True}
    assert swapcheck.inspect_swaps(str(tmp_path / "missing")) == {"devices": [], "disk_swap": False, "known": False}


# ---- the memory check uses MemAvailable, never MemFree --------------------------------------------------------------

def _meminfo(tmp_path, *, free_kb, available_kb):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal:        425172 kB\nMemFree:         %d kB\nMemAvailable:    %d kB\nBuffers: 1 kB\n" % (free_kb, available_kb))
    return str(path)


def test_free_ram_reads_memavailable_not_memfree(tmp_path):
    path = _meminfo(tmp_path, free_kb=5000, available_kb=140000)        # a Pi: little free, plenty reclaimable
    assert runtime.free_ram_bytes(path) == 140000 * 1024
    path = _meminfo(tmp_path, free_kb=300000, available_kb=20000)       # the opposite: the check must follow MemAvailable
    assert runtime.free_ram_bytes(path) == 20000 * 1024


def test_the_unlock_check_follows_memavailable_through_the_default_reader(made, tmp_path, monkeypatch):
    _, path = made
    monkeypatch.setattr(runtime, "MEMINFO_PATH", _meminfo(tmp_path, free_kb=5000, available_kb=140000))
    rt = runtime.VaultRuntime(path, clock=Clock(), log=Logs())          # the default reader, no injected free_ram
    assert rt.unlock(passphrase=PASS)["state"] == "unlocked", "MemFree is tiny but MemAvailable is ample: allowed"
    rt.lock()
    monkeypatch.setattr(runtime, "MEMINFO_PATH", _meminfo(tmp_path, free_kb=300000, available_kb=20000))
    with pytest.raises(VaultError) as info:
        rt.unlock(passphrase=PASS)
    assert info.value.code == "insufficient_memory" and "20 MiB free" in info.value.message, "MemFree is ample but MemAvailable is not: refused"


def test_the_calibration_ceiling_also_uses_memavailable(tmp_path):
    path = _meminfo(tmp_path, free_kb=1000, available_kb=1600000)
    assert kdf.memory_ceiling_bytes(path) == 1600000 * 1024 // 4


def test_note_dropped_logs_one_line_with_the_count_and_no_content(made):
    _, path = made
    logs = Logs()
    rt = make_runtime(path, log=logs)
    rt.note_dropped(7)
    assert logs.lines == [("WARNING", "MCAttach locked: messages dropped",
                           "7 incoming control message(s) were dropped because the queue of messages waiting for the unlock is full")]
