// tests/frontend/test_recipient_picker.mjs
//
// UX5: the shared recipient picker (static/recipient-picker.js): search on short name, long name and node id, "N of M", Favorites
// only (default and remembered), the order (recent, ready, no key), disabled and never selectable no-key rows, the row cap for a
// mesh of 1000+ nodes, and the wiring in the three places that pick a recipient.
// `node tests/frontend/test_recipient_picker.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(read('recipient-picker.js'), sandbox);
const P = sandbox.MCRecipientPicker;

const contact = (id, name, extra = {}) => ({ id, name, short_name: '', favorite: false, can_send: true, can_request_key: false, ...extra });
const ids = (rows) => Array.from(rows).filter((r) => r.kind === 'row').map((r) => r.contact.id);
const plain = (value) => JSON.parse(JSON.stringify(value));

const people = [
    contact('!067a40fa', 'Flint Base', { short_name: 'FLB', favorite: true }),
    contact('!756f9960', 'Flint TAP2', { short_name: 'TAP2' }),
    contact('!b0f14d2a', 'Гараж', { short_name: 'GAR', favorite: true }),
    contact('!1fa065f0', 'Elektroniker.help', { short_name: 'ELH', can_send: false, can_request_key: true }),
    contact('!aa00bb11', 'Zed', { can_send: false }),
];

// ---------------------------------------------------------------- search: short name, long name, node id, any case
{
    const found = (q) => people.filter((c) => P.matches(c, q)).map((c) => c.id);
    assert.deepEqual(plain(found('')), people.map((c) => c.id), 'empty: everything');
    assert.deepEqual(plain(found('   ')), people.map((c) => c.id), 'only spaces: everything');
    assert.deepEqual(plain(found('FLINT')), ['!067a40fa', '!756f9960'], 'long name, any case');
    assert.deepEqual(plain(found('tap2')), ['!756f9960'], 'short name, any case');
    assert.deepEqual(plain(found('elh')), ['!1fa065f0']);
    assert.deepEqual(plain(found('!067A')), ['!067a40fa'], 'node id with the !, any case');
    assert.deepEqual(plain(found('067a40')), ['!067a40fa'], 'node id without the !');
    assert.deepEqual(plain(found('b0f1')), ['!b0f14d2a'], 'hex piece of the id');
    assert.deepEqual(plain(found('гар')), ['!b0f14d2a'], 'a Cyrillic name');
    assert.deepEqual(plain(found('ГАРАЖ')), ['!b0f14d2a'], 'in capitals too');
    assert.deepEqual(plain(found('  flint   base ')), ['!067a40fa'], 'whitespace collapsed');
    assert.deepEqual(plain(found('nobody')), []);
    assert.equal(P.matches({ id: '!x' }, 'x'), true, 'a contact with no name still matches by id');
}

// ---------------------------------------------------------------- the view: groups, order, counts
{
    const v = P.build(people, { query: '', favoritesOnly: false, recent: [] });
    const groups = plain(v.rows.filter((r) => r.kind === 'header').map((r) => r.group));
    assert.deepEqual(groups, ['ready', 'nokey'], 'contacts you can send to, then the nodes without a key in their own group');
    const order = ids(v.rows);
    assert.deepEqual(plain(order.slice(0, 3)), ['!067a40fa', '!756f9960', '!b0f14d2a'].sort(), 'ready ones alphabetical by name');
    assert.deepEqual(plain(order.slice(3)), ['!1fa065f0', '!aa00bb11'], 'no-key ones after them, by name');
    assert.equal(v.found, 5);
    assert.equal(v.total, 5);
    assert.equal(v.readyFound, 3);
    assert.equal(v.truncated, false);
}
{
    // recently used first, only the ones that can still be sent to, only while nothing is typed
    const v = P.build(people, { recent: ['!b0f14d2a', '!1fa065f0', '!gone', '!756f9960'] });
    assert.deepEqual(plain(v.rows.filter((r) => r.kind === 'header').map((r) => r.group)), ['recent', 'ready', 'nokey']);
    assert.deepEqual(plain(ids(v.rows).slice(0, 3)), ['!b0f14d2a', '!756f9960', '!067a40fa'], 'recent in recency order (a no-key or vanished one is skipped), then the rest');
    const typed = P.build(people, { query: 'flint', recent: ['!b0f14d2a', '!756f9960'] });
    assert.deepEqual(plain(typed.rows.filter((r) => r.kind === 'header').length), 0, 'one group on screen: no header');
    assert.deepEqual(plain(ids(typed.rows)), ['!067a40fa', '!756f9960'], 'while typing, plain order of the matches');
}
{
    // favorites only
    const v = P.build(people, { favoritesOnly: true });
    assert.deepEqual(plain(ids(v.rows)).sort(), ['!067a40fa', '!b0f14d2a']);
    assert.equal(v.found, 2);
    assert.equal(v.total, 5, 'M stays the whole list');
    const both = P.build(people, { favoritesOnly: true, query: 'flint' });
    assert.deepEqual(plain(ids(both.rows)), ['!067a40fa']);
    const none = P.build(people, { query: 'zzz' });
    assert.equal(none.found, 0);
    assert.equal(none.rows.length, 0);
    assert.equal(none.total, 5, '"0 of 5"');
}

