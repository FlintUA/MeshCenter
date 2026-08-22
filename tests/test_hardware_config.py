"""Tests for hardware/hardware_config.py - the sudo wrapper around
scripts/meshcenter-hw-config, and the reboot-required pending-setup state
persisted through storage/json_store.py.

subprocess is mocked throughout (the actual bash script's own idempotency/
backup logic is exercised directly in a functional shell test, not here -
this module only needs to prove its own Python-side wiring: correct
argv, error handling, and the pending-state state machine). No server.py
import needed.
"""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from hardware import hardware_config


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------- _run_helper() / enable_i2c() / configure_rtc() ----------------

def test_enable_i2c_success_writes_pending_record(tmp_path):
    mock_run = MagicMock(return_value=_completed(stdout="added: dtparam=i2c_arm=on"))
    with patch("subprocess.run", mock_run):
        result = hardware_config.enable_i2c(str(tmp_path))

    assert result == {"ok": True, "stdout": "added: dtparam=i2c_arm=on"}
    args, kwargs = mock_run.call_args
    assert args[0] == ["sudo", "-n", hardware_config.HELPER_PATH, "enable-i2c"]
    assert kwargs.get("shell") is not True

    pending_file = tmp_path / "hardware_pending.json"
    record = json.loads(pending_file.read_text(encoding="utf-8"))
    assert record["action"] == "enable_i2c"
    assert isinstance(record["set_at"], (int, float))


def test_configure_rtc_success_writes_pending_record_with_model(tmp_path):
    with patch("subprocess.run", return_value=_completed(stdout="added: dtoverlay=i2c-rtc,ds3231")):
        result = hardware_config.configure_rtc(str(tmp_path), "ds3231")

    assert result["ok"] is True
    record = json.loads((tmp_path / "hardware_pending.json").read_text(encoding="utf-8"))
    assert record["action"] == "configure_rtc"
    assert record["model"] == "ds3231"


def test_helper_failure_does_not_write_pending_record(tmp_path):
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="boom")):
        result = hardware_config.enable_i2c(str(tmp_path))

    assert result["ok"] is False
    assert result["reason"] == "boom"
    assert hardware_config.get_pending(str(tmp_path)) is None


def test_sudo_not_configured_gives_actionable_reason(tmp_path):
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="sudo: a password is required")):
        result = hardware_config.enable_i2c(str(tmp_path))

    assert result["ok"] is False
    assert "meshcenter-hw.sudoers" in result["reason"]


def test_helper_timeout_reported_not_raised(tmp_path):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sudo", timeout=15)):
        result = hardware_config.enable_i2c(str(tmp_path))
    assert result["ok"] is False
    assert "timed out" in result["reason"]


def test_status_calls_helper_status_subcommand():
    mock_run = MagicMock(
        return_value=_completed(
            stdout='{"i2c_enabled": true, "rtc_overlay": "ds3231", "i2c_dev_module": true}'
        )
    )
    with patch("subprocess.run", mock_run):
        result = hardware_config.helper_status()
    assert result == {
        "ok": True,
        "i2c_enabled": True,
        "rtc_overlay": "ds3231",
        "i2c_dev_module": True,
    }
    args, _ = mock_run.call_args
    assert args[0] == ["sudo", "-n", hardware_config.HELPER_PATH, "status"]


def test_status_helper_failure_passed_through_unparsed(tmp_path):
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="boom")):
        result = hardware_config.helper_status()
    assert result == {"ok": False, "reason": "boom"}


def test_status_invalid_json_reported_not_raised():
    with patch("subprocess.run", return_value=_completed(stdout="not json")):
        result = hardware_config.helper_status()
    assert result["ok"] is False
    assert "invalid JSON" in result["reason"]


def test_status_non_object_json_reported():
    with patch("subprocess.run", return_value=_completed(stdout="[1, 2, 3]")):
        result = hardware_config.helper_status()
    assert result["ok"] is False
    assert "non-object" in result["reason"]


# ---------------- pending-setup state machine ----------------

def test_get_pending_none_when_nothing_recorded(tmp_path):
    assert hardware_config.get_pending(str(tmp_path)) is None


def test_reconcile_pending_none_when_nothing_recorded(tmp_path):
    assert hardware_config.reconcile_pending(str(tmp_path)) is None


