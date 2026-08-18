"""Tests for server.py's sanitize_text() (server.py:877 as of this writing).

sanitize_text() only strips ASCII control characters and truncates to 500
chars - it does NOT strip quotes, angle brackets, or any HTML/JS-meaningful
characters. That's a deliberate, narrow purpose (stripping control chars that
would corrupt terminal/log output or JSON), not an XSS defense - escaping for
HTML/JS-string contexts is escapeHtml()/escapeJsString()'s job in
static/chat.js (see the recent stored-XSS fix). These tests pin down the
current behavior in both directions: what IS stripped, and - just as
importantly, since it was the exact gap behind that XSS bug - what is NOT.
"""


def test_sanitize_text_empty_and_none_return_empty_string(server_module):
    assert server_module.sanitize_text("") == ""
    assert server_module.sanitize_text(None) == ""


def test_sanitize_text_strips_ascii_control_characters(server_module):
    result = server_module.sanitize_text("hello\x00\x01\x08world\x0b\x0c\x0e\x1f\x7fend")
    assert result == "helloworldend"


def test_sanitize_text_preserves_tab_newline_and_carriage_return(server_module):
    # \x09 (tab), \x0a (LF), \x0d (CR) fall outside the stripped ranges
    # (\x00-\x08, \x0b-\x0c, \x0e-\x1f, \x7f) - deliberately preserved since
    # multi-line message text needs them.
    text = "line1\ttabbed\nline2\r\n"
    assert server_module.sanitize_text(text) == text


def test_sanitize_text_truncates_to_500_characters(server_module):
    text = "a" * 600
    result = server_module.sanitize_text(text)
    assert len(result) == 500
    assert result == "a" * 500


def test_sanitize_text_does_not_strip_quotes_or_html_characters(server_module):
    # Regression guard tied to the stored-XSS fix: sanitize_text() runs on
    # incoming mesh text (node names, messages) upstream of where it later
    # gets rendered - if this function ever started stripping quotes, it
    # would be masking the real fix (escapeJsString() in static/chat.js)
    # rather than duplicating it, and any test asserting quotes-survive here
    # would need updating in lockstep with that call site. Today it
    # correctly does nothing about HTML/JS-meaningful characters - that's
    # by design, not an oversight.
    text = """<script>alert('xss')</script> "quoted" 'single' & <tag>"""
    assert server_module.sanitize_text(text) == text
