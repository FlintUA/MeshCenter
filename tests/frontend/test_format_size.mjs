// tests/frontend/test_format_size.mjs - the shared size formatter (static/format-size.js), all four UI languages.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const dir = path.dirname(fileURLToPath(import.meta.url));
const src = (f) => readFileSync(path.join(dir, '..', '..', 'static', f), 'utf8');

function load(locale) {
    const sandbox = { window: null, console };
    sandbox.window = { I18N: { locale } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src('format-size.js').replace("typeof window !== 'undefined' ? window :", 'window ||'), sandbox, { filename: 'format-size.js' });
    return sandbox.window.MCSize;
}

const cases = {
    en: ['B', 'KB', 'MB', 'GB'], de: ['B', 'KB', 'MB', 'GB'],
    ru: ['Б', 'КБ', 'МБ', 'ГБ'], uk: ['Б', 'КБ', 'МБ', 'ГБ'],
};
for (const [loc, u] of Object.entries(cases)) {
    const S = load(loc);
    assert.equal(S.format(512), '512 ' + u[0], loc + ' bytes');
    assert.equal(S.format(1126), '1.1 ' + u[1], loc + ' kilobytes');
    assert.equal(S.format(131600), '128.5 ' + u[1], loc + ' 128.5');
    assert.equal(S.format(5 * 1024 * 1024), '5.0 ' + u[2], loc + ' megabytes');
    assert.equal(S.format(3 * 1024 ** 3), '3.0 ' + u[3], loc + ' gigabytes');
    assert.equal(S.format(50 * 1024 ** 3), '50.0 ' + u[3], loc + ' big sizes stay in GB');
    assert.equal(S.format(null), '-');
    assert.equal(S.format(undefined), '-');
    assert.equal(S.format(-1), '-');
    assert.equal(S.format('abc'), '-');
    assert.equal(S.unit(1), u[1]);
    assert.equal(S.unit(2), u[2]);
    assert.ok(!S.format(2048).includes('Kb'), 'KB is a kilobyte, never Kb');
}
// the gallery's compact rule: no decimals from 10 KB / 10 MB up
const en = load('en');
assert.equal(en.format(10 * 1024 + 300, { compact: true }), '10 KB');
assert.equal(en.format(2048, { compact: true }), '2.0 KB');
assert.equal(en.format(12 * 1024 * 1024, { compact: true }), '12 MB');
// an unknown language falls back to English units; a regional tag uses its language
assert.equal(load('fr').format(2048), '2.0 KB');
assert.equal(load('ru-RU').format(2048), '2.0 КБ');
// explicit locale argument wins
assert.equal(en.format(2048, { locale: 'uk' }), '2.0 КБ');

// every size the UI writes goes through it: no caller keeps its own English unit table
for (const f of ['files.js', 'media-views.js']) {
    assert.ok(src(f).includes('MCSize'), f + ' uses the shared formatter');
}
const chat = src('chat.js');
assert.ok(chat.includes('MCSize.unit(2)') && chat.includes('MCSize.unit(3)'), 'the system RAM/disk lines use localized units');
console.log('PASS: test_format_size');
