// tests/frontend/test_shared_selection_wiring.mjs
//
// Integration tests for the PR 4 shared navigation-target store across THREE
// real files at once: static/targets.js (the store), static/files.js (the
// Files workspace), and static/chat.js (the Nodes/channel sidebar).
//
// This is the executable counterpart to the reviewer's Finding 1 / "Blocker 1"
// requirement: chat.js must route its node/channel selection through the SAME
// store files.js already uses — not keep a private currentChatId and never
// call store.select()/store.selected()/store.subscribe(). The earlier
// test_files_ui.mjs exercises files.js against the real store but leaves
// chat.js entirely untested; this file closes that gap by loading the REAL
// chat.js alongside the real store and files.js in one vm context and driving
// openChat()/selectNode()/showChatList()/renderChatItem() through the same
// delegation paths the browser uses.
//
// Runnable as `node tests/frontend/test_shared_selection_wiring.mjs`.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(__dirname, '..', '..', 'static');

const targetsSource = readFileSync(path.join(staticDir, 'targets.js'), 'utf8');
const filesSource = readFileSync(path.join(staticDir, 'files.js'), 'utf8');
const chatSourceRaw = readFileSync(path.join(staticDir, 'chat.js'), 'utf8');

// chat.js self-invokes two bootstrap entry points at load time:
//   * init()               — the full page bootstrap (settings, radio health,
//                           telemetry, camera, message polling), and
//   * initializeWorkspace()— theme/panel/map-layout bootstrap.
// Both are page-lifetime concerns orthogonal to the selection wiring under
// test; the integration tests drive the real openChat/selectNode/showChatList/
// renderChatItem directly. Strip only those two trailing auto-invocations.
// Every other top-level statement (function/var declarations, the window.*
// exports, the DOMContentLoaded-guarded initializers, the installCompactNodeCard
// styles, the document-level event listeners) is preserved, so the functions
// under test are the REAL ones.
const chatSource = chatSourceRaw
    .replace(/^init\(\);\s*$/m, '// init() auto-run removed for the test harness')
    .replace(/^initializeWorkspace\(\);\s*$/m, '// initializeWorkspace() auto-run removed for the test harness');

// ---- minimal fake DOM -------------------------------------------------------

class FakeElement {
    constructor(tag, id) {
        this.tagName = String(tag || 'div').toUpperCase();
        this.id = id || '';
        this.innerHTML = '';
        this.textContent = '';
        this.disabled = false;
        this.className = '';
        this.style = {};
        this.value = '';
        this.files = [];
        this.checked = false;
        this._attrs = {};
        this._children = [];
        this.parentNode = null;
        this._listeners = {};
        this._classes = new Set();
        this.dataset = {};
        this.classList = {
            toggle: (name, force) => {
                const on = force === undefined ? !this._classes.has(name) : Boolean(force);
                if (on) this._classes.add(name); else this._classes.delete(name);
                return on;
            },
            add: (name) => { this._classes.add(name); },
            remove: (name) => { this._classes.delete(name); },
            contains: (name) => this._classes.has(name),
        };
    }
    setAttribute(name, value) {
        this._attrs[name] = String(value);
        if (name === 'id') this.id = String(value);
        if (name.startsWith('data-')) {
            const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
            this.dataset[key] = String(value);
        }
    }
    getAttribute(name) {
        if (Object.prototype.hasOwnProperty.call(this._attrs, name)) return this._attrs[name];
        if (name === 'id') return this.id || null;
        return null;
    }
    removeAttribute(name) {
        delete this._attrs[name];
        if (name === 'id') this.id = '';
    }
    appendChild(child) { child.parentNode = this; this._children.push(child); return child; }
    removeChild(child) {
        const i = this._children.indexOf(child);
        if (i >= 0) this._children.splice(i, 1);
        child.parentNode = null;
        return child;
    }
    addEventListener(type, fn) {
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(fn);
    }
    removeEventListener(type, fn) {
        const arr = this._listeners[type] || [];
        const i = arr.indexOf(fn);
        if (i >= 0) arr.splice(i, 1);
    }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    closest() { return null; }
    focus() { /* no-op */ }
    scrollIntoView() { /* no-op */ }
    remove() {
        if (this.parentNode) this.parentNode.removeChild(this);
    }
    replaceWith(node) {
        if (this.parentNode) {
            const i = this.parentNode._children.indexOf(this);
            if (i >= 0) this.parentNode._children.splice(i, 1, node);
            node.parentNode = this.parentNode;
        }
    }
    contains() { return false; }
    matches() { return false; }
    insertAdjacentHTML() { /* no-op */ }
    insertBefore(child) { this.appendChild(child); return child; }
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }; }
    classListFn() { return this.classList; }
}

