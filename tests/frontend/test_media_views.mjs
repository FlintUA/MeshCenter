// tests/frontend/test_media_views.mjs
//
// Gallery G1: the pure view logic (static/media-views.js: sort, selection, zip size rule, lightbox neighbours) and the
// camera full screen (the G1 section of static/chat-camera.js, extracted and run against a fake document).
// `node tests/frontend/test_media_views.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const template = readFileSync(path.join(__dirname, '..', '..', 'templates', 'index.html'), 'utf8').split('\r\n').join('\n');
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(read('media-views.js'), sandbox);
const V = sandbox.MCMediaViews;

const item = (name, extra = {}) => ({ filename: '2026/10/30/' + name, display_name: name, size: 1000, modified_ts: 1000, camera: '', ...extra });
const names = (list) => list.map((i) => i.display_name);

// ---------------------------------------------------------------- normalize
assert.equal(V.normalizeSort('size'), 'size');
assert.equal(V.normalizeSort('bogus'), 'date_desc');
assert.equal(V.normalizeSort(undefined), 'date_desc');
assert.equal(V.normalizeView('list'), 'list');
assert.equal(V.normalizeView('huge'), 'large');
assert.deepEqual(Array.from(V.SORTS), ['date_desc', 'date_asc', 'name', 'size', 'camera']);

// ---------------------------------------------------------------- sort
const pictures = [
    item('b.jpg', { modified_ts: 200, size: 50, camera: 'Garage' }),
    item('a.jpg', { modified_ts: 300, size: 900, camera: '' }),
    item('C10.jpg', { modified_ts: 100, size: 500, camera: 'Yard' }),
    item('C2.jpg', { modified_ts: 400, size: 500, camera: 'Garage' }),
];
const original = names(pictures);
assert.deepEqual(names(V.sortItems(pictures, 'date_desc')), ['C2.jpg', 'a.jpg', 'b.jpg', 'C10.jpg'], 'newest first');
assert.deepEqual(names(V.sortItems(pictures, 'date_asc')), ['C10.jpg', 'b.jpg', 'a.jpg', 'C2.jpg'], 'oldest first');
assert.deepEqual(names(V.sortItems(pictures, 'name')), ['a.jpg', 'b.jpg', 'C2.jpg', 'C10.jpg'], 'case-insensitive, numbers by value');
assert.deepEqual(names(V.sortItems(pictures, 'size')), ['a.jpg', 'C2.jpg', 'C10.jpg', 'b.jpg'], 'largest first, ties newest first');
assert.deepEqual(names(V.sortItems(pictures, 'camera')), ['C2.jpg', 'b.jpg', 'C10.jpg', 'a.jpg'], 'named cameras A to Z (newest first within one), no camera last');
assert.deepEqual(names(pictures), original, 'the caller list is never reordered');
assert.equal(V.sortItems(null, 'name').length, 0);
assert.deepEqual(names(V.sortItems(pictures, 'nonsense')), names(V.sortItems(pictures, 'date_desc')), 'an unknown key is the default');

// ---------------------------------------------------------------- the view is remembered per device
assert.equal(V.viewField(true), 'view_mobile');
assert.equal(V.viewField(false), 'view_desktop');
assert.equal(V.pickView({ view_desktop: 'list', view_mobile: 'small' }, true), 'small', 'a phone reads its own field');
assert.equal(V.pickView({ view_desktop: 'list', view_mobile: 'small' }, false), 'list', 'a desktop reads its own');
assert.equal(V.pickView({ view: 'list' }, true), 'list', 'the old single view is the fallback');
assert.equal(V.pickView({ view: 'list', view_mobile: 'small' }, false), 'list');
assert.equal(V.pickView({ view_mobile: 'bogus' }, true), 'large', 'an invalid value is the default');
assert.equal(V.pickView(null, false), 'large');

// ---------------------------------------------------------------- formatting
assert.equal(V.formatBytes(500), '500 B');
assert.equal(V.formatBytes(2048), '2.0 KB');
assert.equal(V.formatBytes(1.2 * 1024 * 1024), '1.2 MB');
assert.equal(V.formatBytes(250 * 1024 * 1024), '250 MB');
assert.equal(V.formatBytes(3 * 1024 * 1024 * 1024), '3.0 GB');
assert.equal(V.formatBytes(NaN), '-');
assert.equal(V.formatResolution({ width: 3840, height: 2160 }), '3840x2160');
assert.equal(V.formatResolution({ width: 0, height: 10 }), '');
assert.equal(V.formatResolution({}), '');

// ---------------------------------------------------------------- selection
let sel = new Set();
sel = V.toggle(sel, pictures[0].filename);
assert.ok(sel.has(pictures[0].filename));
assert.equal(V.toggle(sel, pictures[0].filename).has(pictures[0].filename), false, 'toggle again removes it');
assert.equal(sel.size, 1, 'toggle returns a new set');
assert.equal(V.allSelected(sel, pictures), false);
const all = V.toggleAll(sel, pictures);
assert.equal(all.size, 4, 'select all selects everything shown');
assert.equal(V.allSelected(all, pictures), true);
assert.equal(V.toggleAll(all, pictures).size, 0, 'select all again clears');
assert.equal(V.allSelected(new Set(), []), false, 'nothing to select is not "all selected"');
assert.equal(V.prune(all, pictures.slice(0, 2)).size, 2, 'a deleted picture leaves the selection');
assert.deepEqual(JSON.parse(JSON.stringify(V.totals(all, pictures))), { count: 4, bytes: 50 + 900 + 500 + 500 });

