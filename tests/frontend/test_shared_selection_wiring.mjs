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

// A faithful-enough HTML escaper for the fake DOM's textContent→innerHTML
// coupling (see FakeElement.textContent below). Matches the five entities the
// browser's innerHTML serialization emits for text content.
function escapeHtmlForTest(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

class FakeElement {
    constructor(tag, id) {
        this.tagName = String(tag || 'div').toUpperCase();
        this.id = id || '';
        this.innerHTML = '';
        this._textContent = '';
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
    // Mirror the browser: assigning `textContent` to an element also writes its
    // HTML-escaped form into `innerHTML`. chat.js's escapeHtml() (and the markup
    // it produces) relies on exactly this coupling; without it, escapeHtml()
    // returns '' for every value in the fake DOM.
    get textContent() { return this._textContent; }
    set textContent(v) {
        this._textContent = String(v);
        this.innerHTML = escapeHtmlForTest(this._textContent);
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
        this._channelCards = [];       // registered by tests for #channelsList .channel-card
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
        if (selector === '#channelsList .channel-card') return this._channelCards.slice();
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
    // PR 5 final correction (section 1): aria-pressed lives ONLY on the inner
    // .node-card-select <button>. Give the fake card that inner button so
    // syncSelectedNodeCard() has a target for its aria-pressed write; the outer
    // card itself must end up with NO aria-pressed attribute.
    const selectBtn = new FakeElement('button', '');
    selectBtn.classList.add('node-card-select');
    selectBtn.dataset = { targetId: nodeId };
    card.selectBtn = selectBtn;
    card.querySelector = (sel) => (sel === '.node-card-select' ? selectBtn : null);
    return card;
}

function makeChatItem(chatId, targetKind) {
    const item = new FakeElement('div', '');
    item.dataset = { chatId, targetKind };
    return item;
}

function makeChannelCard(channelId) {
    const card = new FakeElement('button', '');
    card.dataset = { channelId };
    return card;
}

// ---- minimal HTML fragment parser -------------------------------------------
// Section 12 asks for structure assertions against a real parser, not
// regex/string-equality (which a malformed nest would also pass). The repo is
// deliberately dependency-free (no jsdom), so this is a small standards-shaped
// tokenizer that builds a real FakeElement tree from the well-formed markup
// chat.js emits (double-quoted attributes, explicit close tags, no void
// elements). It respects nesting and sibling order, so a structural bug
// surfaces as a wrong tree here instead of a string that happens to match.
function parseHtmlFragment(html) {
    const root = new FakeElement('div', '__root__');
    const stack = [root];
    const tagRe = /<(\/)?([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>/g;
    const attrRe = /([a-zA-Z:-][a-zA-Z0-9:_-]*)(?:\s*=\s*"([^"]*)")?/g;
    let m;
    while ((m = tagRe.exec(html)) !== null) {
        const closing = m[1] === '/';
        const tagName = m[2].toLowerCase();
        const attrStr = m[3] || '';
        if (closing) {
            if (stack.length > 1 && stack[stack.length - 1].tagName.toLowerCase() === tagName) {
                stack.pop();
            }
            continue;
        }
        const el = new FakeElement(tagName, '');
        attrRe.lastIndex = 0;
        let am;
        while ((am = attrRe.exec(attrStr)) !== null) {
            const name = am[1];
            const val = am[2] === undefined ? '' : am[2];
            el.setAttribute(name, val);
            if (name === 'class') {
                el.className = val;
                val.split(/\s+/).filter(Boolean).forEach(c => el.classList.add(c));
            }
        }
        stack[stack.length - 1].appendChild(el);
        stack.push(el);
    }
    return root;
}

function hasClass(el, cls) {
    return Boolean(el && el.classList && el.classList.contains(cls));
}

function descendants(root, pred) {
    const out = [];
    (function walk(el) {
        if (el !== root && pred(el)) out.push(el);
        (el._children || []).forEach(walk);
    })(root);
    return out;
}

// The tag names that count as "interactive" for the no-nested-controls
// invariant on the selection button.
const INTERACTIVE_TAGS = new Set(['button', 'a', 'input', 'select', 'textarea', 'details']);

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
            case '/api/toggle_ignore': return json({ ok: true, ignored: true });
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
    assert.equal(cardA.selectBtn.getAttribute('aria-pressed'), 'true', 'the inner select button is aria-pressed=true');
    assert.equal(cardA.getAttribute('aria-pressed'), null, 'the outer card carries NO aria-pressed');

    store.select('node', '!bbbbbbbb');   // Files selects B

    assert.equal(cardB.classList.contains('selected'), true, 'card B must gain the selected class');
    assert.equal(cardB.selectBtn.getAttribute('aria-pressed'), 'true', 'card B select button must be aria-pressed=true');
    assert.equal(cardB.getAttribute('aria-pressed'), null, 'outer card B carries NO aria-pressed');
    assert.equal(cardA.classList.contains('selected'), false, 'card A must lose the selected class');
    assert.equal(cardA.selectBtn.getAttribute('aria-pressed'), 'false', 'card A select button must be aria-pressed=false');

    // Clearing the selection (store present, selection null) means NOTHING is
    // selected — the open DM A stays the open conversation but no card
    // re-highlights. This is the final-correction behavior: no currentChatId
    // fallback reappears after the store clears.
    store.clearSelection();
    assert.equal(cardA.classList.contains('selected'), false, 'after clearing the store, no card may re-highlight (open DM A is not the selection)');
    assert.equal(cardA.selectBtn.getAttribute('aria-pressed'), 'false');
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
    assert.equal(cardA.selectBtn.getAttribute('aria-pressed'), 'true', 'card A select button is aria-pressed=true');
    assert.equal(cardA.getAttribute('aria-pressed'), null, 'outer card A carries NO aria-pressed');
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
    assert.equal(cardA.selectBtn.getAttribute('aria-pressed'), 'false', 'card A select button is aria-pressed=false');
    assert.equal(cardA.getAttribute('aria-pressed'), null, 'outer card A carries NO aria-pressed');
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
    assert.equal(cardB.selectBtn.getAttribute('aria-pressed'), 'true', 'card B select button is aria-pressed=true');
    assert.equal(cardB.getAttribute('aria-pressed'), null, 'outer card B carries NO aria-pressed');
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
    assert.equal(cardA.selectBtn.getAttribute('aria-pressed'), 'true', 'card A select button is aria-pressed=true');
    assert.equal(cardA.getAttribute('aria-pressed'), null, 'outer card A carries NO aria-pressed');

    const html = sandbox.renderChatItem({ id: '!aaaaaaaa', is_channel: false, name: 'Alice', type: 'dm' });
    assert.match(html, /selected/, 'renderChatItem must fall back to currentChatId without the store');
    console.log('PASS: test_chat_js_without_store_falls_back_to_current_chat');
}

async function test_keyboard_enter_space_activate_target_controls() {
    // (6) The role="button" channel/DM target controls in the CENTRAL chat list
    // must activate on Enter and Space, prevent Space scroll, and not double-fire
    // on auto-repeat or on non-activation keys.
    //
    // PR 5: the sidebar node card's selectable area is now a REAL <button>
    // (.node-card-select) that the browser keyboard-activates natively, so it no
    // longer routes through handleNodeCardKeydown() (that helper was removed).
    // Only the central chat items (still <div role="button" tabindex="0">) need
    // the synthetic handler, which is what this test now exercises.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });

    let chatClicks = 0;
    let prevented = 0;
    let stopped = 0;

    const chatItem = { dataset: { chatId: '!aaaaaaaa', targetKind: 'node' }, click() { chatClicks++; } };

    function keyEvent(key, repeat = false, target = chatItem) {
        return { key, repeat, target, preventDefault() { prevented++; }, stopPropagation() { stopped++; } };
    }

    sandbox.handleChatItemKeydown(keyEvent('Enter', false, chatItem), chatItem);
    assert.equal(chatClicks, 1, 'Enter must activate the chat item exactly once');
    assert.equal(prevented, 1, 'Enter must be preventDefault-ed');
    assert.equal(stopped, 1, 'Enter must be stopPropagation-ed');

    sandbox.handleChatItemKeydown(keyEvent(' ', false, chatItem), chatItem);
    assert.equal(chatClicks, 2, 'Space must activate the chat item');
    assert.equal(prevented, 2, 'Space must be preventDefault-ed (no page scroll)');

    sandbox.handleChatItemKeydown(keyEvent('Enter', true, chatItem), chatItem);
    assert.equal(chatClicks, 2, 'an auto-repeated Enter must not double-fire');

    sandbox.handleChatItemKeydown(keyEvent('Tab', false, chatItem), chatItem);
    assert.equal(chatClicks, 2, 'a non-activation key must not activate');

    // The removed node-card handler must not be present on the vm context.
    assert.equal(typeof sandbox.handleNodeCardKeydown, 'undefined',
        'handleNodeCardKeydown must be removed now that node cards are native <button>s');
    console.log('PASS: test_keyboard_enter_space_activate_target_controls');
}

// ---- PR 5: desktop Files workspace redesign --------------------------------

async function test_channel_targets_render_into_sidebar() {
    // Requirement (item 2): the global right sidebar renders a Channels section
    // from the shared store's channelTargets(), alongside the Nodes list — there
    // is no files-local targets pane. renderChannelTargets() paints each channel
    // as a native <button class="channel-card"> carrying its canonical channel id.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes({
            '/api/chats': json({ chats: [], channels: [
                { id: 'channel', name: 'LongFast', index: 0 },
                { id: 'channel:1', name: 'Secondary', index: 1 },
            ], total_unread: 0 }),
        }),
    });
    const store = sandbox.window.MeshCenterTargets;

    await store.refresh();
    sandbox.renderChannelTargets();

    const html = sandbox._document.getElementById('channelsList').innerHTML;
    assert.match(html, /channel-card/, 'channels render as channel cards');
    assert.match(html, /data-channel-id="channel"/, 'the primary channel renders with its canonical id');
    assert.match(html, /data-channel-id="channel:1"/, 'a secondary channel renders with its indexed id');
    assert.match(html, /LongFast/, 'the channel display name renders');
    assert.doesNotMatch(html, /data-channel-id="node"/, 'channel cards never carry a node identity');
    console.log('PASS: test_channel_targets_render_into_sidebar');
}

