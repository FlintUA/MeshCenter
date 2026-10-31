// tests/frontend/test_chat_unread_render.mjs
//
// U1: the REAL renderMessages() from static/chat.js, driven with a fake
// scroll container, for the "first unread" behaviour: divider position and
// text, scroll target on the first render, divider persistence across later
// renders (polls), pinned-to-bottom only when already at the bottom, the
// jump-to-latest button, and the N=0 / N>loaded edge cases.
//
//   node tests/frontend/test_chat_unread_render.mjs

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(__dirname, '..', '..', 'static');
const source = readFileSync(path.join(staticDir, 'chat.js'), 'utf8');
const viewsSource = readFileSync(path.join(staticDir, 'chat-views.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `function ${name} not found in static/chat.js`);
    const open = source.indexOf('{', source.indexOf(')', match.index));
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unbalanced braces in ${name}`);
}

const STRINGS = {
    'chat.unread_divider': 'Unread messages ({count})',
    'chat.jump_to_latest': 'Jump to latest message',
    'chat.date_yesterday': 'yesterday',
    'nodes.unknown_node': 'Unknown node',
    'chat.mca_file_received': 'File received',
    'chat.mca_file_sent': 'File sent',
    'chat.mca_open_in_files': 'Open in Files',
};

// Layout model: every rendered .message is 40px tall, the divider 20px; the
// viewport is 200px. Enough for the scroll maths without a real DOM.
function makeContainer() {
    const c = {
        _html: '',
        _top: 0,
        // Like a real scroller: the position is clamped to the scrollable range.
        get scrollTop() { return this._top; },
        set scrollTop(v) { this._top = Math.max(0, Math.min(v, Math.max(0, this.scrollHeight - this.clientHeight))); },
        clientHeight: 200,
        dataset: {},
        scrollToCalls: [],
        get scrollHeight() {
            const messages = (this._html.match(/class="message /g) || []).length;
            const divider = this._html.includes('id="unreadDivider"') ? 20 : 0;
            return messages * 40 + divider;
        },
        set innerHTML(v) { this._html = v; this._top = 0; },
        get innerHTML() { return this._html; },
        addEventListener() {},
        scrollTo(o) { this.scrollToCalls.push(o); },
        getBoundingClientRect() { return { top: 0 }; },
        querySelector(sel) {
            if (sel === '#unreadDivider') {
                if (!this._html.includes('id="unreadDivider"')) return null;
                const before = this._html.split('id="unreadDivider"')[0];
                const offset = (before.match(/class="message /g) || []).length * 40;
                const self = this;
                return { getBoundingClientRect: () => ({ top: offset - self.scrollTop }) };
            }
            if (sel === '.messages-jump-bottom') {
                if (!this._html.includes('messages-jump-bottom')) return null;
                const self = this;
                return {
                    classList: {
                        toggle(cls, on) { self.jumpVisible = Boolean(on); },
                    },
                };
            }
            return null;
        },
    };
    return c;
}

function build() {
    const sandbox = {
        console: { log() {}, error: console.error },
        setTimeout: (fn) => { sandbox._timers.push(fn); return 0; },
        _timers: [],
        Date, Intl, Math, Number, String, Map, Array, Object,
        window: { I18N: { locale: 'en', t: (k, p) => (STRINGS[k] || k).replace('{count}', p && p.count) } },
        lastRenderedSignature: {},
        renderedMessagesById: new Map(),
        currentChatName: 'Test', currentChatType: 'dm',
        activeLocalProfileId: 'p', activeLocalNodeId: '!local',
        normalizeMessageIdentity: (v) => String(v || ''),
        messageBelongsToActiveRadio: (m) => m.kind === 'me',
        buildReplyBlockHtml: () => '',
        initializeMessageActions: () => {},
        formatChannelIndexLabel: (n) => n,
        channelIndexFromChatId: () => 0,
        escapeHtml: (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c])),
        chatViewState: { chatId: null, unreadCount: 0, anchorId: null, pendingPlacement: false, forceBottomOnce: false },
    };
    vm.createContext(sandbox);
    vm.runInContext(viewsSource, sandbox);
    for (const fn of ['formatChatTimestamp', 'isMessagesScrolledToBottom', 'updateMessagesJumpButton',
        'bindMessagesScrollWatcher', 'renderMessages']) {
        vm.runInContext(extractFunction(fn), sandbox);
    }
    vm.runInContext('const MESSAGES_BOTTOM_THRESHOLD_PX = 48;', sandbox);
    return sandbox;
}

const msgs = (n) => Array.from({ length: n }, (_, i) => ({
    id: `m${i}`, kind: 'rx', sender: `S${i}`, text: `t${i}`, time: '10:00:00', ts: 1_800_000_000 + i,
}));
const flush = (sb) => { const t = sb._timers.splice(0); t.forEach((fn) => fn()); };
function open(sb, chatId, unread) {
    sb.chatViewState = { chatId, unreadCount: unread, anchorId: null, pendingPlacement: true, forceBottomOnce: false };
    sb.lastRenderedSignature[chatId] = null;
}
const dividerBefore = (html) => {
    const idx = html.indexOf('id="unreadDivider"');
    const rest = html.slice(idx);
    return /data-message-id="([^"]+)"/.exec(rest)[1];
};

let passed = 0;
function test(name, fn) { fn(); passed += 1; }

test('N>0: divider sits before the N-th message from the end, view scrolls to it near the top', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 10);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    assert.match(c.innerHTML, /Unread messages \(10\)/);
    assert.equal(dividerBefore(c.innerHTML), 'm20');
    // divider offset = 20 messages * 40 = 800, minus the 12px top margin
    assert.equal(c.scrollTop, 788);
});

test('N=0: no divider, lands at the bottom', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 0);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    assert.ok(!c.innerHTML.includes('unreadDivider'));
    assert.equal(c.scrollTop, c.scrollHeight - c.clientHeight);
});

test('N larger than the loaded messages: divider before the oldest loaded one', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 99);
    sb.renderMessages(c, msgs(4), '!a'); flush(sb);
    assert.equal(dividerBefore(c.innerHTML), 'm0');
    assert.match(c.innerHTML, /Unread messages \(99\)/);
});

test('the divider stays at the same message while new messages arrive', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 2);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    assert.equal(dividerBefore(c.innerHTML), 'm28');
    sb.renderMessages(c, msgs(32), '!a'); flush(sb);
    assert.equal(dividerBefore(c.innerHTML), 'm28');
    assert.equal((c.innerHTML.match(/id="unreadDivider"/g) || []).length, 1);
});

test('a new incoming message does not yank the view when the user is reading history', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 10);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    const before = c.scrollTop;
    assert.ok(before + c.clientHeight < c.scrollHeight - 48, 'precondition: not at the bottom');
    sb.renderMessages(c, msgs(31), '!a'); flush(sb);
    assert.equal(c.scrollTop, before);
});

test('a new incoming message follows to the bottom only when already at the bottom', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 0);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    assert.equal(c.scrollTop, c.scrollHeight - c.clientHeight);
    c.scrollTop = c.scrollHeight - 200; // exactly viewport-bottom (clientHeight 200)
    sb.renderMessages(c, msgs(31), '!a'); flush(sb);
    assert.equal(c.scrollTop, c.scrollHeight - c.clientHeight);
});

test('sending a message always jumps to the bottom, even from history', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 10);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    sb.chatViewState.forceBottomOnce = true;
    sb.renderMessages(c, msgs(31), '!a'); flush(sb);
    assert.equal(c.scrollTop, c.scrollHeight - c.clientHeight);
    assert.equal(sb.chatViewState.forceBottomOnce, false);
});

test('jump-to-latest button is visible only away from the bottom', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 10);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    assert.equal(c.jumpVisible, true);
    open(sb, '!b', 0);
    sb.renderMessages(c, msgs(30), '!b'); flush(sb);
    assert.equal(c.jumpVisible, false);
    assert.match(c.innerHTML, /data-chat-action="messages-jump-bottom"/);
    assert.doesNotMatch(c.innerHTML, /onclick=/);
});

test('switching chats drops the divider (state belongs to the open chat only)', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 10);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    open(sb, '!b', 0);
    sb.renderMessages(c, msgs(30), '!b'); flush(sb);
    assert.ok(!c.innerHTML.includes('unreadDivider'));
});

test('a deleted anchor message removes the divider instead of misplacing it', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 2);
    sb.renderMessages(c, msgs(30), '!a'); flush(sb);
    sb.renderMessages(c, msgs(30).filter((m) => m.id !== 'm28'), '!a'); flush(sb);
    assert.ok(!c.innerHTML.includes('unreadDivider'));
});

test('bubbles use ts when present and the legacy time otherwise', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 0);
    const list = msgs(2);
    delete list[0].ts;
    list[0].time = '09:41:07';
    sb.renderMessages(c, list, '!a'); flush(sb);
    assert.match(c.innerHTML, /<div class="time">09:41:07</);
    assert.doesNotMatch(c.innerHTML, /<div class="time">09:41:07<\/div>[\s\S]*<div class="time">10:00:00/);
});

test('message bubbles carry the delegated focus-sender action, never an inline handler', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 0);
    const list = msgs(2); list.push({ id: 'sys', kind: 'system', sender: 'SYSTEM', text: 'x', time: '10:00:00' });
    sb.renderMessages(c, list, '!a'); flush(sb);
    assert.equal((c.innerHTML.match(/class="bubble" data-chat-action="message-focus-sender"/g) || []).length, 2);
    assert.doesNotMatch(c.innerHTML, /onclick=/);
});

test('MCA-UX1: control frames are hidden, an offer and our own transfer are one compact bubble each', () => {
    const sb = build(); const c = makeContainer();
    open(sb, '!a', 0);
    const list = [
        { id: 'hello', kind: 'rx', sender: 'Peer', text: 'hello', time: '10:00:00', ts: 1_800_000_000 },
        { id: 'ack', kind: 'rx', sender: 'Peer', text: 'MCA1:AAAA', mca: { type: 'CONTROL' }, time: '10:00:01', ts: 1_800_000_001 },
        { id: 'offer', kind: 'rx', sender: 'Peer', text: 'MCA1:BBBB', mca: { type: 'OFFER' }, time: '10:00:02', ts: 1_800_000_002 },
        { id: 'sent', kind: 'me', sender: 'Me', text: 'x', mca: { type: 'FILE_SENT', attachment_id: 'att1' }, time: '10:00:03', ts: 1_800_000_003 },
    ];
    sb.renderMessages(c, list, '!a'); flush(sb);
    assert.ok(!c.innerHTML.includes('MCA1:'), 'no raw frame text anywhere');
    assert.ok(!c.innerHTML.includes('data-message-id="ack"'), 'the control frame has no bubble');
    assert.equal((c.innerHTML.match(/data-chat-action="mca-open"/g) || []).length, 2);
    assert.match(c.innerHTML, /File received[\s\S]*Open in Files/);
    assert.match(c.innerHTML, /<div class="message me" data-message-id="sent">[\s\S]*File sent/);
});

test('MCA-UX1: a bubble with the real attachment id opens exactly that transfer; old ones fall back to the nearest match', async () => {
    const calls = { fetch: [], tab: [], selected: [] };
    const sb = {
        console: { log() {}, error: console.error },
        encodeURIComponent,
        fetch: async (url) => {
            calls.fetch.push(url);
            return { ok: true, json: async () => ({ attachments: [
                { id: 'far', direction: 'received', counterparty_contact_id: '!aa', created_at: 100 },
                { id: 'near', direction: 'received', counterparty_contact_id: '!aa', created_at: 5000 },
            ] }) };
        },
        switchMainTab: (tab) => calls.tab.push(tab),
        window: { MeshCenterFiles: { selectTransfer: (id) => calls.selected.push(id) } },
    };
    vm.createContext(sb);
    vm.runInContext(viewsSource, sb);
    vm.runInContext(extractFunction('openTransferFromChatMessage'), sb);

    await sb.openTransferFromChatMessage({ chat_id: '!aa', ts: 5001, mca: { type: 'OFFER', transfer_id: 't', attachment_id: 'exact-id' } });
    assert.equal(calls.fetch.length, 0, 'the real id needs no lookup');
    assert.deepEqual(calls.selected, ['exact-id']);
    assert.deepEqual(calls.tab, ['files']);

    await sb.openTransferFromChatMessage({ chat_id: '!aa', ts: 5001, mca: { type: 'OFFER' } });
    assert.equal(calls.fetch.length, 1, 'an old message without the id uses the nearest-match fallback');
    assert.equal(calls.selected[1], 'near');
});

console.log(`test_chat_unread_render.mjs: ${passed} tests passed`);
