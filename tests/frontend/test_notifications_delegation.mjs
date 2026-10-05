// tests/frontend/test_notifications_delegation.mjs
//
// H2-D / F1.2: the Activity notifications card no longer builds
// onclick="fn('${id}')" attributes from template literals - it renders
// data-chat-action/data-id and relies on the shared delegated listener
// (CHAT_ACTIONS/onChatActionClick in static/chat.js) to dispatch to
// markBackendNotificationRead()/deleteNotification(). First area
// converted; see _chat_action_test_helpers.mjs for the shared plumbing
// this and every later area's test reuses.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';
import {
    HOSTILE_VALUES, extractFunction, loadChatJsFragment,
    assertRenderedAction, FakeTarget,
} from './_chat_action_test_helpers.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat.js'), 'utf8');

// Static guard: the two onclick= attributes this area used to emit must be
// gone for good, not just happen to not fire on a given input.
assert.ok(!/onclick="markBackendNotificationRead/.test(source), 'markBackendNotificationRead must no longer be wired via onclick=');
assert.ok(!/onclick="event\.stopPropagation\(\); deleteNotification/.test(source), 'deleteNotification must no longer be wired via onclick=');
assert.ok(/escapeHtml\(n\.body\)/.test(source), 'n.body must be passed through escapeHtml()');

// --- renderNotificationsCard() output --------------------------------------

class FakeEl { constructor() { this.innerHTML = ''; this.style = {}; } }

for (const hostileId of HOSTILE_VALUES) {
    const list = new FakeEl();
    const elements = { notificationsList: list, notificationsBadge: new FakeEl(), notificationsClearBtn: new FakeEl(), notificationsEmpty: new FakeEl() };
    const sandbox = vm.createContext({
        console,
        document: { getElementById: (id) => elements[id] || null },
        window: { I18N: { t: (k) => `[[${k}]]` } },
        TimeFormatter: { formatTime: () => '12:00' },
        _notifCardExpanded: true,
        expandNotificationsCard: () => {},
    });
    vm.runInContext(`${extractFunction(source, 'escapeHtml')}\n${extractFunction(source, 'renderNotificationsCard')}`, sandbox);
    sandbox.renderNotificationsCard(
        [{ id: hostileId, title: 'x', body: null, level: 'info', read: false, timestamp: Math.floor(Date.now() / 1000) }],
        1,
    );
    assertRenderedAction(list.innerHTML, { action: 'notif-mark-read', expectedValue: hostileId });
    assert.ok(list.innerHTML.includes('data-chat-action="notif-dismiss"'), 'dismiss button must carry data-chat-action="notif-dismiss"');
}

// --- CHAT_ACTIONS / onChatActionClick() dispatch ----------------------------

function buildDispatchSandbox() {
    const calls = { markRead: [], dismiss: [] };
    const sandbox = loadChatJsFragment(source, ['CHAT_ACTIONS', 'onChatActionClick'], {
        markBackendNotificationRead: (id, el) => calls.markRead.push([id, el]),
        deleteNotification: (id, el) => calls.dismiss.push([id, el]),
    });
    return { sandbox, calls };
}

for (const hostileId of HOSTILE_VALUES) {
    const { sandbox, calls } = buildDispatchSandbox();
    const row = new FakeTarget({ 'data-chat-action': 'notif-mark-read', 'data-id': hostileId, _class: 'notifications-item' });
    sandbox.onChatActionClick({ target: row });
    assert.deepEqual(calls.markRead, [[hostileId, row]]);
    assert.equal(calls.dismiss.length, 0);
}

{
    const { sandbox, calls } = buildDispatchSandbox();
    const row = new FakeTarget({ _class: 'notifications-item', 'data-id': HOSTILE_VALUES[0] });
    const dismissBtn = new FakeTarget({ 'data-chat-action': 'notif-dismiss' }, row);
    sandbox.onChatActionClick({ target: dismissBtn });
    assert.deepEqual(calls.dismiss, [[HOSTILE_VALUES[0], row]], 'dismiss must read the id from the closest .notifications-item, not its own attrs');
    assert.equal(calls.markRead.length, 0, 'dismissing must not also mark the row read');
}

console.log('test_notifications_delegation.mjs: ok');
