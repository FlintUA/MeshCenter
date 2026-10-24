"""csrf.js wraps window.fetch (adds the token, recovers a stale one). A request made before it runs would go
out unwrapped and fail with csrf_invalid, so it must be the first script of the page, in <head>, loaded
synchronously (no defer/async/module), after the csrf-token meta tag it reads."""
import re
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "templates" / "index.html"


def _page():
    return INDEX.read_text(encoding="utf-8")


def test_csrf_js_is_the_first_script_and_in_head():
    html = _page()
    head_end = html.index("</head>")
    first = re.search(r"<script\b", html)
    assert first is not None
    tag_end = html.index(">", first.start())
    tag = html[first.start():tag_end + 1]
    assert "csrf.js" in tag, "the first <script> on the page must be csrf.js, got: " + tag
    assert first.start() < head_end, "csrf.js must be inside <head>"
    assert not re.search(r"\b(defer|async)\b|type\s*=\s*[\"']module", tag), "csrf.js must load synchronously"
    assert len(re.findall(r"<script[^>]*csrf\.js", html)) == 1, "csrf.js is loaded exactly once"


def test_csrf_meta_precedes_the_script():
    html = _page()
    assert html.index('name="csrf-token"') < re.search(r"<script[^>]*csrf\.js", html).start()
