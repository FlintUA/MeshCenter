// tests/frontend/test_updates_blocked_ui.mjs
//
// PR #231 review (3rd pass): a minimal, dependency-free behavior test for
// static/chat-updates-security.js's applyUpdate() - specifically, the
// "update blocked because dependencies changed" path added in this pass.
// This project has no build step, no package.json, and no JS test
// framework (CI's own JS coverage is `node --check` - syntax only, see
// .github/workflows/ci.yml) - rather than introducing jest/a browser
// runner as new project infrastructure just for this one file, this uses
// only Node's built-in `vm` module to execute the real source against a
// small stub DOM/fetch/I18N, and Node's built-in `assert`. No npm
// install, no new dependency, runnable as `node
// tests/frontend/test_updates_blocked_ui.mjs`.
//
// What this proves, against the REAL applyUpdate() source (not a
// reimplementation of its logic): on a 409 blocked response, the result
// panel displays the blocked title/file list/instructions, the apply
// button is re-enabled, and - the actual point of this test -
// /api/system/info (pollAfterUpdateRestart()'s own polling target) is
// never fetched, proving restart polling never starts.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.join(__dirname, '..', '..', 'static', 'chat-updates-security.js');
const source = readFileSync(scriptPath, 'utf8');

function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

class FakeElement {
    constructor(id) {
        this.id = id;
        this.innerHTML = '';
        this.textContent = '';
        this.disabled = false;
        this.checked = false;
        this.style = { display: 'none' };
        this._attrs = {};
    }
    setAttribute(name, value) { this._attrs[name] = value; }
    getAttribute(name) { return this._attrs[name]; }
}

function buildSandbox({ fetchImpl, i18nStrings }) {
    const elements = new Map();
    const getEl = (id) => {
        if (!elements.has(id)) elements.set(id, new FakeElement(id));
        return elements.get(id);
    };

    const fetchLog = [];
    const wrappedFetch = async (url, options) => {
        fetchLog.push({ url, options });
        return fetchImpl(url, options);
    };

    const sandbox = {
        console,
        document: {
            getElementById: getEl,
        },
        window: {
            location: { reload: () => {} },
            I18N: {
                t(key, params) {
                    const raw = i18nStrings[key];
                    if (raw === undefined) return `[[${key}]]`;
                    if (!params) return raw;
                    return raw.replace(/\{(\w+)\}/g, (match, name) =>
                        Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
                    );
                },
            },
        },
        navigator: { clipboard: { writeText: async () => {} } },
        escapeHtml,
        showToast: (message) => { sandbox._toasts.push(message); },
        appSettings: {},
        fetch: wrappedFetch,
        confirm: () => true,
        setTimeout,
        Date,
        _toasts: [],
        _elements: elements,
        _fetchLog: fetchLog,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'chat-updates-security.js' });
    return sandbox;
}

const I18N_STRINGS = {
    'system.updates_blocked_deps_title': 'Update blocked — dependencies changed',
    'system.updates_blocked_deps_body': 'This update changes dependency file(s): {files}. It was NOT applied — your installation is unchanged.',
    'system.updates_blocked_deps_instructions': 'Apply it manually, then restart the service yourself:',
    'modals.copy': 'Copy',
    'system.updates_apply_confirm': 'Update MeshCenter from {current} to {latest}?',
};

async function test_blocked_update_shows_instructions_reenables_button_never_polls() {
    const preflightBody = { ok: true, upstream: 'origin/main', behind: 1, ahead: 0, dirty_files: [] };
    const applyBody = {
        ok: false,
        blocked: true,
        requirements_changed: true,
        changed_requirements_files: ['requirements.txt'],
        instructions: 'git merge --ff-only origin/main; pip install -r requirements.txt; systemctl restart meshcenter',
    };

    const sandbox = buildSandbox({
        i18nStrings: I18N_STRINGS,
        fetchImpl: async (url) => {
            if (url === '/api/updates/preflight') {
                return { ok: true, json: async () => preflightBody };
            }
            if (url === '/api/updates/apply') {
                return { ok: false, status: 409, json: async () => applyBody };
            }
            throw new Error(`unexpected fetch in this test: ${url}`);
        },
    });

    const button = sandbox.document.getElementById('updatesApplyBtn');

    await sandbox.applyUpdate(button);

    const panel = sandbox._elements.get('updatesResultPanel');
    assert.equal(panel.style.display, 'block', 'result panel must be shown');
    assert.match(panel.innerHTML, /Update blocked/, 'result panel must show the blocked title');
    assert.match(panel.innerHTML, /requirements\.txt/, 'result panel must list the changed file(s)');
    assert.match(panel.innerHTML, /git merge --ff-only/, 'result panel must show the manual instructions');

    assert.equal(button.disabled, false, 'the apply button must be re-enabled, not left disabled');

    const infoFetches = sandbox._fetchLog.filter((entry) => entry.url === '/api/system/info');
    assert.equal(infoFetches.length, 0, 'pollAfterUpdateRestart() must never start - /api/system/info must never be fetched');

    console.log('PASS: test_blocked_update_shows_instructions_reenables_button_never_polls');
}

async function test_successful_update_still_starts_restart_polling() {
    // Sanity check paired with the test above: confirms this harness (and
    // applyUpdate() itself) still behaves normally on the non-blocked
    // path - polling DOES start when the update actually applied.
    const preflightBody = { ok: true, upstream: 'origin/main', behind: 1, ahead: 0, dirty_files: [] };
    const applyBody = { ok: true, blocked: false, previous_sha: 'abc123', requirements_changed: false, restarted: true };

    const sandbox = buildSandbox({
        i18nStrings: I18N_STRINGS,
        fetchImpl: async (url) => {
            if (url === '/api/updates/preflight') return { ok: true, json: async () => preflightBody };
            if (url === '/api/updates/apply') return { ok: true, status: 202, json: async () => applyBody };
            if (url === '/api/system/info') return { ok: true, json: async () => ({ app_version: 'not-yet-matching' }) };
            throw new Error(`unexpected fetch in this test: ${url}`);
        },
    });

    const button = sandbox.document.getElementById('updatesApplyBtn');
    await sandbox.applyUpdate(button);

    // Give the internal setTimeout(tick, ...) chain one microtask/macrotask
    // turn to fire its first poll.
    await new Promise((resolve) => setTimeout(resolve, 10));

    const infoFetches = sandbox._fetchLog.filter((entry) => entry.url === '/api/system/info');
    assert.ok(infoFetches.length >= 1, 'a successful (non-blocked) update must still start restart polling');

    console.log('PASS: test_successful_update_still_starts_restart_polling');
}

async function main() {
    await test_blocked_update_shows_instructions_reenables_button_never_polls();
    await test_successful_update_still_starts_restart_polling();
    console.log('All frontend tests passed.');
}

main()
    .catch((error) => {
        console.error('FAIL:', error);
        process.exitCode = 1;
    })
    .finally(() => {
        // The successful-update test intentionally leaves a real
        // pollAfterUpdateRestart() setTimeout chain running (that IS the
        // behavior under test) - exit explicitly once assertions are done
        // rather than let the process hang on it or crash on an unstubbed
        // window.location.reload() several seconds later.
        process.exit(process.exitCode || 0);
    });