async function test_channel_card_aria_pressed_syncs_with_store() {
    // Requirement (item 10): sidebar channel cards mirror the shared store's
    // selection exactly like node cards and chat items — selecting a channel via
    // the store toggles aria-pressed on the matching #channelsList .channel-card.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes({
            '/api/chats': json({ chats: [], channels: [{ id: 'channel', name: 'LongFast', index: 0 }], total_unread: 0 }),
        }),
    });
    const store = sandbox.window.MeshCenterTargets;
    await store.refresh();
    sandbox.renderChannelTargets();

    const card = makeChannelCard('channel');
    sandbox._document._channelCards = [card];

    sandbox.syncSelectedChannelCards();
    assert.equal(card.getAttribute('aria-pressed'), 'false', 'no selection means the channel card is unpressed');

    store.select('channel', 'channel');
    sandbox.syncSelectedChannelCards();
    assert.equal(card.classList.contains('selected'), true, 'the selected channel card gains the selected class');
    assert.equal(card.getAttribute('aria-pressed'), 'true', 'the selected channel card is aria-pressed=true');

    store.clearSelection();
    sandbox.syncSelectedChannelCards();
    assert.equal(card.getAttribute('aria-pressed'), 'false', 'clearing the selection unpressed the channel card');
    console.log('PASS: test_channel_card_aria_pressed_syncs_with_store');
}

