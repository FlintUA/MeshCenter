"""UX5: on a phone the Files tab is master/detail - one pane at a time, everything in normal document flow."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _phone_block():
    # the master/detail rules live in the Files panel's own container query (a phone and a narrow desktop centre column alike)
    css = _read("static/ui-kit.css")
    start = css.index("@container filesview (max-width: 700px)")
    return css[start:css.index("MOB1: one screen at a time")]


def test_container_is_the_files_view_and_the_js_threshold_matches():
    assert re.search(r"\.files-view\.workspace-view\s*\{\s*container:\s*filesview\s*/\s*inline-size", _read("static/style-part4.css"))
    assert "FILES_COMPACT_MAX = 700" in _read("static/files.js")


def test_markup_has_view_switch_back_button_and_storage_after_the_list():
    html = _read("templates/index.html")
    assert 'id="filesLayout" data-view="list"' in html
    assert 'id="filesBackBtn"' in html and 'data-files-action="back"' in html
    # the storage summary and the cleanup button come after the transfer rows, not above them
    assert html.index('id="filesArchiveList"') < html.index('id="filesStorage"')
    assert html.index('id="filesSummary"') < html.index('id="filesArchiveList"')


def test_phone_css_shows_one_pane_and_never_fixes_a_height():
    block = _phone_block()
    assert '.files-layout[data-view="detail"] .files-archive' in block
    assert '.files-layout:not([data-view="detail"]) .files-detail' in block
    files_rules = "".join(re.findall(r"\.files-(?:layout|archive|detail|archive-list|detail-body|panel)[^{]*\{[^}]*\}", block))
    assert "min-height: 240px" not in files_rules and "minmax(240px" not in files_rules, "no fixed or minimum pane heights"
    assert "max-height: 60vh" not in files_rules
    # one scroll: the panel
    assert re.search(r"\.files-panel\.mc-workspace-panel\s*\{[^}]*overflow-y:\s*auto", block)


def test_the_old_overlapping_fix_is_gone_and_the_back_button_is_phone_only():
    css = _read("static/style-part4.css")
    assert "grid-auto-rows: auto" not in css.split("the Files tab: the lines above")[-1]
    assert re.search(r"\.files-back-btn\s*\{\s*display:\s*none;", css), "hidden outside the phone layout"
    assert re.search(r"\.files-summary\s*\{[^}]*font-size:\s*12px", css), "the saved-size line uses the small text style"


def test_back_button_label_in_all_four_catalogs():
    for loc in ("en", "de", "ru", "uk"):
        text = _read("static/i18n/%s.json" % loc)
        assert '"back_to_list"' in text and '"back_to_list_short"' in text




def test_phone_layout_does_not_depend_on_container_queries():
    """Safari before 16 (iOS 15) ignores @container: the <= 768 px media query must carry the identical master/detail rules."""
    css = _read("static/ui-kit.css")
    head = "@container filesview (max-width: 700px) {"
    c0 = css.index(head) + len(head)
    cbody = css[c0:css.rfind("/*", 0, css.index("MOB1: one screen at a time"))].strip()
    marker = "Keep this body identical to the container block below. */\n@media (max-width: 768px) {"
    m0 = css.index(marker) + len(marker)
    mbody = css[m0:css.index(head)].strip()
    assert mbody == cbody
    assert '.files-layout[data-view="detail"] .files-archive' in mbody
    assert '.files-layout:not([data-view="detail"]) .files-detail' in mbody
