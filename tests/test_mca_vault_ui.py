"""MCA-VAULT PR 5: the vault UI contract (static checks; the behaviour is in tests/frontend/test_vault_ui.mjs)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from meshsrv.attachments.vault import passphrase as vault_passphrase

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_strength_hint_uses_the_servers_rule_and_common_list():
    js = _read("static/vault-ui.js")
    assert "var MIN_LENGTH = %d;" % vault_passphrase.MIN_LENGTH in js
    listed = re.search(r"var COMMON = \[(.*?)\];", js, re.S).group(1)
    assert set(re.findall(r"'([^']+)'", listed)) == set(vault_passphrase._COMMON)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_hint_gives_the_same_verdict_as_the_server_for_a_table_of_passphrases():
    samples = [
        "", "short", "eleven char", "twelve chars", "passwordpassword", "Password Password", "aaaaaaaaaaaaaaaa", "abcdefghijkl",
        "correct horse battery staple", "Tr0ub4dor&3xyz", "abcdefghijklm1", "ЯЯЯЯЯЯЯЯЯЯЯЯЯЯ", "парольная фраза для хранилища", "1234567890123",
        "UPPERCASEONLYHERE", "lowercaseonlyhere", "mixed Case With 3 words", "x" * 25, "ab" * 10, "Meshtastic Meshtastic",
        "a b c d e f g h i j", "ＡＢＣＤｅｆｇｈｉｊｋｌ", "tab\tseparated\twords here",
    ]
    script = (
        "const V=require(process.argv[1]);const s=JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(s.map(p=>{const a=V.assessPassphrase(p);return [a.ok,a.level,a.length];})));"
    )
    out = subprocess.run(["node", "-e", script, str(ROOT / "static" / "vault-ui.js"), json.dumps(samples)],
                         capture_output=True, text=True, check=True).stdout
    got = json.loads(out)
    for sample, verdict in zip(samples, got):
        expected = vault_passphrase.assess(sample)
        assert verdict == [expected["ok"], expected["level"], expected["length"]], (sample, verdict, expected)


def test_every_status_code_the_api_maps_has_a_message():
    api = _read("api/api_attachments.py")
    block = api[api.index("def _vault_error(error):"):api.index('@app.route("/api/mca/vault", methods=["GET"])')]
    mapped = set(re.findall(r'"([a-z_]+)": \d{3}', block))
    js = _read("static/vault-ui.js")
    listed = set(re.findall(r"'([a-z_]+)'", re.search(r"var ERROR_CODES = \[(.*?)\];", js, re.S).group(1)))
    assert mapped <= listed, mapped - listed


def test_no_secret_is_stored_logged_or_put_in_a_url():
    js = _read("static/vault-ui.js")
    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "console.", "document.cookie", "history.pushState", "location."):
        assert forbidden not in js, forbidden
    assert not re.search(r"['\"][^'\"]*\?[^'\"]*(passphrase|recovery_key)=", js)
    # a secret goes in a JSON body only
    for url in re.findall(r"call\('POST', '([^']+)'", js):
        assert "?" not in url
    # every secret field is a password input (or the recovery key text field) with autocomplete set deliberately
    assert js.count("autocomplete=") >= 1 and "autocomplete: 'current-password'" in js and "autocomplete: 'new-password'" in js


def test_markup_has_the_chip_banner_card_and_notices():
    html = _read("templates/index.html")
    assert 'id="dockVaultChip"' in html and "dock-vault-chip" in html
    assert 'id="filesVaultBanner"' in html and "data-vault-banner" in html
    assert 'id="vaultCard"' in html and 'id="vaultCardBody"' in html
    assert re.search(r'/static/vault-ui\.js\?v=', html)
    assert 'id="updatesRollbackVaultNote"' in html
    files = _read("static/files.js")
    assert 'id="filesSendVaultNotice" data-vault-notice' in files, "the Send dialog offers Unlock"
    assert "a.locked" in files and "vault.locked_name" in files, "locked cards show a placeholder, never a name"
    assert "window.MCVault.noteLocked()" in files, "a 423 vault_locked from any request makes the chip catch up"


def test_roll_back_is_disabled_with_the_reason_while_a_vault_exists():
    js = _read("static/chat-updates-security.js")
    assert "rollback_blocked_reason === 'vault_enabled'" in js
    assert "rollbackBtn.disabled = vaultBlocked" in js
    assert "updatesRollbackVaultNote" in js


def test_the_dialog_is_a_bottom_sheet_on_a_phone_and_the_plain_backup_warning_is_not_dismissible():
    css = _read("static/style-part4.css")
    sheet = css[css.index("MCAttach vault UI"):]
    assert "@media (max-width: 768px)" in sheet and "border-radius: 16px 16px 0 0" in sheet and "align-items: flex-end" in sheet
    js = _read("static/vault-ui.js")
    # the warning is drawn from the status on every read; there is no dismiss control and no stored "hidden" flag
    assert "dismiss" not in js.lower()
