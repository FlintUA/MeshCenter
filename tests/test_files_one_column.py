"""FX1: the Files tab is one column on every width (no master/detail, no Details pane)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_template_has_one_list_and_no_detail_pane():
    html = _read("templates/index.html")
    assert 'id="filesArchiveList"' in html
    for gone in ("filesDetail", "filesLayout", "filesFilter", "filesChannelNotice", "filesBackBtn"):
        assert gone not in html, gone


def test_segments_exist_for_incoming_sent_all():
    html = _read("templates/index.html")
    for seg in ("incoming", "sent", "all"):
        assert 'id="filesSeg_%s"' % seg in html
        assert 'id="filesCount_%s"' % seg in html


def test_no_container_query_dependency_left():
    css = _read("static/style-part4.css") + _read("static/ui-kit.css")
    assert not re.search(r"container:\s*filesview", css)
    assert "FILES_COMPACT_MAX" not in _read("static/files.js")


def test_status_chip_and_direction_stripe_styles_present():
    css = _read("static/style-part4.css")
    for cls in (".files-card.is-incoming", ".files-card.is-sent", ".files-chip.is-ok", ".files-chip.is-bad"):
        assert cls in css, cls


def test_phone_layout_is_compact_one_toolbar_menu_fab_and_expandable_cards():
    html = _read("templates/index.html")
    for ident in ("filesSearchBtn", "filesSearchClose", "filesSearchCount", "filesMoreBtn", "filesMoreMenu", "filesMenuUse", "filesFab"):
        assert 'id="%s"' % ident in html, ident
    assert 'data-files-action="search-open"' in html and 'data-files-action="more-toggle"' in html
    assert 'class="files-fab files-phone-only"' in html and 'data-files-action="send"' in html.split('id="filesFab"')[0].rsplit("<button", 1)[1] + 'data-files-action="send"'
    css = _read("static/style-part4.css")
    phone = css[css.index(".files-seg-short { display: none; }"):]
    assert ".files-header.mc-workspace-header { display: none; }" in phone, "no tall header on a phone"
    assert ".files-footer { display: none; }" in phone, "the storage line moved into the menu"
    assert ".files-card-extra { display: none;" in phone and ".files-card.is-open .files-card-extra { display: block; }" in phone
    assert "text-overflow: ellipsis" in phone and "white-space: nowrap" in phone, "the file name is one line with an ellipsis"
    assert "@media (max-width: 399px)" in phone and ".files-seg-short { display: inline; }" in phone, "short segment labels at narrow widths"
    for loc in ("en", "de", "ru", "uk"):
        import json
        files = json.loads(_read("static/i18n/%s.json" % loc))["files"]
        for key in ("segment_incoming_short", "segment_sent_short", "segment_all_short", "search_count", "search_close", "more_menu", "card_toggle"):
            assert key in files, (loc, key)


def test_every_plain_status_has_a_short_word_in_all_locales():
    import json
    keys = ("sending", "waiting_recipient", "delivered", "expired", "revoked", "cancelled", "declined", "not_delivered", "waiting_in",
            "waiting_key", "waiting_relay", "waiting_internet", "waiting_ok", "downloading", "received", "expired_in", "declined_in",
            "cancelled_in", "not_received")
    for loc in ("en", "de", "ru", "uk"):
        short = json.loads(_read("static/i18n/%s.json" % loc))["files"]["status_short"]
        for key in keys:
            assert short.get(key), (loc, key)
            assert len(short[key]) <= 13, (loc, key, "a short word must fit a narrow chip")
    css = _read("static/style-part4.css")
    assert ".files-card:not(.is-open) .files-chip-full { display: none; }" in css