def test_reconcile_pending_still_waiting_before_reboot(tmp_path):
    with patch("subprocess.run", return_value=_completed(stdout="added: dtparam=i2c_arm=on")):
        hardware_config.enable_i2c(str(tmp_path))

    # boot_time_unix well before set_at simulates "the machine has been up
    # since before this was recorded - no reboot has happened yet".
    same_session_boot_time = time.time() - 3600
    result = hardware_config.reconcile_pending(str(tmp_path), boot_time_unix=same_session_boot_time)

    assert result["resolved"] is False
    # Record must still be there, untouched, for the next poll.
    assert hardware_config.get_pending(str(tmp_path)) is not None


def test_reconcile_pending_rebooted_and_confirmed_enable_i2c(tmp_path):
    with patch("subprocess.run", return_value=_completed(stdout="added: dtparam=i2c_arm=on")):
        hardware_config.enable_i2c(str(tmp_path))

    rebooted_boot_time = time.time() + 10  # "rebooted" after set_at, so > set_at
    with patch("hardware.i2c_service.scan_bus", return_value={"ok": True, "bus": 1, "addresses": []}):
        result = hardware_config.reconcile_pending(str(tmp_path), boot_time_unix=rebooted_boot_time)

    assert result["resolved"] is True
    assert result["confirmed"] is True
    # Pending record cleared once resolved either way.
    assert hardware_config.get_pending(str(tmp_path)) is None


def test_reconcile_pending_rebooted_but_not_confirmed_enable_i2c(tmp_path):
    with patch("subprocess.run", return_value=_completed(stdout="added: dtparam=i2c_arm=on")):
        hardware_config.enable_i2c(str(tmp_path))

    rebooted_boot_time = time.time() + 10
    with patch(
        "hardware.i2c_service.scan_bus",
        return_value={"ok": False, "bus": 1, "reason": "bus 1 may not exist"},
    ):
        result = hardware_config.reconcile_pending(str(tmp_path), boot_time_unix=rebooted_boot_time)

    assert result["resolved"] is True
    assert result["confirmed"] is False
    assert hardware_config.get_pending(str(tmp_path)) is None


def test_reconcile_pending_rebooted_and_confirmed_configure_rtc(tmp_path):
    with patch("subprocess.run", return_value=_completed(stdout="added: dtoverlay=i2c-rtc,ds3231")):
        hardware_config.configure_rtc(str(tmp_path), "ds3231")

    rebooted_boot_time = time.time() + 10
    confirmed_status = {
        "ok": True,
        "stages": {"detected": {"ok": True}, "configured": {"ok": True}, "readable": {"ok": True}},
    }
    with patch("hardware.rtc_service.get_status", return_value=confirmed_status):
        result = hardware_config.reconcile_pending(str(tmp_path), boot_time_unix=rebooted_boot_time)

    assert result["resolved"] is True
    assert result["confirmed"] is True


def test_reconcile_pending_rebooted_but_not_confirmed_configure_rtc(tmp_path):
    with patch("subprocess.run", return_value=_completed(stdout="added: dtoverlay=i2c-rtc,ds3231")):
        hardware_config.configure_rtc(str(tmp_path), "ds3231")

    rebooted_boot_time = time.time() + 10
    unconfirmed_status = {
        "ok": True,
        "stages": {"detected": {"ok": True}, "configured": {"ok": False}, "readable": {"ok": False}},
    }
    with patch("hardware.rtc_service.get_status", return_value=unconfirmed_status):
        result = hardware_config.reconcile_pending(str(tmp_path), boot_time_unix=rebooted_boot_time)

    assert result["resolved"] is True
    assert result["confirmed"] is False


def test_reconcile_pending_is_idempotent_after_resolution(tmp_path):
    with patch("subprocess.run", return_value=_completed(stdout="added: dtparam=i2c_arm=on")):
        hardware_config.enable_i2c(str(tmp_path))

    rebooted_boot_time = time.time() + 10
    with patch("hardware.i2c_service.scan_bus", return_value={"ok": True, "bus": 1, "addresses": []}):
        hardware_config.reconcile_pending(str(tmp_path), boot_time_unix=rebooted_boot_time)

    # Second call after resolution: nothing pending any more, must be a
    # clean no-op, not an error.
    assert hardware_config.reconcile_pending(str(tmp_path), boot_time_unix=rebooted_boot_time) is None


def test_pending_file_is_instance_scoped_not_per_profile(tmp_path):
    # Just asserts the file lands directly under the given data_dir with a
    # fixed name, not nested under a profile-id subdirectory - the actual
    # profile-scoping decision is made by whatever data_dir the caller
    # passes in (server.py passes the instance-scoped DATA_DIR, not a
    # profile directory).
    with patch("subprocess.run", return_value=_completed(stdout="ok")):
        hardware_config.enable_i2c(str(tmp_path))
    assert (tmp_path / "hardware_pending.json").exists()