async function test_node_card_key_actions_are_siblings_not_nested() {
    // Requirement (item 13): the MCA key actions render as SIBLING buttons of the
    // .node-card-select selection button — never nested inside it. The key-row
    // buttons carry data-files-action + data-contact (for files.js's delegated
    // handler) with type="button", and the row never wraps the selection button.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes({
            '/api/mca/contacts': json({ ok: true, contacts: [
                { contact_id: '!aaaaaaaa', status: 'key_unknown', fingerprint: 'fp', key_epoch: 1 },
            ] }),
        }),
    });
    const store = sandbox.window.MeshCenterTargets;
    await store.refresh();

    const node = store.getNode('!aaaaaaaa');
    assert.ok(node, 'precondition: the contact is a node target in the store');
    assert.equal(node.trust_state, 'unknown', 'precondition: unknown trust state');

    const keyRow = sandbox.renderNodeCardKeyActions('!aaaaaaaa');

    assert.match(keyRow, /node-card-key-row/, 'the key row renders');
    assert.match(keyRow, /data-files-action="contact-request-key"/, 'a request-key button renders for an unknown, requestable node');
    assert.match(keyRow, /data-contact="!aaaaaaaa"/, 'the key button carries the contact id for the delegated handler');
    assert.match(keyRow, /type="button"/, 'key buttons are type=button');
    assert.doesNotMatch(keyRow, /node-card-select/, 'the key row must not contain the selection button');

    // The local node and a ready (trusted) node have nothing actionable.
    assert.equal(sandbox.renderNodeCardKeyActions('!11111111'), '', 'the local node has no key actions');

    console.log('PASS: test_node_card_key_actions_are_siblings_not_nested');
}

