"""Tests for scripts/manage_installation_id.py - Task F (final stage) of the
installation-ID rollout, plus its corrective follow-up (an external audit
found show wasn't actually read-only, the service-check ran after a
write-capable call, and --force raced with
meshsrv/installation_time_assignment.py's background thread - see that
PR's own investigation report for the full trace). No prior Python CLI
script in this repo has test coverage (checked - scripts/check-i18n.py,
scripts/check_startup_calls.py, the _smoke_test_*_harness.py files all
have none), so this is the first.

Per the investigation report: test the core logic functions
(show_installation/regenerate_installation) against a real InstanceManager
in a tmp_path, injecting is_service_active/confirm rather than touching
systemctl or input() - not testing argparse's own wiring in detail. The
original test suite's fixture always pre-created a valid instance.json via
load_or_create() before every test, which is exactly why the "show isn't
read-only" bug never surfaced here - this file now covers a missing and a
corrupted instance.json specifically, without that fixture.

manage_installation_id.py defers `from config import DATA_DIR` to main()
specifically so importing the rest of the module never needs config.py -
these tests rely on that and never touch config.py at all.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import manage_installation_id as cli  # noqa: E402

from meshsrv.installation_identity import is_valid_installation_id
from meshsrv.instance_manager import InstanceManager


@pytest.fixture
def manager(tmp_path):
    m = InstanceManager(tmp_path / "instance.json")
    m.load_or_create({})
    return m


def test_show_installation_reports_current_id(manager):
    identity = manager.get()
    output = cli.show_installation(manager)
    assert identity["installation"]["id"] in output
    assert "pending" in output
    assert "(not yet confirmed)" in output


def test_show_installation_reports_resolved_time_source(manager):
    identity = manager.get()
    updated = dict(identity)
    updated["installation"] = dict(identity["installation"])
    updated["installation"]["assigned_at"] = "2026-09-01T07:00:00+00:00"
    updated["installation"]["time_source"] = "system_ntp"
    manager.save(updated)

    output = cli.show_installation(manager)
    assert "system_ntp" in output
    assert "2026-09-01T07:00:00+00:00" in output


def test_show_installation_against_a_missing_file_does_not_create_it(tmp_path):
    instance_path = tmp_path / "instance.json"
    fresh_manager = InstanceManager(instance_path)  # load_or_create() never called

    output = cli.show_installation(fresh_manager)

    assert "No installation identity found yet" in output
    assert not instance_path.exists()


def test_show_installation_against_a_corrupted_file_leaves_bytes_unchanged(tmp_path):
    instance_path = tmp_path / "instance.json"
    corrupted_bytes = b"{not valid json at all"
    instance_path.write_bytes(corrupted_bytes)
    fresh_manager = InstanceManager(instance_path)

    output = cli.show_installation(fresh_manager)

    assert "No installation identity found yet" in output
    assert instance_path.read_bytes() == corrupted_bytes


def test_regenerate_refuses_while_service_active_with_no_override(manager):
    old_id = manager.get()["installation"]["id"]

    exit_code, message = cli.regenerate_installation(
        manager,
        is_service_active_fn=lambda: True,
        confirm_fn=lambda old: True,
        assume_yes=True,
    )

    assert exit_code == 1
    assert "currently running" in message
    assert "force" not in message.lower()
    assert manager.get()["installation"]["id"] == old_id


def test_regenerate_installation_has_no_force_parameter():
    import inspect
    signature = inspect.signature(cli.regenerate_installation)
    assert "force" not in signature.parameters


def test_regenerate_subcommand_has_no_force_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["regenerate", "--yes"])
    assert not hasattr(args, "force")
    with pytest.raises(SystemExit):
        parser.parse_args(["regenerate", "--force"])


def test_regenerate_aborts_when_confirmation_declined(manager):
    old_id = manager.get()["installation"]["id"]

    exit_code, message = cli.regenerate_installation(
        manager,
        is_service_active_fn=lambda: False,
        confirm_fn=lambda old: False,
        assume_yes=False,
    )

    assert exit_code == 1
    assert "Aborted" in message
    assert manager.get()["installation"]["id"] == old_id


def test_regenerate_skips_confirmation_with_assume_yes(manager):
    confirm_calls = []

    exit_code, message = cli.regenerate_installation(
        manager,
        is_service_active_fn=lambda: False,
        confirm_fn=lambda old: confirm_calls.append(old) or True,
        assume_yes=True,
    )

    assert exit_code == 0
    assert confirm_calls == []


def test_regenerate_sets_expected_fields_on_success(manager):
    old_id = manager.get()["installation"]["id"]

    exit_code, message = cli.regenerate_installation(
        manager,
        is_service_active_fn=lambda: False,
        confirm_fn=lambda old: True,
        assume_yes=False,
    )

    assert exit_code == 0
    installation = manager.get()["installation"]
    assert installation["id"] != old_id
    assert is_valid_installation_id(installation["id"])
    assert installation["assigned_at"] is None
    assert installation["time_source"] == "pending"
    assert installation["assignment_reason"] == "regeneration"
    assert old_id in message
    assert installation["id"] in message


def test_regenerate_preserves_every_other_existing_field(manager):
    identity_before = manager.get()
    updated = dict(identity_before)
    updated["active_profile_id"] = "067a40fa"
    updated["radio"] = dict(identity_before["radio"])
    updated["radio"]["node_id"] = "!067a40fa"
    manager.save(updated)

    cli.regenerate_installation(
        manager,
        is_service_active_fn=lambda: False,
        confirm_fn=lambda old: True,
        assume_yes=False,
    )

    identity = manager.get()
    assert identity["active_profile_id"] == "067a40fa"
    assert identity["radio"]["node_id"] == "!067a40fa"


def test_regenerate_service_check_runs_before_any_write_capable_call(tmp_path):
    # Call-order spy: is_service_active_fn must be invoked while the file is
    # still exactly as it started (missing) - proving the check happens
    # before load_or_create()/save(), not after. If the ordering regressed
    # back to the original bug, the file would already exist by the time
    # is_service_active_fn runs.
    instance_path = tmp_path / "instance.json"
    fresh_manager = InstanceManager(instance_path)
    file_existed_at_check_time = []

    def spy_is_active():
        file_existed_at_check_time.append(instance_path.exists())
        return True

    exit_code, _ = cli.regenerate_installation(
        fresh_manager,
        is_service_active_fn=spy_is_active,
        confirm_fn=lambda old: True,
        assume_yes=True,
    )

    assert exit_code == 1
    assert file_existed_at_check_time == [False]
    assert not instance_path.exists()


def test_regenerate_against_a_missing_file_works_end_to_end(tmp_path):
    instance_path = tmp_path / "instance.json"
    fresh_manager = InstanceManager(instance_path)

    exit_code, message = cli.regenerate_installation(
        fresh_manager,
        is_service_active_fn=lambda: False,
        confirm_fn=lambda old: True,
        assume_yes=False,
    )

    assert exit_code == 0
    assert instance_path.exists()
    installation = fresh_manager.get()["installation"]
    assert is_valid_installation_id(installation["id"])
    assert installation["assignment_reason"] == "regeneration"


def test_regenerate_against_a_corrupted_file_works_end_to_end(tmp_path):
    instance_path = tmp_path / "instance.json"
    instance_path.write_bytes(b"{not valid json at all")
    fresh_manager = InstanceManager(instance_path)

    exit_code, message = cli.regenerate_installation(
        fresh_manager,
        is_service_active_fn=lambda: False,
        confirm_fn=lambda old: True,
        assume_yes=False,
    )

    assert exit_code == 0
    installation = fresh_manager.get()["installation"]
    assert is_valid_installation_id(installation["id"])
    assert installation["assignment_reason"] == "regeneration"
    assert "(none)" in message  # old id was unreadable from the corrupted file


def test_is_service_active_returns_false_when_systemctl_unavailable(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.is_service_active() is False


def test_instance_file_path_is_data_dir_slash_instance_json(tmp_path):
    assert cli.instance_file_path(tmp_path) == tmp_path / "instance.json"