class FakeDocument {
    constructor() {
        this.elements = new Map();
        this.listeners = {};
        this.body = new FakeElement('body', '');
        this.head = new FakeElement('head', '');
        this.activeElement = null;
        this.hidden = false;
        this.readyState = 'loading';   // defer chat.js's DOMContentLoaded initializers
        this._nodeCards = [];          // registered by tests for #nodesList .node-card
        this._chatItems = [];          // registered by tests for #channelList/#dmChatList .chat-item
        this._nodeClickHandlerInstalled = false;
    }
    getElementById(id) {
        if (!this.elements.has(id)) this.elements.set(id, new FakeElement('div', id));
        return this.elements.get(id);
    }
    createElement(tag) { return new FakeElement(tag); }
    addEventListener(type, fn) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(fn);
    }
    querySelector() { return null; }
    querySelectorAll(selector) {
        // syncSelectedNodeCard()/syncSelectedChatItems() are the paths the tests
        // observe at the DOM level; return the cards the test registered for the
        // exact selectors, and nothing elsewhere.
        if (selector === '#nodesList .node-card') return this._nodeCards.slice();
        if (selector === '#channelList .chat-item, #dmChatList .chat-item') return this._chatItems.slice();
        return [];
    }
}

// ---- sandbox ----------------------------------------------------------------

let counter = 0;

// chat.js's top-level `window.X = X` export block references these symbols as
// bare identifiers, but they are *defined in the split files*
// (chat-camera.js / chat-map.js / chat-photo.js / chat-telemetry.js), which
// this harness does not load because the selection-wiring under test never
// touches them. In the real page they are globals from those split scripts;
// seeding the same names as no-op globals lets chat.js's export block resolve
// them exactly the way it does in the browser. Every function chat.js defines
// itself is a top-level `function` declaration and therefore overrides any
// matching stub, so none of the real functions under test are affected.
const EXTERNAL_SPLIT_SYMBOLS = [
    'capturePhotoPreview', 'closeCustomTelemetryExport', 'closeTelemetryExportMenu',
    'closeTelemetryModal', 'downloadTelemetryExport', 'exportTelemetryData',
    'fitMeshMapToNodes', 'loadPhotoSettings', 'loadTelemetry',
    'openCustomTelemetryExport', 'openEmbeddedNodeMap', 'openNodeMap',
    'openTelemetryModal', 'refreshPhoto', 'refreshVideoFeed', 'renderMeshMap',
    'restoreCameraImageDefaults', 'runCustomTelemetryExport', 'savePhoto',
    'setCameraPower', 'setTelemetryRange', 'startCameraStream', 'stopCameraStream',
    'switchCameraMode', 'takeScreenshot', 'toggleCameraPower', 'toggleTelemetrySeries',
    'updateCameraControlLabels', 'updateCameraImageControls', 'updateCustomExportMode',
    'updatePhotoSettings', 'updateTelemetryConfig', 'updateVideoSettings',
];

function makeNodeCard(nodeId) {
    const card = new FakeElement('div', '');
    card.dataset = { nodeId };
    return card;
}

function makeChatItem(chatId, targetKind) {
    const item = new FakeElement('div', '');
    item.dataset = { chatId, targetKind };
    return item;
}

function json(body) {
    return { status: 200, json: async () => body, ok: true };
}

