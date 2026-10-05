// tests/frontend/_chat_action_test_helpers.mjs
//
// Shared helpers for H2-D (F1.2) tests: each converted area follows the
// same shape (render with a hostile value -> assert no onclick= remains
// and the value round-trips through the attribute -> dispatch a fake click
// through the real onChatActionClick() -> assert the right target function
// got the exact original value). Without this, every area's test re-wrote
// the same ~100 lines of vm/fake-DOM plumbing test_notifications_delegation.mjs
// needed (see that file for the first, fully-worked example). Leading
// underscore to signal "helper module, not a test" to a human skimming the
// directory - `node tests/frontend/*.mjs` (the CI runner, see ci.yml) does
// still glob-match and run this file, but harmlessly (it only declares
// exports, asserts nothing itself, exits 0).

import assert from 'node:assert/strict';
import vm from 'node:vm';

// A payload set covering every shape F1.1's own test_escaping.mjs HOSTILE
// array was written for - attribute breakout, entity double-decode, script
// injection, newlines, ordinary non-ASCII. Reuse this instead of inventing
// a fresh one per area.
export const HOSTILE_VALUES = [
    `x" onmouseover="pwn()`,
    `a&#39;);pwn();//`,
    `a');pwn();//`,
    `</script><script>pwn()</script>`,
    `line1\nline2\r`,
    `Ölmühle 🛰 "Base" 'TAP2' & <co>`,
];

export function decodeAttr(value) {
    return value.replace(/&(#x[0-9a-f]+|#[0-9]+|amp|lt|gt|quot|apos);/gi, (m, e) => {
        const k = e.toLowerCase();
        if (k === 'amp') return '&';
        if (k === 'lt') return '<';
        if (k === 'gt') return '>';
        if (k === 'quot') return '"';
        if (k === 'apos') return "'";
        if (k.startsWith('#x')) return String.fromCodePoint(parseInt(k.slice(2), 16));
        return String.fromCodePoint(parseInt(k.slice(1), 10));
    });
}

/** Extracts a top-level `function name(...) { ... }` (or `async function`)
 * from `source` by brace-matching from the first `{` after the signature -
 * same technique test_reply_rendering.mjs/test_escaping.mjs already use. */
export function extractFunction(source, name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `function ${name} not found`);
    const open = source.indexOf('{', source.indexOf(')', match.index));
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unbalanced braces in function ${name}`);
}

/** Same idea, for a top-level `const NAME = { ... };` object literal
 * (CHAT_ACTIONS). Brace-matches from the first `{` after `=`. */
export function extractConst(source, name) {
    const match = new RegExp(`const\\s+${name}\\s*=\\s*`).exec(source);
    assert.ok(match, `const ${name} not found`);
    const open = source.indexOf('{', match.index + match[0].length - 1);
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unbalanced braces in const ${name}`);
}

/** Runs `extractFunction`/`extractConst` for each name in `names` (in that
 * order - a later entry may depend on an earlier one, e.g. onChatActionClick
 * depends on CHAT_ACTIONS) plus escapeHtml/escapeJsString, in one vm
 * context seeded with `extraSandbox`. Returns the sandbox object - pull out
 * whatever functions/consts the test needs from it by name. */
export function loadChatJsFragment(source, names, extraSandbox = {}) {
    const sandbox = { console, ...extraSandbox };
    vm.createContext(sandbox);
    const chunks = [extractFunction(source, 'escapeHtml'), extractFunction(source, 'escapeJsString')];
    for (const name of names) {
        chunks.push(/^[A-Z][A-Z0-9_]*$/.test(name) ? extractConst(source, name) : extractFunction(source, name));
    }
    vm.runInContext(chunks.join('\n'), sandbox);
    return sandbox;
}

/** Minimal fake element for dispatch tests: only closest()/getAttribute(),
 * matching what onChatActionClick() and its CHAT_ACTIONS handlers actually
 * call. `attrs` should include `_class` for a `.class-name` closest() match
 * (see FakeTarget.closest below - deliberately not a real selector engine). */
export class FakeTarget {
    constructor(attrs, parent = null) {
        this.attrs = attrs;
        this.parent = parent;
    }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    }
    closest(selector) {
        if (selector === '[data-chat-action]') {
            for (let node = this; node; node = node.parent) {
                if (node.attrs['data-chat-action'] !== undefined) return node;
            }
            return null;
        }
        if (selector.startsWith('.')) {
            const cls = selector.slice(1);
            for (let node = this; node; node = node.parent) {
                if (node.attrs._class === cls) return node;
            }
            return null;
        }
        return null;
    }
}

/** Asserts that `html` carries `data-chat-action="expectedAction"` with a
 * `data-id` (or `dataAttr`, for a differently-named field) that round-trips
 * to `expectedValue` through HTML-attribute decoding - the core "a hostile
 * value survives rendering unchanged, and the handler is wired" check every
 * converted area needs. */
export function assertRenderedAction(html, { action, dataAttr = 'data-id', expectedValue }) {
    assert.ok(html.includes(`data-chat-action="${action}"`), `expected data-chat-action="${action}" in: ${html}`);
    const re = new RegExp(`${dataAttr}="([^"]*)"`);
    const match = re.exec(html);
    assert.ok(match, `expected ${dataAttr}="..." in: ${html}`);
    assert.equal(decodeAttr(match[1]), expectedValue, `${dataAttr} must round-trip to the exact original value`);
}
