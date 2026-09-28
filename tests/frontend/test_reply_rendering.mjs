// tests/frontend/test_reply_rendering.mjs
//
// Reply Metadata Consistency (PR 2): buildReplyBlockHtml() must render one of
// three distinct states and never silently fall through to "no decoration"
// for a message that actually was a reply. Runs the REAL function from
// static/chat.js in a vm sandbox against a stub I18N. Dependency-free:
// `node tests/frontend/test_reply_rendering.mjs`.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat.js'), 'utf8');

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
    'chat.referenced_message': 'Referenced message',
    'chat.original_message_unavailable': 'Original message is not available in this chat',
    'chat.reply_unresolved': 'Reply to an earlier message',
    'nodes.unknown_node': 'Unknown node',
};

function build() {
    const sandbox = {
        console,
        window: { I18N: { t: (k) => (k in STRINGS ? STRINGS[k] : `[[${k}]]`) } },
        // The real escapeHtml() - close enough for these assertions, and
        // keeps the test honest about what actually reaches the DOM.
        escapeHtml: (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c])),
    };
    vm.createContext(sandbox);
    vm.runInContext(extractFunction('buildReplyBlockHtml'), sandbox);
    return sandbox;
}

const sb = build();

// --- state A: resolved reply --------------------------------------------

{
    const html = sb.buildReplyBlockHtml({
        reply_id: 555,
        reply_to: { id: 'abc123', sender: 'Flint Base', text: 'the original question' },
    });
    assert.match(html, /message-reply-quote(?!--unresolved)/, 'a resolved reply uses the clickable quote class');
    assert.match(html, /data-reply-message-id="abc123"/);
    assert.match(html, /Flint Base/);
    assert.match(html, /the original question/);
    assert.doesNotMatch(html, /555/, 'the raw packet/reply id is never shown to the user');
}

// --- state B: reply_id present, original not resolved --------------------

for (const msg of [
    { reply_id: 999999, reply_to: null },
    { reply_id: 999999 }, // reply_to key absent entirely (e.g. an old persisted record)
]) {
    const html = sb.buildReplyBlockHtml(msg);
    assert.match(html, /message-reply-quote--unresolved/, `unresolved state for ${JSON.stringify(msg)}`);
    assert.doesNotMatch(html, /data-reply-message-id/, 'not clickable - nothing to scroll to');
    assert.match(html, /Reply to an earlier message/);
    assert.doesNotMatch(html, /999999/, 'the raw reply id is never shown to the user');
}

// A reply_id of 0 is falsy but still a real id - must not be treated as "no reply".
{
    const html = sb.buildReplyBlockHtml({ reply_id: 0, reply_to: null });
    assert.match(html, /message-reply-quote--unresolved/, 'reply_id=0 still renders as an unresolved reply');
}

// --- state C: an ordinary message -----------------------------------------

for (const msg of [{}, { reply_id: null }, { reply_id: undefined }, { text: 'hi' }]) {
    const html = sb.buildReplyBlockHtml(msg);
    assert.equal(html, '', `no reply decoration for ${JSON.stringify(msg)}`);
}

console.log('test_reply_rendering: all assertions passed');