// ---------------------------------------------------------------- bulk bar and the zip size rule
const MB = 1024 * 1024;
const big = (n, mb) => Array.from({ length: n }, (_, i) => item('p' + i + '.jpg', { size: mb * MB }));
{
    const none = V.bulkState(new Set(), big(2, 1));
    assert.equal(none.any, false);
    assert.equal(none.canDownload, false);
}
{
    const list = big(5, 50); // exactly 250 MB = the cap
    const state = V.bulkState(new Set(list.map((i) => i.filename)), list);
    assert.equal(state.bytes, 250 * MB);
    assert.equal(state.zipTooLarge, false, 'exactly at the cap is allowed');
    assert.equal(state.canDownload, true);
    assert.equal(state.zipMaxBytes, 250 * MB);
}
{
    const list = big(5, 50).concat(big(1, 1).map((i) => ({ ...i, filename: 'x/one.jpg' })));
    const state = V.bulkState(new Set(list.map((i) => i.filename)), list);
    assert.equal(state.zipTooLarge, true, 'one byte over: the download is off and the note says why');
    assert.equal(state.canDownload, false);
    assert.equal(state.canDelete, true, 'deleting does not depend on the zip size');
}
{
    const list = big(501, 0);
    const state = V.bulkState(new Set(list.map((i) => i.filename)), list);
    assert.equal(state.tooMany, true);
    assert.equal(state.canDownload, false);
    assert.equal(state.canDelete, false);
}

// ---------------------------------------------------------------- lightbox neighbours
assert.equal(V.neighbour(0, -1, 5), null, 'no previous before the first');
assert.equal(V.neighbour(4, 1, 5), null, 'no next after the last');
assert.equal(V.neighbour(2, 1, 5), 3);
assert.equal(V.neighbour(2, -1, 5), 1);
assert.equal(V.indexAfterDelete(2, 5), 2, 'the next picture takes the place');
assert.equal(V.indexAfterDelete(4, 5), 3, 'deleting the last shows the new last');
assert.equal(V.indexAfterDelete(0, 1), null, 'nothing left');

// ---------------------------------------------------------------- the real files: wiring that must not drift
const media = read('media.js');
assert.ok(media.includes("fetch('/api/camera/screenshots/zip'") && media.includes("fetch('/api/camera/screenshots/delete'"), 'bulk calls');
assert.ok(media.includes('media: { sort: mediaSort, [mediaViews().viewField(mediaIsPhone())]: mediaView }'), 'sort and the per-device view are remembered through the settings');
assert.ok(media.includes('loading="lazy"') && media.includes('thumb_url'), 'thumbnails load lazily');
assert.ok(!/(^|[^A-Za-z])(confirm|alert|prompt)\(/.test(media), 'no native dialogs');
assert.ok(template.indexOf('media-views.js') < template.indexOf('/static/media.js'), 'media-views.js loads first');

// ---------------------------------------------------------------- camera full screen (the G1 section of chat-camera.js)
{
    const source = read('chat-camera.js');
    const marker = source.indexOf('// FULL SCREEN (G1)');
    const section = source.slice(source.lastIndexOf('// ====', marker));
    const listeners = {};
    const classes = new Set();
    const bodyClasses = new Set();
    const state = { fullscreenElement: null, requested: 0, exited: 0 };
    const wrap = {
        classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) },
    };
    const button = { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, title: '' };
    const document = {
        get fullscreenElement() { return state.fullscreenElement; },
        querySelector: () => wrap,
        getElementById: () => button,
        body: { classList: { toggle: (c, on) => (on ? bodyClasses.add(c) : bodyClasses.delete(c)) } },
        addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
        exitFullscreen: () => { state.exited += 1; state.fullscreenElement = null; return Promise.resolve(); },
    };
    const ctx = { document, window: { I18N: { t: (k) => k } } };
    vm.createContext(ctx);
    vm.runInContext(section, ctx);
    const fire = (name, event = {}) => (listeners[name] || []).forEach((fn) => fn(event));

    // real full screen: the frame is requested, the page follows fullscreenchange
    wrap.requestFullscreen = () => { state.requested += 1; state.fullscreenElement = wrap; return Promise.resolve(); };
    vm.runInContext('toggleCameraFullscreen()', ctx);
    assert.equal(state.requested, 1, 'the whole frame goes full screen (video, snapshot picture and overlay together)');
    fire('fullscreenchange');
    assert.ok(classes.has('is-fullscreen') && bodyClasses.has('camera-fullscreen-active'));
    assert.equal(button.attrs['aria-pressed'], 'true');
    assert.equal(button.attrs['aria-label'], 'camera.fullscreen_exit');
    vm.runInContext('toggleCameraFullscreen()', ctx);
    assert.equal(state.exited, 1, 'the button leaves full screen again');
    fire('fullscreenchange');
    assert.ok(!classes.has('is-fullscreen'));
    assert.equal(button.attrs['aria-label'], 'camera.fullscreen_enter');

    // a browser without element full screen (an iPhone): the frame is stretched over the page, the button and Esc leave it
    delete wrap.requestFullscreen;
    vm.runInContext('toggleCameraFullscreen()', ctx);
    assert.ok(classes.has('is-pseudo-fullscreen') && classes.has('is-fullscreen'), 'fallback: pseudo full screen');
    fire('keydown', { key: 'Escape' });
    assert.ok(!classes.has('is-pseudo-fullscreen') && !classes.has('is-fullscreen'), 'Esc leaves the fallback');
    vm.runInContext('toggleCameraFullscreen()', ctx);
    vm.runInContext('toggleCameraFullscreen()', ctx);
    assert.ok(!classes.has('is-pseudo-fullscreen'), 'the button leaves the fallback too');

    // a request the browser refuses falls back to the stretched frame instead of doing nothing
    wrap.requestFullscreen = () => Promise.reject(new Error('denied'));
    vm.runInContext('toggleCameraFullscreen()', ctx);
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.ok(classes.has('is-pseudo-fullscreen'), 'a refused request: pseudo full screen');
    vm.runInContext('exitCameraFullscreen()', ctx);
    assert.ok(!classes.has('is-pseudo-fullscreen'));
}
const html = template;
assert.ok(html.includes('id="cameraFullscreenBtn"'), 'the button is in the camera frame');
assert.ok(html.indexOf('id="cameraFullscreenBtn"') > html.indexOf('class="video-frame-wrap"'), 'inside the frame wrap');

