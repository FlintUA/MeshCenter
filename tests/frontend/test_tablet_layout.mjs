// tests/frontend/test_tablet_layout.mjs
//
// TAB1: static/tablet-layout.js. Band detection, the drawer-vs-push mode per band, the 520px
// center-column safety net, and the per-band manual-override memory, all against a stub DOM/
// matchMedia/localStorage (no browser needed). `node tests/frontend/test_tablet_layout.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'tablet-layout.js'), 'utf8');

// width/pointer -> which of the three matchMedia queries the module creates reports true
function mqMatches(query, width, coarse) {
    if (query.indexOf('max-width: 768px') !== -1) return width <= 768;
    if (query.indexOf('769px') !== -1 && query.indexOf('1100px') !== -1) return width >= 769 && width <= 1100;
    if (query.indexOf('1101px') !== -1 && query.indexOf('1400px') !== -1) return width >= 1101 && width <= 1400 && coarse;
    return false;
}

function makeEnv(width, coarse, options = {}) {
    const bodyAttrs = {};
    const bodyClasses = new Set();
    const listeners = {};
    const panelWidths = options.panelWidths || { left: 400, right: 400 };
    const panelOpen = { left: options.leftOpen !== false, right: options.rightOpen !== false };
    const store = { ...(options.storage || {}) };
    const localStorage = {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = v; },
    };
    function mkPanel(side) {
        const classes = new Set();
        return {
            id: side === 'left' ? 'baseSidebar' : 'sidebar',
            classList: { toggle: (n, on) => (on ? classes.add(n) : classes.delete(n)), contains: (n) => classes.has(n) },
            _classes: classes,
            hasAttribute: () => false,
            setAttribute() {},
            getBoundingClientRect: () => ({ width: panelOpen[side] ? panelWidths[side] : 0 }),
            focus() {},
        };
    }
    const panels = { left: mkPanel('left'), right: mkPanel('right') };
    function mkButton(id) {
        return { id, focus() {} };
    }
    const buttons = { toggleBaseSidebarBtn: mkButton('toggleBaseSidebarBtn'), toggleSidebarBtn: mkButton('toggleSidebarBtn') };
    const created = [];
    const body = {
        setAttribute: (k, v) => { bodyAttrs[k] = v; },
        removeAttribute: (k) => { delete bodyAttrs[k]; },
        classList: { toggle: (n, on) => (on ? bodyClasses.add(n) : bodyClasses.delete(n)) },
        appendChild: (el) => created.push(el),
    };
    const document = {
        readyState: 'complete',
        body,
        activeElement: null,
        createElement: () => ({ setAttribute() {}, addEventListener() {}, classList: { toggle() {} } }),
        getElementById: (id) => panels.left.id === id ? panels.left : panels.right.id === id ? panels.right : buttons[id] || null,
        addEventListener: (name, fn) => { listeners[name] = fn; },
    };
    const mqObjects = {};
    function matchMedia(query) {
        if (!mqObjects[query]) mqObjects[query] = { matches: mqMatches(query, width, coarse), addEventListener() {}, query };
        return mqObjects[query];
    }
    // Deliberately NOT exposed as window.Workspace: static/chat.js declares it as a top-level
    // `const`, which classic <script> tags share via the global scope but never attach to
    // `window` - exposing it there too would hide a real bug (see tablet-layout.js's own
    // `hasWorkspace()` comment).
    const Workspace = { state: { leftPanel: panelOpen.left, rightPanel: panelOpen.right } };
    const windowObj = {
        matchMedia,
        innerWidth: width,
        document,
        localStorage,
        setBasePanelVisible: (open) => { panelOpen.left = open; Workspace.state.leftPanel = open; },
        setNodesPanelVisible: (open) => { panelOpen.right = open; Workspace.state.rightPanel = open; },
        addEventListener: (name, fn) => { listeners['window:' + name] = fn; },
        setTimeout: (fn) => fn(),
        clearTimeout() {},
    };
    const ctx = {
        window: windowObj, document, localStorage, Workspace,
        setTimeout: (fn) => fn(), clearTimeout() {},
    };
    vm.createContext(ctx);
    vm.runInContext(source, ctx);
    const click = (id) => {
        const target = buttons[id];
        const event = { target: { closest: () => target }, preventDefault() {}, stopImmediatePropagation() {} };
        listeners.click(event);
        return event;
    };
    const keydown = (key) => listeners.keydown({ key });
    return { api: ctx.window.MCTabletLayout, bodyAttrs, panels, panelOpen, Workspace, click, keydown, store };
}

// band() for each named band plus the untouched desktop/phone ones
{
    assert.equal(makeEnv(500, false).api.band(), 'phone');
    assert.equal(makeEnv(768, false).api.band(), 'phone');
    assert.equal(makeEnv(769, false).api.band(), 'tabletPortrait');
    assert.equal(makeEnv(891, false).api.band(), 'tabletPortrait', 'Flint\'s measured real portrait viewport');
    assert.equal(makeEnv(1100, true).api.band(), 'tabletPortrait');
    assert.equal(makeEnv(1101, true).api.band(), 'landscapeTouch');
    assert.equal(makeEnv(1164, true).api.band(), 'landscapeTouch', 'Flint\'s measured real landscape viewport');
    assert.equal(makeEnv(1101, false).api.band(), 'desktop', 'same width, a mouse: not the touch-landscape band');
    assert.equal(makeEnv(1401, true).api.band(), 'desktop', 'past the landscape-touch band, plenty of room either way');
    assert.equal(makeEnv(1600, false).api.band(), 'desktop');
}

