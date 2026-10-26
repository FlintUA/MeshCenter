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


def test_mob2_tab_row_is_as_high_as_its_tabs_and_the_message_box_can_shrink():
    """Reported live on an Android phone: the top of the tab cards was cut off (a fixed 42 px row with 6 px padding
    held 48 px tabs) and the Send button ran past the right edge (a text input's intrinsic width stops it shrinking
    when the phone font is larger)."""
    block = _block()
    row = _rule(block, "    .main-content-tabs {\n        height: auto;")
    assert "min-height: 52px" in row and "align-items: stretch" in row
    assert "min-width: 0" in _rule(block, '    .input-form input[type="text"] {')
    assert "flex: 0 0 auto" in _rule(block, "    .input-form button {")


def test_mob2c_chats_are_two_pages_on_a_phone():
    """MOB2-C: the list takes the whole height or the conversation does (body[data-mobile-chat]); the list rows
    and the actions button are 44 px targets; short messages no longer wrap early."""
    block = _block()
    hide = _rule(block, '    body[data-mobile-chat="list"] .chat-panels > .messages-view,')
    assert "display: none !important" in hide
    page = _rule(block, '    body[data-mobile-chat="list"] .chat-panels > .split-chat-list-container {')
    assert "flex: 1 1 auto !important" in page and "height: 100% !important" in page
    assert "min-height: 56px" in _rule(block, "    .chat-panels > .split-chat-list-container .chat-item {")
    trigger = _rule(block, "    .message-actions-trigger {")
    assert "width: 44px" in trigger and "height: 44px" in trigger
    bubble = _rule(block, "    .message .bubble {")
    assert "padding-right: 12px" in bubble, "no 38 px strip reserved down the whole bubble"
    assert "max-width: 100%" in bubble, "the 85 % cap of style-part1 cut short bubbles to 85 % of their content"
    back = _rule(block, "    .chat-back-btn {")
    assert "min-height: 44px" in back and "min-width: 44px" in back
    assert "display: none" in _rule(block, "    .chat-back-btn[hidden] {")


def test_mob2c_back_button_is_in_the_template_hidden_by_default():
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="chatBackBtn" hidden' in html
    assert html.index('id="chatBackBtn"') < html.index('class="chat-header-info"'), "Back comes first in the header"

