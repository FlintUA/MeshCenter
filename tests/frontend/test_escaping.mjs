// tests/frontend/test_escaping.mjs
//
// Audit review 2026-09-29, F1.1: escapeHtml() and escapeJsString() from
// static/chat.js must be safe for the contexts they are actually used in:
//   - escapeHtml: HTML text AND quoted attribute values (data-*="...", title="...")
//   - escapeJsString: a '...' JS string literal inside a double-quoted inline
//     handler attribute: onclick="fn('${escapeJsString(x)}')"
// Mesh node names and Wi-Fi SSIDs are attacker-controlled (any radio / any AP
// in range), so the hostile inputs below are realistic, not theoretical.
//
// Dependency-free, same as the other tests here: runs the REAL functions from
// static/chat.js in node:vm. The browser's attribute decoding is simulated by
// decodeAttr() below (it handles every entity form the helpers can emit, plus
// the numeric forms an attacker could type).

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `function ${name} not found in static/chat.js`);
    const open = source.indexOf('{', source.indexOf(')', match.index));
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unbalanced braces in ${name}`);
}

const ctx = vm.createContext({});
vm.runInContext(`${extractFunction('escapeHtml')}\n${extractFunction('escapeJsString')}`, ctx);
const { escapeHtml, escapeJsString } = ctx;

// What the HTML parser does to an attribute value before JS sees it.
function decodeAttr(value) {
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

const HOSTILE = [
    `x" onmouseover="pwn()`,          // attribute breakout via "
    `a&#39;);pwn();//`,               // entity decoded to ' before JS parses
    `a&apos;);pwn();//`,
    `a&#x27;);pwn();//`,
    `a');pwn();//`,                   // plain quote
    `a\\');pwn();//`,                 // backslash before quote
    `</script><script>pwn()</script>`,
    `line1\nline2\r`,
    `a b c`,
    `Ölmühle 🛰 "Base" 'TAP2' & <co>`, // ordinary non-ASCII / punctuation must round-trip
    ``,
];

// 1) escapeHtml output contains no raw metacharacters, and round-trips.
for (const input of HOSTILE) {
    const out = escapeHtml(input);
    assert.ok(!/[<>"']/.test(out), `escapeHtml left a raw metachar: ${JSON.stringify(out)}`);
    assert.ok(!/&(?!(amp|lt|gt|quot|#39);)/.test(out), `escapeHtml left a raw &: ${JSON.stringify(out)}`);
    assert.equal(decodeAttr(out), input, 'escapeHtml must round-trip through attribute decoding');
}
assert.equal(escapeHtml(null), '');
assert.equal(escapeHtml(undefined), '');
assert.equal(escapeHtml(0), '0');

// 2) escapeJsString inside onclick="..." : the handler must call the function
//    exactly once with the exact original string, and nothing else may run.
for (const input of HOSTILE) {
    const attr = `openChat('${escapeJsString(input)}', 'dm')`;
    assert.ok(!attr.includes('"'), `raw " would end the attribute: ${attr}`);
    const js = decodeAttr(attr);
    const calls = [];
    let pwned = false;
    const sandbox = vm.createContext({
        openChat: (...args) => calls.push(args),
        pwn: () => { pwned = true; },
    });
    vm.runInContext(js, sandbox);
    assert.equal(pwned, false, `injected code ran for ${JSON.stringify(input)}`);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], [input, 'dm'], `argument changed for ${JSON.stringify(input)}`);
}

// 3) The demo-channel handler shape from renderChatItem must be a valid
//    attribute value (it used JSON.stringify -> raw " -> broken attribute).
{
    const msg = `Channel "Test" isn't configured`;
    const attr = `showToast('${escapeJsString(msg)}', 'info')`;
    assert.ok(!attr.includes('"'));
    const shown = [];
    vm.runInContext(decodeAttr(attr), vm.createContext({ showToast: (m, t) => shown.push([m, t]) }));
    assert.deepEqual(shown, [[msg, 'info']]);
}

// 4) Static guard for the two concrete sinks fixed in F1.1.
assert.ok(!/\$\{net\.ssid\}/.test(source), 'raw ${net.ssid} interpolation is back in chat.js');
assert.ok(!/showToast\(\$\{JSON\.stringify\(/.test(source), 'JSON.stringify inside an inline handler is back');

console.log('test_escaping.mjs: ok');