// A fetch implementation covering every endpoint the store + files.js + chat.js
// reach during activate()/openChat()/showChatList(), with empty/safe bodies.
function defaultRoutes(overrides = {}) {
    return async (url) => {
        if (Object.prototype.hasOwnProperty.call(overrides, url)) return overrides[url];
        // chat.js message/chat routes (no query-string sensitivity needed).
        if (url.startsWith('/api/messages')) return json({ messages: [], nodes: [] });
        if (url.startsWith('/api/chats')) return json({ chats: [], channels: [], total_unread: 0 });
        switch (url) {
            case '/api/nodes_management': return json({ nodes: [], total: 0 });
            case '/api/mca/contacts': return json({ ok: true, contacts: [] });
            case '/api/mca/key-requests': return json({ ok: true, key_requests: [] });
            case '/api/base_status': return json({ node_id: '!11111111', node_name: 'Me', profile_id: 'p1' });
            case '/api/mca/connectivity': return json({ ok: true, internet: 'online', relays: {} });
            case '/api/settings': return json({ ok: true, settings: { meshtastic: { transport: 'serial' } } });
            case '/api/mca/providers': return json({ ok: true, providers: [] });
        }
        if (url.startsWith('/api/attachments')) return json({ ok: true, attachments: [], total: 0 });
        throw new Error('unexpected fetch: ' + url);
    };
}

function buildSandbox({ fetchImpl, loadStore = true }) {
    const document = new FakeDocument();
    const fetchLog = [];
    const wrappedFetch = async (url, options) => {
        fetchLog.push({ url, options });
        return fetchImpl(url, options);
    };

    const i18n = {
        t(key) { return `[[${key}]]`; },
        plural(key, n) { return `[[${key}]]`; },
        applyStaticDom() { /* no-op */ },
        tOrFallback(key, params, fallback) { return fallback; },
    };

    const sandbox = {
        console,
        document,
        window: {
            I18N: i18n,
            setTimeout,
            clearTimeout,
            setInterval,
            clearInterval,
            location: { reload() {}, href: 'http://localhost' },
            open() {},
        },
        // In the browser i18n.js publishes `window.I18N`, which makes `I18N`
        // also resolvable as a bare global (window === globalThis). chat.js's
        // TimeFormatter reads the bare identifier (`I18N?.locale`), so mirror
        // that with a global alias.
        I18N: i18n,
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        navigator: { clipboard: { writeText: async () => {} } },
        crypto: { randomUUID: () => `test-${++counter}` },
        FormData: class {
            constructor() { this._parts = []; }
            append(key, value) { this._parts.push([key, value]); }
        },
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        Date,
        Promise,
        AbortController,
        requestAnimationFrame: () => {},   // no-op: never runs the scroll callbacks
        cancelAnimationFrame: () => {},
        fetch: wrappedFetch,
        showToast: () => {},
        showProgressNotification: () => `notif-${++counter}`,
        updateNotification: () => {},
        _fetchLog: fetchLog,
        _document: document,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    // Seed the split-file globals before chat.js loads so its top-level export
    // block can resolve them (see EXTERNAL_SPLIT_SYMBOLS above).
    for (const name of EXTERNAL_SPLIT_SYMBOLS) sandbox[name] = () => {};
    // Load order mirrors templates/index.html: targets.js -> files.js -> chat.js.
    // `loadStore:false` loads chat.js alone (no shared store) to exercise the
    // store-absent fallback path (final correction, Finding 1).
    if (loadStore) {
        vm.runInContext(targetsSource, sandbox, { filename: 'targets.js' });
        vm.runInContext(filesSource, sandbox, { filename: 'files.js' });
    }
    vm.runInContext(chatSource, sandbox, { filename: 'chat.js' });
    return sandbox;
}

function waitFor(pred, { timeout = 2500, interval = 10 } = {}) {
    const start = Date.now();
    return new Promise((resolve, reject) => {
        const tick = () => {
            if (pred()) return resolve(true);
            if (Date.now() - start > timeout) return reject(new Error('waitFor timed out'));
            setTimeout(tick, interval);
        };
        tick();
    });
}

// ---- tests -------------------------------------------------------------------

async function test_open_chat_node_pushes_selection_to_store() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');

    const sel = store.selected();
    assert.ok(sel, 'opening a DM must select something in the store');
    assert.equal(sel.kind, 'node', 'a DM chat is a node target');
    assert.equal(sel.id, '!aaaaaaaa', 'the node address is the selection identity');
    console.log('PASS: test_open_chat_node_pushes_selection_to_store');
}

