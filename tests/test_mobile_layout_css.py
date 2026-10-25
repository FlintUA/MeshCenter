"""MOB1 changes the layout only on a narrow viewport: the whole block is one @media (max-width: 768px)
rule at the end of static/ui-kit.css, so a desktop viewport cannot see any of it."""
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "ui-kit.css"
MARKER = "MOB1: one screen at a time on a narrow viewport"


def _block():
    text = CSS.read_text(encoding="utf-8")
    start = text.index(MARKER)
    start = text.index("@media", start)
    return text[start:]


def test_mob1_css_is_one_narrow_media_query_at_the_end():
    block = _block()
    assert block.startswith("@media (max-width: 768px) {")
    depth = 0
    opened = False
    for i, ch in enumerate(block):
        depth += ch == "{"
        depth -= ch == "}"
        opened = opened or depth > 0
        if opened and depth == 0:
            assert block[i + 1:].strip() == "", "nothing may follow the MOB1 media query"
            break
    else:
        raise AssertionError("unbalanced braces in the MOB1 block")


def test_mobile_script_is_loaded_after_chat_js():
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8")
    assert "mobile-layout.js" in html
    assert html.index("mobile-layout.js") > html.index("/static/chat.js")


def _rule(block, selector_start):
    """The declarations of the first rule whose selector list starts with `selector_start`."""
    start = block.index(selector_start)
    start = block.index("{", start)
    return block[start + 1:block.index("}", start)]


def test_mob2a_dock_menus_are_fixed_sheets_outside_the_clip_of_the_dock():
    """MOB2-A: the Workspace / Map layout / Notifications menus were clipped to nothing on a phone (children of a
    52 px, overflow: hidden, translateX(-50%) dock), so System, Settings and About could not be opened."""
    block = _block()
    dock = _rule(block, "    .status-dock {\n        left: 0;")
    assert "transform: none" in dock, "a transform on the dock would make it the containing block of the fixed sheets"
    sheets = _rule(block, "    .workspace-popover,\n    .map-layout-popover,\n    .notification-popover-center {")
    assert "position: fixed" in sheets and "max-height" in sheets and "overflow-y: auto" in sheets
    assert "bottom: calc(60px" in sheets, "above the 52 px dock"
    hidden = _rule(block, "    .workspace-popover[hidden],")
    assert "display: none !important" in hidden, "a closed sheet must stay closed (the flex layout must not override [hidden])"
    # the page links come first inside the Workspace sheet
    assert "order: -1" in _rule(block, "    .workspace-popover .workspace-application-section")


def test_mob2a_sheet_backdrop_sits_between_the_page_and_the_dock():
    block = _block()
    backdrop = _rule(block, "    body.mc-sheet-open #mcSheetBackdrop")
    assert "position: fixed" in backdrop and "z-index: 800" in backdrop, "below the dock (z-index 900) that holds the sheet"


def test_mob2b_dock_buttons_are_44px_targets():
    block = _block()
    button = _rule(block, "    .dock-workspace-btn {\n        width: 44px;")
    assert "height: 44px" in button
    assert "height: 44px" in _rule(block, "    .dock-notification-status {")
    assert "font-size: 12px" in _rule(block, "    .dock-notification-status {")


def test_mob2_rules_stay_inside_the_media_query():
    text = CSS.read_text(encoding="utf-8")
    assert text.index("MOB2-A") > text.index(MARKER), "the MOB2 rules belong to the MOB1 media query, nothing leaks to a wide screen"


def test_mob2b_dock_buttons_never_scroll_the_status_button_takes_the_rest():
    block = _block()
    left = _rule(block, "    .status-dock .dock-left {\n        flex: 0 0 auto;")
    assert "overflow: visible" in left, "four 44 px buttons fit at 320 px: no sideways scroll, nothing clipped"
    assert "flex: 1 1 0" in _rule(block, "    .status-dock .dock-center {\n        flex: 1 1 0;")


def test_mob2_tabs_fit_the_width_without_snapping():
    """Reported live on an Android phone: swiping the tab row jerked (scroll-snap) and only a tap placed it. The five
    tabs now share the width (icon above the label), so there is nothing to swipe and nothing to snap."""
    block = _block()
    assert "scroll-snap" not in block, "no scroll snapping on the tab row"
    tab = _rule(block, "    .main-content-tab {\n        flex: 1 1 0;")
    assert "min-width: 0" in tab and "flex-direction: column" in tab and "min-height: 48px" in tab


def test_mob2_settings_cards_are_not_wider_than_the_screen():
    """A 1fr grid track grows to its widest child (364 px): the Settings cards were cut off at the right edge."""
    block = _block()
    assert "grid-template-columns: minmax(0, 1fr)" in _rule(block, "    .settings-grid {")
    assert "min-width: 0" in _rule(block, "    .settings-card,\n    .map-settings-card,")
    assert "flex-wrap: wrap" in _rule(block, "    .settings-segmented {")
