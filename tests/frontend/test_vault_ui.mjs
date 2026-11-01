// tests/frontend/test_vault_ui.mjs - the pure parts of static/vault-ui.js (MCA-VAULT PR 5).
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(dir, '..', '..');
const V = createRequire(import.meta.url)(path.join(root, 'static', 'vault-ui.js'));

// ---- the strength hint
assert.equal(V.assessPassphrase('short').level, 'too_short');
assert.equal(V.assessPassphrase('short').ok, false);
assert.equal(V.assessPassphrase('11 chars abc').ok, true, '12 code points is enough');
assert.equal(V.assessPassphrase('eleven char').level, 'too_short', '11 is not');
assert.equal(V.assessPassphrase('passwordpassword').level, 'common');
assert.equal(V.assessPassphrase('Password Password').level, 'common', 'case and spaces do not hide a common value');
assert.equal(V.assessPassphrase('aaaaaaaaaaaaaaaa').level, 'common', 'three distinct characters or fewer');
assert.equal(V.assessPassphrase('correct horse battery staple').level, 'good');
assert.equal(V.assessPassphrase('abcdefghijklm1').ok, true);
assert.equal(V.assessPassphrase('ＡＢＣＤｅｆｇｈｉｊｋｌ').length, 12, 'NFKC folds full-width characters like the server');
assert.equal(V.assessPassphrase('').level, 'too_short');
assert.equal(V.assessPassphrase(null).level, 'too_short');

// ---- the chip: nothing without a vault
assert.equal(V.chipModel(null), null);
assert.equal(V.chipModel({ ok: true, state: 'disabled' }), null);
assert.equal(V.chipModel({ ok: true, state: 'locked', locked: true }).state, 'locked');
assert.equal(V.chipModel({ ok: true, state: 'unlocked', locked: false }).state, 'unlocked');
assert.equal(V.chipModel({ ok: true, state: 'migrating', locked: true }).state, 'migrating');
assert.equal(V.chipModel({ ok: true, state: 'unlocked', progress: { phase: 'files', done: 1, total: 3 } }).state, 'migrating', 'a running job wins');

// ---- the Files banner
const backup = { name: 'mca-vault-backup-1', bytes: 10, created_at: 1 };
assert.deepEqual(V.bannerModel(null), []);
assert.deepEqual(V.bannerModel({ ok: true, state: 'unlocked', plain_backups: [] }), []);
let lines = V.bannerModel({ ok: true, state: 'locked', locked: true, locked_items: 4, waiting_offers: 1, plain_backups: [backup] });
assert.deepEqual(lines.map((l) => l.kind), ['locked', 'backup'], 'locked first, then the backup warning');
assert.equal(lines[0].items, 4);
assert.equal(lines[0].offers, 1);
lines = V.bannerModel({ ok: true, state: 'unlocked', locked: false, plain_backups: [backup, { ...backup, name: 'mca-vault-backup-2' }] });
assert.deepEqual(lines.map((l) => l.kind), ['backup', 'backup'], 'the warning stays while any plain backup exists, once per folder');
lines = V.bannerModel({ ok: true, state: 'disabled', plain_backups: [backup] });
assert.deepEqual(lines.map((l) => l.kind), ['backup'], 'also with no vault (a vault that was disabled, a folder copied back)');
lines = V.bannerModel({ ok: true, state: 'migrating', locked: true, migration_pending: { direction: 'enable' } });
assert.equal(lines[0].kind, 'job');
assert.equal(lines[0].running, false, 'an interrupted job offers Finish');
assert.equal(V.bannerModel({ ok: true, state: 'migrating', locked: true, progress: { phase: 'files', done: 1, total: 2 } })[0].running, true);

// ---- error codes: every code the API can answer maps to a catalog key; unknown codes get the generic line
assert.equal(V.errorKey('unlock_failed'), 'vault.error.unlock_failed');
assert.equal(V.errorKey('something_new'), 'vault.error.generic');
assert.equal(V.errorKey(undefined), 'vault.error.generic');

// ---- the recovery key type-back
const key = 'ABCD-EFGH-JKLM-NPQR-STUV-WXYZ-2345-6789-ABCD-EFGH-JKLM-NPQR-STUV';
const groups = V.recoveryGroups(key);
assert.equal(groups.length, 13);
for (let i = 0; i < 200; i++) {
    const [a, b] = V.pickTypeBack(13);
    assert.ok(a >= 1 && b <= 13 && a < b, 'two different positions in order: ' + a + ',' + b);
}
assert.deepEqual(V.pickTypeBack(13, () => 0), [1, 2]);
assert.deepEqual(V.pickTypeBack(13, () => 0.999), [12, 13]);
assert.equal(V.typeBackMatches(groups, [3, 5], ['jklm', ' stuv ']), true, 'case and spaces are forgiven');
assert.equal(V.typeBackMatches(groups, [3, 5], ['JKLM', 'XXXX']), false);
assert.equal(V.typeBackMatches(groups, [3, 5], ['', '']), false);
assert.equal(V.typeBackMatches(groups, [3, 5], ['JKLM']), false);

// ---- the catalogs: every key the script or files.js asks for exists in all four languages
const source = readFileSync(path.join(root, 'static', 'vault-ui.js'), 'utf8') + readFileSync(path.join(root, 'static', 'files.js'), 'utf8');
const keys = new Set();
for (const m of source.matchAll(/\b(?:tr|t)\('((?:vault|system\.updates_rollback_vault)[a-z_.]*)'/g)) keys.add(m[1]);
for (const m of source.matchAll(/labelKey: '([a-z_.]+)'/g)) keys.add(m[1]);
for (const code of V.ERROR_CODES) keys.add('vault.error.' + code);
for (const level of ['too_short', 'common', 'weak', 'fair', 'good']) keys.add('vault.strength.' + level);
for (const phase of ['backup', 'files']) keys.add('vault.phase.' + phase);
for (const state of ['locked', 'unlocked', 'migrating']) keys.add('vault.chip_title_' + state);
keys.delete('vault.error.');
keys.delete('vault.phase.');
keys.delete('vault.strength.');
keys.delete('vault.chip_title_');
assert.ok(keys.size > 90, 'found the keys: ' + keys.size);
for (const loc of ['en', 'de', 'ru', 'uk']) {
    const catalog = JSON.parse(readFileSync(path.join(root, 'static', 'i18n', loc + '.json'), 'utf8'));
    const lookup = (k) => k.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), catalog);
    for (const k of keys) assert.equal(typeof lookup(k), 'string', loc + ' is missing ' + k);
}

console.log('PASS: test_vault_ui');
