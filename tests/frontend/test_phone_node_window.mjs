// tests/frontend/test_phone_node_window.mjs
//
// MOB2-D: the phone renders only the first 60 nodes (638 cards = 2240 tap targets was too heavy): own node and
// favourites first, then the most recently heard (the list's default order), "Show more" adds the next batch, a
// search shows every match, a selected node is always included. Pure logic of MCViews.phoneNodeWindow().
// `node tests/frontend/test_phone_node_window.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const window = {};
vm.createContext(window);
window.window = window;
vm.runInContext(readFileSync(path.join(__dirname, '..', '..', 'static', 'chat-views.js'), 'utf8').split('\r\n').join('\n'), window);
const V = window.MCViews;
assert.ok(V.phoneNodeWindow, 'MCViews.phoneNodeWindow is exported');

// 200 nodes, already sorted by last heard (newest first), as computeDisplayNodes() returns them
const nodes = Array.from({ length: 200 }, (_, i) => ({ node_id: `!${String(i).padStart(8, '0')}`, last_seen: 1000 - i, favorite: false }));
const ids = (list) => list.map((n) => n.node_id);

// default: the first 60 in the given order, the rest counted
{
    const w = V.phoneNodeWindow(nodes, { limit: 60, sort: 'last_heard', localNodeId: '' });
    assert.equal(w.nodes.length, 60);
    assert.equal(w.hidden, 140);
    assert.equal(w.total, 200);
    assert.deepEqual(ids(w.nodes), ids(nodes.slice(0, 60)));
}

// own node and favourites first (own, then favourites, each in last-heard order), then the rest by last heard
{
    const list = nodes.map((n) => ({ ...n }));
    list[150].favorite = true; list[90].favorite = true;
    const own = list[120].node_id;
    const w = V.phoneNodeWindow(list, { limit: 60, sort: 'last_heard', localNodeId: own });
    assert.deepEqual(ids(w.nodes).slice(0, 3), [own, list[90].node_id, list[150].node_id]);
    assert.deepEqual(ids(w.nodes).slice(3, 6), ids(list.slice(0, 3)), 'then the most recently heard');
    assert.equal(w.nodes.length, 60);
    assert.equal(new Set(ids(w.nodes)).size, 60, 'no node twice');
}

// the node flagged is_local counts as own too
{
    const list = nodes.map((n) => ({ ...n }));
    list[77].is_local = true;
    assert.equal(V.phoneNodeWindow(list, { limit: 10, sort: 'last_heard' }).nodes[0].node_id, list[77].node_id);
}

// another sort is the user's choice: the order is kept, only the cap applies
{
    const list = nodes.map((n) => ({ ...n }));
    list[150].favorite = true;
    const w = V.phoneNodeWindow(list, { limit: 60, sort: 'name', localNodeId: list[10].node_id });
    assert.deepEqual(ids(w.nodes), ids(list.slice(0, 60)));
}

// Show more: the next batch
{
    const w = V.phoneNodeWindow(nodes, { limit: 120, sort: 'last_heard' });
    assert.equal(w.nodes.length, 120);
    assert.equal(w.hidden, 80);
    const all = V.phoneNodeWindow(nodes, { limit: 500, sort: 'last_heard' });
    assert.equal(all.nodes.length, 200);
    assert.equal(all.hidden, 0);
}

// a node that must be visible (selected, focused from a message) extends the window to include it
{
    const target = nodes[150].node_id;
    const w = V.phoneNodeWindow(nodes, { limit: 60, sort: 'last_heard', mustInclude: [target] });
    assert.ok(ids(w.nodes).includes(target));
    assert.equal(w.nodes.length, 151);
    assert.equal(w.hidden, 49);
    // already inside: nothing extra
    assert.equal(V.phoneNodeWindow(nodes, { limit: 60, sort: 'last_heard', mustInclude: [nodes[5].node_id] }).nodes.length, 60);
    // unknown id: ignored
    assert.equal(V.phoneNodeWindow(nodes, { limit: 60, sort: 'last_heard', mustInclude: ['!nope'] }).nodes.length, 60);
}

// small lists and bad input
{
    assert.equal(V.phoneNodeWindow(nodes.slice(0, 5), { limit: 60, sort: 'last_heard' }).hidden, 0);
    assert.deepEqual(JSON.parse(JSON.stringify(V.phoneNodeWindow([], { limit: 60 }))), { nodes: [], hidden: 0, total: 0 });
    assert.equal(V.phoneNodeWindow(nodes, { sort: 'last_heard' }).nodes.length, 60, 'default batch is 60');
    assert.equal(V.PHONE_NODE_BATCH, 60);
}

console.log('test_phone_node_window.mjs: ok');