// ---------------------------------------------------------------- G1b: selection mode, tap action, long press
{
    const V = sandbox.MCMediaViews;
    assert.equal(V.selectionMode(new Set()), false);
    assert.equal(V.selectionMode(new Set(['a'])), true);
    assert.equal(V.tapAction(new Set(), true), 'open', 'nothing selected: a tap opens the picture');
    assert.equal(V.tapAction(new Set(['a']), true), 'toggle', 'selecting on touch: a tap toggles');
    assert.equal(V.tapAction(new Set(['a']), false), 'open', 'a mouse keeps the checkboxes, a click opens');

    // fake clock: timers run only when the test says so
    let nextId = 1;
    const timers = new Map();
    const fired = [];
    const press = V.createLongPress({
        delay: 500, slop: 10,
        setTimer: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
        clearTimer: (id) => timers.delete(id),
        onFire: (ctx) => fired.push(ctx),
    });
    const runTimers = () => { for (const [id, t] of Array.from(timers)) { timers.delete(id); t.fn(); } };

    press.start(100, 100, 'card-1');
    assert.equal(press.pending, true);
    assert.equal([...timers.values()][0].ms, 500, 'about half a second');
    runTimers();
    assert.deepEqual(fired, ['card-1']);
    assert.equal(press.end(), true, 'end() says the press fired (the click that follows must be swallowed)');
    assert.equal(press.end(), false, 'and only once');

    press.start(100, 100, 'card-2');
    press.move(105, 104);                       // a finger wobble inside the slop
    assert.equal(press.pending, true, 'small movement keeps the press');
    press.move(100, 115);                       // a scroll: more than 10 px
    assert.equal(press.pending, false, 'moving more than 10 px cancels');
    assert.equal(timers.size, 0);
    runTimers();
    assert.deepEqual(fired, ['card-1'], 'a cancelled press never selects');

    press.start(5, 5, 'card-3');
    assert.equal(press.end(), false, 'lifting early is a plain tap');
    assert.equal(timers.size, 0);
    runTimers();
    assert.deepEqual(fired, ['card-1']);
}

// ---------------------------------------------------------------- G2: send selection
{
    const V = sandbox.MCMediaViews;
    const items = Array.from({ length: 12 }, (_, i) => ({ filename: 'd/' + i + '.jpg', display_name: i + '.jpg', size: 100 + i }));
    const pick = (n) => new Set(items.slice(0, n).map((item) => item.filename));
    assert.equal(V.SEND_MAX, 10);
    assert.equal(V.bulkState(new Set(), items).canSend, false, 'nothing selected: no send');
    assert.equal(V.bulkState(pick(1), items).canSend, true);
    assert.equal(V.bulkState(pick(10), items).canSend, true, '10 is the limit');
    const eleven = V.bulkState(pick(11), items);
    assert.equal(eleven.canSend, false);
    assert.equal(eleven.sendTooMany, true, 'more than 10: the toolbar says so');
    assert.equal(eleven.sendMax, 10);
    const chosen = V.sendItems(new Set(['d/3.jpg', 'd/1.jpg']), items);
    assert.deepEqual(chosen.map((c) => c.filename), ['d/1.jpg', 'd/3.jpg'], 'in the order shown');
    assert.deepEqual(Object.keys(chosen[0]).sort(), ['display_name', 'filename', 'size']);
}

console.log('test_media_views.mjs: ok');
