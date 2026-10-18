// tests/frontend/test_node_manager_refresh.mjs
//
// RR1 follow-up: the Node Manager must refresh itself - every 3 s during a transition
// (a reconnect takes 5-25 s; it used to say "Reconnecting" after the link was up until the
// user pressed Refresh), every 30 s otherwise. Runs the REAL functions from static/chat.js
// in a vm sandbox with fake timers. `node tests/frontend/test_node_manager_refresh.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat.js'), 'utf8');

function extract(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    assert.ok(start >= 0, `${startMarker} not found`);
    const end = source.indexOf(endMarker, start);
    assert.ok(end > start, `${endMarker} not found`);
    return source.slice(start, end);
}

const timers = [];
let loads = 0;
const view = { style: { display: 'flex' } };
const sandbox = {
    console,
    document: { hidden: false, getElementById: (id) => (id === 'nodeManagerView' ? view : null) },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    loadNodeManagerDashboard: () => { loads += 1; },
};
vm.createContext(sandbox);
// the block from the timer state through scheduleNodeManagerRefresh()
vm.runInContext(extract('let nodeManagerRefreshTimer', 'async function loadNodeManagerDashboard'), sandbox);

const transition = sandbox.nodeManagerConnectionInTransition;
assert.equal(transition({ mode: 'reconnecting' }), true);
assert.equal(transition({ mode: 'releasing' }), true);
assert.equal(transition({ mode: 'connected', reconnect: { state: 'waiting' } }), true);
assert.equal(transition({ mode: 'disconnected', reconnect: { state: 'attempting' } }), true);
assert.equal(transition({ mode: 'connected', reconnect: { state: 'idle' } }), false);
assert.equal(transition({ mode: 'released' }), false);
assert.equal(transition({ mode: 'connected' }), false);
assert.equal(transition(null), false);
assert.equal(vm.runInContext('NODE_MANAGER_REFRESH_TRANSITION_MS', sandbox), 3000);
assert.equal(vm.runInContext('NODE_MANAGER_REFRESH_IDLE_MS', sandbox), 30000);

// a scheduled refresh reloads while the view is open
sandbox.scheduleNodeManagerRefresh(3000);
assert.equal(timers.at(-1).ms, 3000);
timers.at(-1).fn();
assert.equal(loads, 1);

// ... does nothing once the user left the Node Manager (no timer left running)
view.style.display = 'none';
const before = timers.length;
sandbox.scheduleNodeManagerRefresh(3000);
timers.at(-1).fn();
assert.equal(loads, 1);
assert.equal(timers.length, before + 1, 'no further timer is scheduled after the view closed');

// ... and waits instead of loading while the browser tab is hidden
view.style.display = 'flex';
sandbox.document.hidden = true;
sandbox.scheduleNodeManagerRefresh(3000);
timers.at(-1).fn();
assert.equal(loads, 1);
assert.equal(timers.at(-1).ms, 30000, 'looks again later');

console.log('test_node_manager_refresh: all assertions passed');