// ---- PR 5 final correction: section 1/2/3/4/5 scenarios ----------------------

function makeTestNode(overrides = {}) {
    return Object.assign({
        node_id: '!aaaaaaaa',
        clean_name: 'Alice',
        name: 'Alice',
        long_name: 'Alice Long',
        short_name: 'AL',
        hw_model: 'TBEAM',
        last_text: 'hello world',
        ignored: true,
        favorite: true,
        hops_away: 2,
        age: '5 min',
        rssi: -60,
        snr: 8,
        position: { latitude: 52.52, longitude: 13.405 },
    }, overrides);
}

async function buildStoreSandbox({ contacts = [], key_requests = [] } = {}) {
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes({
            '/api/mca/contacts': json({ ok: true, contacts }),
            '/api/mca/key-requests': json({ ok: true, key_requests }),
        }),
    });
    await sandbox.window.MeshCenterTargets.refresh();
    return sandbox;
}

async function test_node_card_is_flat_accessible_container() {
    // Section 1: the outer .node-card is a plain visual container carrying
    // data-node-id + data-target-kind="node" and NO role/tabindex/aria-pressed;
    // the only aria-pressed carrier is the inner .node-card-select button, which
    // has no interactive descendants; the status row / last text / unignore /
    // map badge are SIBLINGS of that button (never nested inside it).
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes(), loadStore: false });
    const parsed = parseHtmlFragment(sandbox.renderNodeCard(makeTestNode()));
    const card = parsed._children.find(el => hasClass(el, 'node-card'));

    assert.ok(card, 'the rendered node card must have a .node-card root');
    assert.equal(card.getAttribute('data-node-id'), '!aaaaaaaa', 'outer card carries data-node-id');
    assert.equal(card.getAttribute('data-target-kind'), 'node', 'outer card carries data-target-kind="node"');
    assert.equal(card.getAttribute('aria-pressed'), null, 'outer card carries NO aria-pressed');
    assert.equal(card.getAttribute('role'), null, 'outer card carries NO role');
    assert.equal(card.getAttribute('tabindex'), null, 'outer card carries NO tabindex');

    const selects = descendants(card, el => hasClass(el, 'node-card-select'));
    assert.equal(selects.length, 1, 'exactly one .node-card-select button');
    const selectBtn = selects[0];
    assert.equal(selectBtn.parentNode, card, '.node-card-select is a direct child of the card');
    assert.equal(selectBtn.getAttribute('data-target-id'), '!aaaaaaaa', 'select button carries data-target-id');
    assert.equal(selectBtn.getAttribute('aria-pressed'), 'false', 'select button is the aria-pressed carrier (unselected)');

    const interactiveInside = descendants(selectBtn, el => INTERACTIVE_TAGS.has(el.tagName.toLowerCase()));
    assert.equal(interactiveInside.length, 0, '.node-card-select has no interactive descendants');

    const statusRow = descendants(card, el => hasClass(el, 'node-card-status-row'));
    assert.equal(statusRow.length, 1, 'one status row');
    assert.equal(statusRow[0].parentNode, card, 'status row is a sibling of the select button');

    const lastText = descendants(card, el => hasClass(el, 'node-last-text'));
    assert.equal(lastText.length, 1, 'one last-text row');
    assert.equal(lastText[0].parentNode, card, 'last text is a sibling of the select button');

    const unignore = descendants(card, el => el.getAttribute('data-action') === 'unignore');
    assert.equal(unignore.length, 1, 'an ignored node renders one unignore action');
    assert.equal(unignore[0].parentNode, card, 'unignore is a sibling of the select button');

    const mapBadge = descendants(card, el => hasClass(el, 'node-map-badge'));
    assert.equal(mapBadge.length, 1, 'one map badge');
    assert.equal(descendants(selectBtn, el => hasClass(el, 'node-map-badge')).length, 0,
        'the map badge is not nested in the selection button');

    // No store -> no MCA key row.
    assert.equal(descendants(card, el => hasClass(el, 'node-card-key-row')).length, 0,
        'no store means no key row');
    console.log('PASS: test_node_card_is_flat_accessible_container');
}

