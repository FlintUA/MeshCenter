// tests/frontend/test_mobile_sheets.mjs
//
// MOB2-A/B: static/mobile-layout.js on a phone. The dock menus (Workspace, Map layout, Notifications) are bottom
// sheets: while one is open the body gets `mc-sheet-open`, a backdrop element exists and a tap on it (or Escape)
// closes them through the closers chat.js owns. The tab row shows a fade on the side that has more tabs.
// On a wide viewport none of it happens. `node tests/frontend/test_mobile_sheets.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'mobile-layout.js'), 'utf8');

function makeEnv(mobile) {
    const closed = [];
    const registry = {};
    const docListeners = {};
    const observers = [];
    const classSet = new Set();
    const element = (id, extra = {}) => {
        const e = Object.assign({ id, hidden: true, attrs: {}, listeners: {}, classes: new Set(), children: [] }, extra);
        e.classList = { toggle: (n, on) => (on ? e.classes.add(n) : e.classes.delete(n)), contains: (n) => e.classes.has(n) };
        e.setAttribute = (k, v) => { e.attrs[k] = v; };
        e.getAttribute = (k) => (k in e.attrs ? e.attrs[k] : null);
        e.addEventListener = (name, fn) => { (e.listeners[name] = e.listeners[name] || []).push(fn); };
        e.fire = (name, event = {}) => (e.listeners[name] || []).forEach((fn) => fn(event));
        if (id) registry[id] = e;
        return e;
    };
    ['workspacePopover', 'mapLayoutPopover', 'notificationPopover'].forEach((id) => element(id));
    const tabs = element('', { scrollWidth: 600, clientWidth: 400, scrollLeft: 0 });
    const body = {
        classList: { toggle: (n, on) => (on ? classSet.add(n) : classSet.delete(n)), contains: (n) => classSet.has(n) },
        setAttribute() {}, removeAttribute() {},
        appendChild: (child) => { if (child.id) registry[child.id] = child; },
    };
    const document = {
        readyState: 'complete', body,
        getElementById: (id) => registry[id] || null,
        createElement: () => element(''),
        querySelector: (s) => (s === '.main-content-tabs' ? tabs : null),
        addEventListener: (name, fn) => { docListeners[name] = fn; },
    };
    const win = {
        document,
        matchMedia: () => ({ matches: mobile, addEventListener() {} }),
        addEventListener() {},
        I18N: { tOrFallback: (k, p, f) => f },
        MutationObserver: function (cb) { this.observe = () => {}; observers.push(cb); },
        setWorkspacePopover: (open) => { if (!open) { registry.workspacePopover.hidden = true; closed.push('workspace'); } },
        setMapLayoutPopover: (open) => { if (!open) { registry.mapLayoutPopover.hidden = true; closed.push('map'); } },
        closeNotificationCenter: () => { registry.notificationPopover.hidden = true; closed.push('notifications'); },
    };
    win.window = win;
    vm.createContext(win);
    vm.runInContext(source, Object.assign(win, { MutationObserver: win.MutationObserver }));
    const api = win.MCMobileLayout;
    return { win, registry, classSet, closed, docListeners, observers, tabs, api };
}

// a phone: the backdrop exists, an open sheet sets the body class, the backdrop and Escape close it
{
    const env = makeEnv(true);
    assert.ok(env.registry.mcSheetBackdrop, 'the backdrop element is added');
    assert.equal(env.classSet.has('mc-sheet-open'), false);
    env.registry.workspacePopover.hidden = false;
    env.api.syncSheetState();
    assert.equal(env.classSet.has('mc-sheet-open'), true, 'an open sheet marks the body');
    env.registry.mcSheetBackdrop.fire('click');
    assert.deepEqual(env.closed, ['workspace', 'map', 'notifications'], 'a backdrop tap runs every closer chat.js owns');
    env.api.syncSheetState();
    assert.equal(env.classSet.has('mc-sheet-open'), false, 'closed: the mark is gone');

    env.registry.notificationPopover.hidden = false;
    env.api.syncSheetState();
    env.closed.length = 0;
    env.docListeners.keydown({ key: 'Escape' });
    assert.ok(env.closed.includes('notifications'), 'Escape closes the open sheet');
    env.closed.length = 0;
    env.docListeners.keydown({ key: 'Enter' });
    assert.deepEqual(env.closed, [], 'other keys do nothing');
    // the observer re-syncs when a popover toggles itself (the closers in chat.js set `hidden`)
    env.registry.mapLayoutPopover.hidden = false;
    env.observers.forEach((cb) => cb());
    assert.equal(env.classSet.has('mc-sheet-open'), true);
}

// the tab row: a fade on the side that has more, none when everything fits
{
    const env = makeEnv(true);
    env.api.updateTabsCue();
    assert.equal(env.tabs.classes.has('tabs-more-right'), true);
    assert.equal(env.tabs.classes.has('tabs-more-left'), false, 'at the start: nothing hidden on the left');
    env.tabs.scrollLeft = 100;
    env.api.updateTabsCue();
    assert.equal(env.tabs.classes.has('tabs-more-left'), true);
    assert.equal(env.tabs.classes.has('tabs-more-right'), true);
    env.tabs.scrollLeft = 200; // 200 + 400 = 600: the end
    env.api.updateTabsCue();
    assert.equal(env.tabs.classes.has('tabs-more-right'), false);
    assert.equal(env.tabs.classes.has('tabs-more-left'), true);
    env.tabs.scrollWidth = 400; // everything fits
    env.api.updateTabsCue();
    assert.equal(env.tabs.classes.has('tabs-more-left') || env.tabs.classes.has('tabs-more-right'), false);
}

// a wide viewport: no mark, no cue
{
    const env = makeEnv(false);
    env.registry.workspacePopover.hidden = false;
    env.api.syncSheetState();
    assert.equal(env.classSet.has('mc-sheet-open'), false, 'desktop: the popover stays a popover');
    env.docListeners.keydown({ key: 'Escape' });
    assert.deepEqual(env.closed, [], 'desktop: Escape is left to the existing handlers');
    env.api.updateTabsCue();
    assert.equal(env.tabs.classes.has('tabs-more-right'), false, 'desktop: no fade');
}

console.log('test_mobile_sheets.mjs: ok');
