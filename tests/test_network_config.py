"""Tests for meshsrv/network_config.py - the sudo wrapper around
scripts/meshcenter-network-helper (P1 #7/#8 stabilization follow-up).

subprocess is mocked throughout (the actual bash script's own argument-
count guards and idempotent-profile-replace behavior are exercised
directly against the real script in tests/test_network_helper_script.py,
not here - this module only needs to prove its own Python-side wiring:
correct argv, correct stdin passthrough for the password, and error
handling). No server.py import needed.
"""

import subprocess
from unittest.mock import MagicMock, patch

from meshsrv import network_config


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------- list_wifi_connections() ----------------

def test_list_wifi_connections_filters_to_wireless_type():
    mock_run = MagicMock(
        return_value=_completed(
            stdout="HomeWifi:802-11-wireless\nEthernet:802-3-ethernet\nOfficeWifi:802-11-wireless\n"
        )
    )
    with patch("subprocess.run", mock_run):
        result = network_config.list_wifi_connections()

    assert result == {"ok": True, "ssids": {"HomeWifi", "OfficeWifi"}}
    args, kwargs = mock_run.call_args
    assert args[0] == ["sudo", "-n", network_config.HELPER_PATH, "list-connections"]
    assert kwargs.get("input") is None


def test_list_wifi_connections_helper_failure_passed_through():
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="boom")):
        result = network_config.list_wifi_connections()
    assert result == {"ok": False, "reason": "boom"}


# ---------------- scan() ----------------

def test_scan_returns_raw_stdout():
    mock_run = MagicMock(return_value=_completed(stdout="BSS aa:bb:cc:dd:ee:ff(on wlan0)\n\tSSID: TestNet\n"))
    with patch("subprocess.run", mock_run):
        result = network_config.scan()

    assert result["ok"] is True
    assert "TestNet" in result["stdout"]
    args, _ = mock_run.call_args
    assert args[0] == ["sudo", "-n", network_config.HELPER_PATH, "scan"]


# ---------------- connect() - the P1 #8 password-not-in-argv guarantee ----------------

def test_connect_sends_password_via_stdin_not_argv():
    mock_run = MagicMock(return_value=_completed(stdout="Connection successfully activated"))
    with patch("subprocess.run", mock_run):
        result = network_config.connect("HomeWifi", "super-secret-password")

    assert result["ok"] is True
    args, kwargs = mock_run.call_args
    # The password must appear nowhere in the argv list - this is the
    # entire point of P1 #8 (the old code put it in `nmcli ... password
    # <pw>`, visible via ps aux / /proc/<pid>/cmdline).
    assert args[0] == ["sudo", "-n", network_config.HELPER_PATH, "connect", "HomeWifi"]
    assert "super-secret-password" not in args[0]
    assert all("super-secret-password" not in str(a) for a in args[0])
    # It must instead be passed as stdin input.
    assert kwargs.get("input") == "super-secret-password"


def test_connect_open_network_sends_empty_password_via_stdin():
    mock_run = MagicMock(return_value=_completed(stdout="Connection successfully activated"))
    with patch("subprocess.run", mock_run):
        network_config.connect("OpenNet")

    args, kwargs = mock_run.call_args
    assert args[0] == ["sudo", "-n", network_config.HELPER_PATH, "connect", "OpenNet"]
    assert kwargs.get("input") == ""


def test_connect_uses_longer_timeout_than_other_operations():
    mock_run = MagicMock(return_value=_completed(stdout="ok"))
    with patch("subprocess.run", mock_run):
        network_config.connect("HomeWifi", "pw")
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") == network_config.CONNECT_TIMEOUT
    assert network_config.CONNECT_TIMEOUT > network_config.HELPER_TIMEOUT


def test_connect_failure_reason_never_echoes_the_password():
    # Even on failure, the reason string must not have been built by
    # interpolating the password anywhere (e.g. "connect HomeWifi pw
    # failed") - it's whatever stderr the helper produced, which never saw
    # the password as an argument in the first place.
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="Secrets were required, but not provided")):
        result = network_config.connect("HomeWifi", "super-secret-password")
    assert result["ok"] is False
    assert "super-secret-password" not in result["reason"]


# ---------------- forget() ----------------

def test_forget_calls_helper_with_ssid_argv():
    mock_run = MagicMock(return_value=_completed(stdout="deleted"))
    with patch("subprocess.run", mock_run):
        result = network_config.forget("HomeWifi")

    assert result == {"ok": True, "stdout": "deleted"}
    args, _ = mock_run.call_args
    assert args[0] == ["sudo", "-n", network_config.HELPER_PATH, "forget", "HomeWifi"]


# ---------------- shared _run_helper() error handling ----------------

def test_sudo_not_configured_gives_actionable_reason():
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="sudo: a password is required")):
        result = network_config.forget("HomeWifi")
    assert result["ok"] is False
    assert "meshcenter-wifi.sudoers" in result["reason"]


def test_helper_timeout_reported_not_raised():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sudo", timeout=15)):
        result = network_config.list_wifi_connections()
    assert result["ok"] is False
    assert "timed out" in result["reason"]


def test_sudo_missing_reported_not_raised():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        result = network_config.scan()
    assert result["ok"] is False
    assert "sudo is not available" in result["reason"]


def test_unexpected_exception_reported_not_raised():
    with patch("subprocess.run", side_effect=RuntimeError("boom")):
        result = network_config.forget("HomeWifi")
    assert result["ok"] is False
    assert "boom" in result["reason"]
