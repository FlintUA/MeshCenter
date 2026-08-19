"""Tests for meshsrv/meshtastic_transport.py's get_info() - the consolidated
`meshtastic --info` call site used to replace 5 different call sites in
server.py/meshsrv/radio_identity.py.

No server.py import needed. Exercises a real subprocess against a fake
executable CLI shell script written to tmp_path (same #!/bin/sh + chmod
pattern tests/conftest.py already uses for its own fake_meshtastic_cli) -
not mocked - so --port inclusion/omission and a real
subprocess.TimeoutExpired are both genuine.

Skipped entirely on platforms that can't exec a #!/bin/sh script directly
(e.g. a Windows dev box running the suite) - same rationale as
test_runtime_lock.py's fcntl skip: this application only ever runs on
Linux/the Pi in production, and CI runs on a Linux runner.
"""

import os
import stat
import subprocess

import pytest

from meshsrv.meshtastic_transport import get_info


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="requires a POSIX shell to exec the fake CLI script"
)


def _write_fake_cli(tmp_path, body):
    script = tmp_path / "fake_meshtastic"
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def test_get_info_includes_port_when_serial_port_given(tmp_path):
    cli = _write_fake_cli(tmp_path, "#!/bin/sh\necho \"$@\"\n")

    result = get_info(cli, serial_port="/dev/ttyACM0", timeout=5)

    assert "--port /dev/ttyACM0" in result.stdout
    assert "--info" in result.stdout


def test_get_info_omits_port_when_not_given(tmp_path):
    cli = _write_fake_cli(tmp_path, "#!/bin/sh\necho \"$@\"\n")

    result = get_info(cli, timeout=5)

    assert "--port" not in result.stdout
    assert result.stdout.strip() == "--info"


def test_get_info_empty_serial_port_treated_as_omitted(tmp_path):
    cli = _write_fake_cli(tmp_path, "#!/bin/sh\necho \"$@\"\n")

    result = get_info(cli, serial_port="", timeout=5)

    assert "--port" not in result.stdout


def test_get_info_returns_stdout_and_stderr(tmp_path):
    cli = _write_fake_cli(
        tmp_path,
        "#!/bin/sh\necho out-line\necho err-line 1>&2\n",
    )

    result = get_info(cli, timeout=5)

    assert "out-line" in result.stdout
    assert "err-line" in result.stderr


def test_get_info_raises_timeout_expired_on_slow_cli(tmp_path):
    cli = _write_fake_cli(tmp_path, "#!/bin/sh\nsleep 5\n")

    with pytest.raises(subprocess.TimeoutExpired):
        get_info(cli, timeout=0.2)
