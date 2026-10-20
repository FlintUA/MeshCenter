// tests/frontend/test_updates_rollback_ui.mjs
//
// UPD1: the Updates card shows "Last update: from -> to", a one-click Roll back (explicit,
// danger dialog naming both versions, only to the recorded previous version) with the exact
// manual command always visible, says plainly why an update/rollback was refused (including
// "on branch X - update manually"), and shows the blocked-by-dependencies panel for a rollback.
// Runs the REAL functions from static/chat-updates-security.js against the REAL en.json strings.
// `node tests/frontend/test_updates_rollback_ui.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, '..', '..', 'static');
const source = readFileSync(path.join(root, 'chat-updates-security.js'), 'utf8').split('\r\n').join('\n');
const catalog = JSON.parse(readFileSync(path.join(root, 'i18n', 'en.json'), 'utf8'));

function lookup(key) {
    return key.split('.').reduce((node, part) => (node === undefined ? undefined : node[part]), catalog);
}
const I18N = {
    t(key, params) {
        const raw = lookup(key);
        assert.ok(typeof raw === 'string', `missing en.json key: ${key}`);
        return raw.replace(/\{(\w+)\}/g, (m, name) => (params && name in params ? String(params[name]) : m));
    },
};

function extractFunction(name) {
    const match = new RegExp(`^(async )?function ${name}\\(`, 'm').exec(source);
    assert.ok(match, `${name} not found`);
    const end = source.indexOf('\n}\n', match.index);
    return source.slice(match.index, end + 3);
}

class El {
    constructor() { this.style = {}; this.textContent = ''; this.disabled = false; this.innerHTML = ''; this.copyButtons = []; }
    querySelectorAll() { return this.copyButtons; }
}

function makeEnv({ status, fetchImpl, answer = true }) {
    const els = {};
    const get = (id) => (els[id] ||= new El());
    const asked = [];
    const toasts = [];
    const fetches = [];
    const polls = [];
    const results = [];
    const sandbox = {
        console, Date, String, JSON, Promise,
        document: { getElementById: get },
        navigator: { clipboard: { writeText: (t) => { sandbox.copied = t; } } },
        escapeHtml: (v) => String(v),
        showToast: (message, type) => toasts.push({ message, type }),
        appConfirm: async (spec) => { asked.push(spec); return answer; },
        renderUpdatesResult: (html) => results.push(html),
        pollAfterUpdateRestart: (version) => polls.push(version),
        fetch: async (url, options) => { fetches.push({ url, options }); return fetchImpl(url, options); },
        _lastUpdatesStatus: status,
    };
    sandbox.window = sandbox;
    sandbox.I18N = I18N;
    vm.createContext(sandbox);
    for (const name of ['updatesRollbackCommand', 'renderUpdatesLastUpdate', 'copyUpdatesRollbackCommand',
        'updatesRefusalMessage', 'renderUpdatesBlockedPanel', 'rollbackUpdate', 'wireUpdatesCopyButtons', 'renderUpdatesManualSteps']) {
        vm.runInContext(extractFunction(name), sandbox);
    }
    return { sandbox, get, asked, toasts, fetches, polls, results, run: (code) => vm.runInContext(code, sandbox) };
}

const rolledStatus = {
    current_version: '1.12.1', previous_version: '1.12.0', previous_version_sha: 'abc1234def', previous_ref: 'main',
    rollback_available: true,
    last_update: { kind: 'update', from: '1.12.0', to: '1.12.1', at: 1792440111, ok: true },
};