async function test_node_card_key_row_states() {
    // Section 2: every trust/request state renders the right key row — the ready
    // state shows the "files available" status, each actionable state renders the
    // right data-files-action button, and local + no-binding nodes render nothing.
    const ID = '!aaaaaaaa';
    const actionButtons = (row) => descendants(row, el => INTERACTIVE_TAGS.has(el.tagName.toLowerCase()));
    const actionsNamed = (row, name) => descendants(row, el => el.getAttribute('data-files-action') === name);

    let sandbox = await buildStoreSandbox({ contacts: [{ contact_id: ID, status: 'trusted', fingerprint: 'fp', key_epoch: 1 }] });
    let row = parseHtmlFragment(sandbox.renderNodeCardKeyActions(ID))._children[0];
    assert.ok(hasClass(row, 'node-card-key-row'), 'ready renders a key row');
    assert.ok(hasClass(row, 'node-card-key-ready'), 'ready is the "files available" row');
    assert.equal(actionButtons(row).length, 0, 'ready renders no action button');

    sandbox = await buildStoreSandbox({ contacts: [{ contact_id: ID, status: 'confirmation_required', fingerprint: 'fp', key_epoch: 1 }] });
    row = parseHtmlFragment(sandbox.renderNodeCardKeyActions(ID))._children[0];
    assert.equal(actionsNamed(row, 'contact-confirm').length, 1, 'confirmation_required renders a confirm button');

    sandbox = await buildStoreSandbox({ contacts: [{ contact_id: ID, status: 'key_changed', fingerprint: 'fp', key_epoch: 1 }] });
    row = parseHtmlFragment(sandbox.renderNodeCardKeyActions(ID))._children[0];
    assert.equal(actionsNamed(row, 'contact-accept').length, 1, 'changed renders an accept button');
    assert.equal(actionsNamed(row, 'contact-reject').length, 1, 'changed renders a reject button');

    sandbox = await buildStoreSandbox({ contacts: [{ contact_id: ID, status: 'key_unknown', fingerprint: '', key_epoch: null }] });
    row = parseHtmlFragment(sandbox.renderNodeCardKeyActions(ID))._children[0];
    assert.equal(actionsNamed(row, 'contact-request-key').length, 1, 'unknown+idle renders a request-key button');

    sandbox = await buildStoreSandbox({
        contacts: [{ contact_id: ID, status: 'key_unknown', fingerprint: '', key_epoch: null }],
        key_requests: [{ contact_id: ID, key_request_state: 'queued', can_request_key: false }],
    });
    row = parseHtmlFragment(sandbox.renderNodeCardKeyActions(ID))._children[0];
    assert.equal(actionButtons(row).length, 0, 'queued renders no button (request in flight)');

    sandbox = await buildStoreSandbox({
        contacts: [{ contact_id: ID, status: 'key_unknown', fingerprint: '', key_epoch: null }],
        key_requests: [{ contact_id: ID, key_request_state: 'waiting_response', can_request_key: false }],
    });
    row = parseHtmlFragment(sandbox.renderNodeCardKeyActions(ID))._children[0];
    assert.equal(actionButtons(row).length, 0, 'waiting_response renders no button');

    sandbox = await buildStoreSandbox({
        contacts: [{ contact_id: ID, status: 'key_unknown', fingerprint: '', key_epoch: null }],
        key_requests: [{ contact_id: ID, key_request_state: 'retry_available', can_request_key: true }],
    });
    row = parseHtmlFragment(sandbox.renderNodeCardKeyActions(ID))._children[0];
    assert.equal(actionsNamed(row, 'contact-request-key').length, 1, 'retry_available renders a request-key-again button');

    sandbox = await buildStoreSandbox({});
    assert.equal(sandbox.renderNodeCardKeyActions('!11111111'), '', 'the local node renders no key row');
    console.log('PASS: test_node_card_key_row_states');
}

