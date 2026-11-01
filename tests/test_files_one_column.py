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