// ---------------------------------------------------------------- last update + rollback controls
{
    const env = makeEnv({ status: rolledStatus, fetchImpl: async () => { throw new Error('no fetch'); } });
    env.run('renderUpdatesLastUpdate(_lastUpdatesStatus)');
    assert.match(env.get('updatesLastUpdate').textContent, /^Last update: 1\.12\.0 → 1\.12\.1 \(/);
    assert.equal(env.get('updatesLastUpdate').style.display, 'block');
    assert.equal(env.get('updatesRollbackBtn').style.display, 'inline-block');
    assert.equal(env.get('updatesRollbackRow').style.display, 'flex');
    assert.equal(env.get('updatesRollbackCommand').textContent, 'git reset --keep abc1234def && sudo systemctl restart meshcenter.service');
    env.run('copyUpdatesRollbackCommand()');
    assert.equal(env.sandbox.copied, 'git reset --keep abc1234def && sudo systemctl restart meshcenter.service');
}

// detached HEAD -> the matching manual command
{
    const env = makeEnv({ status: { ...rolledStatus, previous_ref: 'detached' }, fetchImpl: async () => ({}) });
    env.run('renderUpdatesLastUpdate(_lastUpdatesStatus)');
    assert.equal(env.get('updatesRollbackCommand').textContent, 'git checkout --detach abc1234def && sudo systemctl restart meshcenter.service');
}

// failed update: shown as failed, no rollback offered; no update at all: row hidden
{
    const failed = { ...rolledStatus, rollback_available: false, last_update: { ...rolledStatus.last_update, ok: false } };
    const env = makeEnv({ status: failed, fetchImpl: async () => ({}) });
    env.run('renderUpdatesLastUpdate(_lastUpdatesStatus)');
    assert.match(env.get('updatesLastUpdate').textContent, /^Last update failed: 1\.12\.0 → 1\.12\.1/);
    assert.equal(env.get('updatesRollbackBtn').style.display, 'none');
    assert.equal(env.get('updatesRollbackRow').style.display, 'none');

    const none = makeEnv({ status: { current_version: '1.12.0' }, fetchImpl: async () => ({}) });
    none.run('renderUpdatesLastUpdate(_lastUpdatesStatus)');
    assert.equal(none.get('updatesLastUpdate').style.display, 'none');
}

// a rollback entry reads as one
{
    const env = makeEnv({
        status: { current_version: '1.12.0', rollback_available: false, last_update: { kind: 'rollback', from: '1.12.1', to: '1.12.0', at: 1, ok: true } },
        fetchImpl: async () => ({}),
    });
    env.run('renderUpdatesLastUpdate(_lastUpdatesStatus)');
    assert.match(env.get('updatesLastUpdate').textContent, /^Rolled back: 1\.12\.1 → 1\.12\.0/);
}

// ---------------------------------------------------------------- rollback: Cancel sends nothing
{
    const env = makeEnv({ status: rolledStatus, answer: false, fetchImpl: async () => { throw new Error('must not be called'); } });
    const button = { disabled: false };
    await env.run('rollbackUpdate')(button);
    assert.equal(env.asked.length, 1);
    assert.equal(env.asked[0].danger, true, 'rollback is a danger action');
    assert.equal(env.asked[0].message, 'Roll back MeshCenter from 1.12.1 to 1.12.0? The service will restart automatically. Your data is not changed.');
    assert.equal(env.asked[0].title, 'Roll back MeshCenter');
    assert.equal(env.asked[0].confirmLabel, 'Roll back');
    assert.equal(env.fetches.length, 0, 'Cancel sends nothing');
    assert.equal(button.disabled, false);
}

// Confirm -> POST /api/updates/rollback, then polls for the PREVIOUS version
{
    const env = makeEnv({
        status: rolledStatus,
        fetchImpl: async () => ({ status: 202, ok: true, json: async () => ({ ok: true, to_version: '1.12.0' }) }),
    });
    await env.run('rollbackUpdate')({ disabled: false });
    assert.deepEqual(env.fetches.map((f) => [f.url, f.options.method]), [['/api/updates/rollback', 'POST']]);
    assert.deepEqual(env.polls, ['1.12.0']);
}

// blocked by the dependency diff -> persistent panel with the manual command, button re-enabled, no polling
{
    const env = makeEnv({
        status: rolledStatus,
        fetchImpl: async () => ({
            status: 409, ok: false,
            json: async () => ({ ok: false, blocked: true, changed_requirements_files: ['requirements.txt'], instructions: 'git reset --keep abc && pip install -r requirements.txt' }),
        }),
    });
    const button = { disabled: false };
    await env.run('rollbackUpdate')(button);
    assert.equal(env.polls.length, 0);
    assert.equal(button.disabled, false);
    assert.match(env.results[0], /Rollback blocked - dependencies differ/);
    assert.match(env.results[0], /Going back to 1\.12\.0 changes dependency file\(s\): requirements\.txt/);
    assert.match(env.results[0], /git reset --keep abc/);
    assert.match(env.results[0], /data-updates-copy="git reset --keep abc/, 'copy button without an inline handler');
    assert.doesNotMatch(env.results[0], /onclick=/);
}

// refused (dirty tree) -> a sentence, no polling
{
    const env = makeEnv({
        status: rolledStatus,
        fetchImpl: async () => ({ status: 409, ok: false, json: async () => ({ ok: false, preflight: { reason: 'dirty_tree', dirty_files: ['server.py'] } }) }),
    });
    const button = { disabled: false };
    await env.run('rollbackUpdate')(button);
    assert.equal(env.polls.length, 0);
    assert.equal(button.disabled, false);
    assert.match(env.toasts[0].message, /Uncommitted local changes exist: server\.py/);
}

// ---------------------------------------------------------------- refusal sentences (update and rollback)
{
    const env = makeEnv({ status: { latest_version: '1.12.1' }, fetchImpl: async () => ({}) });
    const say = (preflight) => env.run(`updatesRefusalMessage(${JSON.stringify(preflight)})`);
    assert.equal(say({ reason: 'unsupported_branch', branch: 'feature/x' }), 'On branch feature/x - update manually (git).');
    assert.equal(say({ reason: 'up_to_date' }), 'Already on the latest release.');
    assert.match(say({ reason: 'ahead' }), /already contains release 1\.12\.1 or newer/);
    assert.match(say({ reason: 'tag_missing', target_tag: 'v1.12.1' }), /Release tag v1\.12\.1 could not be fetched/);
    assert.match(say({ reason: 'no_target' }), /press Check now/);
    assert.match(say({ reason: 'diverged' }), /local commits that are not part of the release/);
    assert.match(say({ reason: 'fetch_failed' }), /Could not reach GitHub/);
    assert.match(say({ reason: 'rollback_unavailable' }), /No rollback is available/);
    assert.equal(say({ reason: 'weird', error: 'boom' }), 'boom');
}

// ---------------------------------------------------------------- v1.12.2: manual step needed
{
    const steps = [
        { file: 'deploy/meshcenter.service', command: 'sed -e "s|__MESH_USER__|$USER|g" /x/deploy/meshcenter.service | sudo tee /etc/systemd/system/meshcenter.service' },
        { file: 'scripts/meshcenter-hw-config', command: "sudo install -m 0755 '/x y/scripts/meshcenter-hw-config' /usr/local/sbin/meshcenter-hw-config" },
    ];
    const env = makeEnv({
        status: { current_version: '1.12.2', last_update: { kind: 'update', from: '1.12.1', to: '1.12.2', at: 1, ok: true, manual_steps: steps } },
        fetchImpl: async () => ({}),
    });
    const box = env.get('updatesManualStep');
    const button = { listeners: {}, getAttribute: (n) => (n === 'data-updates-copy' ? steps[0].command : ''), addEventListener(type, fn) { this.listeners[type] = fn; } };
    box.copyButtons = [button];
    env.run('renderUpdatesManualSteps(_lastUpdatesStatus)');
    assert.equal(box.style.display, 'block');
    assert.match(box.innerHTML, /Manual step needed after this update/);
    assert.match(box.innerHTML, /nothing was blocked/);
    assert.match(box.innerHTML, /<code>deploy\/meshcenter\.service<\/code>/);
    assert.match(box.innerHTML, /<code>scripts\/meshcenter-hw-config<\/code>/);
    assert.match(box.innerHTML, /sudo install -m 0755/);
    assert.equal((box.innerHTML.match(/data-updates-copy=/g) || []).length, 2, 'one Copy button per command');
    assert.doesNotMatch(box.innerHTML, /onclick=/);
    button.listeners.click();
    assert.equal(env.sandbox.copied, steps[0].command, 'Copy puts the exact command on the clipboard');

    // not shown: no steps, a failed update, or no update at all
    for (const last of [{ kind: 'update', ok: true, manual_steps: [] }, { kind: 'update', ok: false, manual_steps: steps }, null, undefined]) {
        const e2 = makeEnv({ status: { current_version: '1.12.2', last_update: last }, fetchImpl: async () => ({}) });
        e2.run('renderUpdatesManualSteps(_lastUpdatesStatus)');
        assert.equal(e2.get('updatesManualStep').style.display, 'none');
        assert.equal(e2.get('updatesManualStep').innerHTML, '');
    }
}

console.log('OK - updates card (last update from -> to, rollback dialog / blocked / refused, refusal sentences)');
