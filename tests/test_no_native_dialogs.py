"""UI1 guard: the browser's native confirm() / alert() / prompt() must not come back.

They look foreign, cannot be themed or translated consistently, block the page and cannot
be operated by automated checks (the RR1 "Release radio" confirm and v1.12.0's Updates
"Update" button both blocked live checks). Use the shared dialog instead:
`appConfirm()` / `appPrompt()` / `appAlert()` from static/app-dialog.js, or a toast.

If something truly has to stay native, add `"<relative path>": "<why>"` to ALLOWLIST below
and justify it in the PR. It is empty on purpose.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# path (posix, relative to the repo root) -> reason. Empty: nothing may stay native.
ALLOWLIST: dict[str, str] = {}

# Vendored / third-party bundles - not our code.
VENDORED = {"static/chart.umd.min.js"}

# `confirm(` / `alert(` / `prompt(` as a call of the global (or window./globalThis.), but
# not `appConfirm(`, `showAlert(`, `MCDialog.confirm(` or a method definition.
NATIVE_CALL = re.compile(
    r"(?<![A-Za-z0-9_$.])(?:(?:window|globalThis|self|top|parent)\s*\.\s*)?(confirm|alert|prompt)\s*\("
)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _strip_comments(text: str, html: bool) -> str:
    # Keep line numbers stable: replace comment bodies with the same number of newlines.
    def blank(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    text = BLOCK_COMMENT.sub(blank, text)
    if html:
        text = HTML_COMMENT.sub(blank, text)
    return text


def find_native_calls(text: str, html: bool = False) -> list[tuple[int, str]]:
    hits = []
    for number, line in enumerate(_strip_comments(text, html).splitlines(), start=1):
        if line.lstrip().startswith("//"):
            continue
        match = NATIVE_CALL.search(line)
        if match:
            hits.append((number, line.strip()))
    return hits


def _sources():
    for path in sorted((REPO_ROOT / "static").rglob("*.js")):
        yield path, False
    for path in sorted((REPO_ROOT / "templates").rglob("*.html")):
        yield path, True


def test_no_native_dialogs_in_frontend_sources():
    offenders = []
    for path, html in _sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in VENDORED or rel in ALLOWLIST:
            continue
        for number, line in find_native_calls(path.read_text(encoding="utf-8"), html):
            offenders.append(f"{rel}:{number}: {line[:120]}")
    assert not offenders, (
        "native confirm()/alert()/prompt() found - use appConfirm()/appPrompt() (static/app-dialog.js) "
        "or showToast() instead:\n  " + "\n  ".join(offenders)
    )


def test_guard_detects_the_forms_it_should():
    assert find_native_calls("if (!confirm('x')) return;")
    assert find_native_calls("if (!window.confirm('x')) return;")
    assert find_native_calls("  alert(msg);")
    assert find_native_calls("const v = window.prompt('a', 'b');")
    assert find_native_calls("globalThis . alert (1)")
    assert find_native_calls('<button onclick="confirm(\'x\') && go()">', html=True)


def test_guard_ignores_the_shared_dialog_and_comments():
    assert not find_native_calls("await appConfirm({ title: 'x' });")
    assert not find_native_calls("await MCDialog.confirm({ title: 'x' });")
    assert not find_native_calls("showAlert('x');")
    assert not find_native_calls("    // used to be a native window.alert() here")
    assert not find_native_calls("/* confirm('x') */\nfoo();")
    assert not find_native_calls("<!-- confirm('x') -->", html=True)
    assert not find_native_calls("confirm: (options) => open(options),")


def test_the_allowlist_is_empty_and_the_guard_sees_the_real_files():
    assert ALLOWLIST == {}
    scanned = [p.relative_to(REPO_ROOT).as_posix() for p, _ in _sources()]
    assert "static/chat.js" in scanned and "templates/index.html" in scanned
    assert "static/app-dialog.js" in scanned
