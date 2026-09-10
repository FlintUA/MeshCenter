"""Frontend reference-integrity tests for the Files workspace (MCAttach).

The Files workspace spans three hand-wired, build-step-free files that must
stay in agreement with each other and with the four i18n catalogs:

  * templates/index.html  - the nav button, the #filesView panel (whose filter
                            tabs / header buttons are dispatched by delegated
                            `data-files-*` attributes, not inline handlers),
                            and the <script> tag that pulls in static/files.js
  * static/chat.js        - `operationalTabs`, the `tab === 'files'` branch in
                            switchMainTab() that calls `MeshCenterFiles.activate()`,
                            and the switch-away guard that calls
                            `MeshCenterFiles.deactivate()`
  * static/files.js       - the module itself, whose one public surface is
                            `window.MeshCenterFiles = {activate, deactivate, refresh}`
                            and whose I18N.t()/t()/tparams() keys must resolve
                            in every catalog (a missing key renders a literal
                            `[[key]]` in the UI).

There is no JS build step or test framework (CI's JS gate is `node --check`,
syntax only), so rather than execute the module this tests the *contract
between* the files: the names index.html/chat.js reach for must exist in
files.js, and every user-facing string files.js looks up must exist in all
four catalogs. It is pure stdlib (pathlib + re + json) and runs under pytest
alongside the rest of the suite, so it is part of the normal CI net - the
executable dependency-free behavior tests live in tests/frontend/ (see
test_files_ui.mjs) and are wired into CI separately.
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

# Every I18N.t('key') / I18N.tOrFallback('key') / t('key') / tparams('key')
# literal in files.js. `t` / `tparams` are the module's own lookup helpers
# that delegate to window.I18N.tOrFallback, so their first argument is the
# same key namespace. The \b guards against `createElement('div')` matching
# the bare `t(` alternative.
_STATIC_KEY_RE = re.compile(r"(?:I18N\.t(?:OrFallback)?|\btparams|\bt)\(\s*'([^']+)'\s*")

# The four fallback-label tables: their *keys* are the dynamic suffix of the
# files.<table>.<X> lookups in filesStateLabel()/filesContactStatusLabel()/
# filesRelayStateLabel()/filesReadinessLabel().
_LABEL_TABLES = {
    "FILES_STATE_LABELS": "files.state",
    "FILES_CONTACT_STATUS_LABELS": "files.contact",
    "FILES_RELAY_STATE_LABELS": "files.relay_state",
    "FILES_READINESS_LABELS": "files.ready_reason",
}
_TABLE_RE = re.compile(r"(?P<name>[A-Z_]+_LABELS)\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)
_TABLE_KEY_RE = re.compile(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")

# The one public module surface: window.MeshCenterFiles = {activate, ...}.
_MODULE_RE = re.compile(r"window\.MeshCenterFiles\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)


def _files_js_source() -> str:
    return _read(FILES_JS)


def _files_js_static_keys() -> set:
    keys = _STATIC_KEY_RE.findall(_files_js_source())
    # Drop trailing-dot prefixes like 'files.state.' / 'files.contact.' /
    # 'files.error.' / 'files.relay_state.' / 'files.ready_reason.' - those are
    # the literal half of a string concatenation ('files.state.' + state), never
    # a complete lookup key, and their suffixes are validated separately by the
    # label-table test.
    return {k for k in keys if not k.endswith(".")}


def _files_js_label_keys() -> dict:
    """name -> set of suffix keys, e.g. {'FILES_STATE_LABELS': {'SENT', ...}}."""
    source = _files_js_source()
    out: dict = {}
    for match in _TABLE_RE.finditer(source):
        name = match.group("name")
        if name in _LABEL_TABLES:
            out[name] = set(_TABLE_KEY_RE.findall(match.group("body")))
    return out


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

    # Header buttons are dispatched by delegated data-files-action attributes
    # (no inline handler with a raw id), and the filter tabs carry the six
    # supported filters including the Received/Sent splits.
    assert 'data-files-action="send"' in html, "Send header button missing data-files-action"
    assert 'data-files-action="providers"' in html, "Providers header button missing data-files-action"
    assert 'data-files-action="refresh"' in html, "Refresh header button missing data-files-action"
    assert 'data-files-filter="received"' in html, "Received filter tab missing"
    assert 'data-files-filter="sent"' in html, "Sent filter tab missing"

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
    assert "MeshCenterFiles.activate" in chat, (
        "chat.js does not call MeshCenterFiles.activate() on entering the Files workspace"
    )
    assert "MeshCenterFiles.deactivate" in chat, (
        "chat.js does not call MeshCenterFiles.deactivate() when leaving the Files workspace"
    )
    assert "I18N.t('nav.files')" in chat or "I18N.t(\"nav.files\")" in chat, (
        "updateStatusDock does not translate the Files dock label via nav.files"
    )


def test_files_js_exposes_module_surface():
    source = _files_js_source()
    match = _MODULE_RE.search(source)
    assert match, "window.MeshCenterFiles module object missing from files.js"

    body = match.group("body")
    for member in ("activate", "deactivate", "refresh"):
        assert re.search(rf"\b{member}\s*:", body), (
            f"window.MeshCenterFiles is missing the {member!r} member"
        )


# ---- i18n key integrity ------------------------------------------------------


def test_every_files_js_i18n_key_resolves_in_all_locales():
    keys = _files_js_static_keys()
    assert keys, "no I18N.t/tOrFallback/t/tparams keys extracted from files.js"
    for locale, flat in CATALOGS.items():
        missing = sorted(k for k in keys if k not in flat)
        assert not missing, f"files.js references unknown keys in {locale}: {missing}"


def test_label_keys_resolve_in_all_locales():
    tables = _files_js_label_keys()
    assert set(tables) == set(_LABEL_TABLES), (
        f"files.js label tables mismatch: found {sorted(tables)}, expected {sorted(_LABEL_TABLES)}"
    )

    dynamic: set = set()
    for name, prefix in _LABEL_TABLES.items():
        suffixes = tables[name]
        assert suffixes, f"{name} is empty or not parsed"
        for s in suffixes:
            dynamic.add(f"{prefix}.{s}")

    for locale, flat in CATALOGS.items():
        missing = sorted(k for k in dynamic if k not in flat)
        assert not missing, f"files.js label tables map to unknown keys in {locale}: {missing}"


def test_shared_keys_files_js_depends_on_exist_in_all_locales():
    # chat.js's dock label and files.js's modal footer/close buttons reach for
    # these shared keys; a missing one degrades to the fallback English (still
    # readable) but the Files workspace should be fully translated in all four
    # locales, so assert they are real catalog keys.
    for locale, flat in CATALOGS.items():
        for key in ("nav.files", "common.cancel", "common.close", "common.refresh"):
            assert key in flat, f"{key!r} missing from {locale} catalog"
