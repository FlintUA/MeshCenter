// tests/frontend/test_mobile_phone_status.mjs
//
// MOB2-E: CPU, RAM, temperature, uptime and version are not in the phone dock (MOB1 hides them). The Workspace
// sheet has a "Status" block; static/mobile-layout.js copies the values from the dock elements into it while the
// sheet is open on a phone, and does nothing on a wide viewport. `node tests/frontend/test_mobile_phone_status.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'mobile-layout.js'), 'utf8');

function makeEnv(mobile, dock) {
    const registry = {};
    const el = (id, text = '') => (registry[id] = { id, textContent: text, hidden: true, classList: { toggle() {}, contains: () => false }, setAttribute() {}, getAttribute: () => null, addEventListener() {} });
    el('workspacePopover');
    for (const id of ['mapLayoutPopover', 'notificationPopover']) el(id);
    for (const [id, text] of Object.entries(dock)) el(id, text);
    ['phoneStatusVersion', 'phoneStatusCpu', 'phoneStatusRam', 'phoneStatusTemp', 'phoneStatusUptime'].forEach((id) => el(id, '--'));
    const brand = { textContent: 'MeshCenter v1.13.4' };
    const document = {
        readyState: 'complete',
        body: { classList: { toggle() {}, contains: () => false }, setAttribute() {}, removeAttribute() {}, appendChild() {} },
        getElementById: (id) => registry[id] || null,
        createElement: () => el('x' + Math.random()),
        querySelector: (s) => (s === '.dock-brand-name' ? brand : null),
        addEventListener() {},
    };
    const window = { document, matchMedia: () => ({ matches: mobile, addEventListener() {} }), addEventListener() {}, I18N: { tOrFallback: (k, p, f) => f }, setInterval: () => 0, clearInterval() {} };
    const ctx = { window, document, setTimeout: (fn) => fn(), setInterval: () => 0, clearInterval() {}, MutationObserver: undefined };
    vm.createContext(ctx);
    vm.runInContext(source, ctx);
    return { api: ctx.window.MCMobileLayout, registry };
}

const dock = { dockCpuMetric: 'CPU 12%', dockRamMetric: 'RAM 70.9%', dockTempMetric: 'TEMP 51.5°C', baseUptimeBadge: 'Uptime 71d 17h' };

// phone: the block is filled from the dock
{
    const env = makeEnv(true, dock);
    env.api.syncPhoneStatus();
    assert.equal(env.registry.phoneStatusCpu.textContent, '12%', 'the label is in the row, not in the value');
    assert.equal(env.registry.phoneStatusRam.textContent, '70.9%');
    assert.equal(env.registry.phoneStatusTemp.textContent, '51.5°C');
    assert.equal(env.registry.phoneStatusUptime.textContent, '71d 17h');
    assert.equal(env.registry.phoneStatusVersion.textContent, 'v1.13.4');
}

// a value the dock does not have (no temperature in a container) leaves a dash, never "undefined"
{
    const env = makeEnv(true, { dockCpuMetric: 'CPU 3%' });
    env.api.syncPhoneStatus();
    assert.equal(env.registry.phoneStatusCpu.textContent, '3%');
    assert.equal(env.registry.phoneStatusTemp.textContent, '--');
}

// wide viewport: untouched
{
    const env = makeEnv(false, dock);
    env.api.syncPhoneStatus();
    assert.equal(env.registry.phoneStatusCpu.textContent, '--');
}

console.log('test_mobile_phone_status.mjs: ok');