async function test_open_chat_channel_pushes_selection_to_store() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    sandbox.window.openChat('channel', 'LongFast', 'channel');

    const sel = store.selected();
    assert.ok(sel, 'opening a channel must select something in the store');
    assert.equal(sel.kind, 'channel', 'a channel chat is a channel target (never a node)');
    assert.equal(sel.id, 'channel', 'the channel chat id is the selection identity');
    console.log('PASS: test_open_chat_channel_pushes_selection_to_store');
}

async function test_show_chat_list_clears_store_selection() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');
    assert.ok(store.selected(), 'selection present after openChat');

    sandbox.window.showChatList();
    assert.equal(store.selected(), null, 'leaving the chat list must clear the store selection');
    console.log('PASS: test_show_chat_list_clears_store_selection');
}

async function test_select_node_same_open_dm_resyncs_store() {
    // A selection made elsewhere (Files) moves the store to B; clicking the
    // already-open node A in chat.js must re-select A in the store.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');
    store.select('node', '!bbbbbbbb');   // simulate a Files selection elsewhere
    assert.equal(store.selected().id, '!bbbbbbbb');

    sandbox.window.selectNode('!aaaaaaaa', 'Alice', 'nodes');
    assert.equal(store.selected().id, '!aaaaaaaa', 're-selecting the open node must re-sync the store');
    console.log('PASS: test_select_node_same_open_dm_resyncs_store');
}

async function test_store_selection_resyncs_cards_without_touching_open_chat() {
    // The chat.js store subscription must re-sync the #nodesList card highlight
    // (selected class + aria-pressed) from a store selection made elsewhere.
    // This proves the store wins over the open-DM fallback: selecting B from
    // Files re-highlights B even though DM A remains the open conversation.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    const cardA = makeNodeCard('!aaaaaaaa');
    const cardB = makeNodeCard('!bbbbbbbb');
    sandbox._document._nodeCards = [cardA, cardB];

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');
    // Opening the DM drives the store, which drives the card highlight to A.
    assert.equal(cardA.classList.contains('selected'), true, 'opening DM A must highlight card A');
    assert.equal(cardA.getAttribute('aria-pressed'), 'true');

    store.select('node', '!bbbbbbbb');   // Files selects B

    assert.equal(cardB.classList.contains('selected'), true, 'card B must gain the selected class');
    assert.equal(cardB.getAttribute('aria-pressed'), 'true', 'card B must be aria-pressed=true');
    assert.equal(cardA.classList.contains('selected'), false, 'card A must lose the selected class');
    assert.equal(cardA.getAttribute('aria-pressed'), 'false', 'card A must be aria-pressed=false');

    // Clearing the selection (store present, selection null) means NOTHING is
    // selected — the open DM A stays the open conversation but no card
    // re-highlights. This is the final-correction behavior: no currentChatId
    // fallback reappears after the store clears.
    store.clearSelection();
    assert.equal(cardA.classList.contains('selected'), false, 'after clearing the store, no card may re-highlight (open DM A is not the selection)');
    assert.equal(cardA.getAttribute('aria-pressed'), 'false');
    assert.equal(cardB.classList.contains('selected'), false);

    console.log('PASS: test_store_selection_resyncs_cards_without_touching_open_chat');
}

async function test_render_chat_item_highlight_reads_store() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;
    const render = sandbox.renderChatItem;

    store.select('node', '!aaaaaaaa');
    const selectedNode = render({ id: '!aaaaaaaa', is_channel: false, name: 'Alice', type: 'dm' });
    assert.match(selectedNode, /selected/, 'the store-selected node chat item must carry the selected class');
    assert.match(selectedNode, /aria-pressed="true"/, 'the store-selected node chat item must be aria-pressed=true');

    const unselectedNode = render({ id: '!bbbbbbbb', is_channel: false, name: 'Bob', type: 'dm' });
    assert.match(unselectedNode, /aria-pressed="false"/, 'an unselected node chat item must be aria-pressed=false');
    assert.doesNotMatch(unselectedNode, /class="chat-item[^"]*\bselected\b/, 'an unselected node chat item must not carry selected');

    store.select('channel', 'channel');
    const selectedChannel = render({ id: 'channel', is_channel: true, name: 'LongFast', type: 'channel' });
    assert.match(selectedChannel, /selected/, 'the store-selected channel item must carry the selected class');
    assert.match(selectedChannel, /aria-pressed="true"/, 'the store-selected channel item must be aria-pressed=true');

    console.log('PASS: test_render_chat_item_highlight_reads_store');
}

