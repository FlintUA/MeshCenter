"""Functional tests for scripts/meshcenter-network-helper's own argument-
count guards (P1 #7/#8 stabilization follow-up).

These run the REAL script via `bash <path> ...` (not the shebang/exec
path, so this works the same on the Windows dev machine's Git Bash as on
Linux CI - no os.name skipif needed, just a `bash` binary on PATH). They
deliberately do NOT need nmcli/iw/root: every case here is rejected by
require_argc()/the empty-SSID checks before the script ever calls out to
nmcli, so these are safe to run anywhere bash is available, unlike a real
connect/scan/forget which needs an actual wireless interface.

This is the concrete verification requested during review: sudoers'
`connect *` / `forget *` glob matches the WHOLE command line as a string,
not per-argv-position, so it would equally allow
`meshcenter-network-helper connect somessid --extra-dangerous-flag`
through at the sudo layer - the script itself must be the thing that
refuses anything past exactly one SSID argument, not just "reasoning"
about what sudoers would technically permit.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HELPER_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "meshcenter-network-helper")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requires a bash binary on PATH")


def _run(*args, stdin_text=""):
    return subprocess.run(
        ["bash", HELPER_SCRIPT, *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=5,
    )


# ---------------- argument-count guards (the review's explicit ask) ----------------

def test_connect_rejects_extra_argv_past_the_ssid():
    result = _run("connect", "HomeWifi", "--extra-dangerous-flag")
    assert result.returncode != 0
    assert "exactly one argument" in result.stderr


def test_connect_rejects_missing_ssid():
    result = _run("connect")
    assert result.returncode != 0
    assert "exactly one argument" in result.stderr


def test_forget_rejects_extra_argv_past_the_ssid():
    result = _run("forget", "HomeWifi", "somethingelse")
    assert result.returncode != 0
    assert "exactly one argument" in result.stderr


def test_forget_rejects_missing_ssid():
    result = _run("forget")
    assert result.returncode != 0
    assert "exactly one argument" in result.stderr


def test_list_connections_rejects_any_argument():
    result = _run("list-connections", "unexpected")
    assert result.returncode != 0
    assert "takes no arguments" in result.stderr


def test_scan_rejects_any_argument():
    result = _run("scan", "unexpected")
    assert result.returncode != 0
    assert "takes no arguments" in result.stderr


def test_connect_rejects_empty_ssid():
    result = _run("connect", "")
    assert result.returncode != 0
    assert "non-empty SSID" in result.stderr


def test_forget_rejects_empty_ssid():
    result = _run("forget", "")
    assert result.returncode != 0
    assert "non-empty SSID" in result.stderr


# ---------------- subcommand dispatch ----------------

def test_no_subcommand_rejected():
    result = _run()
    assert result.returncode != 0
    assert "no subcommand given" in result.stderr


def test_unknown_subcommand_rejected():
    result = _run("delete-everything")
    assert result.returncode != 0
    assert "unknown subcommand" in result.stderr
