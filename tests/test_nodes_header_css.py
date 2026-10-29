"""NL1: the Nodes header row keeps its layout promises (source-level checks, like test_mobile_layout_css.py)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style-part2.css").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "chat.js").read_text(encoding="utf-8")


def _rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, selector
    return match.group(1)


def test_header_row_is_sticky_with_fixed_height_and_tabular_numbers():
    rule = _rule(".nodes-header-row")
    assert "position: sticky" in rule and "top: 0" in rule
    assert re.search(r"height:\s*36px", rule)
    assert "tabular-nums" in rule


def test_pills_are_26px_with_12px_text():
    rule = _rule(".nodes-pill")
    assert re.search(r"height:\s*26px", rule) and re.search(r"font-size:\s*12px", rule)


def test_touch_hit_zone_is_44px_and_uses_pointer_coarse():
    block = CSS[CSS.index("@media (pointer: coarse)"):]
    assert ".nodes-pill::after" in block and ".nodes-header-clear::after" in block
    assert re.search(r"height:\s*44px", block)


def test_clear_slot_is_reserved_while_hidden():
    assert re.search(r"\.nodes-header-clear\[hidden\]\s*\{[^}]*visibility:\s*hidden", CSS)


def test_markup_and_actions_are_wired():
    for action in ("nodes-header-online", "nodes-header-direct", "nodes-header-clear"):
        assert f'data-chat-action="{action}"' in HTML
        assert f"'{action}'" in JS
    assert "toggleNodeHeaderFilter('hideOffline')" in JS and "toggleNodeHeaderFilter('directOnly')" in JS


def test_compact_is_decided_by_measured_fit_with_no_width_fallback():
    assert '.nodes-header-row[data-fit="compact"] .nodes-pill-text' in CSS
    assert "@container" not in CSS and "container-type" not in CSS
    assert ":not([data-fit])" not in CSS
    assert "function fitNodesHeader()" in JS and "MCViews.fitNodesHeaderRow(row" in JS
    assert "new ResizeObserver(() => fitNodesHeader())" in JS
