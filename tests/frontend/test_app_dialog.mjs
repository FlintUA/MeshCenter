// tests/frontend/test_app_dialog.mjs
//
// UI1: the shared app dialog (static/app-dialog.js) - confirm / prompt / alert resolve the
// right value for Confirm, Cancel, Esc, Enter and a backdrop click; a prompt keeps the
// dialog open (with the error under the field) until its fields validate; focus is
// restored; a second call is queued; the frontend-only resolveOpen() hook presses the
// real buttons. Runs the REAL source in a vm sandbox against a tiny fake DOM.
// `node tests/frontend/test_app_dialog.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'app-dialog.js'), 'utf8');

class FakeElement {
    constructor(tagName, doc) {
        this.tagName = tagName.toUpperCase();
        this.doc = doc;
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.listeners = {};
        this.className = '';
        this.textContent = '';
        this.value = '';
        this.hidden = false;
        this.id = '';
    }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    remove() {
        if (!this.parentNode) return;
        this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
        this.parentNode = null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    getAttribute(name) { return this.attributes[name]; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    click() { (this.listeners.click || []).forEach((fn) => fn({ target: this })); }
    focus() { this.doc.activeElement = this; }
    select() {}
    walk(visit) { visit(this); this.children.forEach((c) => c.walk(visit)); }
    find(pred) { let found = null; this.walk((n) => { if (!found && pred(n)) found = n; }); return found; }
    findAll(pred) { const all = []; this.walk((n) => { if (pred(n)) all.push(n); }); return all; }
}

function makeEnv() {
    const keyListeners = [];
    const doc = {
        activeElement: null,
        createElement: (tag) => new FakeElement(tag, doc),
        addEventListener: (type, fn) => { if (type === 'keydown') keyListeners.push(fn); },
        removeEventListener: (type, fn) => {
            const i = keyListeners.indexOf(fn);
            if (type === 'keydown' && i >= 0) keyListeners.splice(i, 1);
        },
    };
    doc.body = new FakeElement('body', doc);
    const outside = new FakeElement('button', doc);
    doc.body.appendChild(outside);
    outside.focus();
    const sandbox = {
        console,
        document: doc,
        I18N: { t: (key) => ({ 'dialog.cancel': 'Cancel', 'dialog.confirm': 'Confirm', 'dialog.ok': 'OK' }[key] || key) },
        Date, Math, Promise,
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    const press = (key, extra = {}) => {
        const event = { key, target: doc.activeElement, stopped: false, prevented: false,
            stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; }, ...extra };
        [...keyListeners].forEach((fn) => fn(event));
        return event;
    };
    const overlay = () => doc.body.children.find((c) => String(c.className).includes('mc-dialog-overlay'));
    const button = (action) => overlay().find((n) => n.attributes['data-dialog-action'] === action);
    return { sandbox, doc, outside, press, overlay, button, keyListeners };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// ---------------------------------------------------------------- confirm
{
    const env = makeEnv();
    const p = env.sandbox.appConfirm({ title: 'T', message: 'M\nline2', confirmLabel: 'Do it' });
    const box = env.overlay();
    assert.ok(box, 'dialog is attached to <body>');
    const dialog = box.children[0];
    assert.equal(dialog.attributes['aria-modal'], 'true');
    assert.equal(dialog.attributes.role, 'dialog');
    assert.equal(dialog.find((n) => n.tagName === 'H3').textContent, 'T');
    assert.equal(dialog.find((n) => String(n.className).includes('mc-dialog-message')).textContent, 'M\nline2');
    assert.equal(env.button('confirm').textContent, 'Do it');
    assert.equal(env.button('cancel').textContent, 'Cancel');
    assert.equal(env.doc.activeElement, env.button('confirm'), 'non-danger starts on Confirm');
    assert.ok(env.sandbox.MCDialog.isOpen());
    env.button('confirm').click();
    assert.equal(await p, true);
    assert.equal(env.overlay(), undefined, 'removed after closing');
    assert.equal(env.keyListeners.length, 0, 'key listener removed');
    assert.equal(env.doc.activeElement, env.outside, 'focus restored');
    assert.equal(env.sandbox.MCDialog.isOpen(), false);
}

// cancel button, Esc, backdrop => false, never true
for (const how of ['cancel', 'esc', 'backdrop']) {
    const env = makeEnv();
    const p = env.sandbox.appConfirm({ title: 'T', message: 'M' });
    if (how === 'cancel') env.button('cancel').click();
    if (how === 'esc') {
        const event = env.press('Escape');
        assert.ok(event.stopped, 'Esc does not leak to handlers behind the dialog');
    }
    if (how === 'backdrop') {
        const overlay = env.overlay();
        overlay.listeners.click.forEach((fn) => fn({ target: overlay }));
    }
    assert.equal(await p, false, how);
}

// a click INSIDE the dialog does not count as a backdrop click
{
    const env = makeEnv();
    const p = env.sandbox.appConfirm({ title: 'T', message: 'M' });
    const overlay = env.overlay();
    overlay.listeners.click.forEach((fn) => fn({ target: overlay.children[0] }));
    assert.ok(env.sandbox.MCDialog.isOpen());
    env.sandbox.MCDialog.resolveOpen(false);
    assert.equal(await p, false);
}

// Enter confirms when focus is not on a button; on a focused button the button's own click runs
{
    const env = makeEnv();
    const p = env.sandbox.appConfirm({ title: 'T', message: 'M' });
    env.doc.activeElement = env.doc.body;
    const event = env.press('Enter');
    assert.ok(event.prevented);
    assert.equal(await p, true);

    const env2 = makeEnv();
    const p2 = env2.sandbox.appConfirm({ title: 'T', message: 'M' });
    env2.button('cancel').focus();
    const event2 = env2.press('Enter');
    assert.equal(event2.prevented, false, 'Enter on a focused Cancel is left to the button');
    assert.ok(env2.sandbox.MCDialog.isOpen());
    env2.button('cancel').click();
    assert.equal(await p2, false);
}

// danger: red confirm, Cancel focused first (a stray Enter can never do the destructive thing)
{
    const env = makeEnv();
    const p = env.sandbox.appConfirm({ title: 'Delete', message: 'Sure?', danger: true });
    assert.ok(String(env.overlay().children[0].className).includes('is-danger'));
    assert.ok(String(env.button('confirm').className).includes('is-danger'));
    assert.equal(env.doc.activeElement, env.button('cancel'));
    env.press('Enter'); // focus is on Cancel (a button) -> not intercepted
    assert.ok(env.sandbox.MCDialog.isOpen());
    env.button('cancel').click();
    assert.equal(await p, false);
}

// focus trap: Tab on the last control wraps to the first, Shift+Tab on the first wraps to the last
{
    const env = makeEnv();
    const p = env.sandbox.appConfirm({ title: 'T', message: 'M' });
    env.button('confirm').focus();
    let event = env.press('Tab');
    assert.ok(event.prevented);
    assert.equal(env.doc.activeElement, env.button('cancel'));
    event = env.press('Tab', { shiftKey: true });
    assert.ok(event.prevented);
    assert.equal(env.doc.activeElement, env.button('confirm'));
    env.sandbox.MCDialog.resolveOpen(true);
    assert.equal(await p, true);
}

// queueing: the second dialog opens only after the first closed, in order
{
    const env = makeEnv();
    const first = env.sandbox.appConfirm({ title: 'one', message: 'm' });
    const second = env.sandbox.appConfirm({ title: 'two', message: 'm' });
    assert.equal(env.doc.body.children.filter((c) => String(c.className).includes('mc-dialog-overlay')).length, 1);
    env.sandbox.MCDialog.resolveOpen(true);
    assert.equal(await first, true);
    await tick();
    assert.equal(env.overlay().find((n) => n.tagName === 'H3').textContent, 'two');
    env.sandbox.MCDialog.resolveOpen(false);
    assert.equal(await second, false);
}

// ---------------------------------------------------------------- alert
{
    const env = makeEnv();
    const p = env.sandbox.appAlert({ title: 'T', message: 'Heads up' });
    assert.equal(env.overlay().children[0].attributes.role, 'alertdialog');
    assert.equal(env.button('cancel'), null, 'an alert has a single OK button');
    assert.equal(env.button('confirm').textContent, 'OK');
    env.press('Escape');
    assert.equal(await p, undefined);
}

// ---------------------------------------------------------------- prompt (host + port)
function portValidate(value) {
    const text = String(value).trim();
    const port = Number(text);
    return /^\d+$/.test(text) && port >= 1 && port <= 65535 ? '' : 'bad port';
}
const hostPortFields = () => [
    { name: 'host', label: 'Host', value: '192.168.2.50', validate: (v) => (String(v).trim() ? '' : 'host required') },
    { name: 'port', label: 'Port', value: '4403', validate: portValidate },
];

{
    const env = makeEnv();
    const p = env.sandbox.appPrompt({ title: 'Edit', fields: hostPortFields(), confirmLabel: 'Save and connect' });
    const inputs = env.overlay().findAll((n) => n.tagName === 'INPUT');
    assert.equal(inputs.length, 2);
    assert.equal(inputs[0].value, '192.168.2.50', 'prefilled');
    assert.equal(inputs[1].value, '4403', 'prefilled');
    assert.equal(env.doc.activeElement, inputs[0], 'first field focused');

    // invalid: empty host and a bad port -> the dialog stays open, errors sit under the fields
    inputs[0].value = '   ';
    inputs[1].value = '70000';
    env.button('confirm').click();
    assert.ok(env.sandbox.MCDialog.isOpen(), 'invalid input keeps the dialog open');
    const errors = env.overlay().findAll((n) => String(n.className).includes('mc-dialog-error'));
    assert.equal(errors[0].hidden, false);
    assert.equal(errors[0].textContent, 'host required');
    assert.equal(errors[1].textContent, 'bad port');
    assert.equal(inputs[1].attributes['aria-invalid'], 'true');
    assert.equal(env.doc.activeElement, inputs[0], 'focus goes to the first invalid field');

    // typing clears that field's error
    inputs[0].listeners.input.forEach((fn) => fn({ target: inputs[0] }));
    assert.equal(errors[0].hidden, true);

    // 0, 65536, "44x03", "" are all refused; 1 and 65535 are fine
    for (const bad of ['0', '65536', '44x03', '', ' ']) {
        inputs[0].value = 'radio.local';
        inputs[1].value = bad;
        env.button('confirm').click();
        assert.ok(env.sandbox.MCDialog.isOpen(), `port "${bad}" refused`);
    }
    inputs[1].value = '65535';
    // Enter in a text field submits
    env.doc.activeElement = inputs[1];
    env.press('Enter');
    assert.deepEqual({ ...(await p) }, { host: 'radio.local', port: '65535' });
    assert.equal(env.sandbox.MCDialog.isOpen(), false);
}

// prompt cancel => null; nothing validated
{
    const env = makeEnv();
    const p = env.sandbox.appPrompt({ title: 'Edit', fields: hostPortFields() });
    env.press('Escape');
    assert.equal(await p, null);
}

// resolveOpen(true, values) fills the fields and still runs validation
{
    const env = makeEnv();
    const p = env.sandbox.appPrompt({ title: 'Edit', fields: hostPortFields() });
    env.sandbox.MCDialog.resolveOpen(true, { port: '99999' });
    assert.ok(env.sandbox.MCDialog.isOpen(), 'the hook cannot bypass validation');
    env.sandbox.MCDialog.resolveOpen(true, { host: 'h', port: '4403' });
    assert.deepEqual({ ...(await p) }, { host: 'h', port: '4403' });
    assert.equal(env.sandbox.MCDialog.resolveOpen(true), false, 'nothing open -> false');
}

// texts are set as textContent, never parsed as markup
{
    const env = makeEnv();
    const p = env.sandbox.appConfirm({ title: '<b>x</b>', message: 'Forget "<img src=x onerror=1>"?' });
    const message = env.overlay().find((n) => String(n.className).includes('mc-dialog-message'));
    assert.equal(message.textContent, 'Forget "<img src=x onerror=1>"?');
    assert.equal(message.children.length, 0);
    env.sandbox.MCDialog.resolveOpen(false);
    await p;
}

console.log('OK - app dialog (confirm/prompt/alert, Esc/Enter/backdrop, validation, queue, focus)');
