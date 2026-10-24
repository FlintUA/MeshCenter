// tests/frontend/test_mobile_layout.mjs
//
// MOB1: static/mobile-layout.js. On a narrow viewport the dock buttons that hide/show the Base and Nodes
// columns open them as full-screen pages instead (and a second press goes back); on a wide viewport the
// script does nothing at all (the desktop handlers must still receive the click, no attribute is set,
// nothing is scrolled). `node tests/frontend/test_mobile_layout.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'mobile-layout.js'), 'utf8');

function makeEnv(initialMobile) {
    const attrs = {};
    const listeners = {};
    const mqListeners = [];
    const mq = { matches: initialMobile, addEventListener: (n, fn) => mqListeners.push(fn) };
    const scrolled = [];
    const mkButton = (id, cls = []) => {
        const b = { id, attrs: {}, classes: new Set(cls) };
        b.closest = () => b;
        b.classList = { toggle: (n, on) => (on ? b.classes.add(n) : b.classes.delete(n)), contains: (n) => b.classes.has(n) };
        b.setAttribute = (k, v) => { b.attrs[k] = v; };
        return b;
    };
    const base = mkButton('toggleBaseSidebarBtn');
    const nodes = mkButton('toggleSidebarBtn');
    const tab = mkButton('mainTabMedia', ['main-content-tab']);
    const activeTab = { scrollIntoView: (o) => scrolled.push(o) };
    const body = { setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: (k) => { delete attrs[k]; } };
    const document = {
        readyState: 'complete', body,
        getElementById: (id) => ({ toggleBaseSidebarBtn: base, toggleSidebarBtn: nodes }[id] || null),
        querySelector: (s) => (s === '.main-content-tab.active' ? activeTab : s === '.main-content-tabs' ? null : null),
        addEventListener: (name, fn) => { listeners[name] = fn; },
    };
    const window = { matchMedia: () => mq, document, addEventListener() {}, setTimeout: (fn) => fn() };
    const ctx = { window, document, setTimeout: (fn) => fn(), MutationObserver: undefined };
    vm.createContext(ctx);
    vm.runInContext(source, ctx);
    const click = (button) => {
        const event = { target: button, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
        listeners.click(event);
        return event;
    };
    return { api: ctx.window.MCMobileLayout, attrs, base, nodes, tab, click, mq, mqListeners, scrolled };
}

// the pane rule
{
    const { api } = makeEnv(true);
    assert.equal(api.nextPane('workspace', 'base'), 'base');
    assert.equal(api.nextPane('base', 'base'), 'workspace', 'the same button again goes back');
    assert.equal(api.nextPane('base', 'nodes'), 'nodes', 'the other button switches page');
    assert.equal(api.nextPane('nodes', 'whatever'), 'workspace');
}

// narrow viewport: the workspace is the default page, the dock buttons open the others and are not passed on
{
    const env = makeEnv(true);
    assert.equal(env.attrs['data-mobile-pane'], 'workspace');
    let e = env.click(env.base);
    assert.ok(e.stopped && e.prevented, 'the desktop hide/show handler must not run on a phone');
    assert.equal(env.attrs['data-mobile-pane'], 'base');
    assert.equal(env.base.attrs['aria-pressed'], 'true');
    assert.equal(env.nodes.attrs['aria-pressed'], 'false');
    e = env.click(env.nodes);
    assert.equal(env.attrs['data-mobile-pane'], 'nodes');
    env.click(env.nodes);
    assert.equal(env.attrs['data-mobile-pane'], 'workspace');
    env.click(env.tab);
    assert.ok(env.scrolled.some((o) => o.inline === 'center'), 'the open tab is scrolled into view on a phone');
}

// wide viewport: nothing happens, the click reaches the desktop handler, no attribute, nothing scrolled
{
    const env = makeEnv(false);
    assert.equal(env.attrs['data-mobile-pane'], undefined);
    const e = env.click(env.base);
    assert.ok(!e.stopped && !e.prevented);
    assert.equal(env.attrs['data-mobile-pane'], undefined);
    env.click(env.tab);
    assert.equal(env.scrolled.length, 0, 'no scrolling on a wide viewport');
}

// the viewport changes while the page is open: the attribute follows
{
    const env = makeEnv(true);
    env.click(env.nodes);
    env.mq.matches = false;
    env.mqListeners.forEach((fn) => fn());
    assert.equal(env.attrs['data-mobile-pane'], undefined, 'a wide viewport has no pane attribute');
    env.mq.matches = true;
    env.mqListeners.forEach((fn) => fn());
    assert.equal(env.attrs['data-mobile-pane'], 'nodes', 'the pane that was open comes back');
}

// the module never touches the persisted desktop workspace state
{
    const code = source.split(/\r?\n/).filter((line) => !line.trim().startsWith('//')).join(' ');
    assert.doesNotMatch(code, /Workspace\./);
    assert.doesNotMatch(code, /localStorage|sessionStorage|fetch\(/);
}

console.log('test_mobile_layout.mjs: ok');