// ---------------------------------------------------------------- fast with 1000+ nodes: only the filtered rows, capped
{
    const many = Array.from({ length: 1500 }, (_, i) => contact('!' + (0x10000000 + i).toString(16), 'Node ' + i, { can_send: i % 10 === 0, favorite: i % 50 === 0 }));
    const t0 = Date.now();
    const all = P.build(many, { query: '', recent: [] });
    assert.equal(all.rows.filter((r) => r.kind === 'row').length, P.CAP, 'at most CAP rows are built');
    assert.equal(all.found, 1500);
    assert.equal(all.truncated, true, 'the "type to narrow" hint is due');
    assert.equal(all.shown, P.CAP);
    const narrow = P.build(many, { query: 'node 14' });
    assert.ok(narrow.found > 0 && narrow.found < 1500);
    for (let i = 0; i < 20; i++) P.build(many, { query: 'node ' + i });
    assert.ok(Date.now() - t0 < 1500, 'filtering 1500 nodes stays fast');
    const html = P.renderListHtml(all, { t: (k, p, f) => f || k }, '');
    assert.ok(html.includes('rp-more') && html.includes('Showing 50 of 1500, type to narrow'), 'the hint says how many are shown');
    assert.ok((html.match(/class="rp-row/g) || []).length <= P.CAP, 'the DOM never gets more than CAP rows');
}

// ---------------------------------------------------------------- the model: default, remembered, selection
{
    const m = P.createModel({});
    m.setContacts(plain(people));
    assert.equal(m.favoritesOnly, true, 'on by default: there are favorites with a known key');
    const noKeyFav = plain(people).map((c) => (c.favorite ? { ...c, can_send: false } : c));
    const m2 = P.createModel({});
    m2.setContacts(noKeyFav);
    assert.equal(m2.favoritesOnly, false, 'off by default when no favorite has a key');
    const m3 = P.createModel({ favoritesOnly: false });
    m3.setContacts(plain(people));
    assert.equal(m3.favoritesOnly, false, 'a remembered "off" wins over the default');
    const m4 = P.createModel({});
    m4.setContacts(plain(people));
    m4.setFavoritesOnly(false);
    m4.setContacts(plain(people));
    assert.equal(m4.favoritesOnly, false, 'once the user chose, a contact refresh does not flip it back');

    // only a contact that can be sent to can be chosen
    assert.equal(m.select('!1fa065f0'), false, 'a node without a key is never a target');
    assert.equal(m.select('!nope'), false);
    assert.equal(m.select('!756f9960'), true);
    assert.equal(m.selectedContact().name, 'Flint TAP2');
    // it loses its key: the choice is dropped, never silently kept
    m.setContacts(plain(people).map((c) => (c.id === '!756f9960' ? { ...c, can_send: false } : c)));
    assert.equal(m.selected, '');
    m.select('!067a40fa');
    m.setContacts(plain(people).filter((c) => c.id !== '!067a40fa'));
    assert.equal(m.selected, '', 'and so is one that disappeared');
}

// ---------------------------------------------------------------- recents
{
    assert.deepEqual(plain(P.recentAfterUse(['a', 'b', 'c'], 'b')), ['b', 'a', 'c'], 'moves to the front, no duplicate');
    assert.deepEqual(plain(P.recentAfterUse([], 'x')), ['x']);
    const long = Array.from({ length: 12 }, (_, i) => 'id' + i);
    assert.equal(P.recentAfterUse(long, 'new').length, P.RECENT_MAX);
    assert.equal(P.recentAfterUse(long, 'new')[0], 'new');
    assert.deepEqual(plain(P.recentAfterUse(['a'], '')), ['a']);
    assert.equal(P.RECENT_MAX, 8);
    assert.equal(P.favoritesField(true), 'favorites_only_mobile');
    assert.equal(P.favoritesField(false), 'favorites_only_desktop');
    const prefs = P.readPrefs({ send_recipients: { favorites_only_desktop: true, favorites_only_mobile: false, recent: ['!a', 7, '!b'] } });
    assert.equal(prefs.favoritesOnly, true, 'a desktop (no phone layout here) reads its own field');
    assert.deepEqual(plain(prefs.recent), ['!a', '!b']);
    assert.equal(P.readPrefs({}).favoritesOnly, null, 'nothing chosen yet: the picker decides');
}

// ---------------------------------------------------------------- the rows: disabled, escaped, selectable
{
    const t = (key, params, fallback) => fallback || key;
    const v = P.build([contact('!aaaa0001', 'Alice <b>&"x"', { favorite: true }), contact('!bbbb0002', 'Bob', { can_send: false, can_request_key: true })], {});
    const html = P.renderListHtml(v, { t, requestKeyAttr: 'data-files-action="send-request-key"' }, '!aaaa0001');
    assert.ok(html.includes('data-rp-id="!aaaa0001"') && html.includes('aria-selected="true"') && html.includes('is-selected'), 'the chosen row is marked');
    assert.ok(!html.includes('<b>'), 'names are escaped');
    assert.ok(html.includes('Alice &lt;b&gt;&amp;&quot;x&quot;'), 'escaped, not dropped');
    assert.ok(html.includes('data-rp-nokey="!bbbb0002"') && html.includes('aria-disabled="true"') && !html.includes('data-rp-id="!bbbb0002"'), 'a no-key row is disabled and has no selectable id');
    assert.ok(html.includes('Key unknown, request the key'), 'with the reason');
    assert.ok(html.includes('data-files-action="send-request-key" data-contact="!bbbb0002"'), 'and the host\'s request action');
    const noAction = P.renderListHtml(v, { t }, '');
    assert.ok(!noAction.includes('rp-request'), 'a host without a request action gets no button (the schedule form)');
    assert.ok(noAction.includes('rp-star'), 'favorites carry a star');
    const empty = P.renderListHtml(P.build(people, { query: 'zzz' }), { t }, '');
    assert.ok(empty.includes('No contact matches'));
    assert.ok(P.renderListHtml(P.build([], {}), { t }, '').includes('No known contacts yet.'));
    const frame = P.frameHtml({ t }, 'x');
    assert.ok(frame.includes('type="text"') && frame.includes('class="rp-clear"') && frame.includes('Favorites only') && frame.includes('rp-count'));
}

// ---------------------------------------------------------------- the wiring and the single-frame CSS
{
    const template = read('../templates/index.html');
    assert.ok(template.includes('recipient-picker.js') && template.indexOf('recipient-picker.js') < template.indexOf('/static/files.js'), 'loaded before files.js');
    const files = read('files.js');
    assert.ok(files.includes('id="filesSendRecipientPicker"') && files.includes('<input type="hidden" id="filesSendRecipient"'), 'the Files and gallery send dialog has the picker and a hidden value');
    assert.ok(!files.includes('<select id="filesSendRecipient">'), 'the native select is gone');
    assert.ok(files.includes('MCRecipientPicker') && files.includes('noteRecipientUsed(recipient.value)') && files.includes('noteRecipientUsed(usedRecipient)'), 'both send paths record the recipient');
    const chat = read('chat.js');
    assert.ok(chat.includes('id="sfPicContactPicker"') && chat.includes('type="hidden" id="sfPicContact"') && !chat.includes('<select id="sfPicContact"'), 'the schedule action uses the same picker');
    assert.ok(chat.includes("noteUsed(saved.params.contact_id)"), 'a saved rule counts as a use');
    const css = read('style-part4.css');
    assert.ok(/\.rp \.rp-input\.rp-input \{[^}]*border: 0 !important[^}]*outline: 0 !important[^}]*background: transparent !important[^}]*font-size: 16px/.test(css), 'one frame, 16px');
    assert.ok(/\.rp-row \{[^}]*min-height: 44px/.test(css), '44 px rows');
    assert.ok(/\.rp-field:focus-within \{[^}]*outline: 2px solid/.test(css), 'the ring is on the wrapper');
    assert.ok(/\.files-provider-probe:empty \{[^}]*display: none/.test(css), 'an empty key-status box is not drawn');
    for (const loc of ['en', 'de', 'ru', 'uk']) {
        const catalog = JSON.parse(readFileSync(path.join(__dirname, '..', '..', 'static', 'i18n', loc + '.json'), 'utf8'));
        for (const key of ['search_placeholder', 'favorites_only', 'group_nokey', 'key_unknown', 'request_key', 'count', 'type_to_narrow', 'no_match']) {
            assert.ok(catalog.recipient && catalog.recipient[key], loc + ' has recipient.' + key);
        }
    }
}

console.log('test_recipient_picker.mjs: ok');