async function test_chat_selection_propagates_to_files_counterparty() {
    // Cross-surface: opening a node chat in chat.js selects the node in the
    // store, and files.js's onStoreSelection turns that into its counterparty
    // filter — observable as a transfers fetch carrying ?counterparty=!aaaaaaaa.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    // Activate the Files workspace so its onStoreSelection subscription is live.
    sandbox.window.MeshCenterFiles.activate();
    await waitFor(() => sandbox._fetchLog.some((e) => e.url.startsWith('/api/attachments?')));

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');

    assert.equal(store.selected().kind, 'node');
    assert.equal(store.selected().id, '!aaaaaaaa');

    await waitFor(() => sandbox._fetchLog.some((e) =>
        e.url.startsWith('/api/attachments') && e.url.includes('counterparty=!aaaaaaaa')
    ), { timeout: 3000 });

    // A channel selection clears the counterparty (a channel is never a file
    // counterparty) rather than leaving the node filter in place.
    sandbox.window.openChat('channel', 'LongFast', 'channel');
    assert.equal(store.selected().kind, 'channel');
    await waitFor(() => sandbox._fetchLog.some((e) =>
        e.url.startsWith('/api/attachments') && !e.url.includes('counterparty=')
    ), { timeout: 3000 });

    console.log('PASS: test_chat_selection_propagates_to_files_counterparty');
}

// ---- PR #256 final correction (Finding 1) scenarios -------------------------

async function test_open_dm_selects_store_card_and_dm_item() {
    // (1) Opening DM A must select node A in the store AND highlight card A and
    // the DM chat item A (aria-pressed="true"), all synchronously.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    const cardA = makeNodeCard('!aaaaaaaa');
    const dmItemA = makeChatItem('!aaaaaaaa', 'node');
    sandbox._document._nodeCards = [cardA];
    sandbox._document._chatItems = [dmItemA];

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');

    assert.equal(store.selected().kind, 'node');
    assert.equal(store.selected().id, '!aaaaaaaa');
    assert.equal(cardA.classList.contains('selected'), true, 'card A must be highlighted');
    assert.equal(cardA.getAttribute('aria-pressed'), 'true');
    assert.equal(dmItemA.classList.contains('selected'), true, 'DM item A must be highlighted');
    assert.equal(dmItemA.getAttribute('aria-pressed'), 'true');
    console.log('PASS: test_open_dm_selects_store_card_and_dm_item');
}

async function test_files_toggle_off_clears_selection_without_fallback() {
    // (2) With DM A open, a Files toggle-off of A clears the store selection
    // (selection === null). Card A and DM item A must unselect, the open
    // conversation stays A, and no currentChatId fallback re-highlights A.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    const cardA = makeNodeCard('!aaaaaaaa');
    const dmItemA = makeChatItem('!aaaaaaaa', 'node');
    sandbox._document._nodeCards = [cardA];
    sandbox._document._chatItems = [dmItemA];

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');
    assert.equal(cardA.classList.contains('selected'), true, 'precondition: card A highlighted after open');
    assert.equal(sandbox._document.getElementById('chatTitle').textContent, '💬 Alice', 'precondition: open conversation is DM A');

    // Files toggles A off (the click-to-toggle on the already-selected node).
    store.toggleSelect('node', '!aaaaaaaa');
    assert.equal(store.selected(), null, 'toggling the selected node clears the store selection');

    // Nothing selected: card A + DM item A unselected, aria-pressed=false.
    assert.equal(cardA.classList.contains('selected'), false, 'card A must unselect');
    assert.equal(cardA.getAttribute('aria-pressed'), 'false');
    assert.equal(dmItemA.classList.contains('selected'), false, 'DM item A must unselect');
    assert.equal(dmItemA.getAttribute('aria-pressed'), 'false');

    // The open conversation is untouched (still DM A) — no fallback reappears.
    assert.equal(sandbox._document.getElementById('chatTitle').textContent, '💬 Alice', 'the open conversation must stay DM A');
    console.log('PASS: test_files_toggle_off_clears_selection_without_fallback');
}

