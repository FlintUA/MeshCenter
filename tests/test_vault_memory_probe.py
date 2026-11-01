"""The memory probe of docs/MCAttach_Vault.md runs, uses a throwaway workspace and reports that nothing is retained."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="the probe reads /proc (Linux)")
def test_the_probe_runs_and_resident_memory_returns_after_a_derivation():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "vault_memory_probe.py"), "32", "1", "2", "trim"],
                         capture_output=True, text=True, timeout=120, cwd=str(ROOT)).stdout
    assert "baseline (imports)" in out and "after header.create (enable)" in out and "unlock 2" in out
    rss = {m.group(1): float(m.group(2)) for m in re.finditer(r"^(.{34}) RSS\s+([0-9.]+) MB", out, re.M)}
    baseline = next(v for k, v in rss.items() if k.startswith("baseline"))
    final = rss[[k for k in rss if k.startswith("after threads + gc")][0]]
    assert final - baseline < 12, "a 32 MiB derivation leaves no large allocation behind: %s" % out


def test_the_probe_refuses_politely_without_proc():
    if os.path.exists("/proc/self/status"):
        pytest.skip("this platform has /proc")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "vault_memory_probe.py")], capture_output=True, text=True)
    assert result.returncode != 0 and "Linux" in (result.stderr + result.stdout)
