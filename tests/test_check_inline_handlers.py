"""Tests for scripts/check_inline_handlers.py (H2-D, F1.3): the XSS
inline-handler ratchet must catch a genuinely new inline handler, catch a
stale allowlist entry, tolerate duplicate-text occurrences under one entry,
and never be fooled by a handler-shaped string sitting inside a comment
(two real sites in static/chat.js are comments documenting the escaping
rules, not live markup - see that script's own module docstring).
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_inline_handlers as mod


def test_strip_comments_removes_line_and_block_comments():
    source = (
        "const x = 1; // onclick=\"pwn('${x}')\"\n"
        "/* a block\n"
        "   onclick=\"pwn('${y}')\" */\n"
        "const z = `onclick=\"real('${z}')\"`;\n"
    )
    stripped = mod.strip_comments(source)
    assert "pwn" not in stripped
    assert "real" in stripped


def test_find_handlers_matches_double_and_single_quoted():
    source = (
        "html += `<button onclick=\"fn('${escapeJsString(x)}')\">go</button>`;\n"
        "html += `<button onclick='other(${y}, event)'>go</button>`;\n"
    )
    found = mod.find_handlers(source)
    assert found == {
        "onclick=\"fn('${escapeJsString(x)}')\"",
        "onclick='other(${y}, event)'",
    }


def test_find_handlers_ignores_data_attribute_substring_match():
    # Real false positive from the H2-D audit: "data-contact=" contains the
    # substring "on" + "tact", which an un-anchored on[a-z]+= pattern would
    # mis-match as if it were an event-handler attribute.
    source = 'html += `<button data-contact="${escapeHtml(t.id)}">x</button>`;'
    assert mod.find_handlers(source) == set()


def test_find_handlers_treats_duplicate_text_as_one_entry():
    source = (
        "a = `<button onclick=\"fn('${escapeJsString(x)}')\">1</button>`;\n"
        "b = `<button onclick=\"fn('${escapeJsString(x)}')\">2</button>`;\n"
    )
    assert len(mod.find_handlers(source)) == 1


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """One scanned file + its own allowlist, isolated under tmp_path -
    mirrors test_check_asset_cache_busting.py's fixture shape but doesn't
    need a real git repo since this checker never touches git."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    js_path = static_dir / "chat.js"
    js_path.write_text("html += `<button onclick=\"fn('${escapeJsString(x)}')\">go</button>`;\n", encoding="utf-8")

    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(
        json.dumps([{"file": "static/chat.js", "snippet": "onclick=\"fn('${escapeJsString(x)}')\""}]),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "ALLOWLIST_PATH", allowlist_path)
    return tmp_path, js_path, allowlist_path


def test_main_passes_when_file_matches_allowlist_exactly(fake_project, capsys):
    assert mod.main() == 0
    assert "OK" in capsys.readouterr().out


def test_scanned_files_globs_every_static_js_not_a_fixed_list(tmp_path):
    # Review follow-up (PR #333): a hardcoded file list let a handler in any
    # OTHER static/*.js file through uncaught - live-confirmed with a new
    # static/zz_new.js during review. scanned_files() must glob instead.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "chat.js").write_text("", encoding="utf-8")
    (static_dir / "zz_new_totally_unlisted_file.js").write_text("", encoding="utf-8")
    found = mod.scanned_files(tmp_path)
    assert "static/chat.js" in found
    assert "static/zz_new_totally_unlisted_file.js" in found


def test_scanned_files_excludes_vendored_and_minified(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "chart.umd.min.js").write_text("", encoding="utf-8")
    (static_dir / "some-other-lib.min.js").write_text("", encoding="utf-8")
    (static_dir / "chat.js").write_text("", encoding="utf-8")
    found = mod.scanned_files(tmp_path)
    assert found == ["static/chat.js"]


def test_main_fails_on_a_new_handler_in_a_brand_new_unlisted_file(fake_project, capsys):
    # The exact regression the hardcoded SCANNED_FILES list missed.
    tmp_path, _js_path, _allowlist_path = fake_project
    new_file = tmp_path / "static" / "zz_new_totally_unlisted_file.js"
    new_file.write_text("html += `<button onclick=\"pwn('${escapeJsString(y)}')\">go</button>`;\n", encoding="utf-8")
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "NEW inline handler" in err
    assert "zz_new_totally_unlisted_file.js" in err


def test_main_fails_on_a_new_unlisted_handler(fake_project, capsys):
    _tmp_path, js_path, _allowlist_path = fake_project
    js_path.write_text(
        "html += `<button onclick=\"fn('${escapeJsString(x)}')\">go</button>`;\n"
        "html += `<button onclick=\"pwn('${escapeJsString(y)}')\">go</button>`;\n",
        encoding="utf-8",
    )
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "NEW inline handler" in err
    assert "pwn" in err


def test_main_fails_on_a_stale_allowlist_entry(fake_project, capsys):
    _tmp_path, js_path, _allowlist_path = fake_project
    js_path.write_text("html += `<button data-chat-action=\"go\">go</button>`;\n", encoding="utf-8")
    assert mod.main() == 1
    assert "STALE allowlist entry" in capsys.readouterr().err


def test_main_ignores_a_handler_shaped_string_inside_a_comment(fake_project, capsys):
    _tmp_path, js_path, allowlist_path = fake_project
    js_path.write_text(
        "// documents the pattern: onclick=\"pwn('${escapeJsString(y)}')\"\n"
        "html += `<button onclick=\"fn('${escapeJsString(x)}')\">go</button>`;\n",
        encoding="utf-8",
    )
    assert mod.main() == 0, "a comment containing handler-shaped text must not count as a new site"
