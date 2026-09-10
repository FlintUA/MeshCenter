"""Frontend reference-integrity tests for the Files workspace (MCAttach).

The Files workspace spans three hand-wired, build-step-free files that must
stay in agreement with each other and with the four i18n catalogs:

  * templates/index.html  - the nav button, the #filesView panel, and the
                            <script> tag that pulls in static/files.js
  * static/chat.js        - `operationalTabs`, the `tab === 'files'` branch
                            in switchMainTab(), and the `typeof
                            openFilesWorkspace === 'function'` guard
  * static/files.js       - the module itself, whose top-level functions are
                            referenced by the other two files, and whose
                            I18N.t()/tOrFallback() keys must resolve in every
                            catalog (a missing key renders a literal `[[key]]`
                            in the UI).

There is no JS build step or test framework (CI's JS gate is `node --check`,
syntax only), so rather than execute the module this tests the *contract
between* the files: the names index.html/chat.js reach for must exist in
files.js, and every user-facing string files.js looks up must exist in all
four catalogs. It is pure stdlib (pathlib + re + json) and runs under pytest
alongside the rest of the suite, so it is part of the normal CI net - unlike
the dependency-free Node frontend tests in tests/frontend/, which are run
manually.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

STATIC = REPO_ROOT / "static"
I18N_DIR = STATIC / "i18n"
LOCALES = ("en", "de", "ru", "uk")

FILES_JS = STATIC / "files.js"
CHAT_JS = STATIC / "chat.js"
INDEX_HTML = REPO_ROOT / "templates" / "index.html"


def _read(name: Path) -> str:
    return name.read_text(encoding="utf-8")


def _flatten(obj: dict, prefix: str = "") -> dict:
    """Mirror static/i18n.js's own flatten(raw, '', {}) - nested objects
    become dotted keys so files.js's 'files.state.SENT' lookups can be
    checked directly against the catalog shape the browser actually uses."""
    out: dict = {}
    for key, value in obj.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{full}."))
        else:
            out[full] = value
    return out


def _catalogs() -> dict:
    return {locale: _flatten(json.loads(_read(I18N_DIR / f"{locale}.json"))) for locale in LOCALES}


CATALOGS = _catalogs()

# Every I18N.t('key') / I18N.tOrFallback('key', ...) literal in files.js.
_STATIC_KEY_RE = re.compile(r"I18N\.t(?:OrFallback)?\(\s*'([^']+)'\s*")

# The two fallback-label tables: their *keys* are the dynamic suffix of the
# files.state.<X> / files.contact.<x> lookups in filesStateLabel() /
# filesContactStatusLabel().
_STATE_TABLE_RE = re.compile(r"FILES_STATE_LABELS\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)
_CONTACT_TABLE_RE = re.compile(r"FILES_CONTACT_STATUS_LABELS\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)
_TABLE_KEY_RE = re.compile(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")

# The names files.js exposes on `window` for cross-file callers.
_EXPORT_RE = re.compile(r"window\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\1;")


def _files_js_source() -> str:
    return _read(FILES_JS)


def _files_js_static_keys() -> set:
    keys = _STATIC_KEY_RE.findall(_files_js_source())
    # Drop trailing-dot prefixes like 'files.state.' / 'files.contact.' /
    # 'files.error.' - those are the literal half of a string concatenation
    # ('files.state.' + state), never a complete lookup key, and their
    # suffixes are validated separately by the label-table and error tests.
    return {k for k in keys if not k.endswith(".")}


def _files_js_state_labels() -> set:
    match = _STATE_TABLE_RE.search(_files_js_source())
    assert match, "FILES_STATE_LABELS table not found in files.js"
    return set(_TABLE_KEY_RE.findall(match.group("body")))


def _files_js_contact_labels() -> set:
    match = _CONTACT_TABLE_RE.search(_files_js_source())
    assert match, "FILES_CONTACT_STATUS_LABELS table not found in files.js"
    return set(_TABLE_KEY_RE.findall(match.group("body")))


# ---- cross-file reference integrity -----------------------------------------


def test_index_html_wires_files_nav_and_panel():
    html = _read(INDEX_HTML)

    # Nav button: id + onclick must agree (switchMainTab's highlight logic
    # derives 'mainTab' + 'Files' from the 'files' argument).
    assert 'id="mainTabFiles"' in html, "Files nav button id missing from index.html"
    assert "switchMainTab('files')" in html, "Files nav button onclick missing from index.html"

    # The workspace panel the nav reveals.
    assert 'id="filesView"' in html, "filesView panel missing from index.html"
    assert 'id="filesWorkspaceTitle"' in html, "filesWorkspaceTitle missing from index.html"

    # The panel's title is translated through the nav.files key.
    assert 'data-i18n="nav.files"' in html, "nav.files data-i18n attribute missing from index.html"

    # files.js must be loaded (with a cache-busting ?v=), and BEFORE chat.js
    # so chat.js's switchMainTab('files') branch can call it.
    files_script = re.search(r'<script src="/static/files\.js\?v=[^"]+"></script>', html)
    assert files_script, "files.js <script> tag (with ?v= cache-buster) missing from index.html"
    chat_script = re.search(r'<script src="/static/chat\.js[^"]*"></script>', html)
    assert chat_script, "chat.js <script> tag missing from index.html"
    assert files_script.start() < chat_script.start(), "files.js must load before chat.js"


def test_chat_js_reaches_files_workspace():
    chat = _read(CHAT_JS)

    assert "'files'" in chat, "operationalTabs does not mention 'files'"
    assert re.search(r"operationalTabs\s*=\s*new Set\(\[[^\]]*'files'[^\]]*\]\)", chat), (
        "'files' is not in the operationalTabs Set"
    )
    assert "tab === 'files'" in chat, "switchMainTab has no tab === 'files' branch"
    assert "typeof openFilesWorkspace === 'function'" in chat, (
        "chat.js does not guard-call openFilesWorkspace()"
    )
    assert "I18N.t('nav.files')" in chat or "I18N.t(\"nav.files\")" in chat, (
        "updateStatusDock does not translate the Files dock label via nav.files"
    )


def test_files_js_exports_every_entrypoint_cross_files_reference():
    source = _files_js_source()
    exports = set(_EXPORT_RE.findall(source))

    # chat.js guards on these two; index.html/chat.js and files.js's own
    # inline onclick handlers depend on the rest.
    required = {
        "openFilesWorkspace",
        "closeFilesWorkspace",
        "loadFilesWorkspace",
        "setFilesFilter",
        "openFilesSendDialog",
        "closeFilesSendDialog",
        "openFilesProviderSettings",
        "closeFilesProviderSettings",
    }
    missing = required - exports
    assert not missing, f"files.js is missing window exports: {sorted(missing)}"


# ---- i18n key integrity ------------------------------------------------------


def test_every_files_js_i18n_key_resolves_in_all_locales():
    keys = _files_js_static_keys()
    assert keys, "no I18N.t/tOrFallback keys extracted from files.js"
    for locale, flat in CATALOGS.items():
        missing = sorted(k for k in keys if k not in flat)
        assert not missing, f"files.js references unknown keys in {locale}: {missing}"


def test_state_and_contact_label_keys_resolve_in_all_locales():
    state_labels = _files_js_state_labels()
    contact_labels = _files_js_contact_labels()
    assert state_labels, "FILES_STATE_LABELS is empty or not parsed"
    assert contact_labels, "FILES_CONTACT_STATUS_LABELS is empty or not parsed"

    dynamic = set()
    for s in state_labels:
        dynamic.add(f"files.state.{s}")
    for c in contact_labels:
        dynamic.add(f"files.contact.{c}")

    for locale, flat in CATALOGS.items():
        missing = sorted(k for k in dynamic if k not in flat)
        assert not missing, f"files.js label tables map to unknown keys in {locale}: {missing}"


def test_shared_keys_files_js_depends_on_exist_in_all_locales():
    # chat.js's dock label and files.js's modal footer/close buttons reach for
    # these shared keys; a missing one degrades to the fallback English (still
    # readable) but the Files workspace should be fully translated in all four
    # locales, so assert they are real catalog keys.
    for locale, flat in CATALOGS.items():
        for key in ("nav.files", "common.cancel", "common.close"):
            assert key in flat, f"{key!r} missing from {locale} catalog"
