"""DASH1 guard as a test: the repository's own texts contain no em/en dashes.

The rule (CLAUDE.md, Writing style): plain hyphen-minus only. The scan itself lives in
scripts/check_no_long_dashes.py (also run as its own CI step); this keeps `pytest` locally
honest too and pins the guard's behaviour.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_no_long_dashes as guard  # noqa: E402

EM = "\u2014"
EN = "\u2013"


def test_repository_texts_have_no_long_dashes():
    violations = guard.find_violations()
    assert not violations, "use a plain hyphen:\n" + "\n".join(
        f"  {p}:{n}: {name}: {text}" for p, n, name, text in violations[:30]
    )


def _write(tmp_path, name, content):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return name


def test_guard_flags_em_and_en_dashes_with_file_and_line(tmp_path):
    a = _write(tmp_path, "docs/a.md", f"fine\nbroken {EM} here\nrange 5{EN}20\n")
    found = guard.find_violations(tmp_path, [a])
    assert [(p, n) for p, n, _, _ in found] == [("docs/a.md", 2), ("docs/a.md", 3)]
    assert "em dash" in found[0][2] and "en dash" in found[1][2]


def test_guard_ignores_plain_hyphens_arrows_and_ellipsis(tmp_path):
    a = _write(tmp_path, "docs/b.md", "a - b, 5-20, x -> y, \u2192, \u2026, -- double\n")
    assert guard.find_violations(tmp_path, [a]) == []


def test_guard_allowlist_covers_licences_vendored_fixtures_and_data_lines(tmp_path):
    files = [
        _write(tmp_path, "LICENSE", f"MIT {EM} text\n"),
        _write(tmp_path, "adapters/meshtastic/LICENSE", f"GPL {EM} text\n"),
        _write(tmp_path, "licenses/Chart.js-MIT.txt", f"x {EM} y\n"),
        _write(tmp_path, "static/chart.umd.min.js", f"/* {EM} */\n"),
        _write(tmp_path, "tests/fixtures/info.txt", f"name {EM} node\n"),
    ]
    assert guard.find_violations(tmp_path, files) == []
    data = _write(tmp_path, "tests/test_mime_allowlist.py", f'v.feed("caf\u00e9 {EM} \u65e5\u672c\u8a9e")\nbad {EM}\n')
    found = guard.find_violations(tmp_path, [data])
    assert [(n) for _, n, _, _ in found] == [2], "only the allowlisted data line is exempt"


def test_guard_skips_binary_and_non_utf8_files(tmp_path):
    (tmp_path / "x.png").write_bytes(b"\x89PNG\x00" + EM.encode("utf-8"))
    (tmp_path / "y.txt").write_bytes(b"\xff\xfe" + EM.encode("utf-8"))
    assert guard.find_violations(tmp_path, ["x.png", "y.txt"]) == []


def test_the_guard_script_itself_is_clean():
    own = Path(guard.__file__).read_text(encoding="utf-8")
    assert EM not in own and EN not in own
