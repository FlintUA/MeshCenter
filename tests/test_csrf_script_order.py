"""csrf.js wraps window.fetch (adds the token, recovers a stale one). A request made before it runs would go
out unwrapped and fail with csrf_invalid, so it must be the first script of the page, in <head>, loaded
synchronously (no defer/async/module), after the csrf-token meta tag it reads."""
import re
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "templates" / "index.html"
CSRF_TAG = re.compile(r"<script[^>]*csrf\.js")
SYNC_BREAKERS = re.compile(r"\b(defer|async)\b|type\s*=\s*[\"']module")


def _page():
    return INDEX.read_text(encoding="utf-8")


def test_csrf_js_is_the_first_script_and_in_head():
    html = _page()
    head_end = html.index("</head>")
    first = re.search(r"<script", html)
    assert first is not None
    tag = html[first.start():html.index(">", first.start()) + 1]
    assert "csrf.js" in tag, "the first <script> on the page must be csrf.js, got: " + tag
    assert first.start() < head_end, "csrf.js must be inside <head>"
    assert not SYNC_BREAKERS.search(tag), "csrf.js must load synchronously"
    assert len(CSRF_TAG.findall(html)) == 1, "csrf.js is loaded exactly once"


def test_csrf_meta_precedes_the_script():
    html = _page()
    assert html.index('name="csrf-token"') < CSRF_TAG.search(html).start()