// modeOf(): which side is a drawer in which band
{
    const { api } = makeEnv(900, true);
    assert.equal(api.modeOf('left', 'tabletPortrait'), 'drawer');
    assert.equal(api.modeOf('right', 'tabletPortrait'), 'drawer');
    assert.equal(api.modeOf('left', 'landscapeTouch'), 'drawer');
    assert.equal(api.modeOf('right', 'landscapeTouch'), 'push');
    assert.equal(api.modeOf('left', 'desktop'), 'push');
    assert.equal(api.modeOf('right', 'desktop'), 'push');
}

// tabletPortrait band default: both panels start collapsed, rendered as drawers
{
    const { bodyAttrs, Workspace } = makeEnv(800, true);
    assert.equal(bodyAttrs['data-sidebar-mode-left'], 'drawer');
    assert.equal(bodyAttrs['data-sidebar-mode-right'], 'drawer');
    assert.equal(Workspace.state.leftPanel, false);
    assert.equal(Workspace.state.rightPanel, false);
}

// landscapeTouch band default: left collapsed (drawer), right shown (push)
{
    const { bodyAttrs, Workspace } = makeEnv(1200, true);
    assert.equal(bodyAttrs['data-sidebar-mode-left'], 'drawer');
    assert.equal(bodyAttrs['data-sidebar-mode-right'], 'push');
    assert.equal(Workspace.state.leftPanel, false);
    assert.equal(Workspace.state.rightPanel, true);
}

// desktop band: no forced default - an existing (returning-user) preference survives untouched
{
    const { Workspace } = makeEnv(1600, false, { leftOpen: true, rightOpen: true });
    assert.equal(Workspace.state.leftPanel, true);
    assert.equal(Workspace.state.rightPanel, true);
}

// drawer toggle: opening the left drawer sets the open class and moves focus into the panel;
// Escape (or the backdrop, exercised via closeAllDrawers) closes it and returns focus to the button
{
    const { api, panels, buttons: _unused, click } = makeEnv(800, true);
    assert.equal(panels.left._classes.has('sidebar-drawer-open'), false);
    click('toggleBaseSidebarBtn');
    assert.equal(panels.left._classes.has('sidebar-drawer-open'), true);
    api.closeAllDrawers(true);
    assert.equal(panels.left._classes.has('sidebar-drawer-open'), false);
}

// opening one drawer in tabletPortrait closes the other (one at a time)
{
    const { api, panels, click } = makeEnv(800, true);
    click('toggleBaseSidebarBtn');
    assert.equal(panels.left._classes.has('sidebar-drawer-open'), true);
    click('toggleSidebarBtn');
    assert.equal(panels.right._classes.has('sidebar-drawer-open'), true);
    assert.equal(panels.left._classes.has('sidebar-drawer-open'), false, 'the first drawer closed when the second opened');
}

// the 520px safety net: two push-mode (desktop-band) panels wide enough to leave no room for the
// center collapse the left one first, then the right, until the center clears 520px
{
    const { api, Workspace } = makeEnv(1280, false, { panelWidths: { left: 400, right: 400 } });
    // 1280 - 400 - 400 = 480 < 520
    assert.equal(Workspace.state.leftPanel, false, 'left collapsed automatically');
    assert.equal(Workspace.state.rightPanel, true, 'collapsing the left alone was already enough (1280-400=880)');
    assert.ok(api.measureCenterWidth() >= 520);
}

{
    // narrow enough that even collapsing just the left is not enough
    const { Workspace } = makeEnv(900, false, { panelWidths: { left: 400, right: 400 } });
    assert.equal(Workspace.state.leftPanel, false);
    assert.equal(Workspace.state.rightPanel, false);
}

// manual override remembered per band until the band itself changes (tabletPortrait: user opens
// the left drawer and leaves it open - a resize that stays inside the same band must not re-close it)
{
    const { api, click, panels } = makeEnv(800, true);
    click('toggleBaseSidebarBtn'); // user opens it
    assert.equal(panels.left._classes.has('sidebar-drawer-open'), true);
    api._applyBandEntry(false); // same band, e.g. a same-band resize tick
    assert.equal(panels.left._classes.has('sidebar-drawer-open'), true, 'manual choice survives a same-band re-check');
}

// ...but a stored choice from a PREVIOUS session for this band (localStorage) is honored on load,
// and does not leak into a different band's defaults
{
    const stored = JSON.stringify({ tabletPortrait: { left: true, right: false } });
    const { Workspace: wsPortrait } = makeEnv(800, true, { storage: { 'meshcenter.workspace.bandPanels': stored } });
    assert.equal(wsPortrait.state.leftPanel, true, 'remembered choice applied instead of the band default');
    const { Workspace: wsLandscape } = makeEnv(1200, true, { storage: { 'meshcenter.workspace.bandPanels': stored } });
    assert.equal(wsLandscape.state.leftPanel, false, 'a different band still gets its own default');
}

console.log('test_tablet_layout.mjs: all assertions passed');