async function test_sidebar_delegated_click_routes_and_excludes() {
    // Section 3: ONE guarded delegated handler reads data-target-id and routes
    // node/channel selection through store.toggleSelect; map/unignore/key actions
    // are excluded so they never toggle selection; one activation = one store
    // transition.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;

    const selectBtn = new FakeElement('button', '');
    selectBtn.classList.add('node-card-select');
    selectBtn.setAttribute('data-target-id', '!aaaaaaaa');
    sandbox.handleSidebarTargetClick({ target: selectBtn });
    assert.equal(store.selected().kind, 'node', 'select-button click selects the node');
    assert.equal(store.selected().id, '!aaaaaaaa', 'select-button click selects the right node id');

    sandbox.handleSidebarTargetClick({ target: selectBtn });
    assert.equal(store.selected(), null, 'a second click on the same target toggles it off');

    const channelBtn = new FakeElement('button', '');
    channelBtn.classList.add('channel-card');
    channelBtn.setAttribute('data-target-id', 'channel');
    sandbox.handleSidebarTargetClick({ target: channelBtn });
    assert.equal(store.selected().kind, 'channel', 'channel-card click selects the channel');
    assert.equal(store.selected().id, 'channel', 'channel-card click selects the right channel id');

    const before = store.selected();
    const unignoreBtn = new FakeElement('button', '');
    unignoreBtn.setAttribute('data-action', 'unignore');
    unignoreBtn.setAttribute('data-node-id', '!aaaaaaaa');
    sandbox.handleSidebarTargetClick({ target: unignoreBtn });
    assert.equal(store.selected(), before, 'unignore is a card action, never a selection');

    store.clearSelection();
    const mapBadge = new FakeElement('button', '');
    mapBadge.classList.add('node-map-badge');
    sandbox.handleSidebarTargetClick({ target: mapBadge });
    assert.equal(store.selected(), null, 'the map badge never toggles selection');

    const keyBtn = new FakeElement('button', '');
    keyBtn.classList.add('node-card-key-btn');
    keyBtn.setAttribute('data-files-action', 'contact-request-key');
    keyBtn.setAttribute('data-contact', '!aaaaaaaa');
    sandbox.handleSidebarTargetClick({ target: keyBtn });
    assert.equal(store.selected(), null, 'a key action never toggles selection');

    const sidebar = sandbox._document.getElementById('sidebar');
    sandbox.installSidebarTargetDelegation();
    sandbox.installSidebarTargetDelegation();
    assert.equal((sidebar._listeners.click || []).length, 1, 'the delegated listener is installed exactly once');
    console.log('PASS: test_sidebar_delegated_click_routes_and_excludes');
}

