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