async function test_files_selects_node_b_updates_cards_without_network_reload() {
    // (3) With DM A open, Files selects node B via the store: card B and DM item
    // B update immediately, A loses highlight, the conversation stays A, and no
    // extra loadChatList()/api/chats fetch is triggered.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    const cardA = makeNodeCard('!aaaaaaaa');
    const cardB = makeNodeCard('!bbbbbbbb');
    const dmItemA = makeChatItem('!aaaaaaaa', 'node');
    const dmItemB = makeChatItem('!bbbbbbbb', 'node');
    sandbox._document._nodeCards = [cardA, cardB];
    sandbox._document._chatItems = [dmItemA, dmItemB];

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');
    // Let openChat's own chat-list load settle so we can measure the delta.
    await waitFor(() => sandbox._fetchLog.some((e) => e.url.startsWith('/api/chats')));
    const chatsBefore = sandbox._fetchLog.filter((e) => e.url.startsWith('/api/chats')).length;

    store.select('node', '!bbbbbbbb');   // Files selects B

    assert.equal(cardB.classList.contains('selected'), true, 'card B must gain the selected class');
    assert.equal(cardB.getAttribute('aria-pressed'), 'true');
    assert.equal(dmItemB.classList.contains('selected'), true, 'DM item B must gain the selected class');
    assert.equal(dmItemB.getAttribute('aria-pressed'), 'true');
    assert.equal(cardA.classList.contains('selected'), false, 'card A must lose the selected class');
    assert.equal(dmItemA.classList.contains('selected'), false, 'DM item A must lose the selected class');

    // The open conversation stays A.
    assert.equal(sandbox._document.getElementById('chatTitle').textContent, '💬 Alice');

    // Give any (incorrect) async reload a chance to fire, then assert none did.
    await new Promise((r) => setTimeout(r, 100));
    const chatsAfter = sandbox._fetchLog.filter((e) => e.url.startsWith('/api/chats')).length;
    assert.equal(chatsAfter, chatsBefore, 'selecting B from Files must not reload /api/chats');
    console.log('PASS: test_files_selects_node_b_updates_cards_without_network_reload');
}

async function test_channel_selection_updates_item_and_never_file_recipient() {
    // (4) Selecting a channel via the store updates the channel chat item
    // immediately (via data-target-kind, not display-name inference), a channel
    // is never a file recipient, and the Files counterparty filter clears.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    const channelItem = makeChatItem('channel', 'channel');
    const dmItemA = makeChatItem('!aaaaaaaa', 'node');
    sandbox._document._chatItems = [channelItem, dmItemA];

    // Subscribe chat.js to the store (the harness strips chat.js's own init()
    // auto-run, which is where the real page subscribes).
    sandbox.ensureStoreSelectionSubscription();

    // Files activates (subscribes) first so the counterparty clear is observable.
    sandbox.window.MeshCenterFiles.activate();
    await waitFor(() => sandbox._fetchLog.some((e) => e.url.startsWith('/api/attachments?')));

    // A node first sets the counterparty filter...
    store.select('node', '!aaaaaaaa');
    await waitFor(() => sandbox._fetchLog.some((e) =>
        e.url.startsWith('/api/attachments') && e.url.includes('counterparty=!aaaaaaaa')
    ), { timeout: 3000 });

    // ...then a channel selection clears it and highlights the channel item.
    store.select('channel', 'channel');

    assert.equal(channelItem.classList.contains('selected'), true, 'the channel item must be highlighted');
    assert.equal(channelItem.getAttribute('aria-pressed'), 'true');
    assert.equal(dmItemA.classList.contains('selected'), false, 'the node item must not be highlighted');

    assert.equal(store.computeCapability({ kind: 'channel' }).can_send_file, false, 'a channel is never a file recipient');

    await waitFor(() => sandbox._fetchLog.some((e) =>
        e.url.startsWith('/api/attachments') && !e.url.includes('counterparty=')
    ), { timeout: 3000 });
    assert.equal(store.selected().kind, 'channel', 'the channel stays the store selection');
    console.log('PASS: test_channel_selection_updates_item_and_never_file_recipient');
}

