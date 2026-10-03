"""Tests for meshsrv/serial_reconnect.py's pure helpers - disconnect-line
detection, device-identity snapshotting, by-id resolution, and the
identity-retry backoff schedule. See that module's own docstring for the
H2-C Phase 2 background these exist to support.
"""
import os

from meshsrv.serial_reconnect import (
    DeviceIdentity,
    capture_device_identity,
    device_identity_changed,
    find_by_id_for_port,
    identity_retry_delay,
    line_signals_disconnect,
    resolve_by_id_target,
)

# Captured verbatim from a real journal line on dev (192.168.2.104),
# 2026-10-03 20:33:16, during the H2-C Phase 1 live replug investigation -
# the [LISTEN WARN] prefix is server.py's own _handle_listener_line()
# classification, not part of the raw CLI stdout line this function
# actually sees.
REAL_DISCONNECT_LINE = (
    "WARNING file:stream_interface.py __reader line:233 Meshtastic serial "
    "port disconnected, disconnecting... device reports readiness to read "
    "but returned no data (device disconnected or multiple access on port?)"
)


def test_line_signals_disconnect_matches_the_real_journal_line():
    assert line_signals_disconnect(REAL_DISCONNECT_LINE) is True


def test_line_signals_disconnect_false_for_ordinary_lines():
    assert line_signals_disconnect("Received nodeinfo: id=!aabbccdd") is False
    assert line_signals_disconnect("") is False
    assert line_signals_disconnect(None) is False


def test_line_signals_disconnect_false_for_an_unrelated_warning():
    assert line_signals_disconnect("WARNING something else entirely happened") is False


def test_capture_device_identity_returns_none_for_a_missing_path():
    assert capture_device_identity("/does/not/exist/anywhere") is None


def test_capture_device_identity_reads_a_real_file(tmp_path):
    fake_port = tmp_path / "fake_tty"
    fake_port.write_text("")

    identity = capture_device_identity(str(fake_port))

    assert identity is not None
    real_stat = os.stat(str(fake_port))
    assert identity.st_ino == real_stat.st_ino
    assert identity.st_rdev == getattr(real_stat, "st_rdev", 0)


def test_capture_device_identity_follows_symlinks(tmp_path):
    real_port = tmp_path / "ttyACM0"
    real_port.write_text("")
    link = tmp_path / "by_id_link"
    try:
        link.symlink_to(real_port)
    except OSError:
        import pytest
        pytest.skip("symlink creation needs privileges on this platform")

    via_link = capture_device_identity(str(link))
    via_real = capture_device_identity(str(real_port))

    assert via_link == via_real


def test_device_identity_changed_true_when_nothing_captured_yet():
    assert device_identity_changed(None, "/dev/ttyACM0") is True


def test_device_identity_changed_true_when_device_is_now_gone(tmp_path):
    port = tmp_path / "ttyACM0"
    port.write_text("")
    old = capture_device_identity(str(port))
    port.unlink()

    assert device_identity_changed(old, str(port)) is True


def test_device_identity_changed_false_for_the_same_unchanged_device(tmp_path):
    port = tmp_path / "ttyACM0"
    port.write_text("")
    old = capture_device_identity(str(port))

    assert device_identity_changed(old, str(port)) is False


def test_device_identity_changed_true_for_a_different_device_at_the_same_path(tmp_path):
    """The actual replug case: the old inode is gone, a new file (new
    inode) now sits at the same path - a different physical device
    re-enumerated under the same name."""
    port = tmp_path / "ttyACM0"
    port.write_text("")
    old = capture_device_identity(str(port))
    port.unlink()
    port.write_text("")  # same path, brand new inode

    assert device_identity_changed(old, str(port)) is True


def test_resolve_by_id_target_returns_empty_for_blank_or_missing():
    assert resolve_by_id_target("") == ""
    assert resolve_by_id_target("   ") == ""
    assert resolve_by_id_target("/dev/serial/by-id/does-not-exist") == ""


def test_resolve_by_id_target_resolves_a_real_symlink(tmp_path):
    real_port = tmp_path / "ttyACM0"
    real_port.write_text("")
    link = tmp_path / "usb-Some_Radio-if00"
    try:
        link.symlink_to(real_port)
    except OSError:
        import pytest
        pytest.skip("symlink creation needs privileges on this platform")

    assert resolve_by_id_target(str(link)) == os.path.realpath(str(real_port))


def test_find_by_id_for_port_returns_empty_when_no_by_id_directory():
    assert find_by_id_for_port("/dev/ttyACM0") == ""


def test_find_by_id_for_port_returns_empty_for_blank_port():
    assert find_by_id_for_port("") == ""


def test_find_by_id_for_port_finds_the_matching_link(tmp_path):
    real_port = tmp_path / "ttyACM0"
    real_port.write_text("")
    by_id_dir = tmp_path / "by-id"
    by_id_dir.mkdir()
    link = by_id_dir / "usb-Some_Radio-if00"
    try:
        link.symlink_to(real_port)
    except OSError:
        import pytest
        pytest.skip("symlink creation needs privileges on this platform")

    result = find_by_id_for_port(str(real_port), by_id_dir=str(by_id_dir))

    assert result == str(link)


def test_find_by_id_for_port_no_match_among_unrelated_links(tmp_path):
    real_port = tmp_path / "ttyACM0"
    real_port.write_text("")
    other_device = tmp_path / "ttyACM1"
    other_device.write_text("")
    by_id_dir = tmp_path / "by-id"
    by_id_dir.mkdir()
    link = by_id_dir / "usb-Other_Radio-if00"
    try:
        link.symlink_to(other_device)
    except OSError:
        import pytest
        pytest.skip("symlink creation needs privileges on this platform")

    assert find_by_id_for_port(str(real_port), by_id_dir=str(by_id_dir)) == ""


def test_identity_retry_delay_follows_the_schedule_then_settles():
    assert identity_retry_delay(0) == 5.0
    assert identity_retry_delay(1) == 10.0
    assert identity_retry_delay(2) == 30.0
    assert identity_retry_delay(3) == 60.0
    assert identity_retry_delay(4) == 60.0
    assert identity_retry_delay(100) == 60.0


def test_identity_retry_delay_clamps_negative_attempts_to_the_first_step():
    assert identity_retry_delay(-1) == 5.0
