"""The page template must contain the elements the JavaScript wires by id.

v1.13.2 shipped without the "Sign out everywhere" button: a merge resolution dropped its block from
templates/index.html, while the route, the click handler and the translations stayed. Nothing failed, because the
handler only looks the button up (`if (button) ...`). These tests make that kind of loss fail the build."""
import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="ignore") as handle:
        return handle.read().replace("\r\n", "\n")


TEMPLATE = _read("templates", "index.html")
TEMPLATE_IDS = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', TEMPLATE))
# built from a prefix by the shared target picker (`${prefix}NodeSelect`), so no literal id exists anywhere
DYNAMIC_IDS = {"tmNodeSelect", "tmChannelSelect"}


def _static_js():
    for path in sorted(glob.glob(os.path.join(ROOT, "static", "*.js"))):
        name = os.path.basename(path)
        if name.endswith(".min.js") or "chart.umd" in name or "leaflet" in name:
            continue
        yield name, _read("static", name)


def _created_by_js(js_all):
    """Ids a script creates itself (template strings with id="x", el.id = 'x', {id: 'x'} attribute maps)."""
    found = set(re.findall(r'\bid=\\?["\']([A-Za-z0-9_-]+)', js_all))
    found |= set(re.findall(r"\.id\s*=\s*['\"]([A-Za-z0-9_-]+)", js_all))
    found |= set(re.findall(r"\bid:\s*['\"]([A-Za-z0-9_-]+)['\"]", js_all))
    return found


def test_sign_out_everywhere_block_is_in_the_template():
    assert "securityLogoutAllBtn" in TEMPLATE_IDS, "the Sign out everywhere button is missing from templates/index.html"
    assert 'data-i18n="system.security_logout_all_btn"' in TEMPLATE
    assert 'data-i18n="system.security_logout_all_note"' in TEMPLATE
    # and it sits in the Security card, next to the plain Sign out
    card = TEMPLATE[TEMPLATE.index('onclick="securityLogout()"'):]
    assert card.index("securityLogoutAllBtn") < 1200


def test_sign_out_everywhere_is_wired_and_translated():
    js = _read("static", "chat-updates-security.js")
    assert "getElementById('securityLogoutAllBtn')" in js and "'/api/auth/logout_all'" in js
    for loc in ("en", "de", "ru", "uk"):
        with open(os.path.join(ROOT, "static", "i18n", loc + ".json"), encoding="utf-8") as handle:
            system = json.load(handle)["system"]
        for key in ("security_logout_all_btn", "security_logout_all_note", "security_logout_all_title", "security_logout_all_confirm",
                    "security_logout_all_not_enabled", "security_logout_all_failed"):
            assert system.get(key), f"{loc} system.{key}"


def test_every_id_a_script_wires_a_listener_to_exists_in_the_template():
    js_files = dict(_static_js())
    created = _created_by_js("\n".join(js_files.values()))
    missing = {}
    for name, source in js_files.items():
        for match in re.finditer(r"getElementById\(['\"]([A-Za-z0-9_-]+)['\"]\)", source):
            ident = match.group(1)
            nearby = source[match.end():match.end() + 260]
            if "addEventListener" not in nearby:
                continue
            if ident not in TEMPLATE_IDS and ident not in created and ident not in DYNAMIC_IDS:
                missing.setdefault(name, set()).add(ident)
    assert not missing, f"scripts wire listeners to ids that no template or script creates: {missing}"