async function test_node_details_visual_clear_vs_selection_clear() {
    // Section 4 refactor: clearNodeDetailsPanel() is VISUAL-only (does not touch
    // the shared store); closeNodeDetails() clears BOTH the store selection and
    // the panel. Two distinct responsibilities, one seam.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;
    sandbox.ensureStoreSelectionSubscription();

    store.select('channel', 'channel');

    sandbox.clearNodeDetailsPanel();
    assert.equal(store.selected().kind, 'channel', 'visual clear must not clear the store selection');
    assert.equal(sandbox._document.getElementById('nodeDetails').className, 'node-details-placeholder');
    assert.equal(sandbox._document.getElementById('nodeDetails').innerHTML, '');

    sandbox.closeNodeDetails();
    assert.equal(store.selected(), null, 'the close button clears the shared selection too');
    assert.equal(sandbox._document.getElementById('nodeDetails').className, 'node-details-placeholder');
    console.log('PASS: test_node_details_visual_clear_vs_selection_clear');
}

async function test_node_details_panel_follows_shared_selection() {
    // Section 4: the node-details panel is driven by the shared store selection
    // through the subscription — a channel selection closes the panel (keeping
    // the channel selected), a node selection fetches details WITHOUT auto-
    // opening a DM, and a toggle-off clears both selection and panel.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;
    sandbox.ensureStoreSelectionSubscription();

    store.select('channel', 'channel');
    assert.equal(sandbox._document.getElementById('nodeDetails').className, 'node-details-placeholder',
        'a channel selection closes the node-details panel');
    assert.equal(store.selected().kind, 'channel', 'the channel stays selected');

    const messagesBefore = sandbox._fetchLog.filter(e => e.url.startsWith('/api/messages')).length;
    store.select('node', '!aaaaaaaa');
    assert.equal(sandbox._document.getElementById('chatTitle').textContent, '',
        'selecting a node in the store must NOT auto-open a DM');
    await waitFor(() => sandbox._fetchLog.filter(e => e.url.startsWith('/api/messages')).length > messagesBefore);

    store.toggleSelect('node', '!aaaaaaaa');
    assert.equal(store.selected(), null, 'toggle-off clears the selection');
    assert.equal(sandbox._document.getElementById('nodeDetails').className, 'node-details-placeholder',
        'clearing the selection closes the node-details panel');
    console.log('PASS: test_node_details_panel_follows_shared_selection');
}

async function test_refresh_sidebar_targets_age_limit_join_and_force() {
    // Section 5: refreshSidebarTargets is the single joinable controller — a
    // normal call is age-limited to <=1/60s, a forced call bypasses the age
    // limit, and concurrent calls join one in-flight refresh instead of issuing
    // their own.
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const store = sandbox.window.MeshCenterTargets;
    let refreshCalls = 0;
    const origRefresh = store.refresh;
    store.refresh = function () {
        refreshCalls++;
        return origRefresh.apply(this, arguments);
    };

    await sandbox.refreshSidebarTargets(false);
    assert.equal(refreshCalls, 1, 'the first normal call refreshes');

    await sandbox.refreshSidebarTargets(false);
    assert.equal(refreshCalls, 1, 'a second normal call within 60s is suppressed');

    await sandbox.refreshSidebarTargets(true);
    assert.equal(refreshCalls, 2, 'a forced call bypasses the age limit');

    const p1 = sandbox.refreshSidebarTargets(true);
    const p2 = sandbox.refreshSidebarTargets(true);
    await Promise.all([p1, p2]);
    assert.equal(refreshCalls, 3, 'concurrent forced calls join a single in-flight refresh');
    console.log('PASS: test_refresh_sidebar_targets_age_limit_join_and_force');
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
    await test_channel_targets_render_into_sidebar();
    await test_channel_card_aria_pressed_syncs_with_store();
    await test_node_card_key_actions_are_siblings_not_nested();
    await test_node_card_is_flat_accessible_container();
    await test_node_card_key_row_states();
    await test_sidebar_delegated_click_routes_and_excludes();
    await test_node_details_visual_clear_vs_selection_clear();
    await test_node_details_panel_follows_shared_selection();
    await test_refresh_sidebar_targets_age_limit_join_and_force();
    console.log('All shared-selection wiring tests passed (22 scenarios).');
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