async function test_chat_js_without_store_falls_back_to_current_chat() {
    // (5) Running chat.js WITHOUT the shared store (targets.js absent) must not
    // throw, and must fall back to currentChatId for the highlight.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes(), loadStore: false });

    assert.equal(typeof sandbox.window.MeshCenterTargets, 'undefined', 'precondition: no store present');

    const cardA = makeNodeCard('!aaaaaaaa');
    sandbox._document._nodeCards = [cardA];

    sandbox.window.openChat('!aaaaaaaa', 'Alice', 'dm');

    assert.equal(cardA.classList.contains('selected'), true, 'without the store, the open DM must highlight via currentChatId');
    assert.equal(cardA.getAttribute('aria-pressed'), 'true');

    const html = sandbox.renderChatItem({ id: '!aaaaaaaa', is_channel: false, name: 'Alice', type: 'dm' });
    assert.match(html, /selected/, 'renderChatItem must fall back to currentChatId without the store');
    console.log('PASS: test_chat_js_without_store_falls_back_to_current_chat');
}

async function test_keyboard_enter_space_activate_target_controls() {
    // (6) The role="button" node/channel/DM target controls must activate on
    // Enter and Space, prevent Space scroll, and not double-fire on auto-repeat
    // or on non-activation keys.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });

    let nodeClicks = 0;
    let chatClicks = 0;
    let prevented = 0;
    let stopped = 0;

    const nodeCard = { dataset: { nodeId: '!aaaaaaaa', targetKind: 'node' }, click() { nodeClicks++; } };
    const chatItem = { dataset: { chatId: '!aaaaaaaa', targetKind: 'node' }, click() { chatClicks++; } };

    function keyEvent(key, repeat = false, target = nodeCard) {
        return { key, repeat, target, preventDefault() { prevented++; }, stopPropagation() { stopped++; } };
    }

    sandbox.handleNodeCardKeydown(keyEvent('Enter'), nodeCard);
    assert.equal(nodeClicks, 1, 'Enter must activate the node card exactly once');
    assert.equal(prevented, 1, 'Enter must be preventDefault-ed');
    assert.equal(stopped, 1, 'Enter must be stopPropagation-ed');

    sandbox.handleNodeCardKeydown(keyEvent(' '), nodeCard);
    assert.equal(nodeClicks, 2, 'Space must activate the node card');
    assert.equal(prevented, 2, 'Space must be preventDefault-ed (no page scroll)');

    sandbox.handleNodeCardKeydown(keyEvent('Enter', true), nodeCard);
    assert.equal(nodeClicks, 2, 'an auto-repeated Enter must not double-fire');

    sandbox.handleNodeCardKeydown(keyEvent('Tab'), nodeCard);
    assert.equal(nodeClicks, 2, 'a non-activation key must not activate');

    sandbox.handleChatItemKeydown(keyEvent('Enter', false, chatItem), chatItem);
    assert.equal(chatClicks, 1, 'Enter must activate the chat item');
    console.log('PASS: test_keyboard_enter_space_activate_target_controls');
}

// ---- runner ------------------------------------------------------------------

async function main() {
    await test_open_chat_node_pushes_selection_to_store();
    await test_open_chat_channel_pushes_selection_to_store();
    await test_show_chat_list_clears_store_selection();
    await test_select_node_same_open_dm_resyncs_store();
    await test_store_selection_resyncs_cards_without_touching_open_chat();
    await test_render_chat_item_highlight_reads_store();
    await test_chat_selection_propagates_to_files_counterparty();
    await test_open_dm_selects_store_card_and_dm_item();
    await test_files_toggle_off_clears_selection_without_fallback();
    await test_files_selects_node_b_updates_cards_without_network_reload();
    await test_channel_selection_updates_item_and_never_file_recipient();
    await test_chat_js_without_store_falls_back_to_current_chat();
    await test_keyboard_enter_space_activate_target_controls();
    console.log('All shared-selection wiring tests passed (13 scenarios).');
}

main()
    .catch((error) => {
        console.error('FAIL:', error);
        process.exitCode = 1;
    })
    .finally(() => {
        // chat.js's openChat() starts a message-polling interval; exit once
        // assertions are done rather than hang on the pending timer.
        process.exit(process.exitCode || 0);
    });
