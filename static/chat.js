// MeshCenter stage2b-preview-2
// ============================================================
// ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
// ============================================================
let currentChatId = null;
let currentChatName = null;
let currentChatType = null;
let lastMessagesSignature = '';
let nodeSearchTerm = '';
let directMessageTarget = null;
let chatListCache = [];              // <-- ЭТО ОБЯЗАТЕЛЬНО!
let messagePollInterval = null;
let showIgnored = false;
let showFavorites = false;
let nodeCache = [];
let deleteTargetChatId = null;
let clearTargetChatId = null;
let totalUnreadCount = 0;
let currentMainTab = 'chats';
let lastOperationalMainTab = 'chats';
let mainTabTransitionSequence = 0;
let cameraActive = false;
let cameraPowerEnabled = true;
let cameraPowerStatus = 'ready';
let cameraPowerRequestInProgress = false;
let isInitialized = false;
let contextChatMode = false;
let contextBaseTab = null;
let radioHealthTimer = null;
let radioCommandRunning = false;
let nodeToolResultTimer = null;
let nodeToolResults = {};
let activeNodeTabs = {}; // Active tab per node.
let nodeRenderCache = {}; // Last rendered signature per node.
let referenceLocationInitialState = '';
let referenceLocationSaving = false;
let messageActionTarget = null;
let renderedMessagesById = new Map();
let activeReply = null;

// Registry for the selected-node card. Future core modules or plugins can
// register an additional tab without rewriting renderNodeDetails().
const NODE_DETAIL_TABS = [
    { id: 'overview', label: 'Overview', render: renderOverviewPane },
    { id: 'radio', label: 'Radio', render: renderRadioPane },
    { id: 'position', label: 'Position', render: renderPositionPane },
    { id: 'data', label: 'Data', render: renderDataPane },
    { id: 'log', label: 'Log', render: renderLogPane }
];

// Translates a request error thrown with a .code (set from the backend's
// error_code field, see requestError.code = data.error_code assignments
// below) via the errors.* catalog namespace, interpolating .params if the
// backend sent any. Falls back to the raw English error.message when the
// code isn't in the catalog yet — most endpoints don't set error_code at
// all, so this must degrade gracefully rather than show a broken marker.
function translateRequestError(error) {
    if (error && error.code) {
        return window.I18N.tOrFallback(
            'errors.' + error.code,
            error.params,
            error.message
        );
    }
    return error?.message || 'Unknown error';
}

function registerNodeDetailTab(tab) {
    if (!tab || !tab.id || !tab.label || typeof tab.render !== 'function') return false;
    if (NODE_DETAIL_TABS.some(item => item.id === tab.id)) return false;
    NODE_DETAIL_TABS.push(tab);
    resetNodeRenderCache();
    return true;
}

// ─── Time Formatter ───────────────────────────────────────────────
const TimeFormatter = {
  _getLocale() {
    return I18N?.locale || navigator.language || 'en';
  },
  _is12h() {
    return (appSettings?.units?.time_format || '24') === '12';
  },
  _serverEpoch:     null,
  _localSnapshotMs: null,
  _serverTimezone:  null,
  _syncStatus:      'pending', // 'synced' | 'degraded' | 'pending'

  // Best-known current time: server-synced clock once /api/time has
  // answered at least once, falling back to the browser clock until then.
  now() {
    if (this._serverEpoch === null) return new Date();
    const elapsedMs = Date.now() - this._localSnapshotMs;
    return new Date((this._serverEpoch * 1000) + elapsedMs);
  },

  syncFromServer(data) {
    this._serverEpoch     = data.utc;
    this._localSnapshotMs = Date.now();
    this._serverTimezone  = data.timezone || null;
    this._syncStatus      = 'synced';
  },

  markDegraded() {
    this._syncStatus = 'degraded';
  },

  // Intl.DateTimeFormat options fragment picking the MeshCenter host's
  // timezone once known via syncFromServer(); empty otherwise so Intl
  // falls back to the browser's own timezone.
  _tzOption() {
    const mode = appSettings?.time?.timezone_mode || 'meshcenter';
    if (mode === 'meshcenter' && this._serverTimezone) {
      return { timeZone: this._serverTimezone };
    }
    return {};
  },

  formatTime(date) {
    if (!date) return '--';
    const d = (date instanceof Date) ? date : new Date(date * 1000);
    if (isNaN(d)) return '--';
    return new Intl.DateTimeFormat(this._getLocale(), {
      hour:   'numeric',
      minute: '2-digit',
      hour12: this._is12h(),
      ...this._tzOption()
    }).format(d);
  },
  formatDate(date) {
    if (!date) return '--';
    const d = (date instanceof Date) ? date : new Date(date * 1000);
    if (isNaN(d)) return '--';
    return new Intl.DateTimeFormat(this._getLocale(), {
      day:   'numeric',
      month: 'long',
      year:  'numeric',
      ...this._tzOption()
    }).format(d);
  },
  formatDateTime(date) {
    if (!date) return '--';
    const d = (date instanceof Date) ? date : new Date(date * 1000);
    if (isNaN(d)) return '--';
    return new Intl.DateTimeFormat(this._getLocale(), {
      day:    'numeric',
      month:  'short',
      year:   'numeric',
      hour:   'numeric',
      minute: '2-digit',
      hour12: this._is12h(),
      ...this._tzOption()
    }).format(d);
  },
  formatTooltip(date, timezone) {
    if (!date) return '--';
    const d = (date instanceof Date) ? date : new Date(date * 1000);
    if (isNaN(d)) return '--';
    const datePart = new Intl.DateTimeFormat(this._getLocale(), {
      weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
      ...this._tzOption()
    }).format(d);
    const timePart = new Intl.DateTimeFormat(this._getLocale(), {
      hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: this._is12h(),
      ...this._tzOption()
    }).format(d);
    return timezone ? `${datePart}\n${timePart}\n${timezone}` : `${datePart}\n${timePart}`;
  }
};

// ─── Time Card — Server Sync ──────────────────────────────────────
const TIME_RESYNC_INTERVAL_MS = 5 * 60 * 1000;
let _timeResyncTimer = null;

async function syncTimeWithServer() {
  try {
    const resp = await fetch('/api/time');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    TimeFormatter.syncFromServer(data);
    updateTimeCardSource(data);
  } catch (e) {
    TimeFormatter.markDegraded();
    updateTimeCardSource(null);
    console.warn('Time sync failed:', e);
  }
}

function updateTimeCardSource(data) {
  const el = document.getElementById('timeClockSource');
  if (!el) return;
  if (!data) {
    el.textContent = TimeFormatter._serverEpoch ? `(${window.I18N.t('time.not_synchronized')})` : '';
    return;
  }
  el.textContent = data.synchronized ? '' : `(${window.I18N.t('time.not_synchronized')})`;
}

function updateTimeCardClock() {
  const now = TimeFormatter.now();

  const clockEl = document.getElementById('timeClockDisplay');
  if (clockEl) {
    clockEl.textContent = TimeFormatter.formatTime(now);
    // Tooltip now only needs timezone + sync status - the date itself is
    // shown right below in .time-hero-date, so repeating it here would be
    // redundant (formatTooltip() used to supply the full date+time+tz).
    const tz = TimeFormatter._serverTimezone;
    clockEl.title = (tz || '')
      + (TimeFormatter._syncStatus === 'degraded' ? `${tz ? '\n' : ''}⚠ ${window.I18N.t('time.not_synchronized')}` : '');
  }

  const dateEl = document.getElementById('timeClockDate');
  if (dateEl) {
    dateEl.textContent = new Intl.DateTimeFormat(
      TimeFormatter._getLocale(), {
        weekday: 'long',
        day:     'numeric',
        month:   'long',
        year:    'numeric',
        ...TimeFormatter._tzOption()
      }
    ).format(now);
  }

  const weekEl = document.getElementById('timeClockWeek');
  if (weekEl) {
    weekEl.textContent = window.I18N.t('time.week_number')
      .replace('{n}', _getISOWeek(now));
  }
}

function _getISOWeek(date) {
  const d = new Date(Date.UTC(
    date.getFullYear(), date.getMonth(), date.getDate()));
  d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
}

function initTimeCard() {
  syncTimeWithServer();
  setInterval(updateTimeCardClock, 1000);
  _timeResyncTimer = setInterval(syncTimeWithServer, TIME_RESYNC_INTERVAL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      syncTimeWithServer();
    }
  });
  loadTimers();
}

// ─── Notifications Card ───────────────────────────────────────────
// Backend event queue (schedule/timer/system sources), polled separately
// from the sessionStorage-based notification center (addNotification() /
// notificationItems above) - this card only calls into addNotification()
// to raise a toast for genuinely-new backend events.
let _notifPollTimer     = null;
let _notifLastSeenId    = sessionStorage.getItem('mc.notif.lastSeenId') || null;
let _notifCardExpanded  = false;
const NOTIF_POLL_MS     = 30_000;

async function pollNotifications() {
  try {
    const resp = await fetch('/api/notifications');
    if (!resp.ok) return;
    const data = await resp.json();
    renderNotificationsCard(data.notifications, data.unread_count);
    showToastsForNew(data.notifications);
  } catch (e) {
    console.warn('Notifications poll failed:', e);
  }
}

function showToastsForNew(notifications) {
  if (!notifications.length) return;
  const newestId = notifications[0].id;

  const newOnes = [];
  for (const n of notifications) {
    if (n.id === _notifLastSeenId) break;
    newOnes.push(n);
  }

  if (_notifLastSeenId !== null) {
    for (const n of newOnes.reverse()) {
      const type = n.level === 'error'   ? 'error'
                 : n.level === 'warning' ? 'warning'
                 : 'info';
      const text = n.body ? `${n.title}: ${n.body}` : n.title;
      addNotification(text, type);
    }
  }

  if (newestId !== _notifLastSeenId) {
    _notifLastSeenId = newestId;
    sessionStorage.setItem('mc.notif.lastSeenId', newestId);
  }
}

function renderNotificationsCard(notifications, unreadCount) {
  const badge = document.getElementById('notificationsBadge');
  const clearBtn = document.getElementById('notificationsClearBtn');
  if (badge) {
    badge.textContent = unreadCount;
    badge.style.display = unreadCount > 0 ? 'inline' : 'none';
  }
  if (clearBtn) {
    clearBtn.style.display = notifications.length > 0 ? 'inline-block' : 'none';
  }
  if (unreadCount > 0 && !_notifCardExpanded) {
    expandNotificationsCard(false);
  }

  const list  = document.getElementById('notificationsList');
  const empty = document.getElementById('notificationsEmpty');
  if (!list) return;

  if (notifications.length === 0) {
    list.innerHTML = '';
    if (empty) empty.style.display = 'block';
    return;
  }
  if (empty) empty.style.display = 'none';

  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const todayStartTs = Math.floor(todayStart.getTime() / 1000);

  let html = '';
  let shownDivider = false;

  for (const n of notifications) {
    if (!shownDivider && n.timestamp < todayStartTs) {
      html += `<div class="notifications-divider" data-i18n="activity.earlier">Earlier</div>`;
      shownDivider = true;
    }
    const icon  = n.level === 'error'   ? '⚠'
                : n.level === 'warning' ? '⚠'
                : '⏱';
    const timeStr = TimeFormatter.formatTime(n.timestamp);
    const readCls = n.read ? 'notifications-item--read' : '';

    html += `
      <div class="notifications-item ${readCls}" data-id="${n.id}"
           onclick="markBackendNotificationRead('${n.id}', this)">
        <span class="notifications-item-icon">${icon}</span>
        <span class="notifications-item-time">${timeStr}</span>
        <span class="notifications-item-title">${escapeHtml(n.title)}</span>
        ${n.body ? `<span class="notifications-item-body">${escapeHtml(n.body)}</span>` : ''}
        <button class="notifications-item-dismiss"
                onclick="event.stopPropagation(); deleteNotification('${n.id}', this.closest('.notifications-item'))"
                aria-label="Dismiss">✕</button>
      </div>`;
  }
  list.innerHTML = html;
}

function toggleNotificationsCard() {
  _notifCardExpanded ? collapseNotificationsCard() : expandNotificationsCard(true);
}

function expandNotificationsCard(animate) {
  _notifCardExpanded = true;
  const body    = document.getElementById('notificationsCardBody');
  const chevron = document.getElementById('notificationsChevron');
  const header  = document.getElementById('notificationsCardHeader');
  if (body)    body.style.display = 'block';
  if (chevron) chevron.textContent = '▾';
  if (header)  header.setAttribute('aria-expanded', 'true');
  fetch('/api/notifications/read-all', { method: 'POST' })
    .then(() => {
      const badge = document.getElementById('notificationsBadge');
      if (badge) badge.style.display = 'none';
    }).catch(() => {});
}

function collapseNotificationsCard() {
  _notifCardExpanded = false;
  const body    = document.getElementById('notificationsCardBody');
  const chevron = document.getElementById('notificationsChevron');
  const header  = document.getElementById('notificationsCardHeader');
  if (body)    body.style.display = 'none';
  if (chevron) chevron.textContent = '▸';
  if (header)  header.setAttribute('aria-expanded', 'false');
}

// Named markBackendNotificationRead (not markNotificationRead) - that name
// is already taken by the older sessionStorage-based notification center
// above (single-arg, marks a client-side item read); this one marks a
// backend-queue event read via the API and takes the row element too.
async function markBackendNotificationRead(id, el) {
  await fetch(`/api/notifications/${id}/read`, { method: 'PATCH' });
  el?.classList.add('notifications-item--read');
}

async function deleteNotification(id, el) {
  await fetch(`/api/notifications/${id}`, { method: 'DELETE' });
  el?.remove();
  await pollNotifications();
}

async function clearAllNotifications() {
  await fetch('/api/notifications', { method: 'DELETE' });
  await pollNotifications();
}

async function testNotification() {
  await fetch('/api/notifications/test', { method: 'POST' });
  await pollNotifications();
}

function initNotificationsCard() {
  pollNotifications();
  _notifPollTimer = setInterval(pollNotifications, NOTIF_POLL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') pollNotifications();
  });
}

// ─── Schedules Card (Time & Timers card, #timeSchedulesSection) ──────
// Backend-driven schedule rules (data/schedules.json via meshsrv/
// schedule_engine.py), rendered into the same #timeSchedulesList /
// #addScheduleBtn hooks Stage 1 placed inside #timeCard.
let scheduleRules = [];
let scheduleEditingId = null;

const SCHEDULE_DAY_ORDER = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

function initSchedulesCard() {
  const section = document.getElementById('timeSchedulesSection');
  if (section) section.style.display = '';

  const addBtn = document.getElementById('addScheduleBtn');
  if (addBtn) addBtn.addEventListener('click', () => openScheduleForm(null));

  loadSchedules();
}

async function loadSchedules() {
  try {
    const resp = await fetch('/api/schedules');
    if (!resp.ok) return;
    scheduleRules = await resp.json();
    renderSchedulesList();
  } catch (e) {
    console.warn('Failed to load schedules:', e);
  }
}

// Locale-aware day-name formatting: instead of a hardcoded translation
// table, ask Intl.DateTimeFormat for each weekday's short name in the
// active locale. 2026-01-04 was a Sunday, so 2026-01-(4+n) walks through
// Sun..Sat for n = 0..6 - DAY_INDEX maps each day code to that offset.
function _formatDays(days) {
    if (!days || days.length === 0) return '';
    if (days.length === 7) return window.I18N.t('time.every_day');
    const weekdays = ['mon','tue','wed','thu','fri'];
    if (days.length === 5 && weekdays.every(d => days.includes(d)))
        return window.I18N.t('time.weekdays');

    const DAY_INDEX = {sun:0, mon:1, tue:2, wed:3, thu:4, fri:5, sat:6};
    const fmt = new Intl.DateTimeFormat(
        TimeFormatter._getLocale(), {weekday: 'short'});
    return days
        .map(d => {
            const ref = new Date(2026, 0, 4 + (DAY_INDEX[d] ?? 0));
            return fmt.format(ref);
        })
        .join(', ');
}

function _scheduleSummaryText(rule) {
  const t = rule.trigger || {};
  if (t.mode === 'interval') {
    return window.I18N.t('time.every_n_min').replace('{n}', t.interval_minutes ?? '?');
  }
  if (t.mode === 'once') {
    return t.datetime ? TimeFormatter.formatDateTime(new Date(t.datetime)) : '';
  }
  const days = Array.isArray(t.days) ? t.days : [];
  const dayLabel = _formatDays(days);
  return `${dayLabel} ${t.time || ''}`.trim();
}

function renderSchedulesList() {
  const list = document.getElementById('timeSchedulesList');
  if (!list) return;

  if (!scheduleRules.length) {
    list.innerHTML = `<div class="time-schedules-empty" data-i18n="time.schedules_empty">${escapeHtml(window.I18N.t('time.schedules_empty'))}</div>`;
    return;
  }

  list.innerHTML = scheduleRules.map(rule => {
    const disabledClass = rule.enabled ? '' : ' is-disabled';
    const summary = escapeHtml(_scheduleSummaryText(rule));
    const label = escapeHtml(rule.label || window.I18N.t('time.label_placeholder'));
    const toggleLabel = rule.enabled ? window.I18N.t('time.disable') : window.I18N.t('time.enable');
    const modeIcon = rule.trigger?.mode === 'interval' ? '🔁'
                    : rule.trigger?.mode === 'once'     ? '📅'
                    : '⏱';
    return `
      <div class="time-schedule-row${disabledClass}" data-id="${escapeHtml(rule.id)}">
        <div class="time-schedule-info" onclick="openScheduleForm('${escapeHtml(rule.id)}')">
          <span class="time-schedule-label"><span class="time-schedule-mode-icon" aria-hidden="true">${modeIcon}</span> ${label}</span>
          <span class="time-schedule-summary">${summary}</span>
        </div>
        <div class="time-schedule-actions">
          <button class="btn btn-sm" title="${escapeHtml(toggleLabel)}" onclick="event.stopPropagation(); toggleScheduleEnabled('${escapeHtml(rule.id)}')">${rule.enabled ? '⏸' : '▶'}</button>
          <button class="btn btn-sm" title="${escapeHtml(window.I18N.t('common.delete'))}" onclick="event.stopPropagation(); deleteScheduleRule('${escapeHtml(rule.id)}')">🗑</button>
        </div>
      </div>`;
  }).join('');
}

async function toggleScheduleEnabled(id) {
  try {
    const resp = await fetch(`/api/schedules/${encodeURIComponent(id)}/toggle`, { method: 'PATCH' });
    if (!resp.ok) return;
    await loadSchedules();
  } catch (e) {
    console.warn('Failed to toggle schedule:', e);
  }
}

async function deleteScheduleRule(id) {
  if (!confirm(window.I18N.t('time.confirm_delete'))) return;
  try {
    const resp = await fetch(`/api/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!resp.ok) return;
    await loadSchedules();
  } catch (e) {
    console.warn('Failed to delete schedule:', e);
  }
}

// ─── Schedule edit form (scheduleModal) ───────────────────────────────

// Dirty-flag tracking (Stage 8 audit fix): closing the modal via the X/
// backdrop/Cancel path used to silently discard whatever the user had
// typed, with no confirmation - _attachDirtyListeners() below wires every
// form control rendered by openScheduleForm() to a single flag that
// closeScheduleForm() checks before actually hiding the modal.
let _scheduleFormDirty = false;

function _attachDirtyListeners() {
  const body = document.getElementById('scheduleModalBody');
  if (!body) return;
  body.querySelectorAll('input, select, textarea').forEach(el => {
    el.addEventListener('change', () => { _scheduleFormDirty = true; });
    el.addEventListener('input',  () => { _scheduleFormDirty = true; });
  });
}

function _renderTargetPicker(prefix, targetType, nodeId, channelIndex) {
  const nodes = Array.isArray(nodeCache) ? nodeCache : [];
  const nodeOptions = nodes
    .filter(n => n && n.node_id)
    .map(n => {
      const name = escapeHtml(n.clean_name || n.name || n.long_name || n.node_id);
      const selected = n.node_id === nodeId ? ' selected' : '';
      return `<option value="${escapeHtml(n.node_id)}"${selected}>${name}</option>`;
    }).join('');

  const channels = Array.isArray(scheduleChannelCache) ? scheduleChannelCache : [];
  const channelOptions = channels
    .filter(c => c && Number.isInteger(c.index))
    .map(c => {
      const selected = c.index === channelIndex ? ' selected' : '';
      return `<option value="${c.index}"${selected}>${escapeHtml(formatChannelIndexLabel(c.name, c.index))}</option>`;
    }).join('');

  const nodeChecked = targetType === 'node' ? ' checked' : '';
  const chanChecked = targetType === 'channel' ? ' checked' : '';

  return `
    <div class="schedule-target-picker" data-prefix="${prefix}">
      <label class="schedule-radio">
        <input type="radio" name="${prefix}TargetType" value="node"${nodeChecked} onchange="_scheduleUpdateTargetVisibility('${prefix}')">
        <span data-i18n="time.target_node">${escapeHtml(window.I18N.t('time.target_node'))}</span>
      </label>
      <label class="schedule-radio">
        <input type="radio" name="${prefix}TargetType" value="channel"${chanChecked} onchange="_scheduleUpdateTargetVisibility('${prefix}')">
        <span data-i18n="time.target_channel">${escapeHtml(window.I18N.t('time.target_channel'))}</span>
      </label>
      <select id="${prefix}NodeSelect" class="schedule-select" style="display:${targetType === 'node' ? '' : 'none'}">
        ${nodeOptions}
      </select>
      <select id="${prefix}ChannelSelect" class="schedule-select" style="display:${targetType === 'channel' ? '' : 'none'}">
        ${channelOptions}
      </select>
    </div>`;
}

function _scheduleUpdateTargetVisibility(prefix) {
  const picker = document.querySelector(`.schedule-target-picker[data-prefix="${prefix}"]`);
  if (!picker) return;
  const checked = picker.querySelector(`input[name="${prefix}TargetType"]:checked`);
  const type = checked ? checked.value : 'node';
  const nodeSelect = document.getElementById(`${prefix}NodeSelect`);
  const chanSelect = document.getElementById(`${prefix}ChannelSelect`);
  if (nodeSelect) nodeSelect.style.display = type === 'node' ? '' : 'none';
  if (chanSelect) chanSelect.style.display = type === 'channel' ? '' : 'none';
}

function _scheduleReadTarget(prefix) {
  const picker = document.querySelector(`.schedule-target-picker[data-prefix="${prefix}"]`);
  if (!picker) return { target_type: 'node', node_id: '', channel_index: 0 };
  const checked = picker.querySelector(`input[name="${prefix}TargetType"]:checked`);
  const targetType = checked ? checked.value : 'node';
  if (targetType === 'channel') {
    const sel = document.getElementById(`${prefix}ChannelSelect`);
    return { target_type: 'channel', node_id: '', channel_index: sel ? parseInt(sel.value, 10) || 0 : 0 };
  }
  const sel = document.getElementById(`${prefix}NodeSelect`);
  return { target_type: 'node', node_id: sel ? sel.value : '', channel_index: 0 };
}

function _renderDataReportParams() {
  // send_data_report's backend field-picker (_get_field_value in
  // meshsrv/schedule_actions.py) is fully implemented against per-node
  // telemetry (server.py's nodes[node_id].device_metrics /
  // environment_metrics / power_metrics), but this stage does not ship a
  // field-picker UI for it yet - only log_entry and mesh_send have edit
  // forms. Kept as an honest placeholder rather than a fake UI.
  return `<p class="schedule-placeholder-note" data-i18n="time.action_data_note">${escapeHtml(window.I18N.t('time.action_data_note'))}</p>`;
}

let scheduleChannelCache = [];

async function _scheduleLoadChannels() {
  try {
    const resp = await fetch('/api/chats');
    if (!resp.ok) return;
    const data = await resp.json();
    scheduleChannelCache = data.channels || [];
  } catch (e) {
    console.warn('Failed to load channels for schedule form:', e);
  }
}

async function openScheduleForm(id) {
  await _scheduleLoadChannels();
  scheduleEditingId = id;
  const rule = id ? scheduleRules.find(r => r.id === id) : null;

  const label = rule?.label || '';
  const trigger = rule?.trigger || { mode: 'daily', time: '08:00', days: ['mon','tue','wed','thu','fri'], interval_minutes: 60, datetime: '' };
  const actions = rule?.actions || [{ type: 'log_entry', params: {} }];
  const primaryAction = actions[0] || { type: 'log_entry', params: {} };
  const notify = rule?.notify || { enabled: false, signal: '', details: '', mesh_message: { enabled: false, target_type: 'node', node_id: '', channel_index: 0 } };
  const meshMsg = notify.mesh_message || {};

  const modalBody = document.getElementById('scheduleModalBody');
  if (!modalBody) return;

  const dayChips = SCHEDULE_DAY_ORDER.map(d => {
    const checked = (trigger.days || []).includes(d) ? ' checked' : '';
    return `<label class="schedule-day-chip">
      <input type="checkbox" value="${d}" class="schedule-day-input"${checked}>
      <span>${escapeHtml(window.I18N.t(`time.day_${d}`))}</span>
    </label>`;
  }).join('');

  modalBody.innerHTML = `
    <div class="schedule-form-row">
      <label data-i18n="time.label_name">${escapeHtml(window.I18N.t('time.label_name'))}</label>
      <input type="text" id="sfLabel" class="schedule-input" maxlength="80"
             placeholder="${escapeHtml(window.I18N.t('time.label_placeholder'))}" value="${escapeHtml(label)}">
    </div>

    <div class="schedule-form-row">
      <label data-i18n="time.when">${escapeHtml(window.I18N.t('time.when'))}</label>
      <select id="sfMode" class="schedule-select" onchange="_scheduleUpdateModeVisibility()">
        <option value="daily"${trigger.mode === 'daily' ? ' selected' : ''}>${escapeHtml(window.I18N.t('time.mode_daily'))}</option>
        <option value="interval"${trigger.mode === 'interval' ? ' selected' : ''}>${escapeHtml(window.I18N.t('time.mode_interval'))}</option>
        <option value="once"${trigger.mode === 'once' ? ' selected' : ''}>${escapeHtml(window.I18N.t('time.mode_once'))}</option>
      </select>
    </div>

    <div class="schedule-form-row" id="sfDailyFields" style="display:${trigger.mode === 'daily' ? '' : 'none'}">
      <label data-i18n="time.time_label">${escapeHtml(window.I18N.t('time.time_label'))}</label>
      <input type="time" id="sfTime" class="schedule-input" value="${escapeHtml(trigger.time || '08:00')}">
      <div class="schedule-days-label" data-i18n="time.days_label">${escapeHtml(window.I18N.t('time.days_label'))}</div>
      <div class="schedule-day-picker" id="sfDays">${dayChips}</div>
    </div>

    <div class="schedule-form-row" id="sfIntervalFields" style="display:${trigger.mode === 'interval' ? '' : 'none'}">
      <label data-i18n="time.repeat">${escapeHtml(window.I18N.t('time.repeat'))}</label>
      <span class="schedule-inline">
        ${escapeHtml(window.I18N.t('time.every_n'))}
        <input type="number" id="sfInterval" class="schedule-input schedule-input--narrow" min="1" max="10080" value="${trigger.interval_minutes || 60}">
        ${escapeHtml(window.I18N.t('time.minutes'))}
      </span>
    </div>

    <div class="schedule-form-row" id="sfOnceFields" style="display:${trigger.mode === 'once' ? '' : 'none'}">
      <label data-i18n="time.datetime_label">${escapeHtml(window.I18N.t('time.datetime_label'))}</label>
      <input type="datetime-local" id="sfOnceDatetime" class="schedule-input" value="${escapeHtml(trigger.datetime || '')}">
    </div>

    <div class="schedule-form-row">
      <label data-i18n="time.what">${escapeHtml(window.I18N.t('time.what'))}</label>
      <span class="schedule-inline-label" data-i18n="time.action_label">${escapeHtml(window.I18N.t('time.action_label'))}</span>
      <select id="sfActionType" class="schedule-select" onchange="_scheduleUpdateActionVisibility()">
        <option value="log_entry"${primaryAction.type === 'log_entry' ? ' selected' : ''}>${escapeHtml(window.I18N.t('time.action_log'))}</option>
        <option value="mesh_send"${primaryAction.type === 'mesh_send' ? ' selected' : ''}>${escapeHtml(window.I18N.t('time.action_mesh'))}</option>
        <option value="send_data_report"${primaryAction.type === 'send_data_report' ? ' selected' : ''}>${escapeHtml(window.I18N.t('time.action_data'))}</option>
      </select>
    </div>

    <div class="schedule-form-row schedule-action-params" id="sfMeshParams" style="display:${primaryAction.type === 'mesh_send' ? '' : 'none'}">
      <input type="text" id="sfMeshMessage" class="schedule-input" maxlength="200"
             placeholder="${escapeHtml(window.I18N.t('time.message_placeholder'))}"
             value="${escapeHtml(primaryAction.type === 'mesh_send' ? (primaryAction.params?.message || '') : '')}">
      ${_renderTargetPicker('ms',
          primaryAction.type === 'mesh_send' ? (primaryAction.params?.target_type || 'node') : 'node',
          primaryAction.type === 'mesh_send' ? (primaryAction.params?.node_id || '') : '',
          primaryAction.type === 'mesh_send' ? (primaryAction.params?.channel_index ?? 0) : 0)}
    </div>

    <div class="schedule-form-row schedule-action-params" id="sfDataParams" style="display:${primaryAction.type === 'send_data_report' ? '' : 'none'}">
      ${_renderDataReportParams()}
    </div>

    <div class="schedule-form-section">
      <div class="schedule-section-heading" data-i18n="time.notify_section">${escapeHtml(window.I18N.t('time.notify_section'))}</div>
      <label class="schedule-checkbox-row">
        <input type="checkbox" id="sfNotifyEnabled" onchange="_scheduleUpdateNotifyVisibility()"${notify.enabled ? ' checked' : ''}>
        <span data-i18n="time.notify_enabled">${escapeHtml(window.I18N.t('time.notify_enabled'))}</span>
      </label>
      <div id="sfNotifyFields" style="display:${notify.enabled ? '' : 'none'}">
        <div class="schedule-form-row">
          <label data-i18n="time.signal_label">${escapeHtml(window.I18N.t('time.signal_label'))}</label>
          <input type="text" id="sfSignal" class="schedule-input" maxlength="120" value="${escapeHtml(notify.signal || '')}">
        </div>
        <div class="schedule-form-row">
          <label data-i18n="time.details_label">${escapeHtml(window.I18N.t('time.details_label'))}</label>
          <input type="text" id="sfDetails" class="schedule-input" maxlength="200" value="${escapeHtml(notify.details || '')}">
        </div>
        <label class="schedule-checkbox-row">
          <input type="checkbox" id="sfNotifyAlsoMesh" onchange="_scheduleUpdateNotifyMeshVisibility()"${meshMsg.enabled ? ' checked' : ''}>
          <span data-i18n="time.notify_also_mesh">${escapeHtml(window.I18N.t('time.notify_also_mesh'))}</span>
        </label>
        <div class="schedule-form-row schedule-action-params" id="sfNotifyMeshParams" style="display:${meshMsg.enabled ? '' : 'none'}">
          ${_renderTargetPicker('nm', meshMsg.target_type || 'node', meshMsg.node_id || '', meshMsg.channel_index ?? 0)}
        </div>
      </div>
    </div>

    <div class="schedule-form-error" id="sfError" style="display:none"></div>
  `;

  const title = document.getElementById('scheduleModalTitle');
  if (title) title.textContent = rule ? (rule.label || window.I18N.t('time.label_placeholder')) : window.I18N.t('time.new_schedule');

  const deleteBtn = document.getElementById('scheduleModalDeleteBtn');
  if (deleteBtn) deleteBtn.style.display = rule ? '' : 'none';

  const modal = document.getElementById('scheduleModal');
  if (modal) modal.style.display = 'flex';

  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const minDt = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  document.getElementById('sfOnceDatetime')?.setAttribute('min', minDt);

  _scheduleFormDirty = false;
  _attachDirtyListeners();
}

function closeScheduleForm() {
  if (_scheduleFormDirty) {
    if (!confirm(window.I18N.t('time.unsaved_changes'))) return;
  }
  _scheduleFormDirty = false;
  const modal = document.getElementById('scheduleModal');
  if (modal) modal.style.display = 'none';
  scheduleEditingId = null;
}

function _scheduleUpdateModeVisibility() {
  const mode = document.getElementById('sfMode')?.value;
  const daily = document.getElementById('sfDailyFields');
  const interval = document.getElementById('sfIntervalFields');
  const once = document.getElementById('sfOnceFields');
  if (daily) daily.style.display = mode === 'daily' ? '' : 'none';
  if (interval) interval.style.display = mode === 'interval' ? '' : 'none';
  if (once) once.style.display = mode === 'once' ? '' : 'none';
}

function _scheduleUpdateActionVisibility() {
  const type = document.getElementById('sfActionType')?.value;
  const mesh = document.getElementById('sfMeshParams');
  const data = document.getElementById('sfDataParams');
  if (mesh) mesh.style.display = type === 'mesh_send' ? '' : 'none';
  if (data) data.style.display = type === 'send_data_report' ? '' : 'none';
}

function _scheduleUpdateNotifyVisibility() {
  const enabled = document.getElementById('sfNotifyEnabled')?.checked;
  const fields = document.getElementById('sfNotifyFields');
  if (fields) fields.style.display = enabled ? '' : 'none';
}

function _scheduleUpdateNotifyMeshVisibility() {
  const enabled = document.getElementById('sfNotifyAlsoMesh')?.checked;
  const fields = document.getElementById('sfNotifyMeshParams');
  if (fields) fields.style.display = enabled ? '' : 'none';
}

function _collectFormData() {
  const errorEl = document.getElementById('sfError');
  const showError = (msg) => {
    if (errorEl) { errorEl.textContent = msg; errorEl.style.display = 'block'; }
    return null;
  };

  const label = document.getElementById('sfLabel')?.value.trim() || '';
  const mode = document.getElementById('sfMode')?.value || 'daily';

  const trigger = { type: 'schedule', mode };
  if (mode === 'daily') {
    trigger.time = document.getElementById('sfTime')?.value || '08:00';
    trigger.days = Array.from(document.querySelectorAll('#sfDays .schedule-day-input:checked')).map(el => el.value);
    if (!trigger.days.length) {
      return showError(window.I18N.t('time.error_no_days'));
    }
  } else if (mode === 'once') {
    const dtVal = document.getElementById('sfOnceDatetime')?.value;
    if (!dtVal) {
      return showError(window.I18N.t('time.error_no_datetime'));
    }
    trigger.datetime = dtVal;
  } else {
    trigger.interval_minutes = parseInt(document.getElementById('sfInterval')?.value, 10) || 60;
  }

  const actionType = document.getElementById('sfActionType')?.value || 'log_entry';
  let action = { type: actionType, params: {} };

  if (actionType === 'mesh_send') {
    const message = document.getElementById('sfMeshMessage')?.value.trim() || '';
    const target = _scheduleReadTarget('ms');
    action.params = { message, ...target };
  } else if (actionType === 'send_data_report') {
    // Backend field-picker exists (meshsrv/schedule_actions.py
    // _get_field_value) but no UI is shipped to configure `fields` /
    // `source_node` yet - saving this action type currently produces an
    // action with empty params, which _do_send_data_report() handles
    // gracefully (logs "no data available" and returns without sending).
    action.params = {};
  }

  const notifyEnabled = document.getElementById('sfNotifyEnabled')?.checked || false;
  const notify = {
    enabled: notifyEnabled,
    signal: document.getElementById('sfSignal')?.value.trim() || '',
    details: document.getElementById('sfDetails')?.value.trim() || '',
    mesh_message: { enabled: false, target_type: 'node', node_id: '', channel_index: 0 }
  };

  if (notifyEnabled) {
    const alsoMesh = document.getElementById('sfNotifyAlsoMesh')?.checked || false;
    if (alsoMesh) {
      if (!notify.signal) {
        return showError(window.I18N.t('time.error_no_signal'));
      }
      const target = _scheduleReadTarget('nm');
      notify.mesh_message = { enabled: true, ...target };
    }
  }

  if (errorEl) errorEl.style.display = 'none';
  return { label, trigger, actions: [action], notify };
}

async function saveScheduleForm() {
  const data = _collectFormData();
  if (!data) return;

  try {
    const url = scheduleEditingId ? `/api/schedules/${encodeURIComponent(scheduleEditingId)}` : '/api/schedules';
    const method = scheduleEditingId ? 'PUT' : 'POST';
    const resp = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
    if (!resp.ok) {
      const errorEl = document.getElementById('sfError');
      if (errorEl) { errorEl.textContent = `HTTP ${resp.status}`; errorEl.style.display = 'block'; }
      return;
    }
    _scheduleFormDirty = false;
    closeScheduleForm();
    await loadSchedules();
  } catch (e) {
    console.warn('Failed to save schedule:', e);
  }
}

async function deleteScheduleFromForm() {
  if (!scheduleEditingId) return;
  if (!confirm(window.I18N.t('time.confirm_delete'))) return;
  try {
    const resp = await fetch(`/api/schedules/${encodeURIComponent(scheduleEditingId)}`, { method: 'DELETE' });
    if (!resp.ok) return;
    _scheduleFormDirty = false;
    closeScheduleForm();
    await loadSchedules();
  } catch (e) {
    console.warn('Failed to delete schedule:', e);
  }
}


// ─── Timers Card (Time & Timers card, #timeTimersSection) ────────────
// In-memory, session-scoped backend timers (meshsrv/timer_service.py) -
// reset on service restart, by design. The countdown itself ticks
// client-side (setInterval below); the backend only learns a timer
// finished when we POST /api/timers/<id>/finish at zero, which runs the
// same notify pipeline (push_notification + optional mesh send) Schedule
// Engine uses. Target picker reuses Stage 6's generic, prefix-based
// _renderTargetPicker()/_scheduleReadTarget() (static/chat.js, Schedules
// Card section above) directly with prefix 'tm' - those helpers were
// already written generic over any prefix, not schedule-specific, so no
// duplicate target-picker implementation is needed here.
let _timers = [];
let _timerTicks = {};

async function loadTimers() {
  try {
    const resp = await fetch('/api/timers');
    if (!resp.ok) return;
    _timers = await resp.json();
    renderTimersList();
  } catch (e) {
    console.warn('Timers load failed:', e);
  }
}

function renderTimersList() {
  const section = document.getElementById('timeTimersSection');
  if (section) section.style.display = 'block';

  const list = document.getElementById('timeTimersList');
  if (!list) return;

  if (!_timers.length) {
    list.innerHTML = `<div class="timers-empty" data-i18n="time.timers_empty">${escapeHtml(window.I18N.t('time.timers_empty'))}</div>`;
    return;
  }

  list.innerHTML = _timers.map(t => {
    const running = !t.stopped_at && !t.finished;
    const label = escapeHtml(t.label || 'Timer');
    return `
      <div class="timer-item ${t.finished ? 'timer-item--finished' : ''}" data-id="${escapeHtml(t.id)}">
        <span class="timer-item-label">${label}</span>
        <span class="timer-display" id="timer-display-${escapeHtml(t.id)}">${_formatTimerDisplay(t)}</span>
        <div class="timer-controls">
          ${running
            ? `<button class="btn btn-xs" onclick="stopTimer('${escapeHtml(t.id)}')" data-i18n="time.timer_stop">${escapeHtml(window.I18N.t('time.timer_stop'))}</button>`
            : `<button class="btn btn-xs" onclick="resetTimer('${escapeHtml(t.id)}')" data-i18n="time.timer_reset">${escapeHtml(window.I18N.t('time.timer_reset'))}</button>`}
          <button class="btn btn-xs btn-danger" title="${escapeHtml(window.I18N.t('time.timer_delete'))}" onclick="deleteTimer('${escapeHtml(t.id)}')">✕</button>
        </div>
      </div>`;
  }).join('');

  _timers.forEach(t => {
    if (!t.stopped_at && !t.finished) _startLocalTick(t);
  });
}

function _formatTimerDisplay(t) {
  const now = Math.floor(TimeFormatter.now().getTime() / 1000);
  const end = t.stopped_at || now;
  const elapsed = end - t.started_at;
  if (t.duration_s) {
    return _formatSeconds(Math.max(0, t.duration_s - elapsed));
  }
  return _formatSeconds(elapsed);
}

function _formatSeconds(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
  return `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
}

function _startLocalTick(timer) {
  if (_timerTicks[timer.id]) return;
  _timerTicks[timer.id] = setInterval(() => {
    const el = document.getElementById(`timer-display-${timer.id}`);
    if (!el) {
      clearInterval(_timerTicks[timer.id]);
      delete _timerTicks[timer.id];
      return;
    }
    const now = Math.floor(TimeFormatter.now().getTime() / 1000);
    const elapsed = now - timer.started_at;

    if (timer.duration_s) {
      const remaining = timer.duration_s - elapsed;
      el.textContent = _formatSeconds(Math.max(0, remaining));
      if (remaining <= 0 && !timer.finished) {
        timer.finished = true;
        clearInterval(_timerTicks[timer.id]);
        delete _timerTicks[timer.id];
        _onTimerFinished(timer.id);
      }
    } else {
      el.textContent = _formatSeconds(elapsed);
    }
  }, 1000);
}

async function _onTimerFinished(id) {
  await fetch(`/api/timers/${encodeURIComponent(id)}/finish`, { method: 'POST' });
  await loadTimers();
  await pollNotifications();
}

async function stopTimer(id) {
  await fetch(`/api/timers/${encodeURIComponent(id)}/stop`, { method: 'PATCH' });
  clearInterval(_timerTicks[id]);
  delete _timerTicks[id];
  await loadTimers();
}

async function resetTimer(id) {
  await fetch(`/api/timers/${encodeURIComponent(id)}/reset`, { method: 'PATCH' });
  clearInterval(_timerTicks[id]);
  delete _timerTicks[id];
  await loadTimers();
}

async function deleteTimer(id) {
  clearInterval(_timerTicks[id]);
  delete _timerTicks[id];
  await fetch(`/api/timers/${encodeURIComponent(id)}`, { method: 'DELETE' });
  await loadTimers();
}

// ─── Timer form preferences (mirrors the Waypoint composer's
// getWaypointComposerDefaults()/saveWaypointComposerDefaults() pattern,
// static/chat.js ~L5908-L6030: preferences persisted server-side via
// /api/settings under appSettings.<section>, profile-scoped through
// profile_defaults[<activeProfileId>], NOT localStorage. Only structural
// preferences are kept here (notify/mesh toggles, target type, selected
// channel/node) - name, duration and signal text stay one-time values,
// same rule the Waypoint form applies to name/description/coordinates. ──

let timerActiveProfileId = "";

function getTimerComposerDefaults(profileId = timerActiveProfileId) {
  const timerSettings = appSettings?.timers || {};
  const normalizedProfileId = normalizeWaypointProfileId(profileId);
  const profileDefaults = normalizedProfileId
    ? timerSettings?.profile_defaults?.[normalizedProfileId]
    : null;
  const saved = profileDefaults && typeof profileDefaults === 'object'
    ? profileDefaults
    : timerSettings;

  const channelIndex = Number(saved?.channel_index);

  return {
    notifyEnabled: Boolean(saved?.notify_enabled),
    meshEnabled: Boolean(saved?.mesh_enabled),
    targetType: saved?.target_type === 'channel' ? 'channel' : 'node',
    channelIndex: Number.isInteger(channelIndex) && channelIndex >= 0 && channelIndex <= 7
      ? channelIndex
      : 0,
    nodeId: String(saved?.node_id || '')
  };
}

async function loadTimerComposerContext() {
  try {
    const response = await fetch('/api/base_status', { cache: 'no-store' });
    if (response.ok) {
      const data = await response.json();
      timerActiveProfileId = normalizeWaypointProfileId(data?.profile_id);
    }
  } catch (error) {
    console.warn('[TIMER] Unable to load profile context:', error);
  }

  return getTimerComposerDefaults(timerActiveProfileId);
}

async function saveTimerComposerDefaults(notifyEnabled, meshEnabled, targetType, channelIndex, nodeId) {
  const profileId = normalizeWaypointProfileId(timerActiveProfileId);
  const prefs = {
    notify_enabled: Boolean(notifyEnabled),
    mesh_enabled: Boolean(meshEnabled),
    target_type: targetType === 'channel' ? 'channel' : 'node',
    channel_index: Number(channelIndex) || 0,
    node_id: String(nodeId || '')
  };

  const timerSettings = {
    ...(appSettings?.timers || {}),
    ...prefs,
    profile_defaults: {
      ...(appSettings?.timers?.profile_defaults || {})
    }
  };

  if (profileId) {
    timerSettings.profile_defaults[profileId] = { ...prefs };
  }

  appSettings = {
    ...(appSettings || {}),
    timers: timerSettings
  };

  try {
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({ timers: timerSettings })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data?.ok) {
      throw new Error(data?.error || 'Unable to save timer defaults');
    }
    appSettings = data.settings || appSettings;
  } catch (error) {
    console.warn('[TIMER] Unable to save composer defaults:', error);
  }
}

function _tfSaveComposerPrefs() {
  const notifyEnabled = document.getElementById('tfNotifyEnabled')?.checked || false;
  const meshEnabled = document.getElementById('tfNotifyAlsoMesh')?.checked || false;
  const target = _scheduleReadTarget('tm');
  saveTimerComposerDefaults(
    notifyEnabled,
    meshEnabled,
    target.target_type,
    target.channel_index,
    target.node_id
  );
}

// Attached once per openTimerForm() render, scoped to this form's own
// elements only - does not touch the shared _scheduleUpdateTargetVisibility()/
// _scheduleReadTarget() helpers, which are also used by the Schedule form's
// 'ms'/'nm' pickers that have no preference-persistence of their own.
function _tfAttachPrefsListeners() {
  document.getElementById('tfNotifyEnabled')
    ?.addEventListener('change', _tfSaveComposerPrefs);
  document.getElementById('tfNotifyAlsoMesh')
    ?.addEventListener('change', _tfSaveComposerPrefs);

  const picker = document.querySelector('.schedule-target-picker[data-prefix="tm"]');
  if (!picker) return;
  picker.querySelectorAll('input[name="tmTargetType"]').forEach(radio => {
    radio.addEventListener('change', _tfSaveComposerPrefs);
  });
  document.getElementById('tmNodeSelect')
    ?.addEventListener('change', _tfSaveComposerPrefs);
  document.getElementById('tmChannelSelect')
    ?.addEventListener('change', _tfSaveComposerPrefs);
}

// ─── Timer creation form (timerModal) ─────────────────────────────────

async function openTimerForm() {
  const modalBody = document.getElementById('timerModalBody');
  if (!modalBody) return;

  // Load the channel list (needed by _renderTargetPicker's channel <select>)
  // and the saved preferences before rendering, so the form opens already
  // showing the remembered state - same "populate before paint" intent as
  // openCreateWaypointDialog(), just synchronous-to-render here because the
  // whole form body (including the target picker) is one innerHTML write.
  await _scheduleLoadChannels();
  const defaults = await loadTimerComposerContext();

  modalBody.innerHTML = `
    <div class="schedule-form-row">
      <label data-i18n="time.timer_name">${escapeHtml(window.I18N.t('time.timer_name'))}</label>
      <input type="text" id="tfLabel" class="schedule-input" maxlength="80" placeholder="Timer">
    </div>

    <div class="schedule-form-row">
      <label data-i18n="time.timer_duration">${escapeHtml(window.I18N.t('time.timer_duration'))}</label>
      <div class="timer-duration-picker">
        <div class="timer-duration-field">
          <input type="number" id="tfHours" min="0" max="99" value="0" class="timer-duration-input">
          <label data-i18n="time.hours">${escapeHtml(window.I18N.t('time.hours'))}</label>
        </div>
        <span class="timer-duration-sep">:</span>
        <div class="timer-duration-field">
          <input type="number" id="tfMinutes" min="0" max="59" value="0" class="timer-duration-input">
          <label data-i18n="time.minutes_short">${escapeHtml(window.I18N.t('time.minutes_short'))}</label>
        </div>
        <span class="timer-duration-sep">:</span>
        <div class="timer-duration-field">
          <input type="number" id="tfSeconds" min="0" max="59" value="0" class="timer-duration-input">
          <label data-i18n="time.seconds_short">${escapeHtml(window.I18N.t('time.seconds_short'))}</label>
        </div>
      </div>
      <div class="schedule-inline-label" data-i18n="time.timer_duration_hint">${escapeHtml(window.I18N.t('time.timer_duration_hint'))}</div>
    </div>

    <div class="schedule-form-section">
      <label class="schedule-checkbox-row">
        <input type="checkbox" id="tfNotifyEnabled" onchange="_tfUpdateNotifyVisibility()"${defaults.notifyEnabled ? ' checked' : ''}>
        <span data-i18n="time.timer_notify">${escapeHtml(window.I18N.t('time.timer_notify'))}</span>
      </label>
      <div id="tfNotifyFields" style="display:${defaults.notifyEnabled ? '' : 'none'}">
        <div class="schedule-form-row">
          <label data-i18n="time.signal_label">${escapeHtml(window.I18N.t('time.signal_label'))}</label>
          <input type="text" id="tfSignal" class="schedule-input" maxlength="120" oninput="tfUpdateSignalCounter()">
          <div class="schedule-inline-label" id="tfSignalCounter">0/120</div>
        </div>
        <label class="schedule-checkbox-row">
          <input type="checkbox" id="tfNotifyAlsoMesh" onchange="_tfUpdateNotifyMeshVisibility()"${defaults.meshEnabled ? ' checked' : ''}>
          <span data-i18n="time.notify_also_mesh">${escapeHtml(window.I18N.t('time.notify_also_mesh'))}</span>
        </label>
        <div class="schedule-form-row schedule-action-params" id="tfNotifyMeshParams" style="display:${defaults.meshEnabled ? '' : 'none'}">
          ${_renderTargetPicker('tm', defaults.targetType, defaults.nodeId, defaults.channelIndex)}
        </div>
      </div>
    </div>

    <div class="schedule-form-error" id="tfError" style="display:none"></div>
  `;

  const modal = document.getElementById('timerModal');
  if (modal) modal.style.display = 'flex';

  _tfAttachPrefsListeners();
}

function closeTimerForm() {
  const modal = document.getElementById('timerModal');
  if (modal) modal.style.display = 'none';
}

function _tfUpdateNotifyVisibility() {
  const enabled = document.getElementById('tfNotifyEnabled')?.checked;
  const fields = document.getElementById('tfNotifyFields');
  if (fields) fields.style.display = enabled ? '' : 'none';
}

function _tfUpdateNotifyMeshVisibility() {
  const enabled = document.getElementById('tfNotifyAlsoMesh')?.checked;
  const fields = document.getElementById('tfNotifyMeshParams');
  if (fields) fields.style.display = enabled ? '' : 'none';
}

function tfUpdateSignalCounter() {
  const el = document.getElementById('tfSignal');
  const counter = document.getElementById('tfSignalCounter');
  if (el && counter) counter.textContent = `${el.value.length}/120`;
}

async function createTimerFromForm() {
  const errorEl = document.getElementById('tfError');
  const showError = (msg) => { if (errorEl) { errorEl.textContent = msg; errorEl.style.display = 'block'; } };

  const label = document.getElementById('tfLabel')?.value.trim() || 'Timer';
  const h = parseInt(document.getElementById('tfHours')?.value, 10) || 0;
  const m = parseInt(document.getElementById('tfMinutes')?.value, 10) || 0;
  const s = parseInt(document.getElementById('tfSeconds')?.value, 10) || 0;
  const duration_s = (h * 3600 + m * 60 + s) || null;

  const notifyEnabled = document.getElementById('tfNotifyEnabled')?.checked || false;
  const notify = {
    enabled: notifyEnabled,
    signal: document.getElementById('tfSignal')?.value.trim() || '',
    mesh_message: { enabled: false, target_type: 'node', node_id: '', channel_index: 0 }
  };

  if (notifyEnabled) {
    if (!notify.signal) {
      return showError(window.I18N.t('time.error_no_signal'));
    }
    const alsoMesh = document.getElementById('tfNotifyAlsoMesh')?.checked || false;
    if (alsoMesh) {
      // Real target picker (Stage 6 pattern), not a hardcoded placeholder -
      // reads whichever of node/channel is selected in the 'tm' picker.
      const target = _scheduleReadTarget('tm');
      notify.mesh_message = { enabled: true, ...target };
    }
  }

  if (errorEl) errorEl.style.display = 'none';

  try {
    const resp = await fetch('/api/timers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, duration_s, notify })
    });
    if (!resp.ok) {
      showError(`HTTP ${resp.status}`);
      return;
    }
    closeTimerForm();
    await loadTimers();
  } catch (e) {
    console.warn('Failed to create timer:', e);
  }
}

// ===== TELEMETRY =====
let telemetryData = {
    temperature: null,
    humidity: null,
    pressure: null,
    voltage: null,
    current: null,
    last_update: null
};
let telemetryHistory = [];
let telemetryChart = null;
let cpuUsageChart = null;
let cpuHistoryRange = '30m';
let cpuStatusTimer = null;
let cpuChartTimer = null;
let telemetryInterval = 300;
let telemetryUpdateInterval = null;
let telemetryTimeRange = 60;
let telemetryFullHistory = [];
const telemetryHistoryCache = new Map();
const TELEMETRY_HISTORY_CACHE_TTL_MS = 120000;
let batteryCurrentSamples = [];
let latestBatteryPercent = null;
let appSettings = {
    units: {
        temperature: "c",
        pressure: "hpa",
        wind: "ms"
    },
    maps: {
        provider: "osm"
    }
};

let waypointActiveProfileId = "";
let waypointComposerChannels = [];

// Active radio identity used to determine message ownership.  Message kind
// alone is not enough because saved profiles may contain historical records
// transmitted by another local radio.
let activeLocalNodeId = "";
let activeLocalProfileId = "";

const SENSOR_COLORS = {
    temperature: '#ef4444',
    humidity: '#3b82f6',
    pressure: '#facc15',
    voltage: '#22c55e',
    current: '#38bdf8',
    power: '#f97316'
};

const SENSOR_BG_COLORS = {
    temperature: 'rgba(239, 68, 68, 0.10)',
    humidity: 'rgba(59, 130, 246, 0.10)',
    pressure: 'rgba(250, 204, 21, 0.14)',
    voltage: 'rgba(34, 197, 94, 0.10)',
    current: 'rgba(56, 189, 248, 0.10)',
    power: 'rgba(249, 115, 22, 0.10)'
};

let telemetryVisibleSeries = {
    environment: {
        temperature: true,
        humidity: true,
        pressure: true
    },
    power: {
        voltage: true,
        current: true,
        power: true
    }
};

function celsiusToFahrenheit(c) {
    return (c * 9 / 5) + 32;
}

function formatTemperature(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return "--";
    }

    const unit = appSettings?.units?.temperature || "c";
    const c = Number(value);

    if (unit === "f") {
        return celsiusToFahrenheit(c).toFixed(1) + "°F";
    }

    if (unit === "both") {
        return c.toFixed(1) + "°C / " + celsiusToFahrenheit(c).toFixed(1) + "°F";
    }

    return c.toFixed(1) + "°C";
}

function temperatureChartUnit() {
    const unit = appSettings?.units?.temperature || "c";
    return unit === "f" ? "°F" : "°C";
}

function temperatureChartValue(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return null;
    }

    const unit = appSettings?.units?.temperature || "c";
    const c = Number(value);

    if (unit === "f") {
        return celsiusToFahrenheit(c);
    }

    return c;
}

function hPaToMmHg(hpa) {
    return hpa * 0.750061683;
}

function formatPressure(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return "--";
    }

    const unit = appSettings?.units?.pressure || "hpa";
    const hpa = Number(value);
    const mmhg = hPaToMmHg(hpa);

    if (unit === "mmhg") {
        return mmhg.toFixed(1) + " mmHg";
    }

    if (unit === "both") {
        return hpa.toFixed(1) + " hPa / " + mmhg.toFixed(1) + " mmHg";
    }

    return hpa.toFixed(1) + " hPa";
}

function pressureChartUnit() {
    const unit = appSettings?.units?.pressure || "hpa";
    return unit === "mmhg" ? "mmHg" : "hPa";
}

function pressureChartValue(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return null;
    }

    const unit = appSettings?.units?.pressure || "hpa";
    const hpa = Number(value);

    if (unit === "mmhg") {
        return hPaToMmHg(hpa);
    }

    return hpa;
}

function normalizeCoordinateInput(value) {
    return String(value ?? '')
        .trim()
        .replace(',', '.');
}

function getReferenceLocationFormState() {
    return JSON.stringify({
        mode:
            document.getElementById('referenceLocationMode')?.value
            || 'disabled',
        latitude: normalizeCoordinateInput(
            document.getElementById('referenceLatitude')?.value
        ),
        longitude: normalizeCoordinateInput(
            document.getElementById('referenceLongitude')?.value
        ),
        node_id:
            document.getElementById('referenceNodeId')?.value
            || ''
    });
}

function updateReferenceLocationSaveButton() {
    const button =
        document.getElementById('referenceLocationSaveButton');

    if (!button) {
        return;
    }

    const changed =
        getReferenceLocationFormState()
        !== referenceLocationInitialState;

    button.disabled = referenceLocationSaving || !changed;
    button.textContent = referenceLocationSaving
        ? window.I18N.t('weather.saving')
        : changed
            ? `💾 ${window.I18N.t('settings.save_reference_location')}`
            : `✓ ${window.I18N.t('settings.reference_location_saved')}`;
}

function markReferenceLocationStateSaved() {
    referenceLocationInitialState =
        getReferenceLocationFormState();
    updateReferenceLocationSaveButton();
}

function updateReferenceLocationFields() {
    const modeSelect =
        document.getElementById('referenceLocationMode');

    const manualFields =
        document.getElementById('referenceLocationManualFields');

    const nodeFields =
        document.getElementById('referenceLocationNodeFields');

    const mode = modeSelect?.value || 'disabled';

    if (manualFields) {
        manualFields.style.display =
            mode === 'manual'
                ? 'grid'
                : 'none';
    }

    if (nodeFields) {
        nodeFields.style.display =
            mode === 'node'
                ? 'grid'
                : 'none';
    }

    updateReferenceLocationSaveButton();
}

function populateReferenceNodeSelect() {
    const select =
        document.getElementById('referenceNodeId');

    if (!select) {
        return;
    }

    const savedNodeId = String(
        appSettings?.reference_location?.node_id || ''
    );

    const currentValue = String(
        select.value || savedNodeId || ''
    );

    select.innerHTML = '';

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = window.I18N.t('settings.select_node');
    select.appendChild(placeholder);

    const availableNodes = Array.isArray(nodeCache)
        ? nodeCache.filter(node =>
            node
            && node.node_id
            && Number.isFinite(
                Number(node?.position?.latitude)
            )
            && Number.isFinite(
                Number(node?.position?.longitude)
            )
        )
        : [];

    availableNodes.sort((a, b) => {
        const nameA = String(
            a.clean_name
            || a.name
            || a.long_name
            || a.node_id
            || ''
        );

        const nameB = String(
            b.clean_name
            || b.name
            || b.long_name
            || b.node_id
            || ''
        );

        return nameA.localeCompare(nameB);
    });

    for (const node of availableNodes) {
        const option = document.createElement('option');

        option.value = node.node_id;

        option.textContent =
            `📍 ${
                node.clean_name
                || node.name
                || node.long_name
                || node.short_name
                || node.node_id
            }`;

        select.appendChild(option);
    }

    /*
     * Если настройки загрузились раньше списка нод,
     * всё равно временно добавляем сохранённую ноду.
     * После загрузки nodeCache список будет построен заново.
     */
    if (
        savedNodeId
        && ![...select.options].some(
            option => option.value === savedNodeId
        )
    ) {
        const savedNode = Array.isArray(nodeCache)
            ? nodeCache.find(
                node => node.node_id === savedNodeId
            )
            : null;

        const option = document.createElement('option');

        option.value = savedNodeId;

        option.textContent =
            `📍 ${
                savedNode?.clean_name
                || savedNode?.name
                || savedNode?.long_name
                || savedNode?.short_name
                || savedNodeId
            }`;

        select.appendChild(option);
    }

    const valueToRestore =
        savedNodeId || currentValue;

    if (
        valueToRestore
        && [...select.options].some(
            option => option.value === valueToRestore
        )
    ) {
        select.value = valueToRestore;
    }
}

function getReferenceLocation() {
    const reference =
        appSettings?.reference_location || {};

    const mode =
        reference.mode || 'disabled';

    if (mode === 'manual') {
        const latitude =
            Number(reference?.manual?.latitude);

        const longitude =
            Number(reference?.manual?.longitude);

        if (
            Number.isFinite(latitude)
            && Number.isFinite(longitude)
        ) {
            return {
                mode: 'manual',
                latitude,
                longitude,
                name:
                    String(reference.place_name || '').trim()
                    || window.I18N.t('settings.manual_position')
            };
        }

        return null;
    }

    if (mode === 'node') {
        const nodeId =
            String(reference.node_id || '');

        if (!nodeId) {
            return null;
        }

        const normalizedNodeId = nodeId.trim().toLowerCase();

        const referenceNode = nodeCache.find(node => {
            const candidateId = String(
                node?.node_id
                || node?.id
                || node?.user?.id
                || ''
            ).trim().toLowerCase();

            return candidateId === normalizedNodeId;
        });

        if (!referenceNode) {
            return {
                mode: 'node',
                node_id: nodeId,
                name: nodeId,
                latitude: null,
                longitude: null
            };
        }

        const latitude = Number(
            referenceNode?.position?.latitude
            ?? referenceNode?.latitude
            ?? referenceNode?.lat
        );

        const longitude = Number(
            referenceNode?.position?.longitude
            ?? referenceNode?.longitude
            ?? referenceNode?.lon
            ?? referenceNode?.lng
        );

        return {
            mode: 'node',
            node_id: nodeId,

            name:
                referenceNode.clean_name
                || referenceNode.name
                || nodeId,

            latitude:
                Number.isFinite(latitude)
                    ? latitude
                    : null,

            longitude:
                Number.isFinite(longitude)
                    ? longitude
                    : null
        };
    }

    return null;
}

function updateReferenceLocationSummary() {
    const nameElement = document.getElementById('weatherLocation');
    const coordinatesElement = document.getElementById('weatherCoordinates');
    const locationButton = document.getElementById('baseReferenceLocation');
    const reference = getReferenceLocation();

    if (!nameElement || !coordinatesElement) return;

    if (!reference) {
        nameElement.textContent = `📍 ${window.I18N.t('settings.location_not_configured')}`;
        coordinatesElement.textContent = window.I18N.t('settings.click_to_configure');
        locationButton?.classList.add('reference-is-disabled');
        return;
    }

    locationButton?.classList.remove('reference-is-disabled');
    const hasCoordinates = Number.isFinite(reference.latitude) && Number.isFinite(reference.longitude);
    const placeName =
        String(appSettings?.reference_location?.place_name || '').trim()
        || String(reference.name || '').trim()
        || window.I18N.t('settings.reference_location');
    nameElement.textContent = `📍 ${placeName}`;
    coordinatesElement.textContent = hasCoordinates
        ? `${reference.latitude.toFixed(5)} • ${reference.longitude.toFixed(5)}`
        : window.I18N.t('settings.position_unavailable');
}

function openReferenceSettings() {
    switchMainTab('settings');

    window.setTimeout(() => {
        const card =
            document.querySelector('.reference-location-card');

        if (card) {
            card.scrollIntoView({
                behavior: 'smooth',
                block: 'center'
            });

            card.classList.add(
                'reference-location-card-highlight'
            );

            window.setTimeout(() => {
                card.classList.remove(
                    'reference-location-card-highlight'
                );
            }, 1200);
        }
    }, 80);
}

function openWeatherProviderSettings() {
    switchMainTab('settings');

    window.setTimeout(() => {
        const card =
            document.querySelector('.weather-provider-card');

        if (card) {
            card.scrollIntoView({
                behavior: 'smooth',
                block: 'center'
            });

            card.classList.add(
                'reference-location-card-highlight'
            );

            window.setTimeout(() => {
                card.classList.remove(
                    'reference-location-card-highlight'
                );
            }, 1200);
        }
    }, 80);
}

async function loadSettings() {
    try {
        const response = await fetch("/api/settings");
        const data = await response.json();

        if (data.ok && data.settings) {
            appSettings = data.settings;
            console.log("[SETTINGS] Loaded:", appSettings);
        }
    } catch (error) {
        console.warn("[SETTINGS] Failed to load:", error);
    }

    updateSettingsUi();
}

function notifySettingsUpdated() {
    document.dispatchEvent(new CustomEvent('meshcenter:settings-updated', {
        detail: { settings: appSettings }
    }));
}

function updateSettingsUi() {
    const languageSelect = document.getElementById('uiLanguage');
    if (languageSelect) {
        languageSelect.value = appSettings?.language || 'auto';
    }

    const units = appSettings?.units || {};

    document.getElementById('unitTempC')?.classList.toggle('active', units.temperature === 'c');
    document.getElementById('unitTempF')?.classList.toggle('active', units.temperature === 'f');

    document.getElementById('unitPressureHpa')?.classList.toggle('active', units.pressure === 'hpa');
    document.getElementById('unitPressureMmhg')?.classList.toggle('active', units.pressure === 'mmhg');

    updateTimeFormatButtons();

    const batteryCapacityInput = document.getElementById('batteryCapacityMah');
    if (batteryCapacityInput) {
        batteryCapacityInput.value = appSettings?.power?.battery_capacity_mah || 3000;
    }

    const recovery = appSettings?.listener_autorecovery || {};

    const enabled = !!recovery.enabled;
    const delay = recovery.delay || 60;

    const checkbox = document.getElementById("listenerRecoveryEnabled");
    const select = document.getElementById("listenerRecoveryDelay");

    if (checkbox)
        checkbox.checked = enabled;

    if (select) {
        select.value = delay;
        select.disabled = !enabled;
    }

    const mapProvider =
    appSettings?.maps?.provider || 'osm';

    const mapProviderSelect =
        document.getElementById('mapProvider');

    if (mapProviderSelect) {
        mapProviderSelect.value = mapProvider;
    }

    const weatherProviderSelect =
        document.getElementById('weatherProviderSelect');

    if (weatherProviderSelect) {
        weatherProviderSelect.value = appSettings?.weather?.provider || 'openweather';
    }

    const referenceLocation =
    appSettings?.reference_location || {};

    const referenceMode =
        referenceLocation.mode || 'disabled';

    const referenceLocationMode =
        document.getElementById('referenceLocationMode');

    const referenceLatitude =
        document.getElementById('referenceLatitude');

    const referenceLongitude =
        document.getElementById('referenceLongitude');

    const referenceNodeId =
        document.getElementById('referenceNodeId');

    if (referenceLocationMode) {
        referenceLocationMode.value = referenceMode;
    }

    const manualLocation =
        referenceLocation.manual || {};

    if (referenceLatitude) {
        referenceLatitude.value =
            manualLocation.latitude ?? '';
    }

    if (referenceLongitude) {
        referenceLongitude.value =
            manualLocation.longitude ?? '';
    }

    populateReferenceNodeSelect();

    if (referenceNodeId) {
        referenceNodeId.value =
            referenceLocation.node_id || '';
    }

    updateReferenceLocationFields();
    updateReferenceLocationSummary();
    markReferenceLocationStateSaved();
    notifySettingsUpdated();
}

// ===== E-PAPER DISPLAY SETTINGS (e-Paper Stage 1 plan, Phase 7) =====
// enabled/refresh_mode/debounce_seconds apply live and autosave on
// change, matching every other setting on this page. pins/spi/timeout
// ("Advanced") deliberately do NOT autosave - a typo there risks
// reproducing the BUSY-hang debugging from Phases 1-2, this time on a
// live device instead of at wiring time. Those only ever apply through
// epaperReinitDisplay(), which is a single explicit action gated by the
// GPIO conflict check and a synchronous re-init confirmation on the
// server before anything is persisted.

async function loadEpaperSettings() {
    const card = document.getElementById('epaperSettingsCard');
    if (!card) return;

    try {
        const response = await fetch('/api/hardware/display/settings', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || !data.ok) {
            card.style.display = 'none';
            return;
        }
        card.style.display = '';

        const config = data.config || {};
        const enabledToggle = document.getElementById('epaperEnabledToggle');
        const modeSelect = document.getElementById('epaperRefreshMode');
        const debounceInput = document.getElementById('epaperDebounceSeconds');
        if (enabledToggle) enabledToggle.checked = !!config.enabled;
        if (modeSelect) modeSelect.value = config.refresh_mode || 'debounce';
        if (debounceInput) debounceInput.value = config.debounce_seconds ?? 30;

        window.epaperAvailableModels = data.available_models || [];
        const modelSelect = document.getElementById('epaperModelSelect');
        if (modelSelect) {
            modelSelect.innerHTML = window.epaperAvailableModels
                .map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.display_name)}</option>`)
                .join('');
            modelSelect.value = config.model || '';
        }

        const pins = config.pins || {};
        const spi = config.spi || {};
        const setField = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.value = value ?? '';
        };
        setField('epaperPinRst', pins.rst);
        setField('epaperPinDc', pins.dc);
        setField('epaperPinCs', pins.cs);
        setField('epaperPinBusy', pins.busy);
        setField('epaperPinPwr', pins.pwr);
        setField('epaperSpiBus', spi.bus);
        setField('epaperSpiDevice', spi.device);
        setField('epaperRefreshTimeout', config.refresh_timeout);
    } catch (error) {
        console.error('[EPAPER] Failed to load settings:', error);
        card.style.display = 'none';
    }
}

async function _epaperPostSettings(payload) {
    try {
        const response = await fetch('/api/hardware/display/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Request failed');
        return true;
    } catch (error) {
        console.error('[EPAPER] Settings update failed:', error);
        showToast(window.I18N.t('settings.epaper_settings_failed'), 'error');
        return false;
    }
}

function setEpaperEnabled(enabled) {
    _epaperPostSettings({ enabled });
}

function setEpaperRefreshMode(mode) {
    const debounceInput = document.getElementById('epaperDebounceSeconds');
    _epaperPostSettings({
        refresh_mode: mode,
        debounce_seconds: debounceInput ? Number(debounceInput.value) : undefined,
    });
}

function setEpaperDebounceSeconds(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return;
    _epaperPostSettings({ debounce_seconds: seconds });
}

function epaperModelChanged(modelId) {
    // Prefills the pin fields with the newly-selected model's own
    // defaults, for review before "Apply & Re-init" - does NOT save or
    // reinit by itself (e-Paper Stage 2 plan, Phase 4: a model switch
    // changes DisplayCapabilities, same explicit-confirm treatment as a
    // GPIO/SPI change, never autosaved).
    const models = window.epaperAvailableModels || [];
    const model = models.find(m => m.id === modelId);
    if (!model) return;

    const pins = model.default_pins || {};
    const setField = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.value = value ?? '';
    };
    setField('epaperPinRst', pins.rst);
    setField('epaperPinDc', pins.dc);
    setField('epaperPinCs', pins.cs);
    setField('epaperPinBusy', pins.busy);
    setField('epaperPinPwr', pins.pwr);
}

async function _epaperTriggerAction(path, button, busyLabel) {
    if (button) {
        button.disabled = true;
        button.dataset.originalText = button.dataset.originalText || button.innerHTML;
    }
    try {
        const response = await fetch(path, { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Request failed');
        showToast(window.I18N.t('settings.epaper_action_queued'), 'success');
    } catch (error) {
        console.error(`[EPAPER] ${path} failed:`, error);
        showToast(window.I18N.t('settings.epaper_settings_failed'), 'error');
    } finally {
        if (button) button.disabled = false;
    }
}

function epaperTestDisplay(button) {
    _epaperTriggerAction('/api/hardware/display/test', button);
}

function epaperClearDisplay(button) {
    _epaperTriggerAction('/api/hardware/display/clear', button);
}

function epaperRefreshDisplay(button) {
    _epaperTriggerAction('/api/hardware/display/refresh', button);
}

async function epaperReinitDisplay(button) {
    const statusEl = document.getElementById('epaperReinitStatus');
    const getInt = id => {
        const el = document.getElementById(id);
        const value = el ? parseInt(el.value, 10) : NaN;
        return Number.isFinite(value) ? value : undefined;
    };
    const getFloat = id => {
        const el = document.getElementById(id);
        const value = el ? parseFloat(el.value) : NaN;
        return Number.isFinite(value) ? value : undefined;
    };

    const modelSelect = document.getElementById('epaperModelSelect');

    const payload = {
        model: modelSelect ? modelSelect.value : undefined,
        pins: {
            rst: getInt('epaperPinRst'),
            dc: getInt('epaperPinDc'),
            cs: getInt('epaperPinCs'),
            busy: getInt('epaperPinBusy'),
            pwr: getInt('epaperPinPwr'),
        },
        spi: {
            bus: getInt('epaperSpiBus'),
            device: getInt('epaperSpiDevice'),
        },
        refresh_timeout: getFloat('epaperRefreshTimeout'),
    };

    if (button) button.disabled = true;
    if (statusEl) {
        statusEl.textContent = window.I18N.t('settings.epaper_reinit_in_progress');
        statusEl.className = 'reference-location-status';
    }

    try {
        const response = await fetch('/api/hardware/display/reinit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Re-init failed');
        if (statusEl) {
            statusEl.textContent = window.I18N.t('settings.epaper_reinit_success');
            statusEl.className = 'reference-location-status reference-location-status-ok';
        }
        showToast(window.I18N.t('settings.epaper_reinit_success'), 'success');
    } catch (error) {
        console.error('[EPAPER] Re-init failed:', error);
        if (statusEl) {
            statusEl.textContent = `${window.I18N.t('settings.epaper_reinit_failed')}: ${error.message || error}`;
            statusEl.className = 'reference-location-status reference-location-status-error';
        }
        showToast(window.I18N.t('settings.epaper_reinit_failed'), 'error');
    } finally {
        if (button) button.disabled = false;
    }
}

async function epaperShowPage(page, button) {
    if (button) button.disabled = true;
    try {
        const response = await fetch(`/api/hardware/display/show/${encodeURIComponent(page)}`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Request failed');
        showToast(window.I18N.t('settings.epaper_action_queued'), 'success');
    } catch (error) {
        console.error(`[EPAPER] show/${page} failed:`, error);
        showToast(window.I18N.t('settings.epaper_settings_failed'), 'error');
    } finally {
        if (button) button.disabled = false;
    }
}

function degreesToRadians(value) {
    return value * Math.PI / 180;
}

function calculateDistanceMeters(
    latitude1,
    longitude1,
    latitude2,
    longitude2
) {
    const earthRadius = 6371000;

    const lat1 =
        degreesToRadians(latitude1);

    const lat2 =
        degreesToRadians(latitude2);

    const deltaLatitude =
        degreesToRadians(latitude2 - latitude1);

    const deltaLongitude =
        degreesToRadians(longitude2 - longitude1);

    const a =
        Math.sin(deltaLatitude / 2) ** 2
        + Math.cos(lat1)
        * Math.cos(lat2)
        * Math.sin(deltaLongitude / 2) ** 2;

    const c =
        2 * Math.atan2(
            Math.sqrt(a),
            Math.sqrt(1 - a)
        );

    return earthRadius * c;
}

function formatNodeDistance(distanceMeters) {
    if (!Number.isFinite(distanceMeters)) {
        return '--';
    }

    if (distanceMeters < 1000) {
        return `${Math.round(distanceMeters)} m`;
    }

    if (distanceMeters < 10000) {
        return `${(distanceMeters / 1000).toFixed(2)} km`;
    }

    return `${(distanceMeters / 1000).toFixed(1)} km`;
}

function calculateBearingDegrees(
    latitude1,
    longitude1,
    latitude2,
    longitude2
) {
    const lat1 =
        degreesToRadians(latitude1);

    const lat2 =
        degreesToRadians(latitude2);

    const deltaLongitude =
        degreesToRadians(longitude2 - longitude1);

    const y =
        Math.sin(deltaLongitude)
        * Math.cos(lat2);

    const x =
        Math.cos(lat1) * Math.sin(lat2)
        - Math.sin(lat1)
        * Math.cos(lat2)
        * Math.cos(deltaLongitude);

    const bearing =
        Math.atan2(y, x) * 180 / Math.PI;

    return (bearing + 360) % 360;
}

function getBearingDirection(bearing) {
    if (!Number.isFinite(bearing)) {
        return '--';
    }

    const directions = [
        'N',
        'NE',
        'E',
        'SE',
        'S',
        'SW',
        'W',
        'NW'
    ];

    const index =
        Math.round(bearing / 45) % 8;

    return directions[index];
}

function getNodeDistanceAndBearing(
    latitude,
    longitude
) {
    const reference =
        getReferenceLocation();

    if (
        !reference
        || !Number.isFinite(reference.latitude)
        || !Number.isFinite(reference.longitude)
    ) {
        return {
            distanceText: '--',
            bearingText: '--'
        };
    }

    const distanceMeters =
        calculateDistanceMeters(
            reference.latitude,
            reference.longitude,
            latitude,
            longitude
        );

    if (distanceMeters < 1) {
        return {
            distanceText: '0 m',
            bearingText: window.I18N.t('nodes.at_reference_location')
        };
    }

    const bearing =
        calculateBearingDegrees(
            reference.latitude,
            reference.longitude,
            latitude,
            longitude
        );

    return {
        distanceText:
            formatNodeDistance(distanceMeters),

        bearingText:
            `${Math.round(bearing)}° `
            + getBearingDirection(bearing)
    };
}


function getBearingArrow(bearing) {
    if (!Number.isFinite(bearing)) {
        return '';
    }

    const arrows = [
        '↑',
        '↗',
        '→',
        '↘',
        '↓',
        '↙',
        '←',
        '↖'
    ];

    return arrows[
        Math.round(bearing / 45) % 8
    ];
}

function getNodeMapBadgeClass(distanceMeters) {
    if (!Number.isFinite(distanceMeters)) {
        return 'node-map-badge-neutral';
    }

    if (distanceMeters < 1000) {
        return 'node-map-badge-near';
    }

    if (distanceMeters < 5000) {
        return 'node-map-badge-medium';
    }

    if (distanceMeters < 20000) {
        return 'node-map-badge-far';
    }

    return 'node-map-badge-very-far';
}

function renderNodeMapBadge(node) {
    const latitude = Number(node?.position?.latitude);
    const longitude = Number(node?.position?.longitude);
    const hasCoordinates =
        Number.isFinite(latitude)
        && Number.isFinite(longitude);

    // No coordinates: do not reserve space and do not show a disabled control.
    if (!hasCoordinates) {
        return '';
    }

    const reference = getReferenceLocation();
    let distanceText = '--';
    let bearingText = window.I18N.t('nodes.no_reference');
    let mapTitle = window.I18N.t('nodes.open_position_on_map');
    let badgeClass = 'node-map-badge-neutral';

    if (
        reference
        && Number.isFinite(reference.latitude)
        && Number.isFinite(reference.longitude)
    ) {
        const distanceMeters = calculateDistanceMeters(
            reference.latitude,
            reference.longitude,
            latitude,
            longitude
        );

        badgeClass = getNodeMapBadgeClass(distanceMeters);

        if (distanceMeters < 1) {
            distanceText = '0 m';
            bearingText = window.I18N.t('nodes.at_reference_location');
            mapTitle = window.I18N.t('nodes.reference_position');
        } else {
            const bearing = calculateBearingDegrees(
                reference.latitude,
                reference.longitude,
                latitude,
                longitude
            );
            const direction = getBearingDirection(bearing);
            distanceText = formatNodeDistance(distanceMeters);
            bearingText = `${getBearingArrow(bearing)} ${Math.round(bearing)}° ${direction}`;
            mapTitle = `${distanceText}, ${Math.round(bearing)}° ${direction}`;
        }
    }

    return `
        <button type="button"
                class="node-map-badge node-map-badge-available ${badgeClass}"
                title="${escapeHtml(mapTitle)}"
                aria-label="${escapeHtml(mapTitle)}"
                onclick="event.stopPropagation(); openNodeMap(${latitude}, ${longitude})">
            <span class="node-map-distance"
                  title="${escapeHtml(window.I18N.t('nodes.distance_from_reference', { distance: distanceText }))}">${escapeHtml(distanceText)}</span>
            <span class="node-map-bearing">${escapeHtml(bearingText)}</span>
        </button>
    `;
}

async function resolveReferencePlaceName(latitude, longitude) {
    try {
        const response = await fetch(
            '/api/settings/reference-location-name',
            {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ latitude, longitude })
            }
        );

        if (!response.ok) {
            return '';
        }

        const data = await response.json();
        return data.ok ? String(data.place_name || '') : '';
    } catch (error) {
        console.warn('[REFERENCE] Place lookup failed:', error);
        return '';
    }
}

async function saveReferenceLocation() {
    if (referenceLocationSaving) {
        return;
    }

    const modeSelect =
        document.getElementById('referenceLocationMode');
    const latitudeInput =
        document.getElementById('referenceLatitude');
    const longitudeInput =
        document.getElementById('referenceLongitude');
    const nodeSelect =
        document.getElementById('referenceNodeId');
    const statusElement =
        document.getElementById('referenceLocationStatus');

    const mode = modeSelect?.value || 'disabled';
    const latitudeValue = normalizeCoordinateInput(latitudeInput?.value);
    const longitudeValue = normalizeCoordinateInput(longitudeInput?.value);
    const nodeId = nodeSelect?.value || '';

    const savedReference =
        appSettings?.reference_location || {};
    const savedManual = savedReference.manual || {};

    const parseSavedCoordinate = value => {
        if (value === null || value === undefined || value === '') {
            return null;
        }

        const parsed = Number.parseFloat(
            normalizeCoordinateInput(value)
        );

        return Number.isFinite(parsed) ? parsed : null;
    };

    const referenceLocation = {
        mode,
        manual: {
            latitude: parseSavedCoordinate(savedManual.latitude),
            longitude: parseSavedCoordinate(savedManual.longitude)
        },
        node_id: savedReference.node_id || '',
        place_name: ''
    };

    let activeLatitude = null;
    let activeLongitude = null;

    if (mode === 'manual') {
        if (latitudeValue === '' || longitudeValue === '') {
            showToast(`❌ ${window.I18N.t('settings.enter_both_coordinates')}`, 'error');
            return;
        }

        const latitude = Number.parseFloat(latitudeValue);
        const longitude = Number.parseFloat(longitudeValue);

        if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) {
            showToast(`❌ ${window.I18N.t('settings.invalid_latitude')}`, 'error');
            return;
        }

        if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
            showToast(`❌ ${window.I18N.t('settings.invalid_longitude')}`, 'error');
            return;
        }

        referenceLocation.manual.latitude = latitude;
        referenceLocation.manual.longitude = longitude;
        activeLatitude = latitude;
        activeLongitude = longitude;

        if (latitudeInput) latitudeInput.value = String(latitude);
        if (longitudeInput) longitudeInput.value = String(longitude);
    }

    if (mode === 'node') {
        if (!nodeId) {
            showToast(`❌ ${window.I18N.t('settings.select_reference_node')}`, 'error');
            return;
        }

        const referenceNode = nodeCache.find(
            node => node.node_id === nodeId
        );

        activeLatitude = Number(referenceNode?.position?.latitude);
        activeLongitude = Number(referenceNode?.position?.longitude);

        if (!Number.isFinite(activeLatitude) || !Number.isFinite(activeLongitude)) {
            showToast(`❌ ${window.I18N.t('settings.node_no_valid_position')}`, 'error');
            return;
        }

        referenceLocation.node_id = nodeId;
    }

    referenceLocationSaving = true;
    updateReferenceLocationSaveButton();

    if (statusElement) {
        statusElement.textContent = window.I18N.t('settings.saving_reference_location');
    }

    if (Number.isFinite(activeLatitude) && Number.isFinite(activeLongitude)) {
        referenceLocation.place_name = await resolveReferencePlaceName(
            activeLatitude,
            activeLongitude
        );
    }

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                reference_location: referenceLocation
            })
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        appSettings = data.settings;
        updateSettingsUi();
        resetNodeRenderCache();
        await loadMessages();

        const selectedNode = nodeCache.find(
            node => node.node_id === currentChatId
        );

        if (selectedNode) {
            renderNodeDetails(selectedNode);
        }

        if (statusElement) {
            statusElement.textContent = window.I18N.t('settings.reference_location_saved');
        }

        showToast(`✅ ${window.I18N.t('settings.reference_location_saved')}`, 'success');
    } catch (error) {
        if (statusElement) {
            statusElement.textContent = window.I18N.t('settings.save_failed_reason', { reason: error.message });
        }

        showToast(
            `❌ ${window.I18N.t('settings.unable_to_save_reference_location', { reason: error.message })}`,
            'error'
        );
    } finally {
        referenceLocationSaving = false;
        updateReferenceLocationSaveButton();
    }
}

async function setMapProvider(provider) {
    const normalizedProvider =
        provider === 'google'
            ? 'google'
            : 'osm';

    const providerName =
        normalizedProvider === 'google'
            ? 'Google Maps'
            : 'OpenStreetMap';

    const maps = {
        ...(appSettings?.maps || {}),
        provider: normalizedProvider
    };

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                maps
            })
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            requestError.params = data.error_params || undefined;
            throw requestError;
        }

        appSettings = data.settings;
        updateSettingsUi();

        const currentNode = nodeCache.find(
            node => node.node_id === currentChatId
        );

        if (currentNode) {
            renderNodeDetails(currentNode);
        }

        showToast(
            `✅ ${window.I18N.t('settings.map_provider_set', { name: providerName })}`,
            'success'
        );

    } catch (error) {
        showToast(
            `❌ ${window.I18N.t('settings.unable_to_save_map_provider', { reason: translateRequestError(error) })}`,
            'error'
        );
    }
}

async function setWeatherProvider(provider) {
    const normalizedProvider =
        provider === 'weatherapi'
            ? 'weatherapi'
            : 'openweather';

    const providerName =
        normalizedProvider === 'weatherapi'
            ? 'WeatherAPI'
            : 'OpenWeather';

    const weather = {
        ...(appSettings?.weather || {}),
        provider: normalizedProvider
    };

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                weather
            })
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            requestError.params = data.error_params || undefined;
            throw requestError;
        }

        appSettings = data.settings;
        updateSettingsUi();

        const apiKeyInput = document.getElementById('weatherProviderApiKeyInput');
        if (apiKeyInput) apiKeyInput.value = '';
        await window.loadWeatherProviderStatus?.();

        showToast(
            `✅ ${window.I18N.t('settings.weather_provider_set', { name: providerName })}`,
            'success'
        );

    } catch (error) {
        showToast(
            `❌ ${window.I18N.t('settings.unable_to_save_weather_provider', { reason: translateRequestError(error) })}`,
            'error'
        );
    }
}

async function setLanguageSetting(value) {
    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language: value })
        });

        const data = await response.json();

        if (!data.ok) {
            alert(window.I18N.t('settings.unable_to_save_settings', { reason: data.error || window.I18N.t('errors.unknown_error') }));
            return;
        }

        // The server resolves "auto" and picks <html lang>/the initial
        // catalog at render time, and nothing here does a live re-render
        // yet (Stage 3+), so a full reload is the simplest correct way to
        // apply the new language.
        window.location.reload();
    } catch (error) {
        alert(window.I18N.t('settings.unable_to_save_settings', { reason: error.message }));
    }
}

function setTimeFormat(fmt) {
    return setUnitSetting('time_format', fmt);
}

function updateTimeFormatButtons() {
    const fmt = appSettings?.units?.time_format || '24';
    document.getElementById('timeFormat24Btn')?.classList.toggle('active', fmt === '24');
    document.getElementById('timeFormat12Btn')?.classList.toggle('active', fmt === '12');
}

async function setUnitSetting(name, value) {
    const units = {
        ...(appSettings?.units || {}),
        [name]: value
    };

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ units })
        });

        const data = await response.json();

        if (!data.ok) {
            alert(window.I18N.t('settings.unable_to_save_settings', { reason: data.error || window.I18N.t('errors.unknown_error') }));
            return;
        }

        appSettings = data.settings;
        updateSettingsUi();

        if (typeof loadSensors === 'function') loadSensors();
        if (typeof loadTelemetry === 'function') loadTelemetry();

        const modal = document.getElementById('telemetryModal');
        if (modal && modal.style.display !== 'none') {
            const type = modal.dataset.type || 'environment';
            renderTelemetryWithRange(type, telemetryTimeRange);
        }

    } catch (error) {
        alert(window.I18N.t('settings.unable_to_save_settings', { reason: error.message }));
    }
}

async function updateListenerRecoverySettings() {

    const enabled =
        document.getElementById("listenerRecoveryEnabled").checked;

    const delay =
        parseInt(
            document.getElementById("listenerRecoveryDelay").value
        );

    document.getElementById("listenerRecoveryDelay").disabled =
        !enabled;

    const listener_autorecovery = {
        enabled,
        delay
    };

    try {

        const response = await fetch("/api/settings", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

            body: JSON.stringify({
                listener_autorecovery
            })

        });

        const data = await response.json();

        if (!data.ok) {

            showToast(window.I18N.t('settings.unable_to_save_settings_short'), "error");
            return;

        }

        appSettings = data.settings;

        updateSettingsUi();

        showToast(
            window.I18N.t('settings.listener_recovery_updated'),
            "success"
        );

    }

    catch (e) {

        showToast(e.message, "error");

    }

}

// ===== PHOTO =====
let photoPreviewResolution = '640x480';
let photoSaveResolution = '3280x2464';
let currentPhotoQuality = 85;
let currentPhotoData = null;

// ===== MESSAGE CACHE =====
let messageCache = {};
let currentLoadRequest = null;
let currentMessageAbortController = null;
const CACHE_TTL = 30000;
const ACTIVE_CHAT_POLL_INTERVAL_MS = 2000;

// ===== RENDER SIGNATURES =====
let lastRenderedSignature = {};

// ============================================================
// NOTIFICATION CENTER / STATUS DOCK
// ============================================================
const NOTIFICATION_STORAGE_KEY = 'meshcenter.notifications.v2';
const NOTIFICATION_MAX_ITEMS = 100;
const NOTIFICATION_VISIBLE_MS = {
    success: 3200,
    info: 3800,
    warning: 5200,
    error: 7000
};

let notificationItems = [];
let notificationCurrent = null;
let notificationTimer = null;
let notificationDockBaseline = {
    message: 'Ready',
    type: 'online'
};

function normalizeNotificationType(type) {
    return ['success', 'warning', 'error', 'info', 'progress'].includes(type)
        ? type
        : 'info';
}

function notificationIcon(type) {
    return ({
        success: '✓',
        warning: '⚠',
        error: '✕',
        info: 'ⓘ',
        progress: '…'
    })[type] || 'ⓘ';
}

function cleanNotificationMessage(message) {
    return String(message ?? '')
        .replace(/^[✅✓☑️]+\s*/u, '')
        .replace(/^[❌✕⛔]+\s*/u, '')
        .replace(/^[⚠️]+\s*/u, '')
        .replace(/^[ℹ️ⓘ]+\s*/u, '')
        .trim();
}

function loadNotificationHistory() {
    try {
        const saved = JSON.parse(
            sessionStorage.getItem(NOTIFICATION_STORAGE_KEY) || '[]'
        );
        notificationItems = Array.isArray(saved)
            ? saved.slice(0, NOTIFICATION_MAX_ITEMS)
            : [];
    } catch (_) {
        notificationItems = [];
    }
    renderNotificationCenter();
    renderDockNotification();
}

function saveNotificationHistory() {
    try {
        sessionStorage.setItem(
            NOTIFICATION_STORAGE_KEY,
            JSON.stringify(notificationItems.slice(0, NOTIFICATION_MAX_ITEMS))
        );
    } catch (_) {}
}

function addNotification(message, type = 'info', options = {}) {
    const item = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        message: cleanNotificationMessage(message),
        type: normalizeNotificationType(type),
        timestamp: Date.now(),
        read: false,
        persistent: Boolean(options.persistent),
        actionLabel: String(options.actionLabel || ''),
        action: typeof options.action === 'function' ? options.action : null
    };

    notificationItems.unshift(item);
    notificationItems = notificationItems.slice(0, NOTIFICATION_MAX_ITEMS);
    saveNotificationHistory();
    renderNotificationCenter();
    return item;
}

function formatNotificationTime(timestamp) {
    const date = new Date(Number(timestamp) || Date.now());
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    return sameDay
        ? TimeFormatter.formatTime(date)
        : TimeFormatter.formatDateTime(date);
}

function renderNotificationCenter() {
    const list = document.getElementById('notificationList');
    const empty = document.getElementById('notificationEmpty');
    if (!list) return;

    list.innerHTML = notificationItems.map(item => `
        <div class="notification-item notification-${escapeHtml(item.type)} ${item.read ? 'is-read' : ''}">
            <button type="button"
                    class="notification-item-main"
                    onclick="markNotificationRead('${escapeJsString(item.id)}')">
                <span class="notification-item-icon" aria-hidden="true">${notificationIcon(item.type)}</span>
                <span class="notification-item-copy">
                    <strong>${escapeHtml(item.message)}</strong>
                    <small>${escapeHtml(formatNotificationTime(item.timestamp))}</small>
                </span>
            </button>
            ${item.actionLabel && item.action ? `
                <button type="button"
                        class="notification-item-action"
                        onclick="runNotificationAction('${escapeJsString(item.id)}')">${escapeHtml(item.actionLabel)}</button>
            ` : ''}
        </div>
    `).join('');

    if (empty) empty.hidden = notificationItems.length !== 0;
}

function setDockStatusBaseline(message, type = 'online') {
    notificationDockBaseline = {
        message: String(message || window.I18N.t('notifications.ready')),
        type: String(type || 'online')
    };
    if (!notificationCurrent) renderDockNotification();
}

function renderDockNotification() {
    const text = document.getElementById('dockStatusText');
    const state = document.getElementById('dockNotificationState');
    const button = document.getElementById('notificationCenterBtn');
    const actionButton = document.getElementById('dockNotificationAction');
    const dismissButton = document.getElementById('dockNotificationDismiss');
    if (!text || !state || !button) return;

    const displayed = notificationCurrent || notificationDockBaseline;
    const type = normalizeNotificationType(displayed.type) === displayed.type
        ? displayed.type
        : 'online';

    text.textContent = displayed.message || window.I18N.t('notifications.ready');
    button.dataset.notificationType = type;
    button.title = notificationCurrent
        ? window.I18N.t('notifications.open_with_message', { message: displayed.message })
        : window.I18N.t('notifications.open');

    state.className = `dock-notification-state dock-state-${type}`;

    const hasAction = Boolean(notificationCurrent?.action);
    if (actionButton) {
        actionButton.hidden = !hasAction;
        actionButton.textContent = notificationCurrent?.actionLabel || window.I18N.t('common.retry');
    }
    if (dismissButton) {
        dismissButton.hidden = !notificationCurrent;
    }
}

function runCurrentNotificationAction(event) {
    event?.stopPropagation();
    if (!notificationCurrent?.action) return;
    const action = notificationCurrent.action;
    clearCurrentNotification();
    action();
}

function dismissCurrentNotification(event) {
    event?.stopPropagation();
    if (notificationCurrent?.id) {
        dismissNotification(notificationCurrent.id);
    } else {
        clearCurrentNotification();
    }
}

function clearCurrentNotification() {
    if (notificationTimer) {
        clearTimeout(notificationTimer);
        notificationTimer = null;
    }
    notificationCurrent = null;
    renderDockNotification();
}

function presentNotification(item, options = {}) {
    if (notificationTimer) {
        clearTimeout(notificationTimer);
        notificationTimer = null;
    }

    notificationCurrent = item;
    renderDockNotification();

    const persistent = options.persistent
        ?? item.persistent
        ?? item.type === 'progress';

    if (!persistent) {
        const duration = Number(options.duration)
            || NOTIFICATION_VISIBLE_MS[item.type]
            || NOTIFICATION_VISIBLE_MS.info;
        notificationTimer = setTimeout(clearCurrentNotification, duration);
    }

    return item.id;
}

function showToast(message, type = 'info', options = {}) {
    const item = addNotification(message, type, options);
    return presentNotification(item, options);
}

function showProgressNotification(message) {
    return showToast(message, 'progress', { persistent: true });
}

function updateNotification(id, message, type = 'info', options = {}) {
    const item = notificationItems.find(entry => entry.id === id);
    if (!item) return showToast(message, type, options);

    item.message = cleanNotificationMessage(message);
    item.type = normalizeNotificationType(type);
    item.timestamp = Date.now();
    item.persistent = Boolean(options.persistent);
    item.actionLabel = String(options.actionLabel || '');
    item.action = typeof options.action === 'function' ? options.action : null;

    notificationItems = [
        item,
        ...notificationItems.filter(entry => entry.id !== id)
    ];
    saveNotificationHistory();
    renderNotificationCenter();
    return presentNotification(item, options);
}

function dismissNotification(id) {
    if (notificationCurrent?.id === id) clearCurrentNotification();
    const item = notificationItems.find(entry => entry.id === id);
    if (item) {
        item.read = true;
        saveNotificationHistory();
        renderNotificationCenter();
    }
}

function runNotificationAction(id) {
    const item = notificationItems.find(entry => entry.id === id);
    if (!item?.action) return;
    closeNotificationCenter();
    item.action();
}

function toggleNotificationCenter(event) {
    event?.stopPropagation();
    const popover = document.getElementById('notificationPopover');
    const button = document.getElementById('notificationCenterBtn');
    if (!popover || !button) return;

    const willOpen = popover.hidden;
    popover.hidden = !willOpen;
    button.setAttribute('aria-expanded', willOpen ? 'true' : 'false');

    if (willOpen) {
        notificationItems.forEach(item => { item.read = true; });
        saveNotificationHistory();
        renderNotificationCenter();
    }
}

function closeNotificationCenter() {
    const popover = document.getElementById('notificationPopover');
    const button = document.getElementById('notificationCenterBtn');
    if (popover) popover.hidden = true;
    if (button) button.setAttribute('aria-expanded', 'false');
}

function markNotificationRead(id) {
    const item = notificationItems.find(entry => entry.id === id);
    if (item) item.read = true;
    saveNotificationHistory();
    renderNotificationCenter();
}

function clearNotifications() {
    notificationItems = [];
    saveNotificationHistory();
    renderNotificationCenter();
}

function initializeNotificationCenter() {
    loadNotificationHistory();
    document.addEventListener('click', event => {
        if (
            !event.target.closest('#notificationPopover')
            && !event.target.closest('#notificationCenterBtn')
        ) {
            closeNotificationCenter();
        }
    });
}

// ============================================================
// UTILITY FUNCTIONS
// ============================================================

function initBaseNodeAvatar() {
    const input = document.getElementById('baseNodeAvatarInput');
    const baseImage = document.getElementById('baseNodeAvatar');
    if (!input || !baseImage) return;

    let localNodeId = '';
    let currentIconUrl = '';
    const fallbackSrc = baseImage.getAttribute('src') || '/static/meshcenter_logo.png';

    const applyIcon = (src) => {
        const safeSrc = src || fallbackSrc;
        currentIconUrl = safeSrc;
        baseImage.src = safeSrc;
        const managerImage = document.getElementById('nodeManagerAvatar');
        if (managerImage) managerImage.src = safeSrc;
    };

    const loadServerIcon = async () => {
        try {
            const statusResponse = await fetch('/api/base_status', { cache: 'no-store' });
            if (!statusResponse.ok) throw new Error('Unable to load local node status.');
            const status = await statusResponse.json();
            localNodeId = String(status.node_id || '').trim();
            if (!localNodeId) throw new Error('Local node ID is unavailable.');

            const iconResponse = await fetch(`/api/nodes/${encodeURIComponent(localNodeId)}/icon`, { cache: 'no-store' });
            if (iconResponse.ok) {
                const blob = await iconResponse.blob();
                applyIcon(URL.createObjectURL(blob));
            } else if (iconResponse.status === 404) {
                applyIcon(fallbackSrc);
            }
        } catch (error) {
            console.warn('Unable to load node icon from MeshCenter:', error);
        }
    };

    const prepareTransparentNodeImage = (file) => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error('Unable to read image file.'));
        reader.onload = () => {
            const source = new Image();
            source.onerror = () => reject(new Error('Unable to decode image file.'));
            source.onload = () => {
                const size = 256;
                const canvas = document.createElement('canvas');
                canvas.width = size;
                canvas.height = size;
                const ctx = canvas.getContext('2d', { alpha: true });
                if (!ctx) return reject(new Error('Canvas is unavailable.'));
                ctx.clearRect(0, 0, size, size);
                const inset = 10;
                const available = size - inset * 2;
                const scale = Math.min(available / source.naturalWidth, available / source.naturalHeight);
                const width = Math.max(1, Math.round(source.naturalWidth * scale));
                const height = Math.max(1, Math.round(source.naturalHeight * scale));
                ctx.drawImage(source, Math.round((size - width) / 2), Math.round((size - height) / 2), width, height);
                canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('Unable to create PNG.')), 'image/png');
            };
            source.src = String(reader.result || '');
        };
        reader.readAsDataURL(file);
    });

    document.addEventListener('click', event => {
        if (event.target.closest('#nodeManagerChangeImageBtn')) input.click();
    });

    input.addEventListener('change', async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        if (!/^image\/(png|jpeg|webp)$/i.test(file.type)) {
            showToast(window.I18N.t('node_manager.please_choose_image'), 'error');
            input.value = '';
            return;
        }
        if (file.size > 2 * 1024 * 1024) {
            showToast(window.I18N.t('node_manager.image_too_large'), 'error');
            input.value = '';
            return;
        }

        try {
            if (!localNodeId) await loadServerIcon();
            if (!localNodeId) throw new Error('Local node ID is unavailable.');
            const pngBlob = await prepareTransparentNodeImage(file);
            const formData = new FormData();
            formData.append('icon', pngBlob, 'node-icon.png');
            const response = await fetch(`/api/nodes/${encodeURIComponent(localNodeId)}/icon`, { method: 'POST', body: formData });
            const result = await response.json().catch(() => ({}));
            if (!response.ok || !result.ok) throw new Error(result.error || window.I18N.t('node_manager.upload_failed'));
            applyIcon(`${result.icon_url}&t=${Date.now()}`);
            localStorage.removeItem('meshcenter.baseNodeAvatar');
            showToast(window.I18N.t('node_manager.image_saved'), 'success');
        } catch (error) {
            console.warn('Unable to save node image:', error);
            showToast(error.message || window.I18N.t('node_manager.unable_to_save_image'), 'error');
        } finally {
            input.value = '';
        }
    });

    window.MeshCenterNodeAvatar = {
        refresh: loadServerIcon,
        current: () => currentIconUrl || fallbackSrc,
        apply: applyIcon
    };
    loadServerIcon();
}

document.addEventListener('DOMContentLoaded', async function() {
    await loadSettings();
    await loadCameraPowerState();
    startCpuMonitoringUi();
    initTimeCard();
    initNotificationsCard();
    initSchedulesCard();

    const title = document.getElementById('appTitle');
    if (title) {
        title.addEventListener('click', function() {
            if (this.classList.contains('is-reloading')) return;

            const appName = this.querySelector('.app-name');
            if (appName) {
                appName.classList.add('brand-fade-out');
                setTimeout(() => {
                    appName.textContent = window.I18N.t('common.reloading');
                    appName.classList.remove('brand-fade-out');
                    appName.classList.add('brand-fade-in');
                }, 150);
            }

            this.classList.add('is-reloading');
            this.style.cursor = 'default';
            setTimeout(() => window.location.reload(true), 520);
        });
    }

    initBaseNodeAvatar();

    const headerStatus = document.getElementById('headerStatusText');

    if (headerStatus) {
        headerStatus.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();

            switchMainTab('system');

            setTimeout(() => {
                document.querySelector('.radio-health-card')?.scrollIntoView({
                    behavior: 'smooth',
                    block: 'start'
                });
            }, 100);
        });
    }

});

function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(value);
    return div.innerHTML;
}

function formatTime(timeStr) {
    if (!timeStr) return '';
    return timeStr;
}

function truncateText(text, maxLen) {
    if (!text) return '';
    if (text.length <= maxLen) return text;
    return text.substring(0, maxLen) + '...';
}

function toggleShowIgnored() {
    const checkbox = document.getElementById('showIgnoredToggle');
    showIgnored = checkbox ? checkbox.checked : false;
    localStorage.setItem('mesh_show_ignored', showIgnored);
    loadMessages();
}

function toggleShowFavorites() {
    const checkbox = document.getElementById('showFavoritesToggle');
    showFavorites = checkbox ? checkbox.checked : false;
    localStorage.setItem('mesh_show_favorites', showFavorites);
    loadMessages();
}

// ============================================================
// CHAT AVATAR / NODE SHORT NAME
// ============================================================
function getChatNodeShortName(chat) {
    if (!chat || chat.is_channel) return '';

    // Prefer a short name already supplied by the chats API.
    const directShortName =
        chat.short_name
        || chat.shortName
        || chat.node_short_name
        || '';

    if (directShortName) {
        return String(directShortName).trim().slice(0, 4);
    }

    // Otherwise resolve the chat's node through the shared node cache.
    const chatNodeId = String(
        chat.node_id
        || chat.nodeId
        || chat.id
        || ''
    ).trim();

    const matchedNode = Array.isArray(nodeCache)
        ? nodeCache.find((node) => {
            const nodeId = String(
                node.node_id
                || node.nodeId
                || node.id
                || ''
            ).trim();

            return nodeId && chatNodeId && nodeId === chatNodeId;
        })
        : null;

    const cachedShortName = matchedNode
        ? (
            matchedNode.short_name
            || matchedNode.shortName
            || matchedNode.short
            || ''
        )
        : '';

    if (cachedShortName) {
        return String(cachedShortName).trim().slice(0, 4);
    }

    // Safe fallback for chats that are not present in nodeCache yet.
    // It keeps the avatar informative instead of returning a generic silhouette.
    const sourceName = String(chat.name || chatNodeId || '?')
        .replace(/[🚫⚑📡📍🔋⚡]/gu, ' ')
        .trim();

    const words = sourceName
        .split(/[\s_\-.:/]+/)
        .map((part) => part.replace(/[^\p{L}\p{N}]/gu, ''))
        .filter(Boolean);

    let fallback = '';

    if (words.length >= 2) {
        fallback = words
            .slice(0, 4)
            .map((part) => part.charAt(0))
            .join('');
    } else if (words.length === 1) {
        fallback = words[0].slice(0, 4);
    }

    return (fallback || '?').toUpperCase();
}

// Appends "[index]" to a channel name, stripping a redundant trailing
// "Channel <index>" fragment and/or an already-appended "[N]" bracket from
// the name first (e.g. the primary channel's resolved name
// "LongFast Channel 0" becomes "LongFast [0]" instead of
// "LongFast Channel 0 [0]", and a name that already arrived pre-bracketed
// as "LongFast [0]" doesn't become "LongFast [0] [0]"). Shared by the main
// chat channel list and the Waypoint composer's channel picker, whose
// `name` values come from different backend sources (server.py's stored
// chats vs. api/api_chat.py's live discovery) that aren't guaranteed to
// agree on whether the index is already embedded — so this needs to be
// idempotent either way.
function formatChannelIndexLabel(name, index) {
    let trimmedName = String(name || '').trim();
    trimmedName = trimmedName.replace(/\s*\[\d+\]\s*$/, '').trim();
    const suffixPattern = new RegExp(`^(.*?)\\s*channel\\s+${index}$`, 'i');
    const match = trimmedName.match(suffixPattern);
    const displayName = (match ? match[1].trim() : trimmedName) || 'Channel';
    return `${displayName} [${index}]`;
}

// ============================================================
// RENDER CHAT ITEM
// ============================================================
function renderChatItem(chat) {
    const isDemo = Boolean(chat.is_demo);
    const isSelected = (chat.id === currentChatId);
    const selectedClass = isSelected ? 'selected' : '';
    const icon = chat.is_channel ? (isDemo ? '🔒' : '📡') : getChatNodeShortName(chat);
    const iconClass = chat.is_channel ? `channel${isDemo ? ' demo' : ''}` : 'dm node-short-name';
    const lastMsg = chat.last_message || window.I18N.t('chat.no_messages_yet_short');
    const time = chat.last_time || '';
    const ignored = chat.ignored ? '🚫 ' : '';
    const favorite = chat.favorite ? '⚑ ' : '';
    const unreadBadge = (chat.unread || 0) > 0 ? `<span class="chat-unread-badge">${chat.unread}</span>` : '';
    const hasUnread = (chat.unread || 0) > 0 ? 'has-unread' : '';

    let lastMsgDisplay = '';
    
    if (chat.is_channel) {
        if (chat.last_sender && lastMsg) {
            lastMsgDisplay = `<span class="chat-last-sender">${escapeHtml(chat.last_sender)}</span> <span class="chat-last-text">${escapeHtml(truncateText(lastMsg, 50))}</span>`;
        } else {
            lastMsgDisplay = `<span class="chat-last-text">${escapeHtml(truncateText(lastMsg, 60))}</span>`;
        }
    } else {
        lastMsgDisplay = `<span class="chat-last-text">${escapeHtml(truncateText(lastMsg, 60))}</span>`;
    }

    const clickHandler = isDemo
        ? `showToast(${JSON.stringify(window.I18N.t('chat.channel_not_configured_toast'))}, 'info')`
        : `openChat('${escapeHtml(chat.id)}', '${escapeHtml(chat.name)}', '${escapeHtml(chat.type)}', 'chat')`;
    const demoClass = isDemo ? 'demo-channel' : '';
    const displayName = chat.is_channel && Number.isInteger(chat.index)
        ? formatChannelIndexLabel(chat.name, chat.index)
        : chat.name;

    return `
        <div class="chat-item ${hasUnread} ${selectedClass} ${demoClass}" data-chat-id="${escapeHtml(chat.id)}" onclick="${clickHandler}" ${isDemo ? `title="${escapeHtml(window.I18N.t('chat.channel_not_configured_title'))}"` : ''}>
            <div class="chat-icon ${iconClass}">${icon}</div>
            <div class="chat-info">
                <div class="chat-name">${ignored}${favorite}${escapeHtml(displayName)}</div>
                <div class="chat-last-msg">${lastMsgDisplay}</div>
            </div>
            <div class="chat-meta">
                <div class="chat-time">${escapeHtml(time)}</div>
                ${unreadBadge}
            </div>
        </div>
    `;
}

// ============================================================
// LOAD CHAT LIST
// ============================================================
let initialChannelRefreshPending = true;
async function loadChatList() {
    console.log('[CHAT] loadChatList called');

    const initialLoading = document.getElementById('initialLoading');
    if (initialLoading) initialLoading.style.display = 'none';

    const channelContainer = document.getElementById('channelList');
    const dmContainer = document.getElementById('dmChatList');
    if (!channelContainer || !dmContainer) {
        console.error('[CHAT] Split chat containers not found');
        return;
    }

    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 15000);
        const forceChannelRefresh = initialChannelRefreshPending;
        const chatsUrl = forceChannelRefresh
            ? '/api/chats?refresh_channels=1'
            : '/api/chats';

        const response = await fetch(chatsUrl, {
            signal: controller.signal,
            headers: { 'Cache-Control': 'no-cache' }
        });
        if (forceChannelRefresh && response.ok) {
            initialChannelRefreshPending = false;
        }
        clearTimeout(timeoutId);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();

        const previousActiveChat = currentChatId
            ? chatListCache.find(chat => chat.id === currentChatId)
            : null;
        const nextChatList = Array.isArray(data.chats) ? data.chats : [];
        const nextActiveChat = currentChatId
            ? nextChatList.find(chat => chat.id === currentChatId)
            : null;

        const previousActivitySignature = previousActiveChat
            ? `${previousActiveChat.last_time || ''}|${previousActiveChat.last_message || ''}`
            : '';
        const nextActivitySignature = nextActiveChat
            ? `${nextActiveChat.last_time || ''}|${nextActiveChat.last_message || ''}`
            : '';

        chatListCache = nextChatList;
        totalUnreadCount = data.total_unread || 0;

        if (
            currentChatId &&
            previousActivitySignature &&
            nextActivitySignature &&
            previousActivitySignature !== nextActivitySignature
        ) {
            loadChatMessages(currentChatId, {
                forceRefresh: true,
                suppressErrorPlaceholder: true
            });
        }

        if (!currentChatId) {
            const chatTitle = document.getElementById('chatTitle');
            if (chatTitle) chatTitle.textContent = totalUnreadCount > 0 ? `💬 ${window.I18N.t('nav.chats')} (${totalUnreadCount})` : `💬 ${window.I18N.t('nav.chats')}`;
            const subtitleEl = document.getElementById('chatSubtitle');
            if (subtitleEl) subtitleEl.textContent = '';
        }

        const apiChannels = Array.isArray(data.channels) ? data.channels : [];
        const channelChatsById = new Map(
            chatListCache.filter(chat => chat.is_channel).map(chat => [chat.id, chat])
        );
        const channels = apiChannels.map(channel => ({
            ...(channelChatsById.get(channel.id) || {}),
            ...channel,
            type: 'channel',
            is_channel: true
        }));

        // Backward-compatible fallback when an older backend is briefly running.
        if (channels.length === 0) {
            const legacyChannel = chatListCache.find(chat => chat.is_channel);
            if (legacyChannel) channels.push(legacyChannel);
        }

        channelContainer.innerHTML = channels.length
            ? channels.map(renderChatItem).join('')
            : `<div class="loading">📡 ${escapeHtml(window.I18N.t('chat.no_configured_channels'))}</div>`;

        // If the active radio channel was removed externally, leave the stale
        // conversation and switch to the first channel that still exists.
        if (currentChatType === 'channel' && currentChatId) {
            const activeChannelStillExists = channels.some(channel => channel.id === currentChatId);
            if (!activeChannelStillExists) {
                const fallbackChannel = channels[0] || null;
                showToast(window.I18N.t('chat.channel_removed_from_radio'), 'info');

                if (fallbackChannel) {
                    window.setTimeout(() => {
                        openChat(fallbackChannel.id, fallbackChannel.name, 'channel', 'channel-sync');
                    }, 0);
                } else {
                    showChatList();
                }
            }
        }

        const dmChats = chatListCache.filter(chat => !chat.is_channel);
        dmContainer.innerHTML = dmChats.length
            ? dmChats.map(renderChatItem).join('')
            : `<div class="loading">💬 ${escapeHtml(window.I18N.t('chat.no_direct_messages_yet'))}</div>`;

        flushPendingSynchronizedScroll();
    } catch (error) {
        console.error('[CHAT] Error:', error);
        const message = error.name === 'AbortError' ? window.I18N.t('errors.request_timeout') : error.message;
        channelContainer.innerHTML = `<div class="loading" style="color:#ff9800;">⚠️ ${escapeHtml(message)}</div>`;
        dmContainer.innerHTML = `<div class="loading">${escapeHtml(window.I18N.t('chat.direct_messages_unavailable'))}</div>`;
    }
}

function mergeNodeCachePreservingPosition(newNodes) {
    const oldNodesById = new Map(
        nodeCache.map(node => [node.node_id, node])
    );

    return (newNodes || []).map(node => {
        const oldNode = oldNodesById.get(node.node_id);

        if (
            !node.position &&
            oldNode?.position
        ) {
            return {
                ...node,
                position: oldNode.position
            };
        }

        return node;
    });
}

// ============================================================
// LOAD MESSAGES
// ============================================================
async function loadMessages() {
    try {
        console.log('[MESSAGES] Loading messages...');
        
        const controller = new AbortController();
        const timeoutId = setTimeout(() => {
            console.log('[MESSAGES] Request timeout, aborting...');
            controller.abort();
        }, 8000);
        
        const response = await fetch('/api/messages', {
            signal: controller.signal,
            headers: { 'Cache-Control': 'no-cache' }
        });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const data = await response.json();
        console.log('[MESSAGES] Received', data.nodes ? data.nodes.length : 0, 'nodes');
        notifyFailedOutgoingMessages(data.messages);

        nodeCache = mergeNodeCachePreservingPosition(
            data.nodes || []
        );
        
        populateReferenceNodeSelect();
        updateReferenceLocationSummary();
        
        if (currentChatId && currentChatType === 'dm') {
            updateChatHeaderStatus();
        }

        const statusEl = document.getElementById('statusText');
        const nodeCountEl = document.getElementById('nodeCount');

        if (statusEl && statusEl.innerHTML !== `🔴 ${window.I18N.t('nodes.error_loading_refresh')}`) {
            statusEl.innerHTML = `🟢 ${window.I18N.t('notifications.mesh_online')}`;
        }

        const allNodes = nodeCache;
        const ignoredNodes = allNodes.filter(n => n.ignored);
        const favoriteNodes = allNodes.filter(n => n.favorite);

        const ignoredCountEl = document.getElementById('ignoredCount');
        if (ignoredCountEl) {
            ignoredCountEl.textContent = window.I18N.t('nodes.ignored_count', { count: ignoredNodes.length });
        }

        const favoritesCountEl = document.getElementById('favoritesCount');
        if (favoritesCountEl) {
            favoritesCountEl.textContent = window.I18N.t('nodes.favorites_count', { count: favoriteNodes.length });
        }
        
        let displayNodes = [];
        
        if (showFavorites && showIgnored) {
            displayNodes = allNodes.filter(n => n.favorite && n.ignored);
        } else if (showFavorites) {
            displayNodes = allNodes.filter(n => n.favorite && !n.ignored);
        } else if (showIgnored) {
            displayNodes = allNodes.filter(n => n.ignored);
        } else {
            displayNodes = allNodes.filter(n => !n.ignored);
        }
        
        if (nodeCountEl) {
            const totalDisplay = displayNodes.length;
            nodeCountEl.innerHTML = '🖥️ ' + escapeHtml(window.I18N.t('nodes.nodes_count', { count: totalDisplay }));
        }

        const nodesList = document.getElementById('nodesList');
        if (!nodesList) return;

        let filteredNodes = displayNodes;
        if (nodeSearchTerm) {
            filteredNodes = filteredNodes.filter(node =>
                node.clean_name.toLowerCase().includes(nodeSearchTerm.toLowerCase()) ||
                node.node_id.toLowerCase().includes(nodeSearchTerm.toLowerCase())
            );
        }

        if (filteredNodes.length === 0) {
            let message = `🔍 ${window.I18N.t('nodes.no_nodes_found')}`;
            if (showFavorites && showIgnored) {
                message = `⚑ ${window.I18N.t('nodes.no_favorite_ignored_nodes_found')}`;
            } else if (showFavorites) {
                message = `⚑ ${window.I18N.t('nodes.no_favorite_nodes_found')}`;
            } else if (showIgnored) {
                message = `🚫 ${window.I18N.t('nodes.no_ignored_nodes_found')}`;
            }
            nodesList.innerHTML = `<div class="loading" style="padding: 16px;">${escapeHtml(message)}</div>`;
        } else {
            nodesList.innerHTML = filteredNodes.map(node => {
                const { activityClass, displayName } = getNodeActivityPresentation(node);
                const isIgnored = node.ignored || false;
                const isFavorite = node.favorite || false;
                const isSelected = currentChatType === 'dm' && currentChatId === node.node_id;
                const cardClasses = ['node-card'];
                if (isIgnored) cardClasses.push('ignored');
                if (isFavorite) cardClasses.push('favorite');
                if (isSelected) cardClasses.push('selected');
                const cardClass = cardClasses.join(' ');
                const lastText = node.last_text
                    ? `<div class="node-last-text"><span class="node-last-text-icon">💬</span><span>${escapeHtml(truncateText(node.last_text, 60))}</span></div>`
                    : '';

                const mapBadge = renderNodeMapBadge(node);
                const favoriteStatus = isFavorite ? '⚑' : ' ';
                const ignoreStatus = isIgnored ? '<span class="node-ignore-mark" title="Ignored">🚫</span>' : '';
                const shortName = node.short_name || '-';
                const hardware = node.hw_model || '-';
                const seenText = formatCompactNodeAge(node.age || node.last_time || '-');
                const hopsText = formatNodeHops(node);
                const signalSegments = renderNodeSignalSegments(node);

                const unignoreBtn = isIgnored
                    ? `<button class="unignore-btn-mini" onclick="event.stopPropagation(); toggleIgnore('${escapeHtml(node.node_id)}')">Unignore</button>`
                    : '';

                return `
                    <div class="${cardClass}" data-node-id="${escapeHtml(node.node_id)}">
                        <div class="node-card-topline">
                            <span class="node-favorite-slot"
                                title="${isFavorite ? 'Favorite node' : 'Not favorite'}">
                                ${favoriteStatus}
                            </span>
                            <span class="node-activity-square ${activityClass}"
                                  title="Node activity"></span>
                            <div class="node-card-name-wrap">
                                <div class="node-card-title">${escapeHtml(displayName)}</div>
                            </div>
                            ${ignoreStatus}
                            ${unignoreBtn}
                        </div>

                        <div class="node-card-identity-row">
                            <span class="node-short-name">${escapeHtml(shortName)}</span>
                            <span class="node-identity-separator">•</span>
                            <span class="node-hardware-name">${escapeHtml(hardware)}</span>
                            <span class="node-identity-separator">•</span>
                            <span class="node-inline-id">${escapeHtml(node.node_id)}</span>
                        </div>

                        <div class="node-card-status-row">
                            <span class="node-hop-count" title="Mesh route hops">${escapeHtml(hopsText)}</span>
                            <div class="node-card-signal-wrap">
                                ${signalSegments}
                            </div>
                            <span class="node-last-seen">🕒 ${escapeHtml(seenText)}</span>
                            ${mapBadge}
                        </div>

                        ${lastText}
                    </div>
                `;
            }).join('');
        }

        // Повторная синхронизация после полной перерисовки списка.
        syncSelectedNodeCard();
        flushPendingSynchronizedScroll();

        const selectedNode = allNodes.find(n => n.node_id === currentChatId);
        if (selectedNode) {
            renderNodeDetails(selectedNode);
        } else {
            renderNodeDetails(null);
        }

        if (currentMainTab === 'map') {
            renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        }

    } catch (error) {
        console.error('[MESSAGES] Error:', error);
        const statusEl = document.getElementById('statusText');
        if (statusEl && error.name !== 'AbortError') {
            statusEl.innerHTML = '🔴 Connection error';
        }
        if (error.name !== 'AbortError') {
            setTimeout(() => loadMessages(), 5000);
        }
    }
}

function getNodeActivityPresentation(node) {
    const rawName = String(
        node?.name
        || node?.clean_name
        || node?.long_name
        || node?.node_id
        || window.I18N.t('nodes.unknown_node')
    ).trim();

    let activityClass = 'activity-unknown';

    if (rawName.startsWith('🟢')) {
        activityClass = 'activity-online';
    } else if (rawName.startsWith('🟡')) {
        activityClass = 'activity-away';
    } else if (rawName.startsWith('🔴')) {
        activityClass = 'activity-offline';
    } else {
        // Fallback for data without a status emoji in node.name.
        const age = String(node?.age || '').toLowerCase();
        const value = parseInt(age, 10);

        if (age.includes('day') || (age.includes('h') && Number.isFinite(value) && value > 24)) {
            activityClass = 'activity-offline';
        } else if (
            age.includes('h')
            || age.includes('day')
            || (age.includes('min') && Number.isFinite(value) && value > 10)
        ) {
            activityClass = 'activity-away';
        } else if (age) {
            activityClass = 'activity-online';
        }
    }

    const displayName = String(
        node?.clean_name
        || rawName.replace(/^[🟢🟡🔴⚪]\s*/u, '')
        || node?.node_id
        || window.I18N.t('nodes.unknown_node')
    ).trim();

    return {
        activityClass,
        displayName
    };
}

function signalBadgeClass(signalQuality) {
    if (signalQuality === 'good') return 'badge-online';
    if (signalQuality === 'medium') return 'badge-medium';
    return 'badge-offline';
}

function signalBadgeText(signalQuality) {
    if (signalQuality === 'good') return '●';
    if (signalQuality === 'medium') return '○';
    return '○';
}

function getNodeSignalLevel(node) {
    const rssi = Number(node?.rssi);
    const snr = Number(node?.snr);

    let level = 0;

    if (Number.isFinite(rssi)) {
        if (rssi >= -70) level = 7;
        else if (rssi >= -80) level = 6;
        else if (rssi >= -90) level = 5;
        else if (rssi >= -100) level = 4;
        else if (rssi >= -110) level = 3;
        else if (rssi >= -120) level = 2;
        else level = 1;

        if (Number.isFinite(snr)) {
            if (snr >= 5) level += 1;
            else if (snr <= -10) level -= 1;
        }
    } else if (node?.signal_quality === 'good') {
        level = 6;
    } else if (node?.signal_quality === 'medium') {
        level = 4;
    } else if (node?.signal_quality) {
        level = 1;
    }

    return Math.max(0, Math.min(7, level));
}

function formatCompactNodeAge(value) {
    const text = String(value ?? '').trim();
    if (!text || text === '-') return '-';

    return text
        .replace(/\s+ago\b/gi, '')
        .replace(/^ago\s+/i, '')
        .trim();
}

function formatNodeHops(node) {
    const raw =
        node?.hops_away
        ?? node?.hopsAway
        ?? node?.hop_count
        ?? node?.hopCount
        ?? node?.hop_start;

    if (raw === null || raw === undefined || raw === '') {
        return 'H?';
    }

    const hops = Number(raw);
    if (!Number.isFinite(hops) || hops < 0) {
        return 'H?';
    }

    return `H${Math.round(hops)}`;
}

function renderNodeSignalSegments(node) {
    const level = getNodeSignalLevel(node);
    const qualityClass = level >= 5
        ? 'signal-good'
        : (level >= 3 ? 'signal-medium' : 'signal-weak');

    const segments = Array.from({ length: 7 }, (_, index) =>
        `<span class="node-signal-segment ${index < level ? 'filled' : ''}"></span>`
    ).join('');

    return `
        <span class="node-signal-indicator ${qualityClass}"
              title="${escapeHtml(window.I18N.t('nodes.signal_quality_of', { level }))}"
              aria-label="${escapeHtml(window.I18N.t('nodes.signal_quality_of', { level }))}">
            ${segments}
        </span>
    `;
}

function checkNodeIgnored(nodeId) {
    try {
        return fetch(`/api/node_status?node_id=${encodeURIComponent(nodeId)}`)
            .then(response => response.json())
            .then(data => data.ignored || false)
            .catch(() => false);
    } catch (error) {
        console.error('Error checking ignore status:', error);
        return false;
    }
}

function hideIgnoredBanner() {
    const banner = document.getElementById('ignoreBanner');
    if (banner) banner.remove();
}

function updateChatHeaderStatus() {
    if (!currentChatId || currentChatType === 'channel') return;
    
    const node = nodeCache.find(n => n.node_id === currentChatId);
    const titleEl = document.getElementById('chatTitle');
    const subtitleEl = document.getElementById('chatSubtitle');
    
    if (!titleEl || !subtitleEl) return;
    
    let statusIcon = '🟢';
    let statusText = window.I18N.t('nodes.status_online');

    if (node && node.age) {
        const age = node.age;
        if (age.includes('h') || age.includes('day') || (age.includes('min') && parseInt(age) > 10)) {
            statusIcon = '🟡';
            statusText = window.I18N.t('nodes.status_away');
        }
        if (age.includes('day') || (age.includes('h') && parseInt(age) > 24)) {
            statusIcon = '🔴';
            statusText = window.I18N.t('nodes.status_radio_offline');
        }
    }

    const shortId = currentChatId ? currentChatId.slice(-4) : '';
    titleEl.innerHTML = `${statusIcon} ${escapeHtml(currentChatName)} <span style="font-size:12px;font-weight:400;color:#888;margin-left:6px;">${escapeHtml(shortId)}</span>`;
    subtitleEl.textContent = window.I18N.t('nodes.direct_message_status', { status: statusText });
    subtitleEl.style.color = statusIcon === '🟢' ? '#2e7d32' : (statusIcon === '🟡' ? '#f57c00' : '#c62828');
}

// ============================================================
// BIDIRECTIONAL CHAT / NODE LIST SCROLL SYNCHRONIZATION
// ============================================================
let pendingChatScrollNodeId = null;
let pendingNodeScrollNodeId = null;
let pendingNodeScrollForceCenter = false;

function isElementFullyVisibleInContainer(element, container) {
    if (!element || !container) return false;

    const elementRect = element.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();

    return (
        elementRect.top >= containerRect.top &&
        elementRect.bottom <= containerRect.bottom &&
        elementRect.left >= containerRect.left &&
        elementRect.right <= containerRect.right
    );
}

function centerElementInContainerIfNeeded(element, container, forceCenter = false) {
    if (!element || !container) return false;

    if (forceCenter || !isElementFullyVisibleInContainer(element, container)) {
        const containerRect = container.getBoundingClientRect();
        const elementRect = element.getBoundingClientRect();
        const currentScrollTop = container.scrollTop;
        const elementCenterWithinContainer =
            (elementRect.top - containerRect.top) +
            currentScrollTop +
            (elementRect.height / 2);
        const targetScrollTop = Math.max(
            0,
            elementCenterWithinContainer - (container.clientHeight / 2)
        );

        container.scrollTo({
            top: targetScrollTop,
            behavior: 'smooth'
        });
    }

    return true;
}

function findElementByDataValue(selector, dataKey, value) {
    const expectedValue = String(value || '');
    return Array.from(document.querySelectorAll(selector)).find(element =>
        String(element.dataset[dataKey] || '') === expectedValue
    ) || null;
}

function scrollChatItemIntoView(nodeId) {
    // Direct Messages now live in their own independently scrolling list.
    // The old selector/container pointed at the removed combined #chatList,
    // so selection from the Nodes panel could no longer scroll the DM list.
    const chatItem = findElementByDataValue(
        '#dmChatList .chat-item',
        'chatId',
        nodeId
    );
    const chatContainer = document.getElementById('dmChatList');

    return centerElementInContainerIfNeeded(chatItem, chatContainer);
}

function scrollNodeCardIntoView(nodeId, forceCenter = false) {
    const nodeCard = findElementByDataValue(
        '#nodesList .node-card',
        'nodeId',
        nodeId
    );
    const nodesContainer = document.querySelector('.nodes-scroll');

    return centerElementInContainerIfNeeded(
        nodeCard,
        nodesContainer,
        forceCenter
    );
}

function flushPendingSynchronizedScroll() {
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            if (pendingChatScrollNodeId) {
                if (scrollChatItemIntoView(pendingChatScrollNodeId)) {
                    pendingChatScrollNodeId = null;
                }
            }

            if (pendingNodeScrollNodeId) {
                if (scrollNodeCardIntoView(
                    pendingNodeScrollNodeId,
                    pendingNodeScrollForceCenter
                )) {
                    pendingNodeScrollNodeId = null;
                    pendingNodeScrollForceCenter = false;
                }
            }
        });
    });
}

function requestSynchronizedListScroll(nodeId, source, options = {}) {
    if (!nodeId) return;

    const forceNodeCenter = Boolean(options?.forceNodeCenter);

    if (source === 'chat') {
        pendingNodeScrollNodeId = String(nodeId);
        pendingNodeScrollForceCenter = forceNodeCenter;
    } else if (source === 'nodes') {
        pendingChatScrollNodeId = String(nodeId);
    } else {
        pendingChatScrollNodeId = String(nodeId);
        pendingNodeScrollNodeId = String(nodeId);
        pendingNodeScrollForceCenter = forceNodeCenter;
    }

    flushPendingSynchronizedScroll();
}

function syncSelectedNodeCard() {
    const selectedNodeId =
        !nodeVisualSelectionCleared &&
        currentChatType === 'dm' &&
        currentChatId
            ? String(currentChatId)
            : '';

    document.querySelectorAll('#nodesList .node-card').forEach(card => {
        const isSelected =
            selectedNodeId !== '' && card.dataset.nodeId === selectedNodeId;

        card.classList.toggle('selected', isSelected);
    });
}

// Mirrors the "channel" / "channel:N" chat id convention used across the
// frontend (see waypointFallbackChannel/normalizeWaypointChannels above and
// CHANNEL_CHAT_ID on the backend) to recover the numeric channel index.
function channelIndexFromChatId(chatId) {
    if (chatId === 'channel') return 0;
    const match = /^channel:(\d+)$/.exec(String(chatId || ''));
    return match ? Number(match[1]) : 0;
}

// ============================================================
// UPDATE CHAT HEADER (NEW)
// ============================================================
function updateChatHeader() {
    const titleEl = document.getElementById('chatTitle');
    const subtitleEl = document.getElementById('chatSubtitle');
    if (!titleEl) return;

    if (!currentChatId) {
        titleEl.textContent = '💬 ' + window.I18N.t('nav.chats');
        if (subtitleEl) {
            subtitleEl.textContent = window.I18N.t('chat.select_chat_subtitle');
            subtitleEl.style.color = '';
        }
        return;
    }

    if (currentChatType === 'channel') {
        const channelLabel = formatChannelIndexLabel(currentChatName, channelIndexFromChatId(currentChatId));
        titleEl.textContent = '📡 ' + channelLabel;
        if (subtitleEl) {
            subtitleEl.textContent = window.I18N.t('chat.channel_subtitle');
            subtitleEl.style.color = '#1a73e8';
        }
    } else {
        titleEl.textContent = '💬 ' + currentChatName;
        if (subtitleEl) {
            subtitleEl.textContent = window.I18N.t('chat.direct_message_subtitle');
            subtitleEl.style.color = '';
        }
    }
}

// ============================================================
// OPEN CHAT (MODIFIED)
// ============================================================
function openChat(chatId, chatName, chatType, selectionSource = 'external') {
    currentChatId = chatId;
    currentChatName = chatName || chatId;
    currentChatType = chatType || 'dm';
    if (currentChatType === 'dm') {
        nodeVisualSelectionCleared = false;
    }
    cancelReply();

    if (currentChatType === 'dm' && chatId !== 'channel') {
        requestSynchronizedListScroll(chatId, selectionSource);
    }

    // Сброс сигнатуры, чтобы принудительно обновить сообщения
    lastRenderedSignature[chatId] = null;

    // Обновляем заголовок
    updateChatHeader();

    // Загружаем сообщения
    const container = document.getElementById('messagesContainer');
    if (container) {
        container.innerHTML = `<div class="loading">⏳ ${escapeHtml(window.I18N.t('chat.loading_messages'))}</div>`;
    }
    loadChatMessages(chatId, { forceRefresh: false });
    startMessagePolling(chatId);

    // Если это DM, обновить детали ноды
    if (chatType === 'dm' && chatId !== 'channel') {
        updateNodeDetails(chatId);
        // Ignore state is already visible in the action segment and Notification Center.
        hideIgnoredBanner();
    } else {
        renderNodeDetails(null);
        hideIgnoredBanner();
    }

    // Убираем контекстный режим (если был)
    if (contextChatMode) {
        contextChatMode = false;
        contextBaseTab = null;
        document.body.classList.remove('context-chat-mode');
    }

    // Настраиваем поле ввода
    const input = document.getElementById('messageInput');
    if (input) {
        input.placeholder = chatType === 'channel'
            ? window.I18N.t('chat.message_placeholder_channel')
            : window.I18N.t('chat.message_placeholder_dm', { name: chatName });
        input.value = '';
        input.focus();
    }

    // Показываем кнопку действий, скрываем кнопку удаления всех DM
    const actionsBtn = document.getElementById('chatActionsBtn');
    if (actionsBtn) actionsBtn.style.display = 'block';
    const deleteDmBtn = document.getElementById('deleteAllDmHeaderBtn');
    if (deleteDmBtn) deleteDmBtn.style.display = 'none';

    // Синхронизируем подсветку и положение в обоих списках.
    syncSelectedNodeCard();
    flushPendingSynchronizedScroll();

    // Обновляем список чатов для подсветки выбранного
    loadChatList();
}

// ============================================================
// SHOW CHAT LIST (MODIFIED)
// ============================================================
function showChatList() {
    currentChatId = null;
    currentChatName = null;
    currentChatType = null;
    cancelReply();

    updateChatHeader();

    const container = document.getElementById('messagesContainer');
    if (container) {
        container.innerHTML = `<div class="loading">💬 ${escapeHtml(window.I18N.t('chat.select_chat_from_list'))}</div>`;
    }

    stopMessagePolling();
    hideIgnoredBanner();
    renderNodeDetails(null);

    const actionsBtn = document.getElementById('chatActionsBtn');
    if (actionsBtn) actionsBtn.style.display = 'none';
    const deleteDmBtn = document.getElementById('deleteAllDmHeaderBtn');
    if (deleteDmBtn) deleteDmBtn.style.display = 'none';

    if (contextChatMode) {
        contextChatMode = false;
        contextBaseTab = null;
        document.body.classList.remove('context-chat-mode');
    }

    loadChatList();
}

// ============================================================
// MESSAGE ACTIONS
// ============================================================
function showMessageActionStatus(text, type = 'info') {
    const prefix = type === 'error' ? '⚠️ ' : type === 'success' ? '✓ ' : '';
    setStatusDockContext(prefix + text);
    window.setTimeout(() => updateStatusDock(currentMainTab), 2200);
}

function closeMessageActionsMenu() {
    const menu = document.getElementById('messageActionsMenu');
    if (menu) {
        menu.classList.remove('open');
        menu.setAttribute('aria-hidden', 'true');
    }

    document.querySelectorAll('.message.actions-open').forEach(message => {
        message.classList.remove('actions-open');
    });
}

function positionMessageActionsMenu(anchorElement, pointerEvent = null) {
    const menu = document.getElementById('messageActionsMenu');
    if (!menu) return;

    menu.classList.add('open');
    menu.setAttribute('aria-hidden', 'false');

    const menuRect = menu.getBoundingClientRect();
    const viewportPadding = 8;
    let left;
    let top;

    if (pointerEvent) {
        left = pointerEvent.clientX;
        top = pointerEvent.clientY;
    } else {
        const anchorRect = anchorElement.getBoundingClientRect();
        left = anchorRect.right - menuRect.width;
        top = anchorRect.bottom + 5;
    }

    left = Math.min(
        Math.max(viewportPadding, left),
        window.innerWidth - menuRect.width - viewportPadding
    );
    top = Math.min(
        Math.max(viewportPadding, top),
        window.innerHeight - menuRect.height - viewportPadding
    );

    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
}

function openMessageActions(messageId, anchorElement, pointerEvent = null) {
    const message = renderedMessagesById.get(String(messageId));
    if (!message) return;

    closeMessageActionsMenu();
    messageActionTarget = message;

    const messageElement = document.querySelector(
        `.message[data-message-id="${CSS.escape(String(messageId))}"]`
    );
    if (messageElement) messageElement.classList.add('actions-open');

    positionMessageActionsMenu(anchorElement || messageElement, pointerEvent);
}

async function copyMessageText() {
    const message = messageActionTarget;
    closeMessageActionsMenu();
    if (!message) return;

    const sender = String(message.sender || 'Unknown').trim();
    const body = String(message.text || '');
    const text = sender ? `${sender}: ${body}` : body;

    try {
        await navigator.clipboard.writeText(text);
        showMessageActionStatus(window.I18N.t('chat.message_copied'), 'success');
    } catch (error) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();

        try {
            document.execCommand('copy');
            showMessageActionStatus(window.I18N.t('chat.message_copied'), 'success');
        } catch (fallbackError) {
            console.error('[MESSAGE ACTIONS] Copy failed:', fallbackError);
            showMessageActionStatus(window.I18N.t('chat.message_copy_failed'), 'error');
        } finally {
            textarea.remove();
        }
    }
}

function buildReplyPayload(message) {
    if (!message) return null;

    return {
        id: String(message.id || ''),
        packet_id: Number.isFinite(Number(message.packet_id)) ? Number(message.packet_id) : null,
        sender: String(message.sender || 'Unknown'),
        node_id: String(message.node_id || ''),
        text: String(message.text || ''),
        time: String(message.time || ''),
        chat_id: String(message.chat_id || currentChatId || ''),
        chat_name: String(message.chat_name || currentChatName || '')
    };
}

function updateReplyComposer() {
    const preview = document.getElementById('replyComposer');
    const sender = document.getElementById('replyComposerSender');
    const text = document.getElementById('replyComposerText');

    if (!preview || !sender || !text) return;

    if (!activeReply) {
        preview.hidden = true;
        sender.textContent = '';
        text.textContent = '';
        return;
    }

    sender.textContent = window.I18N.t('chat.reply_to', { sender: activeReply.sender || window.I18N.t('nodes.unknown_node') });
    text.textContent = activeReply.text || '';
    preview.hidden = false;
}

function normalizeMessageIdentity(value) {
    return String(value || '').trim().toLowerCase();
}

function messageBelongsToActiveRadio(message) {
    if (!message || !['me', 'tx'].includes(message.kind)) return false;

    const activeProfile = normalizeMessageIdentity(activeLocalProfileId);
    const activeNode = normalizeMessageIdentity(activeLocalNodeId);

    const ownerProfile = normalizeMessageIdentity(
        message.owner_profile_id
    );
    const ownerNode = normalizeMessageIdentity(
        message.owner_node_id || message.node_id
    );

    // Prefer the explicit profile owner written by the backend.
    if (ownerProfile && activeProfile) {
        return ownerProfile === activeProfile;
    }

    // Legacy messages are safely resolved by their original transmitting
    // node ID.  This also fixes existing histories without manual migration.
    if (ownerNode && activeNode) {
        return ownerNode === activeNode;
    }

    // Do not claim ownership when identity is unknown.  It is safer to show
    // such a record as received than to attribute it to the wrong radio.
    return false;
}

function messageDirectionLabel(message) {
    if (message?.kind === 'system' || message?.sender === 'SYSTEM ERROR') {
        return window.I18N.t('chat.direction_system');
    }
    return messageBelongsToActiveRadio(message) ? window.I18N.t('chat.direction_sent') : window.I18N.t('chat.direction_received');
}

function startReplyToMessage() {
    const message = messageActionTarget;
    closeMessageActionsMenu();
    if (!message || message.kind === 'system') return;

    activeReply = buildReplyPayload(message);
    updateReplyComposer();

    const input = document.getElementById('messageInput');
    if (input) input.focus();
}

function cancelReply() {
    activeReply = null;
    updateReplyComposer();
}

function showMessageInfo() {
    const message = messageActionTarget;
    closeMessageActionsMenu();
    if (!message) return;

    const fields = [
        [window.I18N.t('waypoints.sender'), message.sender || window.I18N.t('nodes.unknown_node')],
        [window.I18N.t('chat.node_id_label'), message.node_id || '—'],
        [window.I18N.t('chat.chat_label'), message.chat_name || currentChatName || message.chat_id || '—'],
        [window.I18N.t('chat.chat_type_label'), message.chat_type || currentChatType || '—'],
        [window.I18N.t('chat.direction_label'), messageDirectionLabel(message)],
        [window.I18N.t('chat.time_label'), message.time || '—'],
        [window.I18N.t('chat.message_id_label'), message.id || '—'],
        [window.I18N.t('chat.packet_id_label'), message.packet_id ?? '—']
    ];

    const content = document.getElementById('messageInfoContent');
    if (content) {
        content.innerHTML = fields.map(([label, value]) => `
            <div class="message-info-row">
                <span class="message-info-label">${escapeHtml(label)}</span>
                <span class="message-info-value">${escapeHtml(String(value))}</span>
            </div>
        `).join('');
    }

    const text = document.getElementById('messageInfoText');
    if (text) text.textContent = String(message.text || '');

    const modal = document.getElementById('messageInfoModal');
    if (modal) modal.style.display = 'flex';
}

function closeMessageInfo() {
    const modal = document.getElementById('messageInfoModal');
    if (modal) modal.style.display = 'none';
}

function requestDeleteMessage() {
    const message = messageActionTarget;
    closeMessageActionsMenu();
    if (!message) return;

    const preview = String(message.text || '').replace(/\s+/g, ' ').trim();
    const text = document.getElementById('confirmDeleteMessageText');
    if (text) {
        text.textContent = preview
            ? window.I18N.t('chat.delete_message_confirm_with_preview', {
                preview: preview.slice(0, 140) + (preview.length > 140 ? '…' : '')
            })
            : window.I18N.t('chat.delete_message_confirm');
    }

    const errorEl = document.getElementById('confirmDeleteMessageError');
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }

    const modal = document.getElementById('confirmDeleteMessageModal');
    if (modal) modal.style.display = 'flex';
}

function closeConfirmDeleteMessage() {
    const modal = document.getElementById('confirmDeleteMessageModal');
    if (modal) modal.style.display = 'none';

    const errorEl = document.getElementById('confirmDeleteMessageError');
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }
}

async function executeDeleteMessage() {
    const message = messageActionTarget;
    if (!message || !currentChatId) {
        closeConfirmDeleteMessage();
        return;
    }

    const button = document.getElementById('confirmDeleteMessageBtn');
    const errorEl = document.getElementById('confirmDeleteMessageError');
    if (button) {
        button.disabled = true;
        button.textContent = window.I18N.t('chat.deleting');
    }
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }

    try {
        const response = await fetch('/api/messages/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                chat_id: currentChatId,
                message_id: message.id
            })
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || window.I18N.t('chat.delete_failed_http', { status: response.status }));
        }

        invalidateCache(currentChatId);
        lastRenderedSignature[currentChatId] = null;
        closeConfirmDeleteMessage();
        messageActionTarget = null;
        await loadChatMessages(currentChatId);
        loadChatList();
        showMessageActionStatus(window.I18N.t('chat.message_deleted_locally'), 'success');
    } catch (error) {
        console.error('[MESSAGE ACTIONS] Delete failed:', error);
        // The modal stays open on failure by design (so the user can see
        // what went wrong and decide whether to retry or cancel), so the
        // error has to be shown *inside* it - the status dock this used to
        // rely on exclusively sits behind the modal overlay and is
        // invisible while it's open, which made failures look like the
        // dialog had simply frozen.
        if (errorEl) {
            errorEl.textContent = error.message || window.I18N.t('chat.could_not_delete_message');
            errorEl.style.display = 'block';
        }
        showMessageActionStatus(error.message || window.I18N.t('chat.could_not_delete_message'), 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = window.I18N.t('common.delete');
        }
    }
}

function initializeMessageActions() {
    const container = document.getElementById('messagesContainer');
    if (!container || container.dataset.messageActionsReady === '1') return;
    container.dataset.messageActionsReady = '1';

    container.addEventListener('click', event => {
        const button = event.target.closest('.message-actions-trigger');
        if (!button) return;

        event.preventDefault();
        event.stopPropagation();
        openMessageActions(button.dataset.messageId, button);
    });

    container.addEventListener('click', event => {
        const retryBtn = event.target.closest('.message-retry-btn[data-retry-message-id]');
        if (!retryBtn) return;

        event.preventDefault();
        event.stopPropagation();
        retryFailedMessage(retryBtn.dataset.retryMessageId);
    });

    container.addEventListener('click', event => {
        const quote = event.target.closest('.message-reply-quote[data-reply-message-id]');
        if (!quote) return;

        const replyId = quote.dataset.replyMessageId;
        if (!replyId) return;

        const original = container.querySelector(
            `.message[data-message-id="${CSS.escape(replyId)}"]`
        );

        if (!original) {
            showMessageActionStatus(window.I18N.t('chat.original_message_unavailable'), 'error');
            return;
        }

        original.scrollIntoView({ behavior: 'smooth', block: 'center' });
        original.classList.add('message-reply-highlight');
        window.setTimeout(() => original.classList.remove('message-reply-highlight'), 1600);
    });

    container.addEventListener('contextmenu', event => {
        const messageElement = event.target.closest('.message[data-message-id]');
        if (!messageElement) return;

        event.preventDefault();
        openMessageActions(
            messageElement.dataset.messageId,
            messageElement,
            event
        );
    });

    document.addEventListener('click', event => {
        const menu = document.getElementById('messageActionsMenu');
        if (menu && !menu.contains(event.target)) closeMessageActionsMenu();
    });

    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            closeMessageActionsMenu();
            closeMessageInfo();
            closeConfirmDeleteMessage();
            cancelReply();
        }
    });

    window.addEventListener('resize', closeMessageActionsMenu);
    container.addEventListener('scroll', closeMessageActionsMenu, { passive: true });
}

// ============================================================
// RENDER MESSAGES (with force update when container shows loading)
// ============================================================
function renderMessages(container, messages, chatId) {
    if (!container) return;
    
    // Принудительно обновляем, если контейнер показывает загрузку
    const isLoading = container.innerHTML.includes('loading') || container.innerHTML.includes('Loading');
    
    const signature = [
        normalizeMessageIdentity(activeLocalProfileId),
        normalizeMessageIdentity(activeLocalNodeId),
        ...messages.map(m =>
            [
                m.id,
                m.packet_id,
                m.kind,
                m.sender,
                m.node_id,
                m.owner_profile_id,
                m.owner_node_id,
                m.text,
                m.time,
                m.reply_to?.id,
                m.reply_to?.packet_id,
                m.reply_to?.text,
                m.status,
                m.error
            ].join('|')
        )
    ].join('||');
    
    if (!isLoading && lastRenderedSignature[chatId] === signature) {
        console.log(`[RENDER] No changes for chat: ${chatId}, skipping render`);
        return;
    }
    
    lastRenderedSignature[chatId] = signature;
    renderedMessagesById = new Map(
        messages
            .filter(message => message && message.id)
            .map(message => [String(message.id), message])
    );

    if (messages.length === 0) {
        const rawChatName = currentChatName || chatId;
        const chatName = currentChatType === 'channel'
            ? formatChannelIndexLabel(rawChatName, channelIndexFromChatId(chatId))
            : rawChatName;
        container.innerHTML = `<div class="loading">💬 ${escapeHtml(window.I18N.t('chat.no_messages_yet', { name: chatName }))}</div>`;
    } else {
        container.innerHTML = messages.map(msg => {
            // A transmitted record is outgoing only for the radio profile
            // that actually sent it.  Messages from another saved local radio
            // are rendered as received after a profile switch.
            const isMe = messageBelongsToActiveRadio(msg);
            const isSystem = msg.kind === 'system' || msg.sender === 'SYSTEM ERROR';
            const sender = escapeHtml(msg.sender || window.I18N.t('nodes.unknown_node'));
            const text = escapeHtml(msg.text || '');
            const time = escapeHtml(msg.time || '');

            const messageId = escapeHtml(String(msg.id || ''));
            const reply = msg.reply_to && typeof msg.reply_to === 'object' ? msg.reply_to : null;
            const replyBlock = reply ? `
                <button type="button" class="message-reply-quote" data-reply-message-id="${escapeHtml(String(reply.id || ''))}" title="${escapeHtml(window.I18N.t('chat.referenced_message'))}">
                    <span class="message-reply-label">↪ ${escapeHtml(String(reply.sender || window.I18N.t('nodes.unknown_node')))}</span>
                    <span class="message-reply-text">${escapeHtml(String(reply.text || ''))}</span>
                </button>
            ` : '';
            const actionsButton = msg.id ? `
                <button type="button"
                        class="message-actions-trigger"
                        data-message-id="${messageId}"
                        title="${escapeHtml(window.I18N.t('chat.message_actions'))}"
                        aria-label="${escapeHtml(window.I18N.t('chat.message_actions'))}"
                        aria-haspopup="menu">⋮</button>
            ` : '';

            if (isSystem) {
                return `
                    <div class="message system" data-message-id="${messageId}">
                        <div class="bubble">
                            ${actionsButton}
                            ${replyBlock}
                            <div class="text">${text}</div>
                            <div class="time">${time}</div>
                        </div>
                    </div>
                `;
            }

            // Outgoing messages go through /api/send asynchronously now:
            // the HTTP response comes back before the radio has actually
            // transmitted anything, so the bubble shows its own lifecycle
            // independently of the request. Channel broadcasts stop at
            // "sent" (Meshtastic has no per-recipient ACK for broadcast -
            // that's a protocol limit, not something this app can fix).
            // DMs go further: sent (awaiting ACK) -> delivered (mesh ACK
            // seen) or unconfirmed (no ACK within the timeout - the
            // message may still have arrived, this only means the ACK
            // itself never came back).
            let statusBadge = '';
            if (isMe) {
                if (msg.status === 'pending') {
                    statusBadge = `<span class="message-status pending" title="${escapeHtml(window.I18N.t('chat.sending'))}">⏳</span>`;
                } else if (msg.status === 'failed') {
                    const errText = escapeHtml(String(msg.error || window.I18N.t('chat.send_failed')));
                    statusBadge = `
                        <span class="message-status failed" title="${errText}">⚠️
                            <button type="button" class="message-retry-btn" data-retry-message-id="${messageId}">${escapeHtml(window.I18N.t('common.retry'))}</button>
                        </span>
                    `;
                } else if (msg.status === 'delivered') {
                    statusBadge = `<span class="message-status delivered" title="${escapeHtml(window.I18N.t('chat.delivered'))}">✓✓</span>`;
                } else if (msg.status === 'unconfirmed') {
                    const reason = msg.error
                        ? window.I18N.t('chat.unconfirmed_reason', { reason: msg.error })
                        : window.I18N.t('chat.unconfirmed');
                    statusBadge = `<span class="message-status unconfirmed" title="${escapeHtml(reason)}">✓</span>`;
                } else if (msg.chat_type === 'channel') {
                    statusBadge = `<span class="message-status transmitted" title="${escapeHtml(window.I18N.t('chat.transmitted_channel_tooltip'))}">✓</span>`;
                } else {
                    statusBadge = `<span class="message-status sent" title="${escapeHtml(window.I18N.t('chat.sent_awaiting_ack'))}">✓</span>`;
                }
            }

            return `
                <div class="message ${isMe ? 'me' : 'rx'}" data-message-id="${messageId}">
                    <div class="bubble">
                        ${actionsButton}
                        <div class="sender">${sender}</div>
                        ${replyBlock}
                        <div class="text">${text}</div>
                        <div class="time">${time}${statusBadge}</div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    initializeMessageActions();

    setTimeout(() => {
        container.scrollTop = container.scrollHeight;
    }, 50);
}

function invalidateCache(chatId) {
    if (messageCache[chatId]) {
        delete messageCache[chatId];
        console.log(`[CACHE] Invalidated cache for chat: ${chatId}`);
    }
    if (lastRenderedSignature[chatId]) {
        lastRenderedSignature[chatId] = null;
    }
}

async function loadChatMessages(chatId, options = {}) {
    if (!chatId) return;

    const {
        forceRefresh = false,
        suppressErrorPlaceholder = false
    } = options;

    const container = document.getElementById('messagesContainer');
    if (!container) return;

    /*
     * Cache is used only for an immediate first paint.
     * Even when the cached copy is fresh, continue with a network request so
     * an open conversation can never remain stale for CACHE_TTL milliseconds.
     */
    const cached = messageCache[chatId];
    const cacheIsFresh = Boolean(
        cached && (Date.now() - cached.timestamp) < CACHE_TTL
    );

    if (!forceRefresh && cacheIsFresh && currentChatId === chatId) {
        console.log(
            `[CACHE] Rendering cached messages for: ${chatId} ` +
            `(${cached.messages.length} messages), then revalidating`
        );
        renderMessages(container, mergeOptimisticMessages(chatId, cached.messages), chatId);
    }

    // Cancel the previous message request instead of merely forgetting its ID.
    if (currentMessageAbortController) {
        currentMessageAbortController.abort();
    }

    const controller = new AbortController();
    currentMessageAbortController = controller;

    const requestId = `${Date.now()}_${chatId}_${Math.random().toString(16).slice(2)}`;
    currentLoadRequest = requestId;

    const timeoutId = window.setTimeout(() => controller.abort(), 12000);

    try {
        const response = await fetch(
            `/api/messages?chat_id=${encodeURIComponent(chatId)}&_=${Date.now()}`,
            {
                signal: controller.signal,
                cache: 'no-store',
                headers: {
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache'
                }
            }
        );

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        if (
            currentChatId !== chatId ||
            currentLoadRequest !== requestId ||
            controller.signal.aborted
        ) {
            return;
        }

        const messages = Array.isArray(data.messages) ? data.messages : [];
        notifyFailedOutgoingMessages(messages);

        messageCache[chatId] = {
            messages,
            timestamp: Date.now()
        };

        const keys = Object.keys(messageCache);
        if (keys.length > 20) {
            const oldestKey = keys.sort((a, b) => {
                return (messageCache[a].timestamp || 0) -
                       (messageCache[b].timestamp || 0);
            })[0];

            delete messageCache[oldestKey];
            console.log(`[CACHE] Removed oldest entry: ${oldestKey}`);
        }

        renderMessages(container, mergeOptimisticMessages(chatId, messages), chatId);

    } catch (error) {
        if (error && error.name === 'AbortError') {
            return;
        }

        console.error('Error loading messages:', error);

        /*
         * Do not replace already rendered messages with an error during a
         * temporary polling failure. Show the placeholder only when there is
         * no usable cached content.
         */
        if (
            !suppressErrorPlaceholder &&
            currentChatId === chatId &&
            currentLoadRequest === requestId &&
            !messageCache[chatId]
        ) {
            container.innerHTML =
                `<div class="loading">⚠️ ${escapeHtml(window.I18N.t('chat.error_loading_messages'))}</div>`;
        }
    } finally {
        window.clearTimeout(timeoutId);

        if (currentLoadRequest === requestId) {
            currentLoadRequest = null;
        }

        if (currentMessageAbortController === controller) {
            currentMessageAbortController = null;
        }
    }
}

let messagePollingInterval = null;

function startMessagePolling(chatId) {
    stopMessagePolling();

    messagePollingInterval = window.setInterval(() => {
        if (currentChatId === chatId && !document.hidden) {
            loadChatMessages(chatId, {
                forceRefresh: true,
                suppressErrorPlaceholder: true
            });
        }
    }, ACTIVE_CHAT_POLL_INTERVAL_MS);
}

function stopMessagePolling() {
    if (messagePollingInterval) {
        window.clearInterval(messagePollingInterval);
        messagePollingInterval = null;
    }

    if (currentMessageAbortController) {
        currentMessageAbortController.abort();
        currentMessageAbortController = null;
    }

    currentLoadRequest = null;
}


document.addEventListener('visibilitychange', () => {
    if (!document.hidden && currentChatId) {
        loadChatMessages(currentChatId, {
            forceRefresh: true,
            suppressErrorPlaceholder: true
        });
    }
});

// ============================================================
// SEND FORM
// ============================================================
const sendForm = document.getElementById('sendForm');

// ------------------------------------------------------------------
// Optimistic outgoing messages.
//
// /api/send now answers as soon as the message is validated and queued
// (no more waiting for a fresh serial connection + the radio round trip),
// but the actual transmission still finishes in the background. Track
// locally-created bubbles here so they can be shown instantly and later
// reconciled (or dropped) once the authoritative copy shows up through
// normal polling of /api/messages.
// ------------------------------------------------------------------
let pendingOptimisticMessages = {}; // chatId -> Map(clientId -> message)

// Server-confirmed sends that failed in the background (radio busy, serial
// timeout, etc.) surface as status:"failed" on the message itself once
// polling picks it up - this used to also post a "SYSTEM ERROR" chat
// message into the primary channel regardless of which chat actually
// failed, which was both confusing and noisy. Instead, report it once via
// the Notifications log/toast (addNotification/showToast) and remember the
// id so the same failure isn't re-announced on every subsequent poll.
let notifiedFailedMessageIds = new Set();

function notifyFailedOutgoingMessages(messageList) {
    if (!Array.isArray(messageList)) return;

    for (const msg of messageList) {
        if (!msg || msg.kind !== 'me' || msg.status !== 'failed') continue;
        if (!msg.id || notifiedFailedMessageIds.has(msg.id)) continue;

        notifiedFailedMessageIds.add(msg.id);

        const where = msg.chat_name || (msg.chat_type === 'dm' ? window.I18N.t('chat.direct_message_label') : window.I18N.t('chat.generic_channel_label'));
        const preview = String(msg.text || '').trim().slice(0, 60);
        const reason = msg.error || window.I18N.t('errors.send_failed');

        if (typeof showToast === 'function') {
            const base = window.I18N.t('chat.message_not_delivered', { where, reason });
            const suffix = preview ? window.I18N.t('chat.message_preview_suffix', { preview }) : '';
            showToast(base + suffix, 'error');
        }
    }

    // Keep the dedup set from growing forever across a long session.
    if (notifiedFailedMessageIds.size > 500) {
        notifiedFailedMessageIds = new Set(Array.from(notifiedFailedMessageIds).slice(-200));
    }
}

function getActiveLocalSenderName() {
    const el = document.getElementById('baseNodeName');
    const text = el ? el.textContent.trim() : '';
    return text || window.I18N.t('chat.me_fallback');
}

function mergeOptimisticMessages(chatId, serverMessages) {
    const pending = pendingOptimisticMessages[chatId];
    if (!pending || pending.size === 0) return serverMessages;

    // Once the authoritative message (matched by the client_id we
    // generated) appears in a poll response, the local placeholder has
    // done its job and can be dropped.
    const serverClientIds = new Set(
        serverMessages.map(m => m.client_id).filter(Boolean)
    );
    for (const clientId of Array.from(pending.keys())) {
        if (serverClientIds.has(clientId)) {
            pending.delete(clientId);
        }
    }

    if (pending.size === 0) return serverMessages;
    return serverMessages.concat(Array.from(pending.values()));
}

function renderChatIfActive(chatId) {
    if (currentChatId !== chatId) return;
    const container = document.getElementById('messagesContainer');
    if (!container) return;
    const base = (messageCache[chatId] && messageCache[chatId].messages) || [];
    renderMessages(container, mergeOptimisticMessages(chatId, base), chatId);
}

function buildOptimisticMessage(clientId, chatId, chatType, chatName, text, replyTo) {
    return {
        id: `local-${clientId}`,
        client_id: clientId,
        kind: 'me',
        sender: getActiveLocalSenderName(),
        node_id: activeLocalNodeId || '',
        owner_node_id: activeLocalNodeId || '',
        owner_profile_id: activeLocalProfileId || '',
        text: text,
        time: TimeFormatter.formatTime(new Date()),
        chat_id: chatId,
        chat_type: chatType,
        chat_name: chatName,
        reply_to: replyTo || null,
        status: 'pending'
    };
}

async function submitOutgoingMessage(chatId, chatType, chatName, text, replyTo) {
    const clientId = `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
    const tempMsg = buildOptimisticMessage(clientId, chatId, chatType, chatName, text, replyTo);

    if (!pendingOptimisticMessages[chatId]) {
        pendingOptimisticMessages[chatId] = new Map();
    }
    pendingOptimisticMessages[chatId].set(clientId, tempMsg);

    // Paint the bubble before any network round trip happens.
    renderChatIfActive(chatId);
    loadChatList();

    try {
        const payload = { text: text, chat_id: chatId, client_id: clientId };
        if (chatType === 'dm') payload.target_node = chatId;
        if (replyTo) payload.reply_to = replyTo;

        const response = await fetch('/api/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `HTTP ${response.status}`);
        }

        // The server has already stored the real message (status
        // "pending"); pull it in now instead of waiting for the next poll
        // tick, then drop the local placeholder.
        invalidateCache(chatId);
        await loadChatMessages(chatId, { forceRefresh: true, suppressErrorPlaceholder: true });
        loadChatList();

    } catch (error) {
        console.error('Error sending message:', error);
        tempMsg.status = 'failed';
        tempMsg.error = (error && error.message) ? error.message : window.I18N.t('errors.network_error');
        renderChatIfActive(chatId);

        if (typeof showToast === 'function') {
            const preview = String(text || '').trim().slice(0, 60);
            const where = chatName || (chatType === 'dm' ? window.I18N.t('chat.direct_message_label') : window.I18N.t('chat.generic_channel_label'));
            const base = window.I18N.t('chat.message_not_delivered', { where, reason: tempMsg.error });
            const suffix = preview ? window.I18N.t('chat.message_preview_suffix', { preview }) : '';
            showToast(base + suffix, 'error');
        }
    }
}

function retryFailedMessage(messageId) {
    // Case 1: the message never made it past the browser (network error,
    // request never reached the server) - it only exists as a local
    // placeholder.
    for (const pending of Object.values(pendingOptimisticMessages)) {
        for (const [clientId, msg] of pending.entries()) {
            if (msg.id === messageId) {
                pending.delete(clientId);
                submitOutgoingMessage(msg.chat_id, msg.chat_type, msg.chat_name, msg.text, msg.reply_to);
                return;
            }
        }
    }

    // Case 2: the server accepted and stored the message, but the
    // background send worker could not actually transmit it (radio busy,
    // timeout, etc). Re-attempt the SAME stored record via /api/send/retry
    // instead of submitOutgoingMessage(), which would POST a brand-new
    // message and leave both the old "failed" bubble and a new one in the
    // chat history.
    const chatId = currentChatId;
    const cachedMessages = (messageCache[chatId] && messageCache[chatId].messages) || [];
    const serverMsg = cachedMessages.find(m => String(m.id) === String(messageId));
    if (serverMsg) {
        retryStoredMessage(chatId, messageId);
    }
}

async function retryStoredMessage(chatId, messageId) {
    try {
        const response = await fetch('/api/send/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: chatId, message_id: messageId })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `HTTP ${response.status}`);
        }

        invalidateCache(chatId);
        await loadChatMessages(chatId, { forceRefresh: true, suppressErrorPlaceholder: true });
        loadChatList();
    } catch (error) {
        console.error('Error retrying message:', error);
        if (typeof showToast === 'function') {
            const reason = (error && error.message) ? error.message : window.I18N.t('errors.network_error');
            showToast(window.I18N.t('chat.retry_failed', { reason }), 'error');
        }
    }
}

if (sendForm) {
    sendForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const input = document.getElementById('messageInput');
        const text = input ? input.value.trim() : '';
        if (!text || !currentChatId) return;

        if (currentChatType === 'dm' && currentChatId !== 'channel') {
            const isIgnored = await checkNodeIgnored(currentChatId);
            if (isIgnored) {
                if (!confirm(`⚠️ ${window.I18N.t('chat.ignored_node_confirm', { name: currentChatName })}`)) {
                    return;
                }
            }
        }

        const chatId = currentChatId;
        const chatType = currentChatType;
        const chatName = currentChatName;
        const replyTo = activeReply;

        // Clear the composer immediately - the message is already rendered
        // as a pending bubble by submitOutgoingMessage below, there is
        // nothing left in this handler worth blocking input for.
        if (input) {
            input.value = '';
            input.focus();
        }
        cancelReply();

        submitOutgoingMessage(chatId, chatType, chatName, text, replyTo);
    });
}

// ============================================================
// CHAT ACTIONS
// ============================================================
function showChatActions() {
    const modal = document.getElementById('chatActionsModal');
    if (modal) {
        modal.style.display = 'flex';
        const deleteBtn = document.getElementById('deleteChatBtn');
        const clearBtn = document.getElementById('clearChatBtn');
        if (deleteBtn) {
            deleteBtn.style.display = currentChatType === 'channel' ? 'none' : 'block';
        }
        if (clearBtn) {
            clearBtn.style.display = 'block';
        }
    }
}

function closeChatActions() {
    const modal = document.getElementById('chatActionsModal');
    if (modal) modal.style.display = 'none';
}

function showConfirmDelete(chatName, chatId) {
    deleteTargetChatId = chatId;
    const modal = document.getElementById('confirmDeleteModal');
    const text = document.getElementById('confirmDeleteText');
    if (modal && text) {
        text.textContent = window.I18N.t('chat.delete_chat_confirm', { name: chatName });
        modal.style.display = 'flex';
    }
}

function closeConfirmDelete() {
    const modal = document.getElementById('confirmDeleteModal');
    if (modal) modal.style.display = 'none';
    deleteTargetChatId = null;
}

async function executeDeleteChat() {
    if (!deleteTargetChatId) return;
    
    try {
        const response = await fetch('/api/delete_chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ chat_id: deleteTargetChatId })
        });

        closeConfirmDelete();

        if (response.ok) {
            invalidateCache(deleteTargetChatId);
            showChatList();
        } else {
            const error = await response.json();
            const reason = window.I18N.tOrFallback('errors.' + (error.error_code || ''), error.error_params, error.error || window.I18N.t('errors.unknown_error'));
            alert(window.I18N.t('chat.delete_chat_failed', { reason }));
        }
    } catch (error) {
        console.error('Error deleting chat:', error);
        alert(window.I18N.t('errors.network_error'));
    }
}

async function deleteCurrentChat() {
    if (!currentChatId || currentChatType === 'channel') return;
    closeChatActions();
    showConfirmDelete(currentChatName, currentChatId);
}

function showConfirmClear(chatName, chatId) {
    clearTargetChatId = chatId;
    const modal = document.getElementById('confirmClearModal');
    const text = document.getElementById('confirmClearText');
    if (modal && text) {
        text.textContent = window.I18N.t('chat.clear_chat_confirm', { name: chatName });
        modal.style.display = 'flex';
    }
}

function closeConfirmClear() {
    const modal = document.getElementById('confirmClearModal');
    if (modal) modal.style.display = 'none';
    clearTargetChatId = null;
}

async function executeClearChat() {
    if (!clearTargetChatId) return;
    
    try {
        const response = await fetch('/api/clear_chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ chat_id: clearTargetChatId })
        });

        closeConfirmClear();

        if (response.ok) {
            invalidateCache(clearTargetChatId);
            lastMessagesSignature = '';
            await loadChatMessages(clearTargetChatId);
            loadChatList();
            loadMessages();
        } else {
            const error = await response.json();
            const reason = window.I18N.tOrFallback('errors.' + (error.error_code || ''), error.error_params, error.error || window.I18N.t('errors.unknown_error'));
            alert(window.I18N.t('chat.clear_chat_failed', { reason }));
        }
    } catch (error) {
        console.error('Error clearing chat:', error);
        alert(window.I18N.t('errors.network_error'));
    }
}

function clearCurrentChat() {
    if (!currentChatId) return;
    closeChatActions();
    const displayName = currentChatType === 'channel'
        ? formatChannelIndexLabel(currentChatName, channelIndexFromChatId(currentChatId))
        : currentChatName;
    showConfirmClear(displayName, currentChatId);
}

// ============================================================
// NODE OPERATIONS
// ============================================================
function setDirectMessage(nodeId, nodeName) {
    if (directMessageTarget === nodeId) {
        directMessageTarget = null;
        document.querySelectorAll('.node-title-btn').forEach(btn => {
            btn.style.background = 'linear-gradient(135deg, #4a5a7a 0%, #3a4a6a 100%)';
            btn.style.boxShadow = 'none';
        });
        document.getElementById('messageInput')?.focus();
        return;
    }

    directMessageTarget = nodeId;
    document.querySelectorAll('.node-title-btn').forEach(btn => {
        if (btn.dataset.nodeId === nodeId) {
            btn.style.background = '#ff9800';
            btn.style.boxShadow = '0 0 0 3px rgba(255, 152, 0, 0.4)';
        } else {
            btn.style.background = 'linear-gradient(135deg, #4a5a7a 0%, #3a4a6a 100%)';
            btn.style.boxShadow = 'none';
        }
    });

    openChat(nodeId, nodeName, 'dm');
}

async function toggleIgnore(nodeId) {
    try {
        const response = await fetch('/api/toggle_ignore', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ node_id: nodeId })
        });

        if (response.ok) {
            const data = await response.json();

            // [NEW] Update local nodeCache and reset render cache
            const cachedNode = nodeCache.find(node => node.node_id === nodeId);
            if (cachedNode) {
                cachedNode.ignored = Boolean(data.ignored);
            }
            resetNodeRenderCache(nodeId);

            loadMessages();
            loadChatList();
            
            updateNodeDetails(nodeId);
            showToast(
                data.ignored ? window.I18N.t('chat.node_ignored') : window.I18N.t('chat.node_restored'),
                data.ignored ? 'warning' : 'success'
            );

            if (currentChatId === nodeId) {
                // Avoid duplicating the bottom notification with a chat banner.
                hideIgnoredBanner();
                invalidateCache(nodeId);
                lastMessagesSignature = '';
                await loadChatMessages(nodeId);
            }
        } else {
            const error = await response.json();
            alert(window.I18N.t('chat.toggle_ignore_failed', { reason: error.error || window.I18N.t('errors.unknown_error') }));
        }
    } catch (error) {
        console.error('Error toggling ignore:', error);
        alert(window.I18N.t('errors.network_error'));
    }
}

async function toggleFavorite(nodeId) {
    try {
        const response = await fetch('/api/toggle_favorite', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ node_id: nodeId })
        });

        if (response.ok) {
            const data = await response.json();

            // [NEW] Update local nodeCache and reset render cache
            const cachedNode = nodeCache.find(node => node.node_id === nodeId);
            if (cachedNode) {
                cachedNode.favorite = Boolean(data.favorite);
            }
            resetNodeRenderCache(nodeId);

            loadMessages();
            loadChatList();
            updateNodeDetails(nodeId);
            showToast(
                data.favorite ? window.I18N.t('chat.node_added_to_favorites') : window.I18N.t('chat.node_removed_from_favorites'),
                'success'
            );
        } else {
            const error = await response.json();
            alert(window.I18N.t('chat.toggle_favorite_failed', { reason: error.error || window.I18N.t('errors.unknown_error') }));
        }
    } catch (error) {
        console.error('Error toggling favorite:', error);
        alert(window.I18N.t('errors.network_error'));
    }
}

function updateNodeDetails(nodeId) {
    const cachedNode = nodeCache.find(n => n.node_id === nodeId);
    if (cachedNode) {
        renderNodeDetails(cachedNode);
        return;
    }
    
    fetch('/api/messages')
        .then(response => response.json())
        .then(data => {
            nodeCache = mergeNodeCachePreservingPosition(
                data.nodes || []
            );

            populateReferenceNodeSelect();
            updateReferenceLocationSummary();

            const allNodes = nodeCache;
            const selectedNode = allNodes.find(n => n.node_id === nodeId);
            if (selectedNode) {
                renderNodeDetails(selectedNode);
            } else {
                renderNodeDetails(null);
            }
        })
        .catch(error => {
            console.error('Error updating node details:', error);
        });
}

function formatNodePositionUpdated(position) {
    if (!position || typeof position !== 'object') {
        return '--';
    }

    const timestamp = Number(position.updated);

    if (Number.isFinite(timestamp) && timestamp > 0) {
        const updatedDate = new Date(timestamp * 1000);

        return TimeFormatter.formatDateTime(updatedDate);
    }

    return position.updated_time || '--';
}

function getMapProvider() {
    const provider = String(
        appSettings?.maps?.provider || 'osm'
    ).toLowerCase();

    return provider === 'google' ? 'google' : 'osm';
}


let meshMap = null;
let meshMapTileLayer = null;
let meshMapMarkers = new Map();
let meshMapReferenceMarker = null;
let meshMapReferenceLine = null;
let meshMapTargetNodeId = null;
let meshMapResizeObserver = null;
let meshMapResizeTimer = null;
let meshMapWaypointMarkers = new Map();
let meshMapWaypoints = [];
let waypointToolsItems = [];
let waypointToolsSelectedIds = new Set();
let waypointVisibilityPending = new Set();
let meshMapWaypointsVisible = true;
let waypointToolsShowExpired = false;
let waypointToolsRefreshInFlight = false;
let waypointMapRefreshInFlight = false;
let pendingWaypointCoordinates = null;
let waypointSendOperation = null;
let waypointSendNotificationId = null;

let meshMapWaypointPollTimer = null;
let meshMapWaypointSignature = '';
let meshMapWaypointExpiryTimer = null;
let meshMapWaypointKnownIds = new Set();
let meshMapWaypointInitialLoadDone = false;
let selectedWaypointId = null;

function getNodePosition(node) {
    const latitude = Number(node?.position?.latitude);
    const longitude = Number(node?.position?.longitude);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
    return { latitude, longitude };
}

function getNodeDisplayName(node) {
    return String(node?.clean_name || node?.name || node?.long_name || node?.short_name || node?.node_id || 'Unknown node');
}

function escapeJsString(value) {
    return String(value ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/\r/g, '\\r')
        .replace(/\n/g, '\\n');
}

function createMeshMapIcon(kind = 'node') {
    return L.divIcon({
        className: `meshcenter-map-marker ${kind}`,
        html: '<div class="meshcenter-map-marker-dot"></div>',
        iconSize: [20, 20],
        iconAnchor: [10, 10],
        popupAnchor: [0, -11]
    });
}

function updateMeshMapNodeLabelLevel() {
    if (!meshMap) return;

    const container = meshMap.getContainer();
    if (!container) return;

    const zoom = Number(meshMap.getZoom()) || 0;
    let nodeLevel = 'none';

    // Progressive map detail, shifted two zoom steps earlier:
    //  - below 13: selected node label only
    //  - 13: recently active nodes
    //  - 14: active and away nodes
    //  - 15+: all visible node names
    if (zoom >= 15) {
        nodeLevel = 'all';
    } else if (zoom >= 14) {
        nodeLevel = 'away';
    } else if (zoom >= 13) {
        nodeLevel = 'online';
    }

    container.dataset.nodeLabelLevel = nodeLevel;

    // Waypoint labels are navigation aids, so they appear earlier than
    // ordinary node names. Only waypoints already visible by the current
    // map filters receive a label.
    container.dataset.waypointLabelLevel = zoom >= 12 ? 'visible' : 'none';
}

function waypointIconCharacter(iconValue) {
    const value = Number(iconValue);
    if (Number.isInteger(value) && value > 0) {
        try { return String.fromCodePoint(value); } catch (error) { /* fall through */ }
    }
    return '📍';
}

function createWaypointMapIcon(waypoint) {
    const iconChar = waypointIconCharacter(waypoint?.icon);
    const expired = waypoint?.is_active === false || formatWaypointExpiryDetails(waypoint?.expire_at).expired;
    return L.divIcon({
        className: `meshcenter-waypoint-marker${expired ? ' is-expired' : ''}`,
        html: `<div class="meshcenter-waypoint-pin"><span>${escapeHtml(iconChar)}</span></div>`,
        iconSize: [30, 36],
        iconAnchor: [15, 34],
        popupAnchor: [0, -32]
    });
}

function formatWaypointTime(timestamp) {
    const seconds = Number(timestamp);
    if (!Number.isFinite(seconds) || seconds <= 0) return '--';
    return TimeFormatter.formatDateTime(seconds);
}

function formatWaypointExpiryDetails(expireAt) {
    const seconds = Number(expireAt);
    if (!Number.isFinite(seconds) || seconds <= 0) {
        return { relative: window.I18N.t('waypoints.no_expiration'), absolute:'', expired:false };
    }

    let remaining = Math.floor(seconds - Date.now() / 1000);
    const expiresDate = new Date(seconds * 1000);
    const absoluteTime = TimeFormatter.formatTime(expiresDate);
    const absoluteDateTime = TimeFormatter.formatDateTime(expiresDate);

    if (remaining <= 0) {
        return { relative: window.I18N.t('waypoints.expired'), absolute:absoluteDateTime, expired:true };
    }

    const days = Math.floor(remaining / 86400);
    remaining %= 86400;
    const hours = Math.floor(remaining / 3600);
    remaining %= 3600;
    const minutes = Math.floor(remaining / 60);

    const inPrefix = window.I18N.t('waypoints.in_prefix');

    if (days > 0) {
        const tail = hours > 0 ? ` ${hours} h` : (minutes > 0 ? ` ${minutes} min` : '');
        return { relative:`${inPrefix} ${days} d${tail}`, absolute:absoluteDateTime, expired:false };
    }
    if (hours > 0) {
        return {
            relative:`${inPrefix} ${hours} h${minutes > 0 ? ` ${minutes} min` : ''}`,
            absolute:absoluteTime,
            expired:false
        };
    }
    return { relative:`${inPrefix} ${Math.max(1, minutes)} min`, absolute:absoluteTime, expired:false };
}

function waypointExpiryHtml(expireAt) {
    const value = Number(expireAt) || 0;
    const formatted = formatWaypointExpiryDetails(value);
    return `<span class="waypoint-expire-value" data-waypoint-expire="${value}">` +
        `<span class="waypoint-expire-relative">${escapeHtml(formatted.relative)}</span>` +
        (formatted.absolute ? `<small class="waypoint-expire-absolute">${escapeHtml(formatted.absolute)}</small>` : '') +
        `</span>`;
}

function updateOpenWaypointExpiryLabels() {
    document.querySelectorAll('[data-waypoint-expire]').forEach(element => {
        const formatted = formatWaypointExpiryDetails(element.dataset.waypointExpire);
        const relative = element.querySelector('.waypoint-expire-relative');
        const absolute = element.querySelector('.waypoint-expire-absolute');
        if (relative) relative.textContent = formatted.relative;
        if (absolute) absolute.textContent = formatted.absolute;
        element.classList.toggle('is-expired', formatted.expired);
    });
}

function startWaypointExpiryTimer() {
    if (meshMapWaypointExpiryTimer) return;
    updateOpenWaypointExpiryLabels();
    meshMapWaypointExpiryTimer = setInterval(updateOpenWaypointExpiryLabels, 30000);
}

function buildWaypointPopup(waypoint) {
    const lat = Number(waypoint?.latitude);
    const lon = Number(waypoint?.longitude);
    const nav = Number.isFinite(lat) && Number.isFinite(lon)
        ? getNodeDistanceAndBearing(lat, lon)
        : { distanceText:'--', bearingText:'--' };
    const name = waypoint?.name || `Waypoint ${waypoint?.waypoint_id || ''}`;
    const sender = waypoint?.sender_name || waypoint?.sender_id || window.I18N.t('nodes.unknown_node');
    const description = waypoint?.description || window.I18N.t('waypoints.no_description');
    const channel = Number.isFinite(Number(waypoint?.channel_index)) ? Number(waypoint.channel_index) : '--';
    const expired = waypoint?.is_active === false || formatWaypointExpiryDetails(waypoint?.expire_at).expired;
    return `
        <div class="map-popup-name waypoint-popup-name">${escapeHtml(waypointIconCharacter(waypoint?.icon))} ${escapeHtml(name)}</div>
        <div class="map-popup-subtitle">${escapeHtml(description)}</div>
        <div class="map-popup-grid">
            <span>${escapeHtml(window.I18N.t('waypoints.status'))}</span><strong class="${expired ? 'waypoint-status-expired' : 'waypoint-status-active'}">${expired ? escapeHtml(window.I18N.t('waypoints.expired')) : escapeHtml(window.I18N.t('waypoints.active'))}</strong>
            <span>${escapeHtml(window.I18N.t('waypoints.sender'))}</span><strong>${escapeHtml(sender)}</strong>
            <span>${escapeHtml(window.I18N.t('nodes.distance'))}</span><strong>${escapeHtml(nav.distanceText)}</strong>
            <span>${escapeHtml(window.I18N.t('nodes.bearing'))}</span><strong>${escapeHtml(nav.bearingText)}</strong>
            <span>${escapeHtml(window.I18N.t('waypoints.channel_label'))}</span><strong>${escapeHtml(channel)}</strong>
            <span>${escapeHtml(window.I18N.t('waypoints.received'))}</span><strong>${escapeHtml(formatWaypointTime(waypoint?.received_at))}</strong>
            <span>${escapeHtml(window.I18N.t('waypoints.expires'))}</span><strong>${waypointExpiryHtml(waypoint?.expire_at)}</strong>
            <span>${escapeHtml(window.I18N.t('waypoints.coordinates'))}</span><strong>${Number.isFinite(lat) ? lat.toFixed(6) : '--'}, ${Number.isFinite(lon) ? lon.toFixed(6) : '--'}</strong>
        </div>
        <div class="map-popup-actions">
            <button class="map-popup-primary-btn" type="button" onclick="centerMapOnWaypoint('${escapeJsString(waypoint?.waypoint_id)}')">⌖ ${escapeHtml(window.I18N.t('waypoints.center'))}</button>
            <button class="map-popup-action-btn" type="button" onclick="openExternalNodeMap('${Number.isFinite(lat) ? lat : ''}', '${Number.isFinite(lon) ? lon : ''}')">↗ ${escapeHtml(window.I18N.t('waypoints.navigate'))}</button>
            <button class="map-popup-action-btn" type="button" title="${escapeHtml(window.I18N.t('waypoints.copy_coordinates_tooltip'))}" onclick="copyCoordinates('${Number.isFinite(lat) ? lat : ''}', '${Number.isFinite(lon) ? lon : ''}')">📋 ${escapeHtml(window.I18N.t('waypoints.coordinates'))}</button>
            <button class="map-popup-action-btn danger" type="button" onclick="setWaypointHidden('${escapeJsString(waypoint?.waypoint_id)}', true)">🙈 ${escapeHtml(window.I18N.t('waypoints.hide'))}</button>
            <button class="map-popup-action-btn map-popup-close-btn" type="button" onclick="closeWaypointPopup()">✕ ${escapeHtml(window.I18N.t('common.close'))}</button>
        </div>
    `;
}

function updateWaypointBulkControls() {
    const deleteButton = document.getElementById('waypointDeleteSelected');
    if (deleteButton) deleteButton.disabled = waypointToolsSelectedIds.size === 0;
    const selectAll = document.getElementById('waypointSelectAll');
    if (selectAll) {
        const shownIds = waypointToolsItems.map(item => String(item?.waypoint_id));
        const selectedShown = shownIds.filter(id => waypointToolsSelectedIds.has(id)).length;
        selectAll.checked = shownIds.length > 0 && selectedShown === shownIds.length;
        selectAll.indeterminate = selectedShown > 0 && selectedShown < shownIds.length;
    }
}

function toggleWaypointSelection(waypointId, selected) {
    const id = String(waypointId);
    if (selected) waypointToolsSelectedIds.add(id);
    else waypointToolsSelectedIds.delete(id);
    updateWaypointBulkControls();
}

function toggleSelectAllWaypoints(selected) {
    waypointToolsItems.forEach(item => {
        const id = String(item?.waypoint_id);
        if (selected) waypointToolsSelectedIds.add(id);
        else waypointToolsSelectedIds.delete(id);
    });
    renderWaypointToolsList();
}

function renderWaypointToolsList() {
    const container = document.getElementById('waypointToolsList');
    if (!container) return;
    const items = Array.isArray(waypointToolsItems) ? waypointToolsItems : [];
    const validIds = new Set(items.map(item => String(item?.waypoint_id)));
    waypointToolsSelectedIds = new Set([...waypointToolsSelectedIds].filter(id => validIds.has(id)));
    if (!items.length) {
        container.innerHTML = `<div class="waypoint-tools-empty">${escapeHtml(waypointToolsShowExpired ? window.I18N.t('waypoints.no_saved_waypoints') : window.I18N.t('waypoints.no_active_waypoints'))}</div>`;
        updateWaypointBulkControls();
        return;
    }
    container.innerHTML = items.map(item => {
        const id = String(item?.waypoint_id);
        const name = item?.name || `Waypoint ${id}`;
        const sender = item?.sender_name || item?.sender_id || window.I18N.t('nodes.unknown_node');
        const expiry = formatWaypointExpiryDetails(item?.expire_at);
        const hidden = Boolean(item?.is_hidden);
        const expired = item?.is_active === false || expiry.expired;
        const pending = waypointVisibilityPending.has(id);
        const selected = id === String(selectedWaypointId || '');
        return `<div class="waypoint-tools-item ${expired ? 'is-expired' : ''} ${hidden ? 'is-hidden' : ''} ${pending ? 'is-pending' : ''} ${selected ? 'is-selected' : ''}" data-waypoint-id="${escapeHtml(id)}">` +
            `<label class="waypoint-tools-select" title="${escapeHtml(window.I18N.t('waypoints.select_tooltip'))}"><input type="checkbox" ${waypointToolsSelectedIds.has(id) ? 'checked' : ''} onchange="toggleWaypointSelection('${escapeJsString(id)}', this.checked)"></label>` +
            `<button type="button" class="waypoint-tools-main" onclick="showWaypointOnMap('${escapeJsString(id)}')" title="${escapeHtml(window.I18N.t('waypoints.open_on_map_tooltip'))}">` +
            `<span class="waypoint-tools-icon">${escapeHtml(waypointIconCharacter(item?.icon))}</span>` +
            `<span class="waypoint-tools-copy"><strong>${escapeHtml(name)}</strong>` +
            `<small>${escapeHtml(sender)} · ${escapeHtml(expired ? window.I18N.t('waypoints.expired') : expiry.relative)}</small></span></button>` +
            `<button type="button" class="waypoint-tools-visibility" title="${escapeHtml(hidden ? window.I18N.t('waypoints.show_waypoint') : window.I18N.t('waypoints.hide_waypoint'))}" ` +
            `onclick="setWaypointHidden('${escapeJsString(id)}', ${hidden ? 'false' : 'true'})" ${pending ? 'disabled' : ''}>${pending ? '…' : (hidden ? '👁' : '🙈')}</button>` +
            `<button type="button" class="waypoint-tools-delete" title="${escapeHtml(window.I18N.t('modals.delete_locally'))}" onclick="deleteWaypoint('${escapeJsString(id)}')">🗑</button></div>`;
    }).join('');
    updateWaypointBulkControls();
}

function waypointToolsListUrl() {
    // Archive controls only expired records. Map visibility is independent:
    // hidden active waypoints must remain available in the Tools list.
    return waypointToolsShowExpired
        ? '/api/waypoints?include_expired=1&include_hidden=1'
        : '/api/waypoints?include_hidden=1';
}

async function refreshWaypointToolsList(force = false) {
    if (waypointToolsRefreshInFlight && !force) return;
    waypointToolsRefreshInFlight = true;
    try {
        const response = await fetch(waypointToolsListUrl(), { cache:'no-store' });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || window.I18N.t('waypoints.could_not_load'));
        waypointToolsItems = Array.isArray(payload.waypoints) ? payload.waypoints : [];
        renderWaypointToolsList();
    } catch (error) {
        showToast(error.message || window.I18N.t('waypoints.could_not_load'), 'error');
    } finally {
        waypointToolsRefreshInFlight = false;
    }
}

function toggleWaypointArchive(show) {
    waypointToolsShowExpired = Boolean(show);
    refreshWaypointToolsList(true);
}

async function setWaypointHidden(waypointId, hidden, options = {}) {
    const id = String(waypointId);
    if (waypointVisibilityPending.has(id)) return false;
    const toolsIndex = waypointToolsItems.findIndex(item => String(item?.waypoint_id) === id);
    const mapIndex = meshMapWaypoints.findIndex(item => String(item?.waypoint_id) === id);
    const previousTools = toolsIndex >= 0 ? { ...waypointToolsItems[toolsIndex] } : null;
    const previousMap = mapIndex >= 0 ? { ...meshMapWaypoints[mapIndex] } : null;

    waypointVisibilityPending.add(id);
    if (toolsIndex >= 0) waypointToolsItems[toolsIndex] = { ...waypointToolsItems[toolsIndex], is_hidden: Boolean(hidden) };
    if (hidden) {
        meshMapWaypoints = meshMapWaypoints.filter(item => String(item?.waypoint_id) !== id);
    } else if (toolsIndex >= 0) {
        meshMapWaypoints = [
            { ...waypointToolsItems[toolsIndex], is_hidden:false },
            ...meshMapWaypoints.filter(item => String(item?.waypoint_id) !== id)
        ];
    }
    renderWaypointToolsList();
    if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });

    try {
        const response = await fetch(`/api/waypoints/${encodeURIComponent(id)}/hidden`, {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({ hidden:Boolean(hidden) })
        });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || window.I18N.t('waypoints.update_failed'));
        if (payload.waypoint) {
            const item = { ...payload.waypoint, is_hidden:Boolean(hidden) };
            const idx = waypointToolsItems.findIndex(row => String(row?.waypoint_id) === id);
            if (idx >= 0) waypointToolsItems[idx] = item;
            if (!hidden) {
                meshMapWaypoints = [item, ...meshMapWaypoints.filter(row => String(row?.waypoint_id) !== id)];
            }
        }

        // Always synchronize both views after a visibility change. This is
        // essential for expired waypoints because the map and Tools use
        // different API filters.
        await Promise.all([
            refreshMeshMapWaypoints(true),
            refreshWaypointToolsList(true)
        ]);
        if (!options.silent) showToast(hidden ? window.I18N.t('waypoints.hidden_locally') : window.I18N.t('waypoints.visible_again'), 'success');
        return true;
    } catch (error) {
        if (toolsIndex >= 0 && previousTools) waypointToolsItems[toolsIndex] = previousTools;
        if (previousMap) meshMapWaypoints = [previousMap, ...meshMapWaypoints.filter(item => String(item?.waypoint_id) !== id)];
        else meshMapWaypoints = meshMapWaypoints.filter(item => String(item?.waypoint_id) !== id);
        showToast(error.message || window.I18N.t('waypoints.update_failed'), 'error');
        return false;
    } finally {
        waypointVisibilityPending.delete(id);
        renderWaypointToolsList();
        if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
    }
}

async function deleteWaypoint(waypointId) {
    const id = String(waypointId);
    const item = waypointToolsItems.find(row => String(row?.waypoint_id) === id);
    const name = item?.name || `Waypoint ${id}`;
    if (!window.confirm(window.I18N.t('waypoints.delete_named_confirm', { name }))) return;
    try {
        const response = await fetch(`/api/waypoints/${encodeURIComponent(id)}`, { method:'DELETE' });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || window.I18N.t('waypoints.delete_failed'));
        waypointToolsItems = waypointToolsItems.filter(row => String(row?.waypoint_id) !== id);
        meshMapWaypoints = meshMapWaypoints.filter(row => String(row?.waypoint_id) !== id);
        waypointToolsSelectedIds.delete(id);
        renderWaypointToolsList();
        if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        showToast(window.I18N.t('waypoints.deleted_locally'), 'success');
    } catch (error) {
        showToast(error.message || window.I18N.t('waypoints.delete_failed'), 'error');
    }
}

async function deleteSelectedWaypoints() {
    const ids = [...waypointToolsSelectedIds];
    if (!ids.length) return;
    if (!window.confirm(window.I18N.plural('waypoints.delete_selected_confirm', ids.length, { count: ids.length }))) return;
    try {
        const response = await fetch('/api/waypoints/delete', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({ waypoint_ids: ids.map(Number) })
        });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || window.I18N.t('waypoints.delete_failed'));
        const idSet = new Set(ids);
        waypointToolsItems = waypointToolsItems.filter(row => !idSet.has(String(row?.waypoint_id)));
        meshMapWaypoints = meshMapWaypoints.filter(row => !idSet.has(String(row?.waypoint_id)));
        waypointToolsSelectedIds.clear();
        renderWaypointToolsList();
        if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        showToast(window.I18N.plural('waypoints.deleted_count', payload.deleted || ids.length, { count: payload.deleted || ids.length }), 'success');
    } catch (error) {
        showToast(error.message || window.I18N.t('waypoints.delete_failed'), 'error');
    }
}

async function deleteAllWaypoints() {
    if (!window.confirm(window.I18N.t('waypoints.delete_all_confirm'))) return;
    try {
        const response = await fetch('/api/waypoints', { method:'DELETE' });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || window.I18N.t('waypoints.cleanup_failed'));
        waypointToolsItems = [];
        meshMapWaypoints = [];
        waypointToolsSelectedIds.clear();
        renderWaypointToolsList();
        if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        showToast(window.I18N.plural('waypoints.deleted_count', payload.deleted || 0, { count: payload.deleted || 0 }), 'success');
    } catch (error) {
        showToast(error.message || window.I18N.t('waypoints.cleanup_failed'), 'error');
    }
}

function normalizeWaypointProfileId(value) {
    return String(value || '').trim().toLowerCase();
}

function waypointFallbackChannel(index = 0) {
    const safeIndex = Number.isInteger(Number(index))
        ? Math.max(0, Math.min(7, Number(index)))
        : 0;
    return {
        id: safeIndex === 0 ? 'channel' : `channel:${safeIndex}`,
        index: safeIndex,
        name: 'Channel'
    };
}

function normalizeWaypointChannels(channels) {
    if (!Array.isArray(channels)) return [waypointFallbackChannel(0)];

    const byIndex = new Map();
    channels.forEach((channel, fallbackIndex) => {
        const index = Number(channel?.index ?? fallbackIndex);
        if (!Number.isInteger(index) || index < 0 || index > 7) return;

        let name = String(channel?.name || '').trim();
        if (!name || /^channel\s+\d+$/i.test(name)) {
            name = 'Channel';
        }

        byIndex.set(index, {
            id: String(channel?.id || (index === 0 ? 'channel' : `channel:${index}`)),
            index,
            name
        });
    });

    const normalized = [...byIndex.values()]
        .sort((a, b) => a.index - b.index);

    return normalized.length ? normalized : [waypointFallbackChannel(0)];
}

function renderWaypointChannelOptions(channels, preferredIndex = 0) {
    const select = document.getElementById('waypointCreateChannel');
    if (!select) return 0;

    const normalized = normalizeWaypointChannels(channels);
    waypointComposerChannels = normalized;

    select.innerHTML = normalized.map(channel => {
        const label = formatChannelIndexLabel(channel.name, channel.index);
        return `<option value="${channel.index}">${escapeHtml(label)}</option>`;
    }).join('');

    const preferred = Number(preferredIndex);
    const selected = normalized.some(channel => channel.index === preferred)
        ? preferred
        : (normalized.find(channel => channel.index === 0)?.index
            ?? normalized[0].index);

    select.value = String(selected);
    return selected;
}

function getWaypointComposerDefaults(profileId = waypointActiveProfileId) {
    const waypointSettings = appSettings?.waypoints || {};
    const normalizedProfileId = normalizeWaypointProfileId(profileId);
    const profileDefaults = normalizedProfileId
        ? waypointSettings?.profile_defaults?.[normalizedProfileId]
        : null;
    const saved = profileDefaults && typeof profileDefaults === 'object'
        ? profileDefaults
        : waypointSettings;

    const channelIndex = Number(saved?.last_channel_index);
    const durationSeconds = Number(saved?.last_duration_seconds);

    return {
        channelIndex: Number.isInteger(channelIndex) && channelIndex >= 0 && channelIndex <= 7
            ? channelIndex
            : 0,
        durationSeconds: [
            900, 1800, 3600, 10800, 21600,
            43200, 86400, 172800, 604800
        ].includes(durationSeconds) ? durationSeconds : 3600,
        postNotification: saved?.post_notification !== false
    };
}

async function loadWaypointComposerContext() {
    const [baseResult, chatsResult] = await Promise.allSettled([
        fetch('/api/base_status', { cache: 'no-store' })
            .then(response => response.ok ? response.json() : Promise.reject(
                new Error(`Base status HTTP ${response.status}`)
            )),
        fetch('/api/chats', { cache: 'no-store' })
            .then(response => response.ok ? response.json() : Promise.reject(
                new Error(`Channels HTTP ${response.status}`)
            ))
    ]);

    if (baseResult.status === 'fulfilled') {
        waypointActiveProfileId = normalizeWaypointProfileId(
            baseResult.value?.profile_id
        );
    }

    const defaults = getWaypointComposerDefaults(waypointActiveProfileId);
    const channels = chatsResult.status === 'fulfilled'
        ? chatsResult.value?.channels
        : waypointComposerChannels;

    const selectedChannelIndex = renderWaypointChannelOptions(
        channels,
        defaults.channelIndex
    );

    const durationSelect = document.getElementById('waypointCreateDuration');
    if (durationSelect) {
        durationSelect.value = String(defaults.durationSeconds);
    }

    const notifyToggle = document.getElementById(
        'waypointCreatePostNotification'
    );
    if (notifyToggle) {
        notifyToggle.checked = defaults.postNotification;
    }

    return {
        profileId: waypointActiveProfileId,
        selectedChannelIndex,
        defaults
    };
}

async function saveWaypointComposerDefaults(
    channelIndex,
    durationSeconds,
    postNotification
) {
    const profileId = normalizeWaypointProfileId(waypointActiveProfileId);
    const waypointSettings = {
        ...(appSettings?.waypoints || {}),
        // Keep the legacy values as a safe fallback for old installations.
        last_channel_index: Number(channelIndex),
        last_duration_seconds: Number(durationSeconds),
        post_notification: Boolean(postNotification),
        profile_defaults: {
            ...(appSettings?.waypoints?.profile_defaults || {})
        }
    };

    if (profileId) {
        waypointSettings.profile_defaults[profileId] = {
            last_channel_index: Number(channelIndex),
            last_duration_seconds: Number(durationSeconds),
            post_notification: Boolean(postNotification)
        };
    }

    appSettings = {
        ...(appSettings || {}),
        waypoints: waypointSettings
    };

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            cache: 'no-store',
            body: JSON.stringify({ waypoints: waypointSettings })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data?.ok) {
            throw new Error(
                data?.error || 'Unable to save waypoint defaults'
            );
        }
        appSettings = data.settings || appSettings;
    } catch (error) {
        console.warn(
            '[WAYPOINT] Unable to save composer defaults:',
            error
        );
    }
}

async function openCreateWaypointDialog(lat, lon) {
    const latitude = Number(lat);
    const longitude = Number(lon);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;

    pendingWaypointCoordinates = { latitude, longitude };

    const modal = document.getElementById('waypointCreateModal');
    if (!modal) return;

    document.getElementById('waypointCreateName').value = '';
    document.getElementById('waypointCreateDescription').value = '';
    document.getElementById('waypointCreateCoordinates').textContent =
        `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`;

    // Open immediately, then populate profile-specific channels and defaults.
    renderWaypointChannelOptions(
        waypointComposerChannels,
        getWaypointComposerDefaults().channelIndex
    );
    modal.style.display = 'flex';

    try {
        await loadWaypointComposerContext();
    } catch (error) {
        console.warn(
            '[WAYPOINT] Unable to load channel/profile context:',
            error
        );
        const defaults = getWaypointComposerDefaults();
        renderWaypointChannelOptions(
            waypointComposerChannels,
            defaults.channelIndex
        );
        const durationSelect = document.getElementById(
            'waypointCreateDuration'
        );
        if (durationSelect) {
            durationSelect.value = String(defaults.durationSeconds);
        }
        const notifyToggle = document.getElementById(
            'waypointCreatePostNotification'
        );
        if (notifyToggle) {
            notifyToggle.checked = defaults.postNotification;
        }
    }

    setTimeout(
        () => document.getElementById('waypointCreateName')?.focus(),
        50
    );
}

function closeCreateWaypointDialog() {
    const modal = document.getElementById('waypointCreateModal');
    if (modal) modal.style.display = 'none';
    pendingWaypointCoordinates = null;
}

function showWaypointActionStatus(message, state = 'sending', operation = null) {
    const type = state === 'sending'
        ? 'progress'
        : state === 'success'
            ? 'success'
            : 'error';

    const options = state === 'error' && operation
        ? {
            persistent: true,
            actionLabel: window.I18N.t('common.retry'),
            action: () => sendWaypointOperation(operation)
        }
        : {
            persistent: state === 'sending'
        };

    if (waypointSendNotificationId) {
        waypointSendNotificationId = updateNotification(
            waypointSendNotificationId,
            message,
            type,
            options
        );
    } else {
        waypointSendNotificationId = showToast(message, type, options);
    }
}

async function applySuccessfulWaypointResult(result, operation) {
    const waypoint = result?.waypoint;
    if (waypoint) {
        const id = String(waypoint.waypoint_id);
        meshMapWaypoints = [
            waypoint,
            ...meshMapWaypoints.filter(item => String(item.waypoint_id) !== id)
        ];
        waypointToolsItems = [
            waypoint,
            ...waypointToolsItems.filter(item => String(item.waypoint_id) !== id)
        ];
        meshMapWaypointSignature = getWaypointSignature(meshMapWaypoints);
        meshMapWaypointKnownIds.add(id);
        selectedWaypointId = id;

        renderWaypointToolsList();
        if (meshMap) {
            renderMeshMap(meshMapTargetNodeId, {
                preserveViewport: true,
                openPopup: false
            });
        }
        setTimeout(() => centerMapOnWaypoint(id), 250);
    }

    await Promise.allSettled([
        refreshMeshMapWaypoints(true),
        refreshWaypointToolsList(true)
    ]);

    waypointSendOperation = null;
    showWaypointActionStatus(
        window.I18N.t('waypoints.sent', { name: operation.payload.name }),
        'success'
    );
    waypointSendNotificationId = null;
}

async function sendWaypointOperation(operation) {
    if (!operation?.payload) return;

    waypointSendOperation = operation;
    operation.attempts = Number(operation.attempts || 0) + 1;

    showWaypointActionStatus(
        operation.attempts > 1
            ? window.I18N.t('waypoints.retrying', { name: operation.payload.name })
            : window.I18N.t('waypoints.sending', { name: operation.payload.name }),
        'sending'
    );

    try {
        const response = await fetch('/api/waypoints/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            cache: 'no-store',
            body: JSON.stringify(operation.payload)
        });
        const result = await response.json().catch(() => ({}));

        if (!response.ok || !result?.ok) {
            throw new Error(result?.error || window.I18N.t('errors.waypoint_send_failed'));
        }

        await applySuccessfulWaypointResult(result, operation);
    } catch (error) {
        console.error('[WAYPOINT] Send failed:', error);
        showWaypointActionStatus(
            error?.message || window.I18N.t('waypoints.not_sent'),
            'error',
            operation
        );
    }
}

function submitCreateWaypoint() {
    if (!pendingWaypointCoordinates) return;

    const name = document.getElementById('waypointCreateName').value.trim();
    const description = document.getElementById('waypointCreateDescription').value.trim();
    const channelIndex = Number(document.getElementById('waypointCreateChannel').value || 0);
    const duration = Number(document.getElementById('waypointCreateDuration').value || 3600);
    const postNotification = Boolean(
        document.getElementById('waypointCreatePostNotification')?.checked
    );

    if (!name) {
        showToast(window.I18N.t('waypoints.enter_name'), 'warning');
        return;
    }

    const coordinates = { ...pendingWaypointCoordinates };
    const payload = {
        name,
        description,
        latitude: coordinates.latitude,
        longitude: coordinates.longitude,
        channel_index: channelIndex,
        icon: 128205,
        expire_at: Math.floor(Date.now() / 1000) + duration,
        post_notification: postNotification
    };

    const operation = {
        payload,
        attempts: 0,
        createdAt: Date.now()
    };

    // Save only reusable controls. Name, description and coordinates remain one-time values.
    saveWaypointComposerDefaults(
        channelIndex,
        duration,
        postNotification
    );

    // Free the interface immediately. The network operation continues in the background.
    closeCreateWaypointDialog();
    sendWaypointOperation(operation);
}


function scrollWaypointToolsItemIntoView(waypointId, behavior = 'smooth') {
    const id = String(waypointId);
    const container = document.getElementById('waypointToolsList');
    if (!container) return;
    const item = [...container.querySelectorAll('.waypoint-tools-item')]
        .find(row => String(row.dataset.waypointId || '') === id);
    if (!item) return;
    item.scrollIntoView({ behavior, block:'center', inline:'nearest' });
}

async function selectWaypointInTools(waypointId, options = {}) {
    const id = String(waypointId);
    selectedWaypointId = id;

    let item = waypointToolsItems.find(row => String(row?.waypoint_id) === id);
    const mapItem = meshMapWaypoints.find(row => String(row?.waypoint_id) === id);
    const expired = Boolean(
        (item || mapItem)?.is_active === false
        || formatWaypointExpiryDetails((item || mapItem)?.expire_at).expired
    );

    // Archived entries are not present in the active-only Tools query.
    if (!item && expired && !waypointToolsShowExpired) {
        waypointToolsShowExpired = true;
        const archiveToggle = document.getElementById('waypointArchiveToggle');
        if (archiveToggle) archiveToggle.checked = true;
        await refreshWaypointToolsList(true);
        item = waypointToolsItems.find(row => String(row?.waypoint_id) === id);
    }

    renderWaypointToolsList();
    requestAnimationFrame(() => scrollWaypointToolsItemIntoView(id, options.behavior || 'smooth'));
}

function handleWaypointMarkerSelected(waypointId) {
    const id = String(waypointId);
    selectedWaypointId = id;
    selectWaypointInTools(id).catch(error => {
        console.debug('Waypoint list selection failed:', error);
    });
}

async function showWaypointOnMap(waypointId) {
    const id = String(waypointId);
    selectedWaypointId = id;
    let waypoint = meshMapWaypoints.find(item => String(item?.waypoint_id) === id)
        || waypointToolsItems.find(item => String(item?.waypoint_id) === id);
    if (!waypoint) {
        await refreshWaypointToolsList(true);
        waypoint = waypointToolsItems.find(item => String(item?.waypoint_id) === id);
    }
    if (!waypoint) {
        showToast(window.I18N.t('waypoints.no_longer_available'), 'error');
        return;
    }

    // Clicking a saved entry is the primary navigation action. Hidden entries
    // are restored first, including archived/expired waypoints.
    if (waypoint?.is_hidden) {
        const restored = await setWaypointHidden(id, false, { silent:true });
        if (!restored) return;
    } else {
        await refreshMeshMapWaypoints(true);
    }

    meshMapWaypointsVisible = true;
    const toggle = document.getElementById('mapWaypointsToggle');
    if (toggle) toggle.checked = true;

    // Keep the Tools sidebar open on desktop. openMapView only changes the
    // central workspace; it must not select the waypoint sender node.
    switchMainTab('map');
    await selectWaypointInTools(id);
    requestAnimationFrame(() => {
        renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        scheduleMeshMapResize(0);
        scheduleMeshMapResize(120);
    });

    // Wait until Leaflet has rebuilt the waypoint marker, then center and open
    // its popup. A second attempt covers slower mobile/tablet layouts.
    const focus = () => centerMapOnWaypoint(id);
    setTimeout(focus, 180);
    setTimeout(focus, 520);
}

function getWaypointSignature(items) {
    return JSON.stringify((items || []).map(item => [
        item.waypoint_id, item.updated_at, item.is_active, item.is_hidden,
        item.latitude, item.longitude, item.sender_id
    ]));
}

async function refreshMeshMapWaypoints(forceRender = false) {
    if (waypointMapRefreshInFlight && !forceRender) return;
    waypointMapRefreshInFlight = true;
    try {
        const response = await fetch('/api/waypoints?include_expired=1', { cache:'no-store' });
        if (!response.ok) return;
        const payload = await response.json();
        const items = Array.isArray(payload?.waypoints) ? payload.waypoints : [];
        const signature = getWaypointSignature(items);
        const changed = signature !== meshMapWaypointSignature;
        const incomingIds = new Set(items.map(item => String(item?.waypoint_id)));

        if (meshMapWaypointInitialLoadDone) {
            items.forEach(item => {
                const id = String(item?.waypoint_id);
                if (id && !meshMapWaypointKnownIds.has(id)) {
                    const sender = item?.sender_name || item?.sender_id || window.I18N.t('nodes.unknown_node');
                    showToast(`📍 ${window.I18N.t('waypoints.received_notification', { name: item?.name || window.I18N.t('waypoints.unnamed'), sender })}`, 'info');
                }
            });
        }
        meshMapWaypointInitialLoadDone = true;
        meshMapWaypointKnownIds = incomingIds;
        meshMapWaypointSignature = signature;
        meshMapWaypoints = items;
        if ((changed || forceRender) && meshMap && document.getElementById('mapView')?.style.display !== 'none') {
            renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        }
    } catch (error) {
        console.debug('Waypoint refresh failed:', error);
    } finally {
        waypointMapRefreshInFlight = false;
    }
}

function startMeshMapWaypointPolling() {
    if (meshMapWaypointPollTimer) return;
    refreshMeshMapWaypoints(false);
    startWaypointExpiryTimer();
    meshMapWaypointPollTimer = setInterval(() => refreshMeshMapWaypoints(false), 5000);
}

function toggleMeshMapWaypoints(visible) {
    meshMapWaypointsVisible = Boolean(visible);
    renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
}

function centerMapOnWaypoint(waypointId) {
    const id = String(waypointId);
    const waypoint = meshMapWaypoints.find(item => String(item?.waypoint_id) === id)
        || waypointToolsItems.find(item => String(item?.waypoint_id) === id);
    if (!waypoint) return;

    selectedWaypointId = id;
    selectWaypointInTools(id).catch(() => {});

    const map = ensureMeshMap();
    if (!map) return;
    const lat = Number(waypoint.latitude);
    const lon = Number(waypoint.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        showToast(window.I18N.t('waypoints.coordinates_unavailable'), 'error');
        return;
    }

    if (!meshMapWaypointMarkers.has(id) && !waypoint?.is_hidden) {
        renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
    }

    const target = L.latLng(lat, lon);
    const currentCenter = map.getCenter();
    const distanceMeters = currentCenter.distanceTo(target);
    const currentZoom = Number(map.getZoom()) || 13;
    const targetZoom = Math.min(15, Math.max(13, currentZoom));

    const openPopup = () => {
        const marker = meshMapWaypointMarkers.get(id);
        if (marker) marker.openPopup();
    };

    map.stop();

    // Nearby points move without zoom changes. Distant points use a short,
    // restrained fly animation and never zoom in beyond level 15.
    if (distanceMeters <= 4500) {
        map.once('moveend', openPopup);
        map.panTo(target, { animate:true, duration:.45, easeLinearity:.35, noMoveStart:false });
        setTimeout(openPopup, 520);
    } else {
        map.once('moveend', openPopup);
        map.flyTo(target, targetZoom, {
            animate:true,
            duration:.58,
            easeLinearity:.32,
            noMoveStart:false
        });
        setTimeout(openPopup, 680);
    }
}

function scheduleMeshMapResize(delay = 0) {
    if (!meshMap) return;
    if (meshMapResizeTimer) clearTimeout(meshMapResizeTimer);

    meshMapResizeTimer = setTimeout(() => {
        meshMapResizeTimer = null;
        requestAnimationFrame(() => {
            if (meshMap) meshMap.invalidateSize({ animate:false, pan:false });
        });
    }, Math.max(0, Number(delay) || 0));
}

function ensureMeshMap() {
    if (meshMap || typeof L === 'undefined') return meshMap;
    const container = document.getElementById('meshMap');
    if (!container) return null;

    const reference = getReferenceLocation();
    const initialCenter = reference
        ? [reference.latitude, reference.longitude]
        : [49.5881, 11.0078];

    meshMap = L.map(container, {
        zoomControl: true,
        preferCanvas: true
    }).setView(initialCenter, 13);

    meshMapTileLayer = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
    }).addTo(meshMap);

    meshMap.on('contextmenu', event => {
        openCreateWaypointDialog(event.latlng.lat, event.latlng.lng);
    });

    // Reveal node labels progressively as the operator zooms in.
    // Updating a data attribute is intentionally lightweight and avoids
    // rebuilding all Leaflet markers during every zoom step.
    meshMap.on('zoom zoomend', updateMeshMapNodeLabelLevel);
    updateMeshMapNodeLabelLevel();

    // Keep Leaflet synchronized with workspace width changes. This removes
    // the visible delay when either sidebar is shown or hidden.
    if (typeof ResizeObserver !== 'undefined') {
        meshMapResizeObserver = new ResizeObserver(() => scheduleMeshMapResize(0));
        meshMapResizeObserver.observe(container);
        const mapView = document.getElementById('mapView');
        if (mapView && mapView !== container) meshMapResizeObserver.observe(mapView);
    }

    scheduleMeshMapResize(0);
    startMeshMapWaypointPolling();
    return meshMap;
}

function buildMapPopup(node) {
    const pos = getNodePosition(node);
    const navigation = pos ? getNodeDistanceAndBearing(pos.latitude, pos.longitude) : { distanceText:'--', bearingText:'--' };
    const source = node?.position?.source || window.I18N.t('nodes.source_radio');
    const updated = formatNodePositionUpdated(node?.position);
    const age = node?.age || node?.last_seen || '--';
    const nodeId = escapeJsString(node?.node_id || '');
    const nodeName = escapeJsString(getNodeDisplayName(node));

    return `
        <div class="map-popup-name">${escapeHtml(getNodeDisplayName(node))}</div>
        <div class="map-popup-grid">
            <span>${escapeHtml(window.I18N.t('nodes.distance'))}</span><strong>${escapeHtml(navigation.distanceText)}</strong>
            <span>${escapeHtml(window.I18N.t('nodes.bearing'))}</span><strong>${escapeHtml(navigation.bearingText)}</strong>
            <span>${escapeHtml(window.I18N.t('nodes.source'))}</span><strong>${escapeHtml(source)}</strong>
            <span>${escapeHtml(window.I18N.t('nodes.last_update'))}</span><strong>${escapeHtml(updated || age)}</strong>
        </div>
        <div class="map-popup-actions">
            <button class="map-popup-primary-btn" onclick="openChat('${nodeId}', '${nodeName}', 'dm')">💬 ${escapeHtml(window.I18N.t('nodes.message_button'))}</button>
            <button class="map-popup-action-btn" onclick="runNodeTool('request_telemetry', '${nodeId}', '${nodeName}', this)">📊 ${escapeHtml(window.I18N.t('nodes.telemetry_short'))}</button>
            <button class="map-popup-action-btn" onclick="runNodeTool('request_position', '${nodeId}', '${nodeName}', this)">📍 ${escapeHtml(window.I18N.t('nodes.position_short'))}</button>
            <button class="map-popup-action-btn" onclick="setNodeAsReference('${nodeId}')">📌 ${escapeHtml(window.I18N.t('nodes.reference_short'))}</button>
            <button class="map-popup-action-btn" onclick="copyCoordinates('${pos ? pos.latitude : ''}', '${pos ? pos.longitude : ''}')">📋 ${escapeHtml(window.I18N.t('waypoints.coordinates'))}</button>
        </div>
    `;
}

function renderMeshMap(targetNodeId = null, options = {}) {
    const map = ensureMeshMap();
    if (!map) {
        showToast(window.I18N.t('nodes.map_library_load_failed'), 'error');
        return;
    }

    const preserveViewport = Boolean(options && options.preserveViewport);
    const openTargetPopup = options?.openPopup !== false;
    const savedCenter = preserveViewport ? map.getCenter() : null;
    const savedZoom = preserveViewport ? map.getZoom() : null;

    if (options?.clearSelection) {
        meshMapTargetNodeId = null;
    } else if (targetNodeId) {
        meshMapTargetNodeId = String(targetNodeId);
    }
    meshMapMarkers.forEach(marker => marker.remove());
    meshMapMarkers.clear();
    meshMapWaypointMarkers.forEach(marker => marker.remove());
    meshMapWaypointMarkers.clear();
    if (meshMapReferenceMarker) { meshMapReferenceMarker.remove(); meshMapReferenceMarker = null; }
    if (meshMapReferenceLine) { meshMapReferenceLine.remove(); meshMapReferenceLine = null; }

    const positionedNodes = nodeCache.filter(node => getNodePosition(node));
    const bounds = [];

    // Group nodes that occupy the same visual location. Four decimal places
    // are about 11 metres in latitude and are appropriate for labels that
    // would otherwise overlap at city-level zooms.
    const nodeCoordinateGroups = new Map();
    positionedNodes.forEach(node => {
        const pos = getNodePosition(node);
        const groupKey = `${Number(pos.latitude).toFixed(4)},${Number(pos.longitude).toFixed(4)}`;
        if (!nodeCoordinateGroups.has(groupKey)) {
            nodeCoordinateGroups.set(groupKey, []);
        }
        nodeCoordinateGroups.get(groupKey).push(String(node.node_id));
    });

    positionedNodes.forEach(node => {
        const pos = getNodePosition(node);
        const selected = String(node.node_id) === String(meshMapTargetNodeId);
        const marker = L.marker([pos.latitude, pos.longitude], {
            icon: createMeshMapIcon(selected ? 'selected' : 'node'),
            title: getNodeDisplayName(node),
            riseOnHover: true,
            zIndexOffset: selected ? 1000 : 0
        }).addTo(map);

        marker.bindPopup(buildMapPopup(node), {
            className: 'meshcenter-map-popup',
            maxWidth: 300,
            offset: L.point(0, -10)
        });

        // The selected node keeps its prominent callout at every zoom level.
        // Other node names appear progressively and reuse exactly the same
        // activity classification as the node cards.
        if (selected) {
            marker.bindTooltip(escapeHtml(getNodeDisplayName(node)), {
                permanent: true,
                direction: 'bottom',
                offset: [0, 14],
                opacity: 1,
                className: 'meshcenter-map-selected-label'
            });
        } else {
            const activity = getNodeActivityPresentation(node);
            const coordinateGroupKey =
                `${Number(pos.latitude).toFixed(4)},${Number(pos.longitude).toFixed(4)}`;
            const coordinateGroup = nodeCoordinateGroups.get(coordinateGroupKey) || [];
            const groupIndex = Math.max(
                0,
                coordinateGroup.indexOf(String(node.node_id))
            );
            const groupCount = Math.max(1, coordinateGroup.length);
            const verticalOffset = (groupIndex - ((groupCount - 1) / 2)) * 15;

            marker.bindTooltip(escapeHtml(activity.displayName), {
                permanent: true,
                direction: 'right',
                offset: [9, verticalOffset],
                opacity: 1,
                className:
                    `meshcenter-map-node-label ${activity.activityClass}` +
                    `${groupCount > 1 ? ' is-coordinate-grouped' : ''}`
            });
        }

        marker.on('click', () => {
            const selectedId = String(node.node_id);
            meshMapTargetNodeId = selectedId;

            // Update the orange marker and reference line immediately instead
            // of waiting for the next 6-second node refresh.
            renderMeshMap(selectedId, {
                preserveViewport: true,
                openPopup: true
            });
            selectNode(node.node_id, getNodeDisplayName(node), 'map');
        });
        meshMapMarkers.set(String(node.node_id), marker);
        bounds.push([pos.latitude, pos.longitude]);
    });

    if (meshMapWaypointsVisible) {
        meshMapWaypoints.forEach(waypoint => {
            const latitude = Number(waypoint?.latitude);
            const longitude = Number(waypoint?.longitude);
            if (!Number.isFinite(latitude) || !Number.isFinite(longitude) || waypoint?.is_hidden) return;
            const expired = waypoint?.is_active === false || formatWaypointExpiryDetails(waypoint?.expire_at).expired;
            const marker = L.marker([latitude, longitude], {
                icon: createWaypointMapIcon(waypoint),
                title: waypoint?.name || 'Waypoint',
                riseOnHover: true,
                zIndexOffset: expired ? 720 : 850,
                opacity: expired ? 0.72 : 1
            }).addTo(map);
            marker.bindPopup(buildWaypointPopup(waypoint), {
                className:'meshcenter-map-popup meshcenter-waypoint-popup',
                maxWidth:320,
                offset:L.point(0, -12),
                closeButton:false
            });

            marker.bindTooltip(
                escapeHtml(String(waypoint?.name || 'Waypoint').trim()),
                {
                    permanent: true,
                    direction: 'right',
                    offset: [15, 0],
                    opacity: 1,
                    className:
                        `meshcenter-map-waypoint-label${expired ? ' is-expired' : ' is-active'}`
                }
            );

            marker.on('click', () => {
                handleWaypointMarkerSelected(waypoint.waypoint_id);
            });
            marker.on('popupopen', () => {
                handleWaypointMarkerSelected(waypoint.waypoint_id);
                setTimeout(updateOpenWaypointExpiryLabels, 0);
            });
            meshMapWaypointMarkers.set(String(waypoint.waypoint_id), marker);
            bounds.push([latitude, longitude]);
        });
    }

    const reference = getReferenceLocation();
    if (reference) {
        const referenceLocationLabel = reference.name || window.I18N.t('settings.reference_location');
        meshMapReferenceMarker = L.marker([reference.latitude, reference.longitude], {
            icon: createMeshMapIcon('reference'),
            title: referenceLocationLabel,
            zIndexOffset: 700
        }).addTo(map).bindPopup(`<div class="map-popup-name">${escapeHtml(referenceLocationLabel)}</div><div class="map-popup-grid"><span>${escapeHtml(window.I18N.t('nodes.type_label'))}</span><strong>${escapeHtml(window.I18N.t('nodes.reference_short'))}</strong><span>${escapeHtml(window.I18N.t('settings.latitude'))}</span><strong>${reference.latitude.toFixed(6)}</strong><span>${escapeHtml(window.I18N.t('settings.longitude'))}</span><strong>${reference.longitude.toFixed(6)}</strong></div>`, { className:'meshcenter-map-popup' });
        bounds.push([reference.latitude, reference.longitude]);

        const targetNode = positionedNodes.find(node => String(node.node_id) === String(meshMapTargetNodeId));
        const targetPos = getNodePosition(targetNode);
        if (targetPos) {
            meshMapReferenceLine = L.polyline([
                [reference.latitude, reference.longitude],
                [targetPos.latitude, targetPos.longitude]
            ], {
                color:'#df5d68', weight:2, opacity:.88, dashArray:'7 7'
            }).addTo(map);
        }
    }

    updateMeshMapNodeLabelLevel();

    const countEl = document.getElementById('mapNodeCount');
    if (countEl) {
        const visibleWaypoints = meshMapWaypointsVisible
            ? meshMapWaypoints.filter(item => !item?.is_hidden)
            : [];
        const activeWaypointCount = visibleWaypoints.filter(item => item?.is_active !== false && !formatWaypointExpiryDetails(item?.expire_at).expired).length;
        const expiredWaypointCount = visibleWaypoints.length - activeWaypointCount;
        const waypointCountText = window.I18N.plural('waypoints.waypoint_count_plural', visibleWaypoints.length, { count: visibleWaypoints.length });
        const waypointSummary = expiredWaypointCount > 0
            ? `${waypointCountText} (${activeWaypointCount} ${window.I18N.t('waypoints.count_active')}, ${expiredWaypointCount} ${window.I18N.t('waypoints.count_expired')})`
            : waypointCountText;
        const nodeCountText = window.I18N.plural('nodes.node_count_plural', positionedNodes.length, { count: positionedNodes.length });
        countEl.textContent = `${nodeCountText} · ${waypointSummary}`;
    }

const targetNode = positionedNodes.find(
    node => String(node.node_id) === String(meshMapTargetNodeId)
);

const targetPos = getNodePosition(targetNode);

const title = document.getElementById("mapViewTitle");
const subtitle = document.getElementById("mapViewSubtitle");

if (targetNode && targetPos) {
    if (title)
        title.textContent = `🗺 ${window.I18N.t('nodes.map_title_for', { name: getNodeDisplayName(targetNode) })}`;
    if (subtitle) {
        const nav = getNodeDistanceAndBearing(
            targetPos.latitude,
            targetPos.longitude
        );
        subtitle.textContent =
            `${nav.distanceText} · ${nav.bearingText} · ` +
            `${targetNode.position?.source || window.I18N.t('nodes.source_radio')}`;
    }
    if (preserveViewport &&
        savedCenter &&
        Number.isFinite(savedZoom)) {
        map.setView(savedCenter, savedZoom, {
            animate: false
        });
    } else {
        map.flyTo(
            [targetPos.latitude, targetPos.longitude],
            Math.max(map.getZoom(), 15),
            {
                animate: true,
                duration: 0.55,
                easeLinearity: 0.25
            }
        );
    }
    if (openTargetPopup) {
        const openPopup = () => {
            const marker =
                meshMapMarkers.get(String(targetNode.node_id));
            if (marker)
                marker.openPopup();
        };
        if (preserveViewport) {
            requestAnimationFrame(openPopup);
        } else {
            map.once("moveend", openPopup);
            setTimeout(openPopup, 700);
        }
    }
} else {
        if (title) title.textContent = `🗺 ${window.I18N.t('nodes.map_title')}`;
        if (subtitle) subtitle.textContent = window.I18N.t('nodes.map_subtitle_default');

        if (preserveViewport && savedCenter && Number.isFinite(savedZoom)) {
            map.setView(savedCenter, savedZoom, { animate:false });
        } else if (bounds.length === 1) {
            map.setView(bounds[0], 15);
        } else if (bounds.length > 1) {
            map.fitBounds(bounds, { padding:[45,45], maxZoom:15 });
        }
    }

    scheduleMeshMapResize(0);
    scheduleMeshMapResize(120);
}

function fitMeshMapToNodes() {
    const map = ensureMeshMap();
    if (!map) return;
    const points = nodeCache.map(getNodePosition).filter(Boolean).map(pos => [pos.latitude, pos.longitude]);
    if (meshMapWaypointsVisible) {
        meshMapWaypoints.forEach(item => {
            const lat = Number(item?.latitude);
            const lon = Number(item?.longitude);
            if (Number.isFinite(lat) && Number.isFinite(lon) && !item?.is_hidden) points.push([lat, lon]);
        });
    }
    const reference = getReferenceLocation();
    if (reference) points.push([reference.latitude, reference.longitude]);
    if (points.length === 1) map.flyTo(points[0], 15, { duration:.6 });
    else if (points.length > 1) map.fitBounds(points, { padding:[45,45], maxZoom:15 });
}

function openEmbeddedNodeMap(latitude, longitude, nodeId = null) {
    const lat = Number(latitude);
    const lon = Number(longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        showToast(window.I18N.t('nodes.position_coordinates_unavailable'), 'error');
        return;
    }

    let targetId = nodeId ? String(nodeId) : '';
    if (!targetId && currentChatType === 'dm' && currentChatId) targetId = String(currentChatId);
    if (!targetId) {
        const match = nodeCache.find(node => {
            const pos = getNodePosition(node);
            return pos && Math.abs(pos.latitude - lat) < 1e-7 && Math.abs(pos.longitude - lon) < 1e-7;
        });
        targetId = match ? String(match.node_id) : '';
    }

    meshMapTargetNodeId = targetId;
    if (typeof setMapLayoutMode === 'function') {
        if (MapLayout.state.mode === 'off') setMapLayoutMode('full', false);
        else applyMapLayout();
    } else {
        switchMainTab('map');
    }
    requestAnimationFrame(() => renderMeshMap(meshMapTargetNodeId));
}

function buildNodeMapUrl(latitude, longitude) {
    const lat = Number(latitude);
    const lon = Number(longitude);

    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        return null;
    }

    if (getMapProvider() === 'google') {
        return `https://www.google.com/maps?q=${encodeURIComponent(`${lat},${lon}`)}`;
    }

    return (
        'https://www.openstreetmap.org/' +
        `?mlat=${encodeURIComponent(lat)}` +
        `&mlon=${encodeURIComponent(lon)}` +
        `#map=16/${encodeURIComponent(lat)}/${encodeURIComponent(lon)}`
    );
}

function openNodeMap(latitude, longitude, nodeId = null) {
    openEmbeddedNodeMap(latitude, longitude, nodeId);
}

function openExternalNodeMap(latitude, longitude) {
    const url = buildNodeMapUrl(latitude, longitude);
    if (!url) {
        showToast(window.I18N.t('nodes.position_coordinates_unavailable'), 'error');
        return;
    }
    window.open(url, '_blank', 'noopener,noreferrer');
}

function renderNodePositionBlock(node) {
    const position = node?.position;
    const latitude = Number(position?.latitude);
    const longitude = Number(position?.longitude);

    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
        return '';
    }

    const altitude = Number(position?.altitude);
    const altitudeText = Number.isFinite(altitude)
        ? `${Math.round(altitude)} m`
        : '--';

    const navigation =
    getNodeDistanceAndBearing(
        latitude,
        longitude
    );    

    const precisionLabels = {
        full: "Full",
        medium: "Medium",
        low: "Low"
    };

    const precisionKey = String(position?.precision_label || "").toLowerCase();

    const precisionText =
        precisionLabels[precisionKey]
        || position?.precision_label
        || position?.precision
        || "--";

    return `
        <div class="node-position-card">
            <div class="node-position-heading">
                <span>📍 Last known position</span>
                <span class="node-position-updated">
                    ${escapeHtml(formatNodePositionUpdated(position))}
                </span>
            </div>

            <div class="node-position-grid">
                <div class="node-position-item">
                    <span class="label">Latitude:</span>
                    <span class="value">${escapeHtml(latitude.toFixed(6))}</span>
                </div>

                <div class="node-position-item">
                    <span class="label">Longitude:</span>
                    <span class="value">${escapeHtml(longitude.toFixed(6))}</span>
                </div>

                <div class="node-position-item">
                    <span class="label">Altitude:</span>
                    <span class="value">${escapeHtml(altitudeText)}</span>
                </div>

                <div class="node-position-item node-position-navigation">
                    <span class="label">Distance:</span>
                    <span class="value">
                        ${escapeHtml(navigation.distanceText)}
                    </span>

                    <span class="node-position-bearing">
                        ${escapeHtml(navigation.bearingText)}
                    </span>
                </div>
            </div>

            <div class="node-position-actions">
                <button type="button"
                        class="node-position-map-btn"
                        onclick='openNodeMap(${latitude}, ${longitude}, ${JSON.stringify(String(node?.node_id || ""))})'>
                    🗺 Show on map
                </button>

                <div class="node-position-action-reserve"></div>
            </div>
        </div>
    `;
}

// ============================================================
// НОВАЯ ДЕТАЛЬНАЯ КАРТОЧКА НОДЫ
// ============================================================

// Build a signature from every value currently used by the selected-node card.
// The signature is only a fast "nothing changed" guard. When values do change,
// the card is patched in place rather than replaced, so polling does not flash.
function generateNodeDetailSignature(node) {
    if (!node || !node.node_id) return '';

    const position = node.position || {};
    const telemetry = node.telemetry || {};
    const deviceMetrics = node.device_metrics || {};
    const environmentMetrics = node.environment_metrics || {};
    const powerMetrics = node.power_metrics || {};

    return JSON.stringify([
        node.node_id,
        node.clean_name,
        node.name,
        node.short_name,
        node.hw_model,
        node.role,
        node.age,
        node.last_heard,
        node.first_seen,
        node.rssi,
        node.snr,
        node.signal_quality,
        node.hop_start,
        node.hops_away,
        node.relay_node,
        node.last_relay,
        node.ignored,
        node.favorite,
        node.last_text,
        node.last_text_time,
        node.last_position_time,
        node.last_telemetry_time,
        node.messages_count,
        node.packet_count,
        node.battery_level,
        node.voltage,
        node.channel_utilization,
        node.air_util_tx,
        node.uptime,
        position.latitude,
        position.longitude,
        position.altitude,
        position.time,
        position.timestamp,
        position.source,
        position.precision,
        telemetry,
        deviceMetrics,
        environmentMetrics,
        powerMetrics,
        getReferenceLocation()
    ]);
}

function resetNodeRenderCache(nodeId = null) {
    if (nodeId) {
        delete nodeRenderCache[nodeId];
        return;
    }
    nodeRenderCache = {};
}

// Small DOM morphing helper. It updates text and attributes in the existing
// elements, preserving the card itself, scroll position and interaction state.
function patchNodeDetailDom(currentNode, nextNode) {
    if (!currentNode || !nextNode) return;

    if (currentNode.nodeType !== nextNode.nodeType) {
        currentNode.replaceWith(nextNode.cloneNode(true));
        return;
    }

    if (currentNode.nodeType === Node.TEXT_NODE) {
        if (currentNode.nodeValue !== nextNode.nodeValue) {
            currentNode.nodeValue = nextNode.nodeValue;
        }
        return;
    }

    if (currentNode.nodeType !== Node.ELEMENT_NODE) return;

    if (currentNode.tagName !== nextNode.tagName) {
        currentNode.replaceWith(nextNode.cloneNode(true));
        return;
    }

    for (const attr of Array.from(currentNode.attributes)) {
        if (!nextNode.hasAttribute(attr.name)) currentNode.removeAttribute(attr.name);
    }
    for (const attr of Array.from(nextNode.attributes)) {
        if (currentNode.getAttribute(attr.name) !== attr.value) {
            currentNode.setAttribute(attr.name, attr.value);
        }
    }

    const currentChildren = Array.from(currentNode.childNodes);
    const nextChildren = Array.from(nextNode.childNodes);
    const commonLength = Math.min(currentChildren.length, nextChildren.length);

    for (let i = 0; i < commonLength; i += 1) {
        patchNodeDetailDom(currentChildren[i], nextChildren[i]);
    }

    for (let i = currentChildren.length - 1; i >= nextChildren.length; i -= 1) {
        currentNode.removeChild(currentNode.childNodes[i]);
    }

    for (let i = commonLength; i < nextChildren.length; i += 1) {
        currentNode.appendChild(nextChildren[i].cloneNode(true));
    }
}

function renderOrPatchNodeDetailCard(details, html, nodeId) {
    const template = document.createElement('template');
    template.innerHTML = html.trim();
    const nextCard = template.content.firstElementChild;
    const currentCard = details.querySelector(':scope > .node-detail-card');

    if (currentCard && currentCard.dataset.nodeId === nodeId && nextCard) {
        patchNodeDetailDom(currentCard, nextCard);
    } else {
        details.replaceChildren(nextCard);
    }
}

let closedNodeDetailId = null;
// The active DM may remain open after the operator closes node details,
// but the map/card selection should be visually cleared until a node is
// explicitly selected again.
let nodeVisualSelectionCleared = false;

function closeNodeDetails() {
    const currentCard = document.querySelector('#nodeDetails > .node-detail-card');
    closedNodeDetailId = currentCard?.dataset?.nodeId || null;
    nodeVisualSelectionCleared = true;

    const details = document.getElementById('nodeDetails');
    if (details) {
        details.className = 'node-details-placeholder';
        details.innerHTML = '';
    }

    document.getElementById('nodeActionsMenu')?.remove();

    // Remove the visual focus from the compact card immediately and prevent
    // the next node-list refresh from restoring it automatically.
    document.querySelectorAll('#nodesList .node-card.selected').forEach(card => {
        card.classList.remove('selected');
    });

    // Clear only the map selection. Keep the current DM open and preserve
    // the current map viewport.
    meshMapTargetNodeId = null;
    if (typeof MapLayout !== 'undefined' && MapLayout.state.mode !== 'off') {
        renderMeshMap(null, {
            preserveViewport: true,
            openPopup: false,
            clearSelection: true
        });
    }
}

function renderNodeDetails(node) {
    const details = document.getElementById('nodeDetails');
    if (!details) return;

    if (!node || typeof node !== 'object') {
        details.className = 'node-details-placeholder';
        details.innerHTML = window.I18N.t('nodes.select_node_below');
        return;
    }

    const nodeId = node.node_id;
    if (closedNodeDetailId && String(closedNodeDetailId) === String(nodeId)) {
        details.className = 'node-details-placeholder';
        details.innerHTML = '';
        return;
    }
    const signature = generateNodeDetailSignature(node);
    const existingCard = details.querySelector(':scope > .node-detail-card');

    if (existingCard?.dataset.nodeId === nodeId && nodeRenderCache[nodeId] === signature) {
        return;
    }

    const displayName = node.clean_name || node.name || nodeId;
    const shortName = node.short_name || '-';
    const hwModel = node.hw_model || '-';
    const role = node.role || 'CLIENT';
    const lastSeen = node.age || window.I18N.t('nodes.never_seen');
    const hops = node.hop_start || node.hops_away || '?';
    const rssi = node.rssi || '--';
    const snr = node.snr || '--';
    const isIgnored = node.ignored || false;
    const isFavorite = node.favorite || false;

    // ---- Позиция ----
    const position = node.position || {};
    const hasPosition = Number.isFinite(position.latitude) && Number.isFinite(position.longitude);
    let distanceText = '--', bearingText = '--';
    if (hasPosition) {
        const ref = getReferenceLocation();
        if (ref && Number.isFinite(ref.latitude) && Number.isFinite(ref.longitude)) {
            const distM = calculateDistanceMeters(ref.latitude, ref.longitude, position.latitude, position.longitude);
            distanceText = formatNodeDistance(distM);
            const bearing = calculateBearingDegrees(ref.latitude, ref.longitude, position.latitude, position.longitude);
            bearingText = `${Math.round(bearing)}° ${getBearingDirection(bearing)}`;
        }
    }

    // ---- Батарея / телеметрия ----
    const battery = node.battery_level ?? '--';
    const voltage = node.voltage ?? '--';

    // ---- Последнее сообщение ----
    const lastText = node.last_text || '';

    // ---- Строим HTML ----
    const html = `
        <div class="node-detail-card" data-node-id="${escapeHtml(nodeId)}">
            <!-- Верхняя панель -->
            <div class="node-detail-header">
                <div class="node-detail-title-wrap">
                    <span class="node-detail-activity ${getNodeActivityPresentation(node).activityClass}" title="${escapeHtml(window.I18N.t('nodes.activity_status'))}" aria-hidden="true"></span>
                    <span class="node-detail-name">${escapeHtml(displayName)}</span>
                </div>
                <button type="button" class="node-detail-close" onclick="closeNodeDetails()" title="${escapeHtml(window.I18N.t('nodes.close_node_details'))}" aria-label="${escapeHtml(window.I18N.t('nodes.close_node_details'))}">×</button>
            </div>

            <!-- Вторая строка: ID, модель, роль + node actions -->
            <div class="node-detail-subheader">
                <div class="node-detail-identity-line">
                    <span class="node-detail-short-id">${escapeHtml(shortName)}</span>
                    <span class="node-detail-separator">•</span>
                    <span class="node-detail-role">${escapeHtml(role)}</span>
                    <span class="node-detail-separator">•</span>
                    <span class="node-detail-hw">${escapeHtml(hwModel)}</span>
                    <span class="node-detail-separator">•</span>
                    <button type="button" class="node-detail-id" onclick="copyNodeId('${escapeHtml(nodeId)}')" title="${escapeHtml(window.I18N.t('nodes.click_to_copy_node_id'))}" aria-label="${escapeHtml(window.I18N.t('nodes.copy_node_id'))}">${escapeHtml(truncateText(nodeId, 12))}</button>
                </div>
            </div>

            <!-- Третья строка: статус + segmented actions -->
            <div class="node-detail-status-row">
                <div class="node-detail-status-copy">
                    <span class="node-detail-last-seen">🕒 ${escapeHtml(lastSeen)}</span>
                    <span class="node-detail-hops">${escapeHtml(window.I18N.t('nodes.hops', { value: hops }))}</span>
                </div>
                <div class="node-detail-header-actions node-detail-action-group">
                    <button type="button"
                            class="node-detail-state-btn node-detail-favorite-btn ${isFavorite ? 'active' : ''}"
                            onclick="toggleFavorite('${escapeHtml(nodeId)}')"
                            title="${escapeHtml(isFavorite ? window.I18N.t('nodes.remove_from_favorites') : window.I18N.t('nodes.add_to_favorites'))}"
                            aria-label="${escapeHtml(isFavorite ? window.I18N.t('nodes.remove_node_from_favorites') : window.I18N.t('nodes.add_node_to_favorites'))}"
                            aria-pressed="${isFavorite ? 'true' : 'false'}">
                        <span aria-hidden="true">⚑</span>
                    </button>

                    <button type="button"
                            class="node-detail-state-btn node-detail-ignore-btn ${isIgnored ? 'active' : ''}"
                            onclick="toggleIgnore('${escapeHtml(nodeId)}')"
                            title="${escapeHtml(isIgnored ? window.I18N.t('nodes.stop_ignoring_node') : window.I18N.t('nodes.ignore_node'))}"
                            aria-label="${escapeHtml(isIgnored ? window.I18N.t('nodes.stop_ignoring_node') : window.I18N.t('nodes.ignore_node'))}"
                            aria-pressed="${isIgnored ? 'true' : 'false'}">
                        <span aria-hidden="true">🚫</span>
                    </button>

                    <button class="node-detail-actions-btn"
                            onclick="toggleNodeActionsMenu(event)"
                            aria-label="${escapeHtml(window.I18N.t('nodes.more_node_actions'))}"
                            title="${escapeHtml(window.I18N.t('nodes.more_actions'))}">
                        ⋮
                    </button>
                </div>
            </div>

            <!-- Вкладки -->
            <div class="node-detail-tabs" role="tablist" aria-label="${escapeHtml(window.I18N.t('nodes.node_details_aria'))}">
                ${NODE_DETAIL_TABS.map((tab, index) => `
                    <button type="button"
                            class="node-detail-tab ${index === 0 ? 'active' : ''}"
                            data-tab="${escapeHtml(tab.id)}"
                            role="tab"
                            aria-selected="${index === 0 ? 'true' : 'false'}"
                            onclick="switchNodeDetailTab('${escapeHtml(tab.id)}', '${escapeHtml(nodeId)}')">${escapeHtml(tab.label)}</button>
                `).join('')}
            </div>

            <!-- Контент вкладок -->
            <div class="node-detail-content">
                ${NODE_DETAIL_TABS.map((tab, index) => `
                    <div class="node-detail-pane ${index === 0 ? 'active' : ''}"
                         id="pane-${escapeHtml(tab.id)}-${escapeHtml(nodeId)}"
                         data-node-id="${escapeHtml(nodeId)}"
                         role="tabpanel">
                        ${tab.render(node)}
                    </div>
                `).join('')}
            </div>
        </div>
    `;

    details.className = '';
    renderOrPatchNodeDetailCard(details, html, nodeId);
    nodeRenderCache[nodeId] = signature;

    // Restore the active tab immediately after the in-place patch.
    const savedTab = activeNodeTabs[nodeId] || 'overview';
    switchNodeDetailTab(savedTab, nodeId);

    // ---- Выпадающее меню Actions (вставляем после карточки) ----
    document.getElementById('nodeActionsMenu')?.remove();
    const actionsMenu = document.createElement('div');
    actionsMenu.className = 'node-actions-menu';
    actionsMenu.id = 'nodeActionsMenu';
    actionsMenu.style.display = 'none';
    actionsMenu.innerHTML = `
        <div class="node-actions-menu-inner">
            <button onclick="openChat('${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', 'dm')">📨 ${escapeHtml(window.I18N.t('nodes.send_message'))}</button>
            <button onclick="runNodeTool('request_position', '${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', this)">📍 ${escapeHtml(window.I18N.t('nodes.request_position'))}</button>
            <button onclick="runNodeTool('request_telemetry', '${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', this)">📊 ${escapeHtml(window.I18N.t('nodes.request_telemetry'))}</button>
            <button onclick="runNodeTool('traceroute', '${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', this)">🔍 ${escapeHtml(window.I18N.t('nodes.traceroute'))}</button>
            <button onclick="setNodeAsReference('${escapeHtml(nodeId)}')">📍 ${escapeHtml(window.I18N.t('nodes.set_as_reference'))}</button>
        </div>
    `;
    details.parentNode.insertBefore(actionsMenu, details.nextSibling);
    ensureNodeActionsCloser();
}

// ============================================================
// ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РЕНДЕРИНГА
// ============================================================

function renderOverviewPane(node) {
    const rssi = node.rssi ?? '--';
    const snr = node.snr ?? '--';
    const hops = node.hop_start ?? node.hops_away ?? '?';
    const battery = formatBatteryPercent(node.battery_level);
    const voltage = node.voltage ?? '--';
    const lastText = node.last_text || '';
    const hasPosition = Number.isFinite(node.position?.latitude) && Number.isFinite(node.position?.longitude);
    let distanceText = '--', bearingText = '--';
    if (hasPosition) {
        const ref = getReferenceLocation();
        if (ref && Number.isFinite(ref.latitude) && Number.isFinite(ref.longitude)) {
            const distM = calculateDistanceMeters(ref.latitude, ref.longitude, node.position.latitude, node.position.longitude);
            distanceText = formatNodeDistance(distM);
            const bearing = calculateBearingDegrees(ref.latitude, ref.longitude, node.position.latitude, node.position.longitude);
            bearingText = `${Math.round(bearing)}° ${getBearingDirection(bearing)}`;
        }
    }

    return `
        <div class="node-detail-overview">
            <div class="node-detail-tiles">
                <div class="tile">
                    <span class="tile-label">RSSI</span>
                    <span class="tile-value">${escapeHtml(rssi)} dBm</span>
                </div>
                <div class="tile">
                    <span class="tile-label">SNR</span>
                    <span class="tile-value">${escapeHtml(snr)} dB</span>
                </div>
                <div class="tile">
                    <span class="tile-label">${escapeHtml(window.I18N.t('nodes.hops_label'))}</span>
                    <span class="tile-value">${escapeHtml(hops)}</span>
                </div>
                <div class="tile">
                    <span class="tile-label">${escapeHtml(window.I18N.t('nodes.distance'))}</span>
                    <span class="tile-value">${escapeHtml(distanceText)}</span>
                </div>
                <div class="tile">
                    <span class="tile-label">${escapeHtml(window.I18N.t('nodes.bearing'))}</span>
                    <span class="tile-value">${escapeHtml(bearingText)}</span>
                </div>
                ${battery !== '--' ? `
                <div class="tile">
                    <span class="tile-label">${escapeHtml(window.I18N.t('node_panel.battery'))}</span>
                    <span class="tile-value">${escapeHtml(battery)}%</span>
                </div>` : ''}
                ${voltage !== '--' ? `
                <div class="tile">
                    <span class="tile-label">${escapeHtml(window.I18N.t('node_panel.voltage'))}</span>
                    <span class="tile-value">${escapeHtml(voltage)} V</span>
                </div>` : ''}
            </div>
            ${lastText ? `
            <div class="node-detail-last-msg">
                <span class="last-msg-label">${escapeHtml(window.I18N.t('nodes.last_message_label'))}</span>
                <span class="last-msg-text">${escapeHtml(truncateText(lastText, 80))}</span>
                <span class="last-msg-time">${escapeHtml(node.last_time || '')}</span>
            </div>` : ''}
            <div class="node-detail-quick-actions">
                <button class="quick-action" onclick="openChat('${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', 'dm')">💬 ${escapeHtml(window.I18N.t('nodes.message_button'))}</button>
                <button class="quick-action" onclick="openExternalNodeMap(${node.position?.latitude || 0}, ${node.position?.longitude || 0})" ${!hasPosition ? 'disabled' : ''}>🗺 ${escapeHtml(window.I18N.t('nodes.external_map'))}</button>
                <button class="quick-action" onclick="toggleNodeActionsMenu(event)">⚡ ${escapeHtml(window.I18N.t('nodes.more'))}</button>
            </div>
        </div>
    `;
}

function formatSignalQualityLabel(value) {
    const text = String(value ?? '--').trim();
    if (!text || text === '--') return '--';
    return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

function renderRadioPane(node) {
    const rssi = node.rssi ?? '--';
    const snr = node.snr ?? '--';
    const hops = node.hop_start ?? node.hops_away ?? '?';
    const lastSeen = node.age || window.I18N.t('nodes.never_seen');
    const relay = node.relay_node || '--';
    const signalQuality = formatSignalQualityLabel(node.signal_quality);

    // Простая история (заглушка)
    let historyHtml = `<div class="radio-history-placeholder">${escapeHtml(window.I18N.t('nodes.signal_history_unavailable'))}</div>`;

    return `
        <div class="node-detail-radio">
            <div class="radio-params">
                <div class="radio-param"><span class="label">${escapeHtml(window.I18N.t('nodes.signal_quality'))}</span><span class="value">${escapeHtml(signalQuality)}</span></div>
                <div class="radio-param"><span class="label">RSSI</span><span class="value">${escapeHtml(rssi)} dBm</span></div>
                <div class="radio-param"><span class="label">SNR</span><span class="value">${escapeHtml(snr)} dB</span></div>
                <div class="radio-param"><span class="label">${escapeHtml(window.I18N.t('nodes.hops_label'))}</span><span class="value">${escapeHtml(hops)}</span></div>
                <div class="radio-param"><span class="label">${escapeHtml(window.I18N.t('nodes.last_relay'))}</span><span class="value">${escapeHtml(relay)}</span></div>
                <div class="radio-param"><span class="label">${escapeHtml(window.I18N.t('nodes.last_heard'))}</span><span class="value">${escapeHtml(lastSeen)}</span></div>
            </div>
            <div class="radio-history">
                <div class="radio-history-header">
                    <span>${escapeHtml(window.I18N.t('nodes.signal_history'))}</span>
                    <span class="radio-history-range" title="${escapeHtml(window.I18N.t('nodes.time_ranges_tooltip'))}">30m · 1h · 6h · 24h</span>
                </div>
                ${historyHtml}
            </div>
            <div class="radio-actions">
                <button class="radio-action" onclick="runNodeTool('traceroute', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">🔍 ${escapeHtml(window.I18N.t('nodes.run_traceroute'))}</button>
                <button class="radio-action" onclick="refreshNodeMetrics('${escapeHtml(node.node_id)}')">↻ ${escapeHtml(window.I18N.t('common.refresh'))}</button>
            </div>
        </div>
    `;
}

function renderPositionPane(node) {
    const pos = node.position || {};
    const hasPosition = Number.isFinite(pos.latitude) && Number.isFinite(pos.longitude);
    const lat = hasPosition ? pos.latitude.toFixed(6) : '--';
    const lon = hasPosition ? pos.longitude.toFixed(6) : '--';
    const alt = Number.isFinite(pos.altitude) ? `${Math.round(pos.altitude)} m` : '--';
    const age = pos.updated_time || node.age || '--';
    const source = pos.source || window.I18N.t('nodes.source_radio');
    const precision = pos.precision_label || '--';

    let distanceText = '--', bearingText = '--';
    if (hasPosition) {
        const ref = getReferenceLocation();
        if (ref && Number.isFinite(ref.latitude) && Number.isFinite(ref.longitude)) {
            const distM = calculateDistanceMeters(ref.latitude, ref.longitude, pos.latitude, pos.longitude);
            distanceText = formatNodeDistance(distM);
            const bearing = calculateBearingDegrees(ref.latitude, ref.longitude, pos.latitude, pos.longitude);
            bearingText = `${Math.round(bearing)}° ${getBearingDirection(bearing)}`;
        }
    }

    const referenceName = (() => {
        const ref = getReferenceLocation();
        return ref ? ref.name : window.I18N.t('nodes.reference_not_set');
    })();

    return `
        <div class="node-detail-position">
            ${hasPosition ? `
            <div class="position-coords">
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('settings.latitude'))}</span><span class="value">${escapeHtml(lat)}</span></div>
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('settings.longitude'))}</span><span class="value">${escapeHtml(lon)}</span></div>
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('nodes.altitude'))}</span><span class="value">${escapeHtml(alt)}</span></div>
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('nodes.distance'))}</span><span class="value">${escapeHtml(distanceText)}</span></div>
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('nodes.bearing'))}</span><span class="value">${escapeHtml(bearingText)}</span></div>
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('nodes.position_age'))}</span><span class="value">${escapeHtml(age)}</span></div>
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('nodes.source'))}</span><span class="value">${escapeHtml(source)}</span></div>
                <div class="coord"><span class="label">${escapeHtml(window.I18N.t('nodes.precision'))}</span><span class="value">${escapeHtml(precision)}</span></div>
            </div>
            <div class="position-actions">
                <button onclick='openNodeMap(${pos.latitude}, ${pos.longitude}, ${JSON.stringify(String(node.node_id || ""))})'>🗺 ${escapeHtml(window.I18N.t('nodes.locate_on_map'))}</button>
                <button onclick="copyCoordinates('${pos.latitude}', '${pos.longitude}')">📋 ${escapeHtml(window.I18N.t('nodes.copy_coordinates'))}</button>
                <button onclick="setNodeAsReference('${escapeHtml(node.node_id)}')">📍 ${escapeHtml(window.I18N.t('nodes.set_as_reference'))}</button>
                <button onclick="runNodeTool('request_position', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">📡 ${escapeHtml(window.I18N.t('nodes.request_new_position'))}</button>
            </div>
            <div class="position-reference">${escapeHtml(window.I18N.t('nodes.reference_prefix', { name: referenceName }))}</div>
            ` : `
            <div class="position-no-data">
                <span>📍 ${escapeHtml(window.I18N.t('nodes.no_known_position'))}</span>
                <button onclick="runNodeTool('request_position', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">${escapeHtml(window.I18N.t('nodes.request_position'))}</button>
            </div>
            `}
        </div>
    `;
}

function telemetryValuePresent(value) {
    return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
}

function formatTelemetryNumber(value, decimals, fallback = '--') {
    if (!telemetryValuePresent(value)) return fallback;
    return Number(value).toFixed(decimals);
}

function formatBatteryPercent(value) {
    if (!telemetryValuePresent(value)) return '--';
    const number = Number(value);
    return String(Math.min(100, Math.max(0, Math.round(number))));
}

function formatPowerWattsFromMilliwatts(value) {
    if (!telemetryValuePresent(value)) return '--';
    return (Number(value) / 1000).toFixed(3);
}

function nodeMetricPresent(node, metricName, groupName = '') {
    if (!node || !telemetryValuePresent(node[metricName])) return false;

    const numericValue = Number(node[metricName]);
    const group = groupName && node[groupName] && typeof node[groupName] === 'object'
        ? node[groupName]
        : null;
    const explicitlyReceived = Boolean(
        group &&
        Object.prototype.hasOwnProperty.call(group, metricName) &&
        telemetryValuePresent(group[metricName]) &&
        (group.updated || group.source)
    );

    // A live Meshtastic node cannot meaningfully report 0 V. Old versions of
    // MeshCenter stored missing metrics as zero, so suppress those placeholders.
    if (metricName === 'voltage') return numericValue > 0;

    // Zero battery is valid. Environment zeroes can also be valid readings.
    if (['battery_level', 'temperature', 'humidity', 'pressure', 'channel_utilization', 'air_util_tx', 'uptime_seconds'].includes(metricName)) {
        return true;
    }

    // Current and power can genuinely be zero, but only when the corresponding
    // Power Metrics packet was explicitly observed.
    if (['current', 'power'].includes(metricName)) {
        return numericValue !== 0 || explicitlyReceived;
    }

    return explicitlyReceived || numericValue !== 0;
}

function queueTelemetryHistoryPrefetch(nodeId) {
    if (!nodeId || telemetryHistoryCache.has(nodeId)) return;

    window.setTimeout(() => {
        fetchTelemetryHistoryData(nodeId).catch(error => {
            console.debug('[TELEMETRY] History prefetch skipped:', error);
        });
    }, 250);
}

function renderDataPane(node) {
    queueTelemetryHistoryPrefetch(node.node_id);

    const hasBattery = nodeMetricPresent(node, 'battery_level', 'device_metrics');
    const hasDeviceVoltage = nodeMetricPresent(node, 'voltage', 'device_metrics');
    const hasChannelUtil = nodeMetricPresent(node, 'channel_utilization', 'device_metrics');
    const hasAirUtil = nodeMetricPresent(node, 'air_util_tx', 'device_metrics');
    const hasUptime = nodeMetricPresent(node, 'uptime_seconds', 'device_metrics');

    const hasTemperature = nodeMetricPresent(node, 'temperature', 'environment_metrics');
    const hasHumidity = nodeMetricPresent(node, 'humidity', 'environment_metrics');
    const hasPressure = nodeMetricPresent(node, 'pressure', 'environment_metrics');

    const hasPowerVoltage = nodeMetricPresent(node, 'voltage', 'power_metrics') || hasDeviceVoltage;
    const hasCurrent = nodeMetricPresent(node, 'current', 'power_metrics');
    const hasPowerValue = nodeMetricPresent(node, 'power', 'power_metrics') ||
        (hasPowerVoltage && hasCurrent);

    const hasEnv = hasTemperature || hasHumidity || hasPressure;
    const hasPower = hasPowerVoltage || hasCurrent || hasPowerValue;
    const nodeId = escapeHtml(node.node_id);
    const nodeName = escapeHtml(node.clean_name || node.name || node.node_id);

    const deviceRows = [
        hasBattery ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.battery'))}</span><span class="value">${escapeHtml(formatBatteryPercent(node.battery_level))}%</span></div>` : '',
        hasDeviceVoltage ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.voltage'))}</span><span class="value">${escapeHtml(formatTelemetryNumber(node.voltage, 3))} V</span></div>` : '',
        hasChannelUtil ? `<div><span class="label">${escapeHtml(window.I18N.t('nodes.channel_utilization'))}</span><span class="value">${escapeHtml(formatTelemetryNumber(node.channel_utilization, 2))}%</span></div>` : '',
        hasAirUtil ? `<div><span class="label">${escapeHtml(window.I18N.t('nodes.air_utilization_tx'))}</span><span class="value">${escapeHtml(formatTelemetryNumber(node.air_util_tx, 2))}%</span></div>` : '',
        hasUptime ? `<div><span class="label">${escapeHtml(window.I18N.t('nodes.uptime'))}</span><span class="value">${escapeHtml(formatUptime(node.uptime_seconds))}</span></div>` : ''
    ].filter(Boolean).join('');

    const environmentRows = [
        hasTemperature ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.temperature'))}</span><span class="value">${formatTemperature(node.temperature)}</span></div>` : '',
        hasHumidity ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.humidity'))}</span><span class="value">${escapeHtml(formatTelemetryNumber(node.humidity, 1))}%</span></div>` : '',
        hasPressure ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.pressure'))}</span><span class="value">${formatPressure(node.pressure)}</span></div>` : ''
    ].filter(Boolean).join('');

    const powerRows = [
        hasPowerVoltage ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.voltage'))}</span><span class="value">${escapeHtml(formatTelemetryNumber(node.voltage, 3))} V</span></div>` : '',
        hasCurrent ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.current'))}</span><span class="value">${escapeHtml(formatTelemetryNumber(node.current, 1))} mA</span></div>` : '',
        hasPowerValue ? `<div><span class="label">${escapeHtml(window.I18N.t('node_panel.power'))}</span><span class="value">${escapeHtml(formatPowerWattsFromMilliwatts(node.power))} W</span></div>` : ''
    ].filter(Boolean).join('');

    return `
        <div class="node-detail-data">
            <div class="data-group">
                <div class="data-group-title">📟 ${escapeHtml(window.I18N.t('nodes.device'))}</div>
                ${deviceRows ? `<div class="data-grid">${deviceRows}</div>` : `<div class="data-no-data">${escapeHtml(window.I18N.t('nodes.no_device_metrics'))}</div>`}
            </div>
            <div class="data-group">
                <div class="data-group-title">🌡️ ${escapeHtml(window.I18N.t('node_panel.environment'))}</div>
                ${hasEnv ? `<div class="data-grid">${environmentRows}</div>` : `<div class="data-no-data">${escapeHtml(window.I18N.t('nodes.no_environment_metrics'))}</div>`}
            </div>
            <div class="data-group">
                <div class="data-group-title">⚡ ${escapeHtml(window.I18N.t('node_panel.power'))}</div>
                ${hasPower ? `<div class="data-grid">${powerRows}</div>` : `<div class="data-no-data">${escapeHtml(window.I18N.t('nodes.no_power_metrics'))}</div>`}
            </div>
            <div class="data-actions">
                <button onclick="runNodeTool('request_telemetry', '${nodeId}', '${nodeName}', this)">📊 ${escapeHtml(window.I18N.t('nodes.request_telemetry'))}</button>
                ${hasPower ? `<button onclick="viewTelemetryHistory('${nodeId}', 'power')">⚡ ${escapeHtml(window.I18N.t('nodes.power_history'))}</button>` : ''}
                ${hasEnv ? `<button onclick="viewTelemetryHistory('${nodeId}', 'environment')">🌡️ ${escapeHtml(window.I18N.t('nodes.environment_history'))}</button>` : ''}
            </div>
        </div>
    `;
}

function renderLogPane(node) {
    // Сводка
    const summary = {
        first_seen: node.first_seen || '--',
        last_heard: node.age || window.I18N.t('nodes.never_seen'),
        last_text: node.last_time || window.I18N.t('nodes.never_seen'),
        last_position: node.position?.updated_time || window.I18N.t('nodes.never_seen'),
        last_telemetry: node.telemetry_time || window.I18N.t('nodes.never_seen'),
        packets: node.packets_received ?? '--',
        messages: node.messages_received ?? '--'
    };


    return `
        <div class="node-detail-log">
            <div class="log-summary">
                <div class="log-summary-item"><span class="label">${escapeHtml(window.I18N.t('nodes.first_seen'))}</span><span class="value">${escapeHtml(summary.first_seen)}</span></div>
                <div class="log-summary-item"><span class="label">${escapeHtml(window.I18N.t('nodes.last_heard'))}</span><span class="value">${escapeHtml(summary.last_heard)}</span></div>
                <div class="log-summary-item"><span class="label">${escapeHtml(window.I18N.t('nodes.last_text'))}</span><span class="value">${escapeHtml(summary.last_text)}</span></div>
                <div class="log-summary-item"><span class="label">${escapeHtml(window.I18N.t('nodes.last_position'))}</span><span class="value">${escapeHtml(summary.last_position)}</span></div>
                <div class="log-summary-item"><span class="label">${escapeHtml(window.I18N.t('nodes.last_telemetry'))}</span><span class="value">${escapeHtml(summary.last_telemetry)}</span></div>
                <div class="log-summary-item"><span class="label">${escapeHtml(window.I18N.t('nodes.packets'))}</span><span class="value">${escapeHtml(summary.packets)}</span></div>
                <div class="log-summary-item"><span class="label">${escapeHtml(window.I18N.t('nodes.messages'))}</span><span class="value">${escapeHtml(summary.messages)}</span></div>
            </div>
            <div class="log-events">
                <div class="log-events-title">${escapeHtml(window.I18N.t('nodes.event_history'))}</div>
                <div class="log-history-placeholder">${escapeHtml(window.I18N.t('nodes.event_history_unavailable'))}</div>
            </div>
        </div>
    `;
}

// ============================================================
// УПРАВЛЕНИЕ ВКЛАДКАМИ
// ============================================================

function switchNodeDetailTab(tabName, nodeId) {
    if (!nodeId || !NODE_DETAIL_TABS.some(tab => tab.id === tabName)) return;

    activeNodeTabs[nodeId] = tabName;
    const card = document.querySelector(`.node-detail-card[data-node-id="${CSS.escape(nodeId)}"]`);
    if (!card) return;

    card.querySelectorAll('.node-detail-pane').forEach(pane => {
        pane.classList.toggle('active', pane.id === `pane-${tabName}-${nodeId}`);
    });

    card.querySelectorAll('.node-detail-tab').forEach(tab => {
        const active = tab.dataset.tab === tabName;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
}

// ============================================================
// ДЕЙСТВИЯ
// ============================================================

function toggleNodeActionsMenu(event) {
    event.stopPropagation();
    const menu = document.getElementById('nodeActionsMenu');
    if (!menu) return;
    const isVisible = menu.style.display === 'block';
    menu.style.display = isVisible ? 'none' : 'block';
}

async function copyNodeId(nodeId) {
    const value = String(nodeId || '').trim();
    if (!value) return;

    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(value);
        } else {
            const textarea = document.createElement('textarea');
            textarea.value = value;
            textarea.setAttribute('readonly', '');
            textarea.style.position = 'fixed';
            textarea.style.left = '-9999px';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            textarea.setSelectionRange(0, textarea.value.length);
            const copied = document.execCommand('copy');
            textarea.remove();
            if (!copied) throw new Error('Copy command was rejected.');
        }
        showToast(`✅ ${window.I18N.t('nodes.node_id_copied')}`, 'success');
    } catch (error) {
        console.warn('Unable to copy Node ID:', error);
        showToast(`❌ ${window.I18N.t('nodes.node_id_copy_failed')}`, 'error');
    }
}

async function copyCoordinates(lat, lon) {
    const latitude = Number(lat);
    const longitude = Number(lon);

    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
        showToast(`❌ ${window.I18N.t('nodes.coordinates_unavailable')}`, 'error');
        return;
    }

    const coordinates = `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`;

    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(coordinates);
        } else {
            const textArea = document.createElement('textarea');
            textArea.value = coordinates;
            textArea.setAttribute('readonly', '');
            textArea.style.position = 'fixed';
            textArea.style.left = '-9999px';
            textArea.style.top = '0';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();

            const copied = document.execCommand('copy');
            textArea.remove();

            if (!copied) {
                throw new Error('Fallback clipboard copy failed');
            }
        }

        showToast(`✅ ${window.I18N.t('nodes.coordinates_copied')}`, 'success');
    } catch (error) {
        console.warn('[WAYPOINT] Failed to copy coordinates:', error);
        showToast(`❌ ${window.I18N.t('nodes.coordinates_copy_failed')}`, 'error');
    }
}

function closeWaypointPopup() {
    if (meshMap && typeof meshMap.closePopup === 'function') {
        meshMap.closePopup();
    }
}

function setNodeAsReference(nodeId) {
    // Устанавливаем текущую ноду как референс
    // Сохраняем в настройках
    const ref = {
        mode: 'node',
        node_id: nodeId
    };
    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reference_location: ref })
    })
    .then(response => response.json())
    .then(data => {
        if (data.ok) {
            appSettings = data.settings;
            updateSettingsUi();
            notifySettingsUpdated();
            showToast(`✅ ${window.I18N.t('nodes.reference_node_set')}`, 'success');
            // Перерисовать карточку
            const node = nodeCache.find(n => n.node_id === nodeId);
            if (node) renderNodeDetails(node);
        } else {
            showToast(`❌ ${window.I18N.t('nodes.set_reference_failed')}`, 'error');
        }
    })
    .catch(() => showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error'));
}

function refreshNodeMetrics(nodeId) {
    // Просто обновляем данные
    loadMessages();
    showToast(`↻ ${window.I18N.t('nodes.refreshing_local_data')}`, 'info');
}

function viewTelemetryHistory(nodeId, type = 'power') {
    const node = nodeCache.find(item => item.node_id === nodeId);
    const nodeName = node?.clean_name || node?.name || nodeId;
    const historyType = type === 'environment' ? 'environment' : 'power';
    openTelemetryModal(historyType, nodeId, nodeName);
}

let nodeActionsCloserInstalled = false;
function ensureNodeActionsCloser() {
    if (nodeActionsCloserInstalled) return;
    nodeActionsCloserInstalled = true;
    document.addEventListener('click', (event) => {
        const menu = document.getElementById('nodeActionsMenu');
        const button = document.querySelector('.node-detail-actions-btn');
        if (menu && button && !menu.contains(event.target) && !button.contains(event.target)) {
            menu.style.display = 'none';
        }
    });
}

function setNodeToolsBusy(isBusy) {
    radioCommandRunning = Boolean(isBusy);

    const toolsButton = document.getElementById('nodeToolsBtn');
    const toolsMenu = document.getElementById('nodeToolsMenu');

    if (toolsButton) {
        toolsButton.disabled = radioCommandRunning;

        toolsButton.innerHTML = radioCommandRunning
            ? `<span>⏳ ${escapeHtml(window.I18N.t('nodes.working'))}</span>`
            : `<span>🛠 ${escapeHtml(window.I18N.t('nodes.tools'))}</span><span id="nodeToolsArrow">▾</span>`;
    }

    if (radioCommandRunning && toolsMenu) {
        toolsMenu.style.display = 'none';
    }
}

function toggleNodeToolsMenu(forceOpen = null) {
    if (radioCommandRunning && forceOpen !== false) {
        showToast(window.I18N.t('nodes.another_command_running'), 'info');
        return;
    }

    const menu = document.getElementById('nodeToolsMenu');
    const arrow = document.getElementById('nodeToolsArrow');

    if (!menu) return;

    const currentlyOpen = menu.style.display === 'block';
    const shouldOpen = forceOpen === null
        ? !currentlyOpen
        : Boolean(forceOpen);

    menu.style.display = shouldOpen ? 'block' : 'none';

    if (arrow) {
        arrow.textContent = shouldOpen ? '▴' : '▾';
    }
}


function closeNodeToolsMenu() {
    toggleNodeToolsMenu(false);
}


function getTracerouteNodeName(nodeId) {
    const normalizedId = String(nodeId || '').toLowerCase();

    const node = nodeCache.find(item =>
        String(item.node_id || '').toLowerCase() === normalizedId
    );

    if (!node) {
        return nodeId;
    }

    return (
        node.clean_name ||
        node.name ||
        node.long_name ||
        node.short_name ||
        nodeId
    );
}


function parseTracerouteLine(line) {
    const text = String(line || '').trim();

    if (!text) {
        return {
            nodes: [],
            hopCount: 0
        };
    }

    const parts = text
        .split(/\s*-->\s*/)
        .map(part => part.trim())
        .filter(Boolean);

    const nodes = parts.map(part => {
        const match = part.match(
            /^(![0-9a-f]{8}|Unknown)(?:\s*\(([^)]+)\))?$/i
        );

        if (!match) {
            return {
                id: part,
                name: part,
                snr: ''
            };
        }

        const nodeId = match[1];
        const nodeName = nodeId.toLowerCase() === 'unknown'
            ? window.I18N.t('nodes.unknown_node')
            : getTracerouteNodeName(nodeId);

        return {
            id: nodeId,
            name: nodeName,
            snr: match[2] || ''
        };
    });

    return {
        nodes,
        hopCount: Math.max(0, nodes.length - 1)
    };
}


function parseTracerouteOutput(output) {
    const text = String(output || '');

    const forwardMatch = text.match(
        /Route traced towards destination:\s*\n([^\n]+)/i
    );

    const returnMatch = text.match(
        /Route traced back to us:\s*\n([^\n]+)/i
    );

    return {
        forward: parseTracerouteLine(
            forwardMatch?.[1]?.trim() || ''
        ),

        returnRoute: parseTracerouteLine(
            returnMatch?.[1]?.trim() || ''
        )
    };
}


function renderTracerouteChain(route) {
    if (!route || !Array.isArray(route.nodes) || !route.nodes.length) {
        return `
            <div class="route-empty">
                ${escapeHtml(window.I18N.t('nodes.route_unavailable'))}
            </div>
        `;
    }

    const nodesHtml = route.nodes.map((node, index) => {
        const isFirst = index === 0;
        const isLast = index === route.nodes.length - 1;

        const knownName =
            node.name &&
            node.id &&
            node.name.toLowerCase() !== node.id.toLowerCase();

        const nodeClasses = [
            'route-chain-node',
            isFirst ? 'route-chain-source' : '',
            isLast ? 'route-chain-destination' : ''
        ].filter(Boolean).join(' ');

        const nodeLabel = isFirst
            ? window.I18N.t('nodes.route_source')
            : (isLast ? window.I18N.t('nodes.route_destination') : '');

        const connector = !isLast
            ? `
                <div class="route-chain-connector">
                    <span class="route-chain-line"></span>

                    <span class="route-snr-badge">
                        ${escapeHtml(
                            route.nodes[index + 1].snr || window.I18N.t('nodes.snr_unknown')
                        )}
                    </span>

                    <span class="route-chain-arrow">↓</span>
                </div>
            `
            : '';

        return `
            <div class="${nodeClasses}">
                <div class="route-chain-dot"></div>

                <div class="route-chain-node-content"
                     title="${escapeHtml(node.id)}">

                    ${nodeLabel
                        ? `<span class="route-endpoint-label">${escapeHtml(nodeLabel)}</span>`
                        : ''
                    }

                    <div class="route-chain-name">
                        ${escapeHtml(node.name || node.id)}
                    </div>

                    ${knownName
                        ? `
                            <div class="route-chain-id">
                                ${escapeHtml(node.id)}
                            </div>
                        `
                        : ''
                    }
                </div>
            </div>

            ${connector}
        `;
    }).join('');

    return `
        <div class="route-chain-meta">
            ${escapeHtml(window.I18N.plural('nodes.hop_count', route.hopCount, { count: route.hopCount }))}
        </div>

        <div class="route-chain">
            ${nodesHtml}
        </div>
    `;
}

function formatDurationSeconds(totalSeconds) {
    const seconds = Number(totalSeconds);

    if (!Number.isFinite(seconds) || seconds < 0) {
        return null;
    }

    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    const parts = [];

    if (days > 0) {
        parts.push(`${days}d`);
    }

    if (hours > 0) {
        parts.push(`${hours}h`);
    }

    if (minutes > 0 || parts.length === 0) {
        parts.push(`${minutes}m`);
    }

    return parts.join(' ');
}


function formatTelemetryCliOutput(output) {
    const text = String(output || '');

    return text.replace(
        /Uptime:\s*(\d+)\s*s\b/i,
        (fullMatch, secondsText) => {
            const formatted = formatDurationSeconds(secondsText);

            return formatted
                ? `${window.I18N.t('nodes.uptime')}: ${formatted}`
                : fullMatch;
        }
    );
}

function renderNodeToolResult(nodeId, type, title, message, details = '') {
    const result = document.getElementById('nodeToolResult');
    if (!result) return;

    nodeToolResults[nodeId] = {
        type,
        title,
        message,
        details
    };

    result.className = `node-tool-result ${type}`;
    result.style.display = 'block';
    result.dataset.nodeId = nodeId;

    result.innerHTML = `
        <div class="node-tool-result-header">
            <strong>${escapeHtml(title)}</strong>

            <button type="button"
                    class="node-tool-result-close"
                    onclick="closeNodeToolResult('${escapeHtml(nodeId)}')"
                    title="${escapeHtml(window.I18N.t('common.close'))}">
                ×
            </button>
        </div>

        <div class="node-tool-result-message">
            ${escapeHtml(message)}
        </div>

        ${details}
    `;
}


function closeNodeToolResult(nodeId = null) {
    const result = document.getElementById('nodeToolResult');

    if (nodeId) {
        delete nodeToolResults[nodeId];
    }

    if (
        result &&
        (!nodeId || result.dataset.nodeId === nodeId)
    ) {
        result.style.display = 'none';
        result.innerHTML = '';
        result.className = 'node-tool-result';
        delete result.dataset.nodeId;
    }

    if (nodeToolResultTimer) {
        clearTimeout(nodeToolResultTimer);
        nodeToolResultTimer = null;
    }
}

function scheduleNodeToolResultClose(nodeId, delay = 20000) {
    if (nodeToolResultTimer) {
        clearTimeout(nodeToolResultTimer);
    }

    nodeToolResultTimer = setTimeout(() => {
        closeNodeToolResult(nodeId);
    }, delay);
}

async function waitForNodeTelemetryResponse(nodeId, requestedAt, timeoutMs = 50000) {
    const deadline = Date.now() + timeoutMs;
    const requestTimestamp = Number(requestedAt || 0);

    while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 2500));

        try {
            await loadMessages();
        } catch (error) {
            console.warn('[NODE TOOLS] Telemetry refresh failed:', error);
            continue;
        }

        const refreshedNode = nodeCache.find(item => item.node_id === nodeId);
        const telemetryTimestamp = Number(refreshedNode?.last_telemetry_time || 0);

        if (refreshedNode && telemetryTimestamp >= requestTimestamp) {
            if (currentChatId === nodeId) {
                renderNodeDetails(refreshedNode);
            }
            return refreshedNode;
        }
    }

    return null;
}


async function runNodeTool(action, nodeId, nodeName, button) {

    if (!action || !nodeId) return;

    if (radioCommandRunning) {
        showToast(
            window.I18N.t('nodes.another_command_running'),
            'info'
        );
        return;
    }

    closeNodeToolsMenu();
    setNodeToolsBusy(true);

    const originalText = button?.innerHTML || '';

    const toolConfig = {
        traceroute: {
            pendingTitle: `🛰 ${window.I18N.t('nodes.traceroute')}`,
            pendingMessage: window.I18N.t('nodes.checking_route_to', { name: nodeName }),
            successToast: `✅ ${window.I18N.t('nodes.traceroute_completed', { name: nodeName })}`,
            errorTitle: `❌ ${window.I18N.t('nodes.traceroute_failed')}`,
            errorToastPrefix: `❌ ${window.I18N.t('nodes.traceroute_failed')}`
        },

        request_telemetry: {
            pendingTitle: `📊 ${window.I18N.t('nodes.request_telemetry')}`,
            pendingMessage: window.I18N.t('nodes.requesting_telemetry_from', { name: nodeName }),
            successToast: `✅ ${window.I18N.t('nodes.telemetry_request_completed', { name: nodeName })}`,
            errorTitle: `❌ ${window.I18N.t('nodes.telemetry_request_failed')}`,
            errorToastPrefix: `❌ ${window.I18N.t('nodes.telemetry_request_failed')}`
        },

        request_position: {
            pendingTitle: `📍 ${window.I18N.t('nodes.request_position')}`,
            pendingMessage: window.I18N.t('nodes.requesting_position_from', { name: nodeName }),
            successToast: `✅ ${window.I18N.t('nodes.position_request_completed', { name: nodeName })}`,
            errorTitle: `❌ ${window.I18N.t('nodes.position_request_failed')}`,
            errorToastPrefix: `❌ ${window.I18N.t('nodes.position_request_failed')}`
        }
    };

    const currentTool = toolConfig[action];

    if (!currentTool) {
        showToast(window.I18N.t('nodes.unsupported_tool_action'), 'error');
        setNodeToolsBusy(false);
        return;
    }

    if (button) {
        button.disabled = true;
        button.innerHTML = `
            <span>⏳</span>
            <span>${escapeHtml(window.I18N.t('nodes.running'))}</span>
        `;
    }

    renderNodeToolResult(
        nodeId,
        'pending',
        currentTool.pendingTitle,
        currentTool.pendingMessage
    );

    try {
        const response = await fetch('/api/node_tools', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                action: action,
                node_id: nodeId
            })
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            if (response.status === 409 || data.status === 'busy') {
                throw new Error(
                    window.I18N.t('nodes.another_command_running')
                );
            }

            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            requestError.params = data.error_params || undefined;
            throw requestError;
        }

        if (action === 'traceroute') {
            const route = parseTracerouteOutput(data.output);

            const routeDetails = `
            <div class="route-grid">

                <div class="route-card route-card-forward">
                    <div class="route-card-header">
                        <span class="route-badge route-forward">
                            ${escapeHtml(window.I18N.t('nodes.route_forward'))}
                        </span>
                    </div>

                    ${renderTracerouteChain(route.forward)}
                </div>

                <div class="route-card route-card-return">
                    <div class="route-card-header">
                        <span class="route-badge route-return">
                            ${escapeHtml(window.I18N.t('nodes.route_return'))}
                        </span>
                    </div>

                    ${renderTracerouteChain(route.returnRoute)}
                </div>

            </div>
        `;

            renderNodeToolResult(
                nodeId,
                "success",
                `🛰 ${window.I18N.t('nodes.traceroute_to', { name: data.node_name || nodeName })}`,
                "",
                routeDetails
            );
            scheduleNodeToolResultClose(nodeId, 20000);
        }

        else if (action === 'request_telemetry') {
            const requestedAt = Number(data.requested_at || (Date.now() / 1000));
            const responseTimeoutMs = Number(data.response_timeout_seconds || 50) * 1000;

            renderNodeToolResult(
                nodeId,
                'pending',
                `📊 ${window.I18N.t('nodes.telemetry_request_sent_to', { name: data.node_name || nodeName })}`,
                window.I18N.t('nodes.listener_active_waiting')
            );

            showToast(
                `📡 ${window.I18N.t('nodes.telemetry_request_sent', { name: data.node_name || nodeName })}`,
                'info'
            );

            const refreshedNode = await waitForNodeTelemetryResponse(
                nodeId,
                requestedAt,
                responseTimeoutMs
            );

            if (refreshedNode) {
                // A fresh response may have created or merged a history record.
                // Discard the two-minute prefetch cache so View History reads it.
                telemetryHistoryCache.delete(nodeId);
                telemetryHistoryCache.delete('__local__');

                const device = refreshedNode.device_metrics || {};
                const details = `
                    <div class="telemetry-request-output">
                        <div class="telemetry-request-status">
                            ${escapeHtml(window.I18N.t('nodes.response_received_via_listener'))}
                        </div>
                        <div class="telemetry-request-note">
                            ${escapeHtml(window.I18N.t('node_panel.battery'))}: ${formatBatteryPercent(device.battery_level)}% ·
                            ${escapeHtml(window.I18N.t('node_panel.voltage'))}: ${device.voltage ?? '--'} V ·
                            ${escapeHtml(window.I18N.t('nodes.updated_label'))}: ${refreshedNode.last_telemetry_time_text || window.I18N.t('nodes.just_now')}
                        </div>
                    </div>
                `;

                renderNodeToolResult(
                    nodeId,
                    'success',
                    `📊 ${window.I18N.t('nodes.telemetry_received_from', { name: data.node_name || nodeName })}`,
                    '',
                    details
                );
                showToast(
                    `✅ ${window.I18N.t('nodes.telemetry_received', { name: data.node_name || nodeName })}`,
                    'success'
                );
            } else {
                renderNodeToolResult(
                    nodeId,
                    'error',
                    `⚠️ ${window.I18N.t('nodes.no_fresh_telemetry_title')}`,
                    window.I18N.t('nodes.no_fresh_telemetry_message')
                );
                showToast(
                    `⚠️ ${window.I18N.t('nodes.telemetry_sent_no_response', { name: data.node_name || nodeName })}`,
                    'info'
                );
            }

            scheduleNodeToolResultClose(nodeId, 20000);
        }

        else if (action === 'request_position') {
            const rawOutput = String(data.output || '').trim();

            const positionDetails = rawOutput
                ? `
                    <div class="telemetry-request-output">
                        <div class="telemetry-request-status">
                            ${escapeHtml(window.I18N.t('nodes.request_completed_by_cli'))}
                        </div>

                        <pre>${escapeHtml(rawOutput)}</pre>
                    </div>
                `
                : `
                    <div class="telemetry-request-output">
                        <div class="telemetry-request-status">
                            ${escapeHtml(window.I18N.t('nodes.position_request_sent_successfully'))}
                        </div>

                        <div class="telemetry-request-note">
                            ${escapeHtml(window.I18N.t('nodes.response_may_arrive_async'))}
                        </div>
                    </div>
                `;

            renderNodeToolResult(
                nodeId,
                'success',
                `📍 ${window.I18N.t('nodes.position_from', { name: data.node_name || nodeName })}`,
                '',
                positionDetails
            );

            if (data.position_saved && data.position) {
                const cachedIndex = nodeCache.findIndex(
                    item => item.node_id === nodeId
                );

                if (cachedIndex >= 0) {
                    nodeCache[cachedIndex] = {
                        ...nodeCache[cachedIndex],
                        position: data.position
                    };

                    if (currentChatId === nodeId) {
                        renderNodeDetails(nodeCache[cachedIndex]);
                    }
                } else {
                    await loadMessages();
                }
            }

            scheduleNodeToolResultClose(nodeId, 20000);
        }
        
        const completedName = data.node_name || nodeName;

        let successMessage = `✅ ${window.I18N.t('nodes.command_completed', { name: completedName })}`;

        if (action === 'traceroute') {
            successMessage = `✅ ${window.I18N.t('nodes.traceroute_completed', { name: completedName })}`;
        } else if (action === 'request_telemetry') {
            successMessage = '';
        } else if (action === 'request_position') {
            successMessage = `✅ ${window.I18N.t('nodes.position_request_completed', { name: completedName })}`;
        }

        if (successMessage) {
            showToast(successMessage, 'success');
        }

    } catch (error) {
        console.error('[NODE TOOLS] Error:', error);

        const translatedMessage = translateRequestError(error);

        renderNodeToolResult(
            nodeId,
            'error',
            currentTool.errorTitle,
            translatedMessage
        );

        showToast(translatedMessage, 'error');

    } finally {
        if (button) {
            button.disabled = false;
            button.innerHTML = originalText;
        }

        setNodeToolsBusy(false);
        setTimeout(loadRadioHealth, 1000);
    }
}

function clearNodeSearch() {
    nodeSearchTerm = '';
    const searchInput = document.getElementById('nodeSearchInput');
    if (searchInput) searchInput.value = '';
    loadMessages();
}

function installCompactNodeCardStyles() {
    if (document.getElementById('meshcenter-node-card-v11-styles')) return;

    const style = document.createElement('style');
    style.id = 'meshcenter-node-card-v11-styles';
    style.textContent = `
        .node-card.selected,
        .node-card.favorite.selected,
        .node-card.ignored.selected {
            background: #fff0f1 !important;
            border-color: #d96a73 !important;
            box-shadow:
                0 0 0 1px rgba(217, 106, 115, 0.16),
                0 3px 10px rgba(132, 53, 61, 0.10) !important;
        }

        body[data-theme="dark"] .node-card.selected,
        body[data-theme="dark"] .node-card.favorite.selected,
        body[data-theme="dark"] .node-card.ignored.selected,
        html[data-theme="dark"] .node-card.selected,
        html[data-theme="dark"] .node-card.favorite.selected,
        html[data-theme="dark"] .node-card.ignored.selected {
            background: linear-gradient(135deg, #43252d, #38222a) !important;
            border-color: #df6b76 !important;
            box-shadow:
                0 0 0 1px rgba(223, 107, 118, 0.18),
                0 3px 12px rgba(0, 0, 0, 0.20) !important;
        }

        .node-hop-count {
            flex: 0 0 auto;
            min-width: 24px;
            padding: 2px 5px;
            border-radius: 5px;
            background: rgba(91, 111, 126, 0.10);
            color: #4f6473;
            font-size: 11px;
            font-weight: 700;
            line-height: 1.2;
            text-align: center;
            white-space: nowrap;
        }

        .node-map-badge.node-map-badge-available {
            cursor: pointer;
        }
    `;
    document.head.appendChild(style);
}

installCompactNodeCardStyles();

function installNodeCardClickHandler() {
    const nodesList = document.getElementById('nodesList');
    if (!nodesList || nodesList.dataset.nodeClickHandlerInstalled === '1') return;

    nodesList.dataset.nodeClickHandlerInstalled = '1';
    nodesList.addEventListener('click', event => {
        const card = event.target.closest('.node-card');
        if (!card || !nodesList.contains(card)) return;

        // Preserve independent controls if interactive elements are added later.
        if (event.target.closest('button, a, input, select, textarea, [data-stop-node-select]')) {
            return;
        }

        const nodeId = card.dataset.nodeId;
        if (!nodeId) return;

        const node = nodeCache.find(item => String(item.node_id) === String(nodeId));
        const nodeName = node?.clean_name || node?.name || nodeId;

        selectNode(nodeId, nodeName, 'nodes');
    });
}

function selectNode(nodeId, nodeName, selectionSource = 'nodes') {
    const normalizedNodeId = String(nodeId || '');
    if (!normalizedNodeId) return;

    closedNodeDetailId = null;
    nodeVisualSelectionCleared = false;

    const mapIsOpen =
        typeof MapLayout !== 'undefined' && MapLayout.state.mode !== 'off';
    const selectedFromMap = selectionSource === 'map';

    // A click on the map must keep the current viewport. A click on the
    // compact Nodes list must move the map to the selected node.
    const preserveMapViewport = selectedFromMap;

    // Even when the DM is already open, repeat the map navigation. This is
    // important when the operator selects the same node again after panning
    // the map elsewhere.
    const sameOpenDirectMessage =
        String(currentChatId || '') === normalizedNodeId &&
        currentChatType === 'dm';

    if (!sameOpenDirectMessage) {
        openChat(
            normalizedNodeId,
            nodeName,
            'dm',
            selectedFromMap ? 'external' : 'nodes'
        );
    } else {
        syncSelectedNodeCard();
        updateNodeDetails(normalizedNodeId);
    }

    if (selectedFromMap) {
        requestSynchronizedListScroll(normalizedNodeId, 'external', {
            forceNodeCenter: true
        });
    } else {
        requestSynchronizedListScroll(normalizedNodeId, 'nodes');
    }

    // Render only once. Previously two preserveViewport renders were issued
    // around openChat(), so selecting a card changed the marker/popup but left
    // the map at its old position.
    if (mapIsOpen) {
        meshMapTargetNodeId = normalizedNodeId;
        renderMeshMap(normalizedNodeId, {
            preserveViewport: preserveMapViewport,
            openPopup: true
        });
    }
}

// ============================================================
// SENSORS & BASE STATUS
// ============================================================
function formatEstimatedRuntime(hours) {
    if (!Number.isFinite(hours) || hours <= 0) return '--';
    if (hours >= 48) {
        const days = Math.floor(hours / 24);
        const remainingHours = Math.round(hours % 24);
        return `${days}d ${remainingHours}h`;
    }
    if (hours >= 1) {
        const wholeHours = Math.floor(hours);
        const minutes = Math.round((hours - wholeHours) * 60);
        return `${wholeHours}h ${minutes}m`;
    }
    return `${Math.max(1, Math.round(hours * 60))}m`;
}

function deriveBatteryCurrentMa(currentMa, powerMw = null, voltageV = null) {
    const directCurrent = Number(currentMa);
    if (Number.isFinite(directCurrent) && directCurrent > 5) return directCurrent;

    const power = Number(powerMw);
    const voltage = Number(voltageV);
    if (Number.isFinite(power) && power > 0 && Number.isFinite(voltage) && voltage > 0.1) {
        // P[mW] / U[V] = I[mA]
        const derivedCurrent = power / voltage;
        if (Number.isFinite(derivedCurrent) && derivedCurrent > 5) return derivedCurrent;
    }

    return null;
}

function updateBatteryRuntime(currentMa, batteryPercent, powerMw = null, voltageV = null) {
    const runtimeEl = document.getElementById('batteryRuntime');
    if (!runtimeEl) return;

    if (Number.isFinite(batteryPercent)) {
        latestBatteryPercent = Math.max(0, Math.min(100, batteryPercent));
    }

    const effectiveCurrent = deriveBatteryCurrentMa(currentMa, powerMw, voltageV);
    if (Number.isFinite(effectiveCurrent)) {
        batteryCurrentSamples.push(effectiveCurrent);
        if (batteryCurrentSamples.length > 10) batteryCurrentSamples.shift();
    }

    const capacityMah = Number(appSettings?.power?.battery_capacity_mah || 3000);
    const percent = latestBatteryPercent;
    const averageCurrent = batteryCurrentSamples.length
        ? batteryCurrentSamples.reduce((sum, value) => sum + value, 0) / batteryCurrentSamples.length
        : null;

    if (!Number.isFinite(percent)) {
        runtimeEl.textContent = window.I18N.t('node_panel.waiting_for_charge_data');
        runtimeEl.title = window.I18N.t('node_panel.battery_percent_unavailable');
        return;
    }

    if (!Number.isFinite(averageCurrent) || averageCurrent <= 5) {
        runtimeEl.textContent = window.I18N.t('node_panel.waiting_for_data');
        runtimeEl.title = window.I18N.t('node_panel.current_unavailable');
        return;
    }

    const remainingMah = capacityMah * (percent / 100);
    const runtimeHours = remainingMah / averageCurrent;
    runtimeEl.textContent = formatEstimatedRuntime(runtimeHours);
    runtimeEl.title = window.I18N.t('node_panel.runtime_estimate_note', {
        capacity: Math.round(capacityMah),
        current: Math.round(averageCurrent)
    });
}

async function updateBatteryCapacitySetting() {
    const input = document.getElementById('batteryCapacityMah');
    if (!input) return;

    const capacity = Math.max(100, Math.min(50000, parseInt(input.value, 10) || 3000));
    input.value = capacity;

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ settings: { power: { battery_capacity_mah: capacity } } })
        });
        const data = await response.json();
        if (!data.ok) throw new Error(data.error || window.I18N.t('node_panel.battery_capacity_save_failed'));
        appSettings = data.settings || appSettings;
        updateBatteryRuntime(null, latestBatteryPercent);
        showToast(window.I18N.t('node_panel.battery_capacity_set', { capacity }), 'success');
    } catch (error) {
        console.error('Error updating battery capacity:', error);
        showToast(window.I18N.t('node_panel.battery_capacity_save_failed'), 'error');
    }
}

async function loadSensors() {
    try {
        const response = await fetch('/api/sensors');
        const data = await response.json();

        const sensorsCard = document.getElementById('sensorsCard');
        if (sensorsCard && (data.temperature !== null || data.voltage !== null)) {
            sensorsCard.style.display = 'block';

            console.log("Sensors API:", data);
            console.log("Temperature:", data.temperature);

            document.getElementById('tempValue').textContent = formatTemperature(data.temperature);
            document.getElementById('humValue').textContent = data.humidity !== null ? data.humidity.toFixed(1) : '--';
            document.getElementById('presValue').textContent = formatPressure(data.pressure);
            document.getElementById('voltValue').textContent = data.voltage !== null ? data.voltage.toFixed(2) : '--';
            document.getElementById('currValue').textContent = data.current !== null ? Math.round(data.current) : '--';
            document.getElementById('powValue').textContent = data.power !== null ? Math.round(data.power) : '--';

            if (data.battery_percent !== null) {
                const batteryIndicator = document.getElementById('batteryIndicator');
                if (batteryIndicator) batteryIndicator.style.display = 'block';
                const percent = Math.min(100, Math.max(0, data.battery_percent));
                const batteryFill = document.getElementById('batteryFill');
                if (batteryFill) {
                    batteryFill.style.width = percent + '%';
                    const hue = Math.max(0, Math.min(120, percent * 1.2));
                    batteryFill.style.background = `hsl(${hue} 72% 44%)`;
                }
                const batteryPercent = document.getElementById('batteryPercent');
                if (batteryPercent) batteryPercent.textContent = percent + '%';
                updateBatteryRuntime(Number(data.current), percent, Number(data.power), Number(data.voltage));
            } else {
                updateBatteryRuntime(Number(data.current), null, Number(data.power), Number(data.voltage));
            }

            document.getElementById('sensorUpdate').textContent = window.I18N.t('weather.updated_at', { time: data.last_update || '--' });
        }
    } catch (error) {
        console.error('Error loading sensors:', error);
    }
}

async function loadBaseStatus() {
    try {
        const response = await fetch('/api/base_status', { cache: 'no-store' });
        const data = await response.json();

        const previousNodeId = activeLocalNodeId;
        const previousProfileId = activeLocalProfileId;

        activeLocalNodeId = String(data.node_id || '').trim();
        activeLocalProfileId = String(
            data.profile_id || activeLocalNodeId.replace(/^!/, '')
        ).trim().toLowerCase();

        const identityChanged =
            normalizeMessageIdentity(previousNodeId)
                !== normalizeMessageIdentity(activeLocalNodeId)
            || normalizeMessageIdentity(previousProfileId)
                !== normalizeMessageIdentity(activeLocalProfileId);

        if (identityChanged) {
            // Force the current chat to be redrawn using the new active-radio
            // ownership context.  The next normal message poll performs the
            // render, so no additional network request is required here.
            lastRenderedSignature = {};
        }

        const nameEl = document.getElementById('baseNodeName');
        const uptimeEl = document.getElementById('baseUptimeBadge');
        if (nameEl) nameEl.textContent = data.node_name || 'Flint Base';
        if (uptimeEl) {
            const uptime = data.uptime_seconds !== null ? formatUptime(data.uptime_seconds) : '--';
            uptimeEl.textContent = window.I18N.t('node_panel.uptime_label', { value: uptime });
        }

        const percent = data.real_battery !== null ? Number(data.real_battery) :
            (data.battery_level !== null ? Number(data.battery_level) : null);
        if (Number.isFinite(percent)) {
            latestBatteryPercent = Math.max(0, Math.min(100, percent));
            updateBatteryRuntime(null, latestBatteryPercent);
        }
    } catch (error) {
        console.error('Error loading base status:', error);
    }
}

function formatUptime(seconds) {
    seconds = Number(seconds);
    if (isNaN(seconds)) return '--';
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
}

// ============================================================
// EVENT LISTENERS
// ============================================================
const WORKSPACE_STORAGE_KEY = 'meshcenter.workspace';
const WORKSPACE_DEFAULTS = Object.freeze({
    leftPanel: true,
    rightPanel: true,
    theme: 'system',
    compactMode: false
});

const MAP_LAYOUT_STORAGE_KEY = 'meshcenter.mapLayout.v1';

const MapLayout = {
    state: { mode: 'off', position: 'bottom' },
    load() {
        try {
            const saved = JSON.parse(localStorage.getItem(MAP_LAYOUT_STORAGE_KEY) || '{}');
            this.state.mode = ['off', 'full', 'split'].includes(saved.mode) ? saved.mode : 'off';
            this.state.position = ['top', 'bottom'].includes(saved.position) ? saved.position : 'bottom';
        } catch (error) {
            console.warn('[MAP] Unable to load map layout:', error);
        }
    },
    save() {
        try { localStorage.setItem(MAP_LAYOUT_STORAGE_KEY, JSON.stringify(this.state)); }
        catch (error) { console.warn('[MAP] Unable to save map layout:', error); }
    }
};

function setMapLayoutPopover(open) {
    const popover = document.getElementById('mapLayoutPopover');
    const button = document.getElementById('mapMenuBtn');
    if (!popover || !button) return;
    popover.hidden = !open;
    button.classList.toggle('active', open);
    button.setAttribute('aria-expanded', String(open));
}

function syncMapLayoutControls() {
    document.querySelectorAll('input[name="mapLayoutMode"]').forEach(input => {
        input.checked = input.value === MapLayout.state.mode;
    });
    document.querySelectorAll('input[name="mapSplitPosition"]').forEach(input => {
        input.checked = input.value === MapLayout.state.position;
    });
    const split = document.getElementById('mapSplitPosition');
    if (split) split.classList.toggle('disabled', MapLayout.state.mode !== 'split');
    document.getElementById('mapMenuBtn')?.classList.toggle('map-active', MapLayout.state.mode !== 'off');
}

function setMapLayoutMode(mode, persist = true) {
    if (!['off', 'full', 'split'].includes(mode)) return;
    MapLayout.state.mode = mode;
    if (persist) MapLayout.save();
    setMapLayoutPopover(false);

    if (currentMainTab === 'map' && mode !== 'full') {
        const fallback = ['chats', 'video', 'media', 'devices'].includes(lastOperationalMainTab)
            ? lastOperationalMainTab
            : 'chats';
        switchMainTab(fallback);
        return;
    }
    applyMapLayout();
}

function setMapSplitPosition(position) {
    if (!['top', 'bottom'].includes(position)) return;
    MapLayout.state.position = position;
    MapLayout.save();
    applyMapLayout();
}

function getOperationalViewElements() {
    return {
        chatHeader: document.getElementById('chatHeader'),
        chatPanels: document.querySelector('.chat-panels'),
        video: document.getElementById('videoView'),
        media: document.getElementById('mediaView'),
        devices: document.getElementById('devicesView')
    };
}

function restoreOperationalView() {
    const views = getOperationalViewElements();
    const messagesView = document.getElementById('messagesView');
    const chatListContainer = document.getElementById('chatListContainer');

    // First hide every operational workspace. This prevents stale inline
    // display values left by Full Map from leaking into another tab.
    if (views.chatHeader) views.chatHeader.style.display = 'none';
    if (document.querySelector('.chat-panels')) document.querySelector('.chat-panels').style.display = 'none';
    if (views.video) views.video.style.display = 'none';
    if (views.media) views.media.style.display = 'none';
    if (views.devices) views.devices.style.display = 'none';

    if (currentMainTab === 'chats') {
        if (views.chatHeader) views.chatHeader.style.display = 'flex';
        const chatPanels = document.querySelector('.chat-panels');
        if (chatPanels) chatPanels.style.display = 'flex';
        if (chatListContainer) chatListContainer.style.display = 'block';
        if (messagesView) messagesView.style.display = 'flex';
    } else if (currentMainTab === 'video') {
        if (views.video) views.video.style.display = 'flex';
    } else if (currentMainTab === 'media') {
        if (views.media) views.media.style.display = 'flex';
    } else if (currentMainTab === 'devices') {
        if (views.devices) views.devices.style.display = 'flex';
    }
}

function hideOperationalViewsForFullMap() {
    const views = getOperationalViewElements();
    const chatPanels = document.querySelector('.chat-panels');
    if (views.chatHeader) views.chatHeader.style.display = 'none';
    if (chatPanels) chatPanels.style.display = 'none';
    if (views.video) views.video.style.display = 'none';
    if (views.media) views.media.style.display = 'none';
    if (views.devices) views.devices.style.display = 'none';
}

function applyMapLayout() {
    const area = document.querySelector('.chat-area');
    const mapView = document.getElementById('mapView');
    if (!area || !mapView) return;

    const mode = MapLayout.state.mode;
    const position = MapLayout.state.position;
    const isOperational = ['chats', 'video', 'media', 'devices'].includes(currentMainTab);
    const shouldShowMap = mode !== 'off' && (isOperational || currentMainTab === 'map');

    area.classList.toggle('map-layout-full', shouldShowMap && mode === 'full');
    area.classList.toggle('map-layout-split', shouldShowMap && mode === 'split');
    area.classList.toggle('map-on-top', shouldShowMap && mode === 'split' && position === 'top');
    area.classList.toggle('map-on-bottom', shouldShowMap && mode === 'split' && position === 'bottom');

    // Full Map intentionally covers the active workspace. Split and Hide must
    // always restore it; previously inline display:none values remained and
    // caused blank Camera/Chats views and a chat header without chat panels.
    if (isOperational) {
        if (shouldShowMap && mode === 'full') hideOperationalViewsForFullMap();
        else restoreOperationalView();
    }

    if (shouldShowMap) {
        mapView.style.display = 'flex';
        if (mode === 'full') updateStatusDock('map');
        else updateStatusDock(currentMainTab);
        requestAnimationFrame(() => renderMeshMap(meshMapTargetNodeId, { preserveViewport: true, openPopup: false }));
    } else if (currentMainTab !== 'map') {
        mapView.style.display = 'none';
        if (isOperational) updateStatusDock(currentMainTab);
    }

    syncMapLayoutControls();
    scheduleMeshMapResize(0);
    scheduleMeshMapResize(120);
}

function initializeMapLayout() {
    MapLayout.load();
    syncMapLayoutControls();

    document.getElementById('mapMenuBtn')?.addEventListener('click', event => {
        event.stopPropagation();
        const popover = document.getElementById('mapLayoutPopover');
        setWorkspacePopover(false);
        setMapLayoutPopover(Boolean(popover?.hidden));
    });
    document.getElementById('mapLayoutPopoverClose')?.addEventListener('click', () => setMapLayoutPopover(false));
    document.querySelectorAll('input[name="mapLayoutMode"]').forEach(input => {
        input.addEventListener('change', () => setMapLayoutMode(input.value));
    });
    document.querySelectorAll('input[name="mapSplitPosition"]').forEach(input => {
        input.addEventListener('change', () => setMapSplitPosition(input.value));
    });
    document.addEventListener('click', event => {
        if (!event.target.closest('.dock-map-wrap')) setMapLayoutPopover(false);
    });
    applyMapLayout();
}

const Workspace = {
    state: { ...WORKSPACE_DEFAULTS },

    load() {
        try {
            const stored = JSON.parse(localStorage.getItem(WORKSPACE_STORAGE_KEY) || '{}');
            this.state = this.sanitize(stored);
            this.migrateLegacyPanelSettings();
        } catch (error) {
            console.warn('[WORKSPACE] Unable to load preferences:', error);
            this.state = { ...WORKSPACE_DEFAULTS };
        }
        return this.state;
    },

    sanitize(value) {
        const source = value && typeof value === 'object' ? value : {};
        return {
            leftPanel: source.leftPanel !== false,
            rightPanel: source.rightPanel !== false,
            theme: ['system', 'light', 'dark'].includes(source.theme) ? source.theme : 'system',
            compactMode: source.compactMode === true
        };
    },

    migrateLegacyPanelSettings() {
        try {
            const hasWorkspace = localStorage.getItem(WORKSPACE_STORAGE_KEY) !== null;
            if (hasWorkspace) return;
            const oldLeft = localStorage.getItem('meshcenter.basePanelHidden');
            const oldRight = localStorage.getItem('meshcenter.nodesPanelHidden');
            if (oldLeft !== null) this.state.leftPanel = oldLeft !== '1';
            if (oldRight !== null) this.state.rightPanel = oldRight !== '1';
            this.save();
        } catch (error) {
            console.warn('[WORKSPACE] Legacy preference migration failed:', error);
        }
    },

    save() {
        try {
            localStorage.setItem(WORKSPACE_STORAGE_KEY, JSON.stringify(this.state));
        } catch (error) {
            console.warn('[WORKSPACE] Unable to save preferences:', error);
        }
    },

    update(patch, persist = true) {
        this.state = this.sanitize({ ...this.state, ...patch });
        this.apply();
        if (persist) this.save();
    },

    resolveTheme() {
        if (this.state.theme !== 'system') return this.state.theme;
        return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    },

    applyTheme() {
        const resolvedTheme = this.resolveTheme();
        const themeChanged = document.documentElement.dataset.theme !== resolvedTheme;
        document.documentElement.dataset.theme = resolvedTheme;
        document.documentElement.dataset.themePreference = this.state.theme;
        document.documentElement.style.colorScheme = resolvedTheme;
        // Chart.js bakes axis/grid/legend/tooltip colors in at creation
        // time, so a live theme switch needs an explicit rebuild of any
        // currently-open telemetry chart.
        if (themeChanged) _refreshTelemetryChartOnThemeChange();
    },

    apply() {
        setBasePanelVisible(this.state.leftPanel, false);
        setNodesPanelVisible(this.state.rightPanel, false);
        this.applyTheme();
        document.body.classList.toggle('workspace-compact', this.state.compactMode);
        this.syncControls();
    },

    syncControls() {
        const left = document.getElementById('workspaceBasePanel');
        const right = document.getElementById('workspaceNodesPanel');
        const compact = document.getElementById('workspaceCompactMode');
        if (left) left.checked = this.state.leftPanel;
        if (right) right.checked = this.state.rightPanel;
        if (compact) compact.checked = this.state.compactMode;
        document.querySelectorAll('input[name="workspaceTheme"]').forEach(input => {
            input.checked = input.value === this.state.theme;
        });
    }
};

function applyPanelState(panel, button, isHidden, panelName) {
    if (!panel || !button) return;
    panel.classList.toggle('panel-hidden', isHidden);
    button.classList.toggle('panel-is-hidden', isHidden);
    button.setAttribute('aria-pressed', String(isHidden));
    const action = isHidden ? 'Show' : 'Hide';
    button.title = `${action} ${panelName} panel`;
    button.setAttribute('aria-label', `${action} ${panelName} panel`);
    panel.classList.remove('hidden');
}

function refreshMapAfterWorkspaceResize() {
    if (MapLayout.state.mode === 'off' || !meshMap) return;
    scheduleMeshMapResize(0);
    scheduleMeshMapResize(90);
    scheduleMeshMapResize(240);
}

function setBasePanelVisible(isVisible, persist = true) {
    applyPanelState(
        document.getElementById('baseSidebar'),
        document.getElementById('toggleBaseSidebarBtn'),
        !Boolean(isVisible),
        'Base'
    );
    refreshMapAfterWorkspaceResize();
    if (persist) Workspace.update({ leftPanel: Boolean(isVisible) });
}

function setNodesPanelVisible(isVisible, persist = true) {
    applyPanelState(
        document.getElementById('sidebar'),
        document.getElementById('toggleSidebarBtn'),
        !Boolean(isVisible),
        'Nodes'
    );
    refreshMapAfterWorkspaceResize();
    if (persist) Workspace.update({ rightPanel: Boolean(isVisible) });
}

// Backward-compatible wrappers for older code/export names.
// The previous UI API used "Hidden" booleans, while Workspace stores
// the clearer "Visible" state. Keeping these wrappers prevents a startup
// ReferenceError and allows any legacy call sites to continue working.
function setBasePanelHidden(isHidden, persist = true) {
    setBasePanelVisible(!Boolean(isHidden), persist);
}

function setNodesPanelHidden(isHidden, persist = true) {
    setNodesPanelVisible(!Boolean(isHidden), persist);
}

function setWorkspacePopover(open) {
    const popover = document.getElementById('workspacePopover');
    const button = document.getElementById('workspaceMenuBtn');
    if (!popover || !button) return;
    popover.hidden = !open;
    button.classList.toggle('active', open);
    button.setAttribute('aria-expanded', String(open));
}

function openWorkspacePage(page) {
    const allowedPages = new Set(['system', 'settings', 'about', 'map', 'node-manager']);
    if (!allowedPages.has(page)) return;
    setWorkspacePopover(false);
    switchMainTab(page);
}

function closeWorkspacePage() {
    const fallbackTab = ['chats', 'video', 'media', 'devices'].includes(lastOperationalMainTab)
        ? lastOperationalMainTab
        : 'chats';
    switchMainTab(fallbackTab);
}

function initializeWorkspace() {
    Workspace.load();
    Workspace.apply();
    initializeMapLayout();

    document.getElementById('toggleBaseSidebarBtn')?.addEventListener('click', () => {
        Workspace.update({ leftPanel: !Workspace.state.leftPanel });
    });
    document.getElementById('toggleSidebarBtn')?.addEventListener('click', () => {
        Workspace.update({ rightPanel: !Workspace.state.rightPanel });
    });
    document.getElementById('workspaceMenuBtn')?.addEventListener('click', event => {
        event.stopPropagation();
        const popover = document.getElementById('workspacePopover');
        setWorkspacePopover(Boolean(popover?.hidden));
    });
    document.getElementById('workspacePopoverClose')?.addEventListener('click', () => setWorkspacePopover(false));
    document.getElementById('workspaceBasePanel')?.addEventListener('change', event => {
        Workspace.update({ leftPanel: event.target.checked });
    });
    document.getElementById('workspaceNodesPanel')?.addEventListener('change', event => {
        Workspace.update({ rightPanel: event.target.checked });
    });
    document.getElementById('workspaceCompactMode')?.addEventListener('change', event => {
        Workspace.update({ compactMode: event.target.checked });
    });
    document.querySelectorAll('input[name="workspaceTheme"]').forEach(input => {
        input.addEventListener('change', event => {
            if (event.target.checked) Workspace.update({ theme: event.target.value });
        });
    });

    // When Theme is set to System, follow Windows/browser changes live.
    const systemThemeQuery = window.matchMedia?.('(prefers-color-scheme: dark)');
    const handleSystemThemeChange = () => {
        if (Workspace.state.theme === 'system') Workspace.applyTheme();
    };
    if (systemThemeQuery?.addEventListener) {
        systemThemeQuery.addEventListener('change', handleSystemThemeChange);
    } else if (systemThemeQuery?.addListener) {
        systemThemeQuery.addListener(handleSystemThemeChange);
    }
    document.addEventListener('click', event => {
        const popover = document.getElementById('workspacePopover');
        const button = document.getElementById('workspaceMenuBtn');

        // The Workspace markup may be unavailable while another view is being
        // rendered. Never let the global click handler break the rest of the UI.
        if (!popover || !button || popover.hidden) return;

        if (!popover.contains(event.target) && !button.contains(event.target)) {
            setWorkspacePopover(false);
        }
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') setWorkspacePopover(false);
    });
}

initializeWorkspace();

document.getElementById('nodeSearchInput')?.addEventListener('input', (e) => {
    nodeSearchTerm = e.target.value;
    loadMessages();
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeChatActions();
        closeConfirmDelete();
        closeConfirmClear();
        closeDeleteAllDmModal();
        if (isEmojiPickerOpen) {
            closeEmojiPicker();
        }
        if (currentChatId) {
            showChatList();
        }
    }
});

document.getElementById('chatActionsModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeChatActions();
    }
});

document.getElementById('confirmDeleteModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeConfirmDelete();
    }
});

document.getElementById('confirmClearModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeConfirmClear();
    }
});

document.getElementById('confirmDeleteAllDmModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeDeleteAllDmModal();
    }
});

// ============================================================
// EMOJI
// ============================================================
const EMOJI_DATA = {
    smileys: [
        '😊', '😂', '❤️', '🔥', '👍', '💯', '🎉', '✨',
        '🤔', '😎', '💪', '🙏', '🥰', '😍', '🤗', '🫶',
        '😘', '😗', '😙', '🥲', '😅', '😆', '🤣', '🥹',
        '😌', '😏', '😒', '😔', '😕', '🙃', '🤑', '😲',
        '😳', '😱', '🤯', '🥳', '🤩', '😇', '🥺', '🤪',
        '😜', '😝', '🫠', '🤭', '🫣', '🤫', '🤥', '😶'
    ],
    gestures: [
        '👋', '🤚', '🖐️', '✋', '🖖', '👌', '🤌', '🤏',
        '✌️', '🤞', '🫰', '🤟', '🤘', '👈', '👉', '👆',
        '👇', '☝️', '👍', '👎', '👊', '✊', '🤛', '🤜',
        '👏', '🙌', '🫶', '🤲', '🤝', '🙏', '✍️', '💅'
    ],
    food: [
        '🍕', '🍔', '🌮', '🌯', '🥗', '🍣', '🍱', '🍜',
        '🍲', '🍛', '🍙', '🍚', '🍘', '🥟', '🍤', '🍗',
        '🥩', '🍖', '🥓', '🧀', '🥚', '🍳', '🥞', '🧇',
        '🥐', '🥖', '🍞', '🧈', '🧂', '🍿', '🧁', '🍰',
        '🎂', '🍪', '🍩', '🍫', '🍬', '🍭', '🍮', '☕',
        '🍵', '🧃', '🥤', '🧋', '🍺', '🍷', '🥂', '🍾'
    ],
    activities: [
        '🎉', '🎊', '🎁', '🎈', '🎆', '🎇', '✨', '🎯',
        '🎮', '🎲', '♟️', '🧩', '🎨', '🖌️', '🎭', '🎬',
        '🎤', '🎧', '🎼', '🎹', '🥁', '🎸', '🎺', '🎻',
        '📚', '📖', '✍️', '🧑‍💻', '🏃', '🚶', '🥾', '🚴',
        '🏕️', '🎣', '🧗', '🏊', '⚽', '🏀', '🏈', '⚾',
        '🎾', '🏐', '🏓', '🏸', '🥏', '🎱', '🏆', '🏅'
    ],
    travel: [
        '🚗', '🚕', '🚙', '🚌', '🚎', '🚐', '🛻', '🚚',
        '🚛', '🚜', '🏍️', '🛵', '🚲', '🛴', '🚁', '✈️',
        '🛩️', '🛫', '🛬', '🚀', '🚢', '⛵', '🚤', '🛶',
        '🚂', '🚆', '🚇', '🚉', '🏠', '🏡', '🏢', '🏥',
        '🏫', '🏭', '⛺', '🏕️', '🏔️', '⛰️', '🌋', '🏖️',
        '🏝️', '🌲', '🌳', '🗺️', '📍', '🧭', '🛣️', '🚧'
    ],
    objects: [
        '🔋', '🪫', '🔌', '💡', '🔦', '🕯️', '📡', '📶',
        '📱', '☎️', '☢️', '💻', '🖥️', '⌨️', '🖱️', '🖨️',
        '📷', '📹', '🎥', '📻', '🎙️', '🎧', '🔊', '🔔',
        '⌚', '⏰', '⏱️', '🧭', '⚙️', '🔧', '🪛', '🔩',
        '🔨', '🧰', '🪚', '⛏️', '🧲', '🔬', '🔭', '🛰️',
        '📊', '📈', '📉', '📋', '📁', '📂', '📄', '📝',
        '📌', '☣️', '✂️', '🔒', '🔓', '🔑', '🧯', '⚠️'
    ],
    weather: [
        '☀️', '🌤️', '⛅', '🌥️', '☁️', '🌦️', '🌧️', '⛈️',
        '🌩️', '🌨️', '❄️', '☃️', '⛄', '🌪️', '🌫️', '🌈',
        '🔥', '💧', '🌊', '🌡️', '🌬️', '💨', '⚡', '☔',
        '🌙', '🌛', '🌜', '🌚', '🌝', '⭐', '🌟', '💫',
        '🕐', '🕑', '🕒', '🕓', '🕔', '🕕', '🕖', '🕗',
        '🕘', '🕙', '🕚', '🕛', '⏰', '⏱️', '⏲️', '⌛'
    ]
};

let currentEmojiCategory = 'smileys';
let isEmojiPickerOpen = false;

function openEmojiPicker() {
    const picker = document.getElementById('emojiPicker');
    if (!picker) return;
    
    isEmojiPickerOpen = true;
    picker.style.display = 'flex';
    renderEmojiCategory(currentEmojiCategory);
    
    document.querySelectorAll('.emoji-cat-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.cat === currentEmojiCategory);
    });
}

function closeEmojiPicker() {
    const picker = document.getElementById('emojiPicker');
    if (picker) {
        picker.style.display = 'none';
    }
    isEmojiPickerOpen = false;
}

function toggleEmojiPicker() {
    if (isEmojiPickerOpen) {
        closeEmojiPicker();
    } else {
        openEmojiPicker();
    }
}

function renderEmojiCategory(category) {
    const grid = document.getElementById('emojiGrid');
    if (!grid) return;
    
    const emojis = EMOJI_DATA[category] || EMOJI_DATA.smileys;
    grid.innerHTML = emojis.map(emoji => 
        `<button class="emoji-item" data-emoji="${emoji}">${emoji}</button>`
    ).join('');
}

function insertEmoji(emoji) {
    const input = document.getElementById('messageInput');
    if (!input) return;
    
    const start = input.selectionStart;
    const end = input.selectionEnd;
    const text = input.value;
    
    input.value = text.substring(0, start) + emoji + text.substring(end);
    const newPos = start + emoji.length;
    input.selectionStart = input.selectionEnd = newPos;
    
    input.focus();
    closeEmojiPicker();
}

document.addEventListener('DOMContentLoaded', function() {
    const emojiBtn = document.getElementById('emojiBtn');
    if (emojiBtn) {
        emojiBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            toggleEmojiPicker();
        });
    }
    
    const closeBtn = document.getElementById('emojiCloseBtn');
    if (closeBtn) {
        closeBtn.addEventListener('click', function() {
            closeEmojiPicker();
        });
    }
    
    document.querySelectorAll('.emoji-cat-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const cat = this.dataset.cat;
            currentEmojiCategory = cat;
            document.querySelectorAll('.emoji-cat-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            renderEmojiCategory(cat);
        });
    });
    
    document.getElementById('emojiGrid')?.addEventListener('click', function(e) {
        const item = e.target.closest('.emoji-item');
        if (item) {
            const emoji = item.dataset.emoji;
            if (emoji) {
                insertEmoji(emoji);
            }
        }
    });
    
    document.addEventListener('click', function(e) {
        const picker = document.getElementById('emojiPicker');
        const btn = document.getElementById('emojiBtn');
        if (isEmojiPickerOpen && picker && btn) {
            if (!picker.contains(e.target) && !btn.contains(e.target)) {
                closeEmojiPicker();
            }
        }
    });
    
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && isEmojiPickerOpen) {
            closeEmojiPicker();
        }
    });
    
    document.querySelector('.messages-container')?.addEventListener('scroll', function() {
        if (isEmojiPickerOpen) {
            closeEmojiPicker();
        }
    });
});

// ============================================================
// SWITCH SIDEBAR TAB
// ============================================================
function switchSidebarTab(tab) {
    if (tab === 'tools') setTimeout(() => refreshWaypointToolsList(false), 0);
    document.querySelectorAll('.sidebar-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    
    document.querySelectorAll('.sidebar-tab-content').forEach(content => {
        content.style.display = content.id === 'tab-' + tab ? 'flex' : 'none';
        content.classList.toggle('active', content.id === 'tab-' + tab);
    });
    
    if (tab === 'tools') {
        loadNodesManagement();
    } else if (tab === 'nodes') {
        loadMessages();
        if (currentChatId && currentChatType === 'dm') {
            updateNodeDetails(currentChatId);
        }
    }
}

// ============================================================
// NODE MANAGEMENT
// ============================================================
async function loadNodesManagement() {
    const container = document.getElementById('nodesManagementList');
    if (!container) return;
    
    try {
        const response = await fetch('/api/nodes_management');
        const data = await response.json();
        
        document.getElementById('totalNodesCount').textContent = data.total || 0;
        
        if (data.nodes.length === 0) {
            container.innerHTML = `<div class="loading">${escapeHtml(window.I18N.t('nodes.no_nodes_found'))}</div>`;
            return;
        }

        container.innerHTML = data.nodes.map(node => {
            const statusClass = node.ignored ? 'ignored' : 'normal';
            const statusText = node.ignored ? window.I18N.t('node_manager.status_ignored') : window.I18N.t('node_manager.status_normal');
            const activityClass = node.ignored ? 'activity-unknown' : 'activity-online';

            return `
                <div class="nodes-management-item">
                    <span class="node-activity-square ${activityClass}" title="${escapeHtml(statusText)}"></span>
                    <div class="name-wrapper">
                        <span class="name">${escapeHtml(node.name)}</span>
                        <span class="id">${escapeHtml(node.node_id)}</span>
                    </div>
                    <span class="status ${statusClass}">${escapeHtml(statusText)}</span>
                </div>
            `;
        }).join('');
    } catch (error) {
        console.error('Error loading nodes management:', error);
        container.innerHTML = `<div class="loading">⚠️ ${escapeHtml(window.I18N.t('node_manager.error_loading_nodes'))}</div>`;
    }
}

// ============================================================
// EXPORT/IMPORT
// ============================================================
async function exportNodesCSV() {
    try {
        const response = await fetch('/api/nodes_export');
        const data = await response.json();
        
        if (!data.nodes || data.nodes.length === 0) {
            showToast(`❌ ${window.I18N.t('node_manager.no_nodes_to_export')}`, 'error');
            return;
        }

        const headers = ['"Node Name","Node ID","Last Seen","RSSI","SNR","Role","Short Name","HW Model"'];
        const rows = data.nodes.map(n => 
            `"${escapeCsv(n.name)}","${n.node_id}","${n.last_time || ''}","${n.rssi || ''}","${n.snr || ''}","${n.role || 'CLIENT'}","${n.short_name || ''}","${n.hw_model || ''}"`
        );
        
        const csv = headers.concat(rows).join('\n');
        const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `meshtastic_nodes_${new Date().toISOString().slice(0,10)}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        
        showToast(`✅ ${window.I18N.t('node_manager.exported_csv', { count: data.nodes.length })}`, 'success');
    } catch (error) {
        console.error('Export CSV error:', error);
        showToast(`❌ ${window.I18N.t('node_manager.export_failed')}`, 'error');
    }
}

async function exportNodesJSON() {
    try {
        const response = await fetch('/api/nodes_export');
        const data = await response.json();
        
        if (!data.nodes || data.nodes.length === 0) {
            showToast(`❌ ${window.I18N.t('node_manager.no_nodes_to_export')}`, 'error');
            return;
        }

        const blob = new Blob([JSON.stringify(data.nodes, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `meshtastic_nodes_${new Date().toISOString().slice(0,10)}.json`;
        a.click();
        URL.revokeObjectURL(url);

        showToast(`✅ ${window.I18N.t('node_manager.exported_json', { count: data.nodes.length })}`, 'success');
    } catch (error) {
        console.error('Export JSON error:', error);
        showToast(`❌ ${window.I18N.t('node_manager.export_failed')}`, 'error');
    }
}

async function importNodesCSV(event) {
    const file = event.target.files[0];
    if (!file) return;
    
    const reader = new FileReader();
    reader.onload = async function(e) {
        try {
            const text = e.target.result;
            const lines = text.split('\n').filter(line => line.trim());
            if (lines.length < 2) {
                showToast(`❌ ${window.I18N.t('node_manager.invalid_csv_file')}`, 'error');
                return;
            }
            
            const headerLine = lines[0].replace(/^"|"$/g, '').split('","');
            const headers = headerLine.map(h => h.replace(/"/g, '').trim());
            
            const nodes = [];
            for (let i = 1; i < lines.length; i++) {
                const line = lines[i].replace(/^"|"$/g, '').split('","');
                const node = {};
                headers.forEach((h, idx) => {
                    const val = (line[idx] || '').replace(/"/g, '').trim();
                    if (h === 'Node Name') node.name = val;
                    else if (h === 'Node ID') node.node_id = val;
                    else if (h === 'Short Name') node.short_name = val;
                    else if (h === 'HW Model') node.hw_model = val;
                    else if (h === 'Role') node.role = val;
                    else if (h === 'Last Seen') node.last_time = val;
                    else if (h === 'RSSI') node.rssi = val;
                    else if (h === 'SNR') node.snr = val;
                });
                if (node.node_id) nodes.push(node);
            }
            
            if (nodes.length === 0) {
                showToast(`❌ ${window.I18N.t('node_manager.no_valid_nodes_csv')}`, 'error');
                return;
            }

            const response = await fetch('/api/nodes_import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes })
            });

            const result = await response.json();
            if (result.ok) {
                showToast(`✅ ${window.I18N.t('node_manager.imported_csv', { count: result.imported_count })}`, 'success');
                loadMessages();
                loadNodesManagement();
            } else {
                showToast(`❌ ${window.I18N.t('node_manager.import_failed_reason', { reason: result.error })}`, 'error');
            }
        } catch (error) {
            console.error('Import CSV error:', error);
            showToast(`❌ ${window.I18N.t('node_manager.import_failed')}`, 'error');
        }
    };
    reader.readAsText(file);
    event.target.value = '';
}

async function importNodesJSON(event) {
    const file = event.target.files[0];
    if (!file) return;
    
    const reader = new FileReader();
    reader.onload = async function(e) {
        try {
            const nodes = JSON.parse(e.target.result);
            if (!Array.isArray(nodes) || nodes.length === 0) {
                showToast(`❌ ${window.I18N.t('node_manager.invalid_json_file')}`, 'error');
                return;
            }

            const response = await fetch('/api/nodes_import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes })
            });

            const result = await response.json();
            if (result.ok) {
                showToast(`✅ ${window.I18N.t('node_manager.imported_json', { count: result.imported_count })}`, 'success');
                loadMessages();
                loadNodesManagement();
            } else {
                showToast(`❌ ${window.I18N.t('node_manager.import_failed_reason', { reason: result.error })}`, 'error');
            }
        } catch (error) {
            console.error('Import JSON error:', error);
            showToast(`❌ ${window.I18N.t('node_manager.import_failed')}`, 'error');
        }
    };
    reader.readAsText(file);
    event.target.value = '';
}

function escapeCsv(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/"/g, '""');
}

async function restartListener() {
    if (!confirm(window.I18N.t('system.restart_listener_confirm'))) {
        return;
    }

    const button = document.getElementById('restartListenerBtn');
    const originalText = button?.textContent || `🔄 ${window.I18N.t('system.restart_listener_button_label')}`;

    if (button) {
        button.disabled = true;
        button.textContent = window.I18N.t('system.restarting_listener');
    }

    try {
        const response = await fetch('/api/restart_listener', { method: 'POST' });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        showToast(`✅ ${window.I18N.t('system.listener_restart_requested')}`, 'success');
        setTimeout(loadRadioHealth, 1000);
    } catch (error) {
        showToast(`❌ ${window.I18N.t('system.restart_failed', { reason: error.message })}`, 'error');
    } finally {
        if (button) {
            setTimeout(() => {
                button.disabled = false;
                button.textContent = originalText;
            }, 1500);
        }
    }
}

async function rescanNodes() {
    resetNodeRenderCache();
    const btn = document.getElementById('rescanNodesBtn');
    const originalText = btn.textContent;
    
    try {
        btn.disabled = true;
        btn.textContent = `⏳ ${window.I18N.t('system.scanning')}`;

        const response = await fetch('/api/rescan_nodes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.ok) {
            btn.textContent = `⏳ ${window.I18N.t('system.waiting_for_nodes')}`;
            await new Promise(resolve => setTimeout(resolve, 5000));

            await loadMessages();
            await loadChatList();

            btn.textContent = `✅ ${window.I18N.t('system.done_exclaim')}`;
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 2000);

            showToast(`✅ ${window.I18N.t('system.network_rescanned')}`, 'success');
        } else {
            showToast(`❌ ${window.I18N.t('common.error_prefix', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
            btn.textContent = originalText;
            btn.disabled = false;
        }
    } catch (error) {
        console.error('Rescan error:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
        btn.textContent = originalText;
        btn.disabled = false;
    }
}

// ============================================================
// DELETE ALL DM CHATS
// ============================================================
let deleteAllDmState = 'first';

function deleteAllDmChats() {
    deleteAllDmState = 'first';
    const modal = document.getElementById('confirmDeleteAllDmModal');
    const text = document.getElementById('deleteAllDmText');
    const btn = document.getElementById('confirmDeleteAllDmBtn');
    
    if (modal && text) {
        text.textContent = `⚠️ ${window.I18N.t('chat.delete_all_dm_confirm')}`;
        btn.textContent = window.I18N.t('modals.delete_all');
        btn.style.background = '';
        modal.style.display = 'flex';
    }
}

function closeDeleteAllDmModal() {
    const modal = document.getElementById('confirmDeleteAllDmModal');
    if (modal) modal.style.display = 'none';
    deleteAllDmState = 'first';
}

function executeDeleteAllDm() {
    const btn = document.getElementById('confirmDeleteAllDmBtn');
    const text = document.getElementById('deleteAllDmText');
    
    if (deleteAllDmState === 'first') {
        deleteAllDmState = 'second';
        text.textContent = `⚠️ ${window.I18N.t('chat.delete_all_dm_confirm2')}`;
        btn.textContent = window.I18N.t('chat.yes_delete_everything');
        btn.style.background = '#c62828';
        return;
    }

    btn.disabled = true;
    btn.textContent = `⏳ ${window.I18N.t('chat.deleting')}`;

    fetch('/api/delete_all_dm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.ok) {
            closeDeleteAllDmModal();
            loadChatList();
            loadMessages();
            if (currentChatType === 'dm') {
                showChatList();
            }
            showToast(`✅ ${window.I18N.t('chat.deleted_dm_count', { count: data.deleted_count })}`, 'success');
        } else {
            showToast(`❌ ${window.I18N.t('common.error_prefix', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
            btn.disabled = false;
            btn.textContent = window.I18N.t('modals.delete_all');
            btn.style.background = '';
        }
    })
    .catch(error => {
        console.error('Delete all DM error:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
        btn.disabled = false;
        btn.textContent = window.I18N.t('modals.delete_all');
        btn.style.background = '';
    });
}

// ============================================================
// EXPORT/IMPORT MENUS
// ============================================================
function showExportOptions() {
    closeFormatMenus();
    const menu = document.getElementById('exportOptionsMenu');
    if (menu) menu.style.display = 'block';
}

function showImportOptions() {
    closeFormatMenus();
    const menu = document.getElementById('importOptionsMenu');
    if (menu) menu.style.display = 'block';
}

function closeFormatMenus() {
    const exportMenu = document.getElementById('exportOptionsMenu');
    const importMenu = document.getElementById('importOptionsMenu');
    if (exportMenu) exportMenu.style.display = 'none';
    if (importMenu) importMenu.style.display = 'none';
}

document.addEventListener('click', function(e) {
    const exportMenu = document.getElementById('exportOptionsMenu');
    const importMenu = document.getElementById('importOptionsMenu');
    const exportBtn = document.querySelector('.nodes-tool-btn.export');
    const importBtn = document.querySelector('.nodes-tool-btn.import');
    
    if (exportMenu && exportMenu.style.display === 'block' && !exportMenu.contains(e.target) && !exportBtn?.contains(e.target)) {
        exportMenu.style.display = 'none';
    }
    if (importMenu && importMenu.style.display === 'block' && !importMenu.contains(e.target) && !importBtn?.contains(e.target)) {
        importMenu.style.display = 'none';
    }
        const nodeToolsMenu = document.getElementById('nodeToolsMenu');
    const nodeToolsButton = document.getElementById('nodeToolsBtn');

    if (
        nodeToolsMenu &&
        nodeToolsMenu.style.display === 'block' &&
        !nodeToolsMenu.contains(e.target) &&
        !nodeToolsButton?.contains(e.target)
    ) {
        closeNodeToolsMenu();
    }
});

// ============================================================
// TELEMETRY FUNCTIONS
// ============================================================
let telemetryModalRequestId = 0;

function renderTelemetryCardsLayout(type) {
    const cards = document.getElementById('telemetryCards');
    if (!cards) return;

    if (type === 'power') {
        cards.innerHTML = `
            <div class="telemetry-card" id="powerVoltageCard">
                <div class="card-label">⚡ ${escapeHtml(window.I18N.t('node_panel.voltage'))}</div>
                <div class="card-value" id="powerVoltageValue">--</div>
                <div class="card-range">
                    <span class="range-min" id="powerVoltageMin">--</span>
                    <span class="range-sep">—</span>
                    <span class="range-max" id="powerVoltageMax">--</span>
                </div>
            </div>
            <div class="telemetry-card" id="powerCurrentCard">
                <div class="card-label">🔌 ${escapeHtml(window.I18N.t('node_panel.current'))}</div>
                <div class="card-value" id="powerCurrentValue">--</div>
                <div class="card-range">
                    <span class="range-min" id="powerCurrentMin">--</span>
                    <span class="range-sep">—</span>
                    <span class="range-max" id="powerCurrentMax">--</span>
                </div>
            </div>
            <div class="telemetry-card" id="powerPowerCard">
                <div class="card-label">⚡ ${escapeHtml(window.I18N.t('node_panel.power'))}</div>
                <div class="card-value" id="powerPowerValue">--</div>
                <div class="card-range">
                    <span class="range-min" id="powerPowerMin">--</span>
                    <span class="range-sep">—</span>
                    <span class="range-max" id="powerPowerMax">--</span>
                </div>
            </div>`;
        return;
    }

    cards.innerHTML = `
        <div class="telemetry-card" id="environmentTemperatureCard">
            <div class="card-label">🌡️ ${escapeHtml(window.I18N.t('node_panel.temperature'))}</div>
            <div class="card-value" id="environmentTemperatureValue">--</div>
            <div class="card-range">
                <span class="range-min" id="environmentTemperatureMin">--</span>
                <span class="range-sep">—</span>
                <span class="range-max" id="environmentTemperatureMax">--</span>
            </div>
        </div>
        <div class="telemetry-card" id="environmentHumidityCard">
            <div class="card-label">💧 ${escapeHtml(window.I18N.t('node_panel.humidity'))}</div>
            <div class="card-value" id="environmentHumidityValue">--</div>
            <div class="card-range">
                <span class="range-min" id="environmentHumidityMin">--</span>
                <span class="range-sep">—</span>
                <span class="range-max" id="environmentHumidityMax">--</span>
            </div>
        </div>
        <div class="telemetry-card" id="environmentPressureCard">
            <div class="card-label">📊 ${escapeHtml(window.I18N.t('node_panel.pressure'))}</div>
            <div class="card-value" id="environmentPressureValue">--</div>
            <div class="card-range">
                <span class="range-min" id="environmentPressureMin">--</span>
                <span class="range-sep">—</span>
                <span class="range-max" id="environmentPressureMax">--</span>
            </div>
        </div>`;
}

function latestTelemetryValue(records, key) {
    for (let index = records.length - 1; index >= 0; index -= 1) {
        const value = records[index]?.[key];
        if (value !== null && value !== undefined && Number.isFinite(Number(value))) {
            return Number(value);
        }
    }
    return null;
}

async function loadTelemetry() {
    console.log('[TELEMETRY] loadTelemetry called');
    try {
        const response = await fetch('/api/telemetry');
        const data = await response.json();
        telemetryData = data;
        updateTelemetryUI();
    } catch (error) {
        console.error('[TELEMETRY] Error loading telemetry:', error);
    }
}

async function fetchTelemetryHistoryData(nodeId = '', force = false) {
    const cacheKey = nodeId || '__local__';
    const cached = telemetryHistoryCache.get(cacheKey);
    const now = Date.now();

    if (!force && cached && cached.data && now - cached.timestamp < TELEMETRY_HISTORY_CACHE_TTL_MS) {
        return cached.data;
    }

    if (!force && cached && cached.promise) {
        return cached.promise;
    }

    const requestPromise = (async () => {
        const params = new URLSearchParams({ limit: '5000' });
        if (nodeId) params.set('node_id', nodeId);

        const response = await fetch(`/api/telemetry/history?${params.toString()}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();
        telemetryHistoryCache.set(cacheKey, {
            timestamp: Date.now(),
            data
        });
        return data;
    })();

    telemetryHistoryCache.set(cacheKey, {
        timestamp: now,
        promise: requestPromise
    });

    try {
        return await requestPromise;
    } catch (error) {
        telemetryHistoryCache.delete(cacheKey);
        throw error;
    }
}

async function loadTelemetryHistory(nodeId = '', force = false) {
    try {
        const historyData = await fetchTelemetryHistoryData(nodeId, force);

        telemetryFullHistory = historyData.history || [];
        telemetryHistory = telemetryFullHistory;

        if (historyData.config) {
            telemetryInterval = historyData.config.interval || 300;
            const select = document.getElementById('telemetryInterval');
            if (select) select.value = telemetryInterval;
        }

        console.log('[TELEMETRY] History records:', telemetryHistory.length);
    } catch (error) {
        console.error('[TELEMETRY] Error loading telemetry history:', error);
        throw error;
    }
}

function updateTelemetryUI() {
    const data = telemetryData;
    
    const envValue = document.getElementById('telemetryEnvValue');
    const envUpdate = document.getElementById('telemetryEnvUpdate');
    if (envValue) {
        let parts = [];
        if (data.temperature !== null && data.temperature !== undefined) {
            parts.push(formatTemperature(data.temperature));
        }
        if (data.humidity !== null && data.humidity !== undefined) {
            parts.push(`${data.humidity.toFixed(1)}%`);
        }
        if (data.pressure !== null && data.pressure !== undefined) {
            parts.push(formatPressure(data.pressure));
        }
        envValue.textContent = parts.length > 0 ? parts.join('  ') : '—';
    }
    if (envUpdate) {
        envUpdate.textContent = data.last_update ? `⏱${data.last_update}` : '';
    }
    
    const powerValue = document.getElementById('telemetryPowerValue');
    const powerUpdate = document.getElementById('telemetryPowerUpdate');
    if (powerValue) {
        let parts = [];
        if (data.voltage !== null && data.voltage !== undefined) {
            parts.push(`${data.voltage.toFixed(3)}V`);
        }
        if (data.current !== null && data.current !== undefined && data.current > 0) {
            parts.push(`${data.current.toFixed(1)}mA`);
        }
        powerValue.textContent = parts.length > 0 ? parts.join('  ') : '—';
    }
    if (powerUpdate) {
        powerUpdate.textContent = data.last_update ? `⏱${data.last_update}` : '';
    }

    // /api/telemetry can contain the current reading even when /api/sensors
    // has not yet refreshed it. It also provides a power/voltage fallback.
    updateBatteryRuntime(
        Number(data.current),
        latestBatteryPercent,
        Number(data.power),
        Number(data.voltage)
    );
    
    const statusEl = document.getElementById('telemetryStatus');
    if (statusEl) {
        if (data.last_update) {
            statusEl.textContent = `🟢 ${data.last_update}`;
        } else {
            statusEl.textContent = `⚪ ${window.I18N.t('nodes.no_data')}`;
        }
    }
}

async function updateTelemetryConfig() {
    const select = document.getElementById('telemetryInterval');
    if (!select) return;
    const interval = parseInt(select.value, 10);
    
    try {
        const response = await fetch('/api/telemetry/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ interval: interval })
        });
        
        const data = await response.json();
        if (data.ok) {
            telemetryInterval = interval;
            showToast(`✅ ${window.I18N.t('nodes.interval_set_to', { minutes: interval / 60 })}`, 'success');
        } else {
            showToast(`❌ ${window.I18N.t('nodes.interval_update_failed')}`, 'error');
        }
    } catch (error) {
        console.error('Error updating telemetry config:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
    }
}

async function openTelemetryModal(type, nodeId = '', nodeName = '') {
    const modal = document.getElementById('telemetryModal');
    const title = document.getElementById('telemetryModalTitle');
    const container = document.getElementById('telemetryChartContainer');

    if (!modal || !title || !container) {
        console.error('Modal elements not found');
        return;
    }

    const requestId = ++telemetryModalRequestId;

    modal.dataset.type = type;
    modal.dataset.nodeId = nodeId || '';
    modal.dataset.nodeName = nodeName || '';
    modal.dataset.requestId = String(requestId);
    renderTelemetryCardsLayout(type);
    modal.style.display = 'flex';
    container.innerHTML = `<div class="loading">⏳ ${escapeHtml(window.I18N.t('nodes.loading_telemetry_data'))}</div>`;

    const labels = {
        'environment': `🌡️ ${window.I18N.t('nodes.environment_sensors')}`,
        'power': `⚡ ${window.I18N.t('nodes.power_sensors')}`
    };
    const telemetryTitle = labels[type] || `📊 ${window.I18N.t('modals.telemetry')}`;
    title.textContent = nodeName
        ? `${telemetryTitle} - ${nodeName}`
        : telemetryTitle;

    const footer = document.getElementById('telemetryFooter');
    if (footer) {
        footer.innerHTML = `
        <div class="telemetry-time-controls">
            <button class="time-btn active" data-range="60" onclick="setTelemetryRange(60)">1h</button>
            <button class="time-btn" data-range="360" onclick="setTelemetryRange(360)">6h</button>
            <button class="time-btn" data-range="720" onclick="setTelemetryRange(720)">12h</button>
            <button class="time-btn" data-range="1440" onclick="setTelemetryRange(1440)">24h</button>
            <button class="time-btn" data-range="10080" onclick="setTelemetryRange(10080)">7d</button>
            <button class="time-btn" data-range="43200" onclick="setTelemetryRange(43200)">30d</button>
        </div>

        <div class="telemetry-footer-actions">
            <button class="telemetry-export-btn" onclick="exportTelemetryData()">⬇ ${escapeHtml(window.I18N.t('nodes.export'))}</button>
            <span class="telemetry-records-count" id="telemetryRecordsCount">📊 ${escapeHtml(window.I18N.plural('nodes.records_count', 0, { count: 0 }))}</span>
        </div>
        `;
    }

    try {
        if (!nodeId) {
            await loadTelemetry();
        }
        await loadTelemetryHistory(nodeId);

        if (requestId !== telemetryModalRequestId
            || modal.dataset.type !== type
            || modal.dataset.nodeId !== (nodeId || '')) {
            return;
        }

        renderTelemetryWithRange(type, telemetryTimeRange);
        } catch (error) {
        console.error('Error loading telemetry modal:', error);
        container.innerHTML = `<div class="loading">⚠️ ${escapeHtml(window.I18N.t('nodes.error_loading_telemetry'))}</div>`;
    }
}

function setTelemetryRange(minutes) {
    telemetryTimeRange = minutes;
    
    document.querySelectorAll('.time-btn').forEach(btn => {
        btn.classList.toggle('active', parseInt(btn.dataset.range) === minutes);
    });
    
    const modal = document.getElementById('telemetryModal');
    const type = modal ? modal.dataset.type : 'environment';
    renderTelemetryWithRange(type, minutes);
}

function toggleTelemetrySeries(seriesName) {
    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';

    if (!telemetryVisibleSeries[type] || !(seriesName in telemetryVisibleSeries[type])) {
        return;
    }

    telemetryVisibleSeries[type][seriesName] = !telemetryVisibleSeries[type][seriesName];

    const activeCount = Object.values(telemetryVisibleSeries[type]).filter(Boolean).length;

    if (activeCount === 0) {
        telemetryVisibleSeries[type][seriesName] = true;
    }

    renderTelemetryWithRange(type, telemetryTimeRange);
}

function exportTelemetryData() {
    openCustomTelemetryExport();
}

function closeTelemetryExportMenu() {
    const menu = document.getElementById('telemetryExportMenu');
    if (menu) menu.remove();
}

function openCustomTelemetryExport() {
    closeTelemetryExportMenu();

    const oldModal = document.getElementById('customTelemetryExportModal');
    if (oldModal) oldModal.remove();

    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';

    const now = new Date();
    const rangeMinutes = telemetryTimeRange || 1440;
    const from = new Date(now.getTime() - rangeMinutes * 60 * 1000);

    const seriesText = getTelemetryVisibleSeriesText(type);
    const rangeLabel = getTelemetryRangeLabel(rangeMinutes);

    const overlay = document.createElement('div');
    overlay.id = 'customTelemetryExportModal';
    overlay.className = 'custom-export-overlay';

    overlay.innerHTML = `
        <div class="custom-export-dialog">
            <div class="custom-export-header">
                <div>
                    <div class="custom-export-title">📤 ${escapeHtml(window.I18N.t('nodes.export_telemetry_title'))}</div>
                    <div class="custom-export-subtitle">${escapeHtml(window.I18N.t('nodes.export_telemetry_subtitle'))}</div>
                </div>
                <button class="custom-export-close" onclick="closeCustomTelemetryExport()">×</button>
            </div>

            <div class="custom-export-body">

                <div class="export-section">
                    <div class="export-section-title">${escapeHtml(window.I18N.t('nodes.export_source'))}</div>

                    <label class="export-radio-row">
                        <input type="radio" name="exportRangeMode" value="visible" checked onchange="updateCustomExportMode()">
                        <span>${escapeHtml(window.I18N.t('nodes.current_visible_range', { range: rangeLabel }))}</span>
                    </label>

                    <label class="export-radio-row">
                        <input type="radio" name="exportRangeMode" value="custom" onchange="updateCustomExportMode()">
                        <span>${escapeHtml(window.I18N.t('nodes.custom_range'))}</span>
                    </label>
                </div>

                <div class="export-section custom-export-range" id="customExportRangeFields" style="display:none;">
                    <div class="export-date-grid">
                        <label>
                            <span>${escapeHtml(window.I18N.t('nodes.date_from'))}</span>
                            <input type="datetime-local" id="exportStartDate" value="${datetimeLocalValue(from)}">
                        </label>

                        <label>
                            <span>${escapeHtml(window.I18N.t('nodes.date_to'))}</span>
                            <input type="datetime-local" id="exportEndDate" value="${datetimeLocalValue(now)}">
                        </label>
                    </div>
                </div>

                <div class="export-section">
                    <div class="export-section-title">${escapeHtml(window.I18N.t('nodes.series'))}</div>
                    <div class="export-series-summary">${escapeHtml(seriesText)}</div>
                </div>

                <div class="export-section">
                    <div class="export-section-title">${escapeHtml(window.I18N.t('nodes.format'))}</div>

                    <div class="export-format-row">
                        <label class="export-format-option">
                            <input type="radio" name="exportFormat" value="csv" checked>
                            <span>📄 CSV</span>
                        </label>

                        <label class="export-format-option">
                            <input type="radio" name="exportFormat" value="json">
                            <span>📄 JSON</span>
                        </label>
                    </div>
                </div>

            </div>

            <div class="custom-export-footer">
                <button class="custom-export-cancel" onclick="closeCustomTelemetryExport()">${escapeHtml(window.I18N.t('common.cancel'))}</button>
                <button class="custom-export-primary" onclick="runCustomTelemetryExport()">⬇ ${escapeHtml(window.I18N.t('nodes.export'))}</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
}

function closeCustomTelemetryExport() {
    const modal = document.getElementById('customTelemetryExportModal');
    if (modal) modal.remove();
}

function updateCustomExportMode() {
    const mode = document.querySelector('input[name="exportRangeMode"]:checked')?.value || 'visible';
    const fields = document.getElementById('customExportRangeFields');

    if (fields) {
        fields.style.display = mode === 'custom' ? 'block' : 'none';
    }
}

function datetimeLocalValue(date) {
    const pad = n => String(n).padStart(2, '0');

    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function getTelemetryRangeLabel(minutes) {
    if (minutes < 1440) return `${minutes / 60}h`;
    return `${minutes / 1440}d`;
}

function getTelemetryVisibleSeriesText(type) {
    const visible = telemetryVisibleSeries[type] || {};
    const labels = {
        temperature: window.I18N.t('node_panel.temperature'),
        humidity: window.I18N.t('node_panel.humidity'),
        pressure: window.I18N.t('node_panel.pressure'),
        voltage: window.I18N.t('node_panel.voltage'),
        current: window.I18N.t('node_panel.current'),
        power: window.I18N.t('node_panel.power')
    };

    const active = Object.keys(visible)
        .filter(key => visible[key])
        .map(key => labels[key] || key);

    return active.length > 0 ? active.join(' • ') : window.I18N.t('nodes.no_series_selected');
}

function runCustomTelemetryExport() {
    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';

    const format = document.querySelector('input[name="exportFormat"]:checked')?.value || 'csv';
    const mode = document.querySelector('input[name="exportRangeMode"]:checked')?.value || 'visible';

    const series = Object.keys(telemetryVisibleSeries[type] || {})
        .filter(key => telemetryVisibleSeries[type][key])
        .join(',');

    const nodeId = modal?.dataset?.nodeId || '';
    let url = `/api/export/telemetry?type=${encodeURIComponent(type)}&format=${encodeURIComponent(format)}&series=${encodeURIComponent(series)}`;
    if (nodeId) {
        url += `&node_id=${encodeURIComponent(nodeId)}`;
    }

    if (mode === 'custom') {
        const startValue = document.getElementById('exportStartDate')?.value;
        const endValue = document.getElementById('exportEndDate')?.value;

        if (!startValue || !endValue) {
            alert(window.I18N.t('nodes.select_start_end_date'));
            return;
        }

        const startTs = Math.floor(new Date(startValue).getTime() / 1000);
        const endTs = Math.floor(new Date(endValue).getTime() / 1000);

        if (!startTs || !endTs || startTs >= endTs) {
            alert(window.I18N.t('nodes.invalid_date_range'));
            return;
        }

        url += `&start=${encodeURIComponent(startTs)}&end=${encodeURIComponent(endTs)}`;
    } else {
        url += `&range=${encodeURIComponent(telemetryTimeRange || 1440)}`;
    }

    closeCustomTelemetryExport();
    window.location.href = url;
}

function downloadTelemetryExport(format) {
    closeTelemetryExportMenu();

    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';
    const range = telemetryTimeRange || 1440;

    const url = `/api/export/telemetry?type=${encodeURIComponent(type)}&range=${encodeURIComponent(range)}&format=${encodeURIComponent(format)}`;
    window.location.href = url;
}

function telemetryRecordHasType(record, type) {
    if (!record || typeof record !== 'object') return false;

    if (type === 'environment') {
        return [record.temperature, record.humidity, record.pressure]
            .some(value => value !== null && value !== undefined && Number.isFinite(Number(value)));
    }

    // Voltage is the baseline power/device metric and is available on most nodes.
    return [record.voltage, record.current, record.power]
        .some(value => value !== null && value !== undefined && Number.isFinite(Number(value)));
}

// Called from Workspace.applyTheme() when the resolved theme actually
// changes. Re-reads the currently open telemetry modal's type/range
// instead of caching state, so it stays correct even if the user changed
// range/series selection since the chart was first drawn.
function _refreshTelemetryChartOnThemeChange() {
    if (!telemetryChart) return;
    const modal = document.getElementById('telemetryModal');
    const type = modal?.dataset.type;
    if (type) renderTelemetryWithRange(type, telemetryTimeRange);
}

function renderTelemetryWithRange(type, minutes) {
    const container = document.getElementById('telemetryChartContainer');
    const recordsCount = document.getElementById('telemetryRecordsCount');
    
    if (!container) return;
    
    const now = Date.now() / 1000;
    const cutoff = now - (minutes * 60);
    
    const filteredRecords = telemetryFullHistory.filter(record => {
        const timestamp = Number(record?.timestamp);
        return Number.isFinite(timestamp)
            && timestamp >= cutoff
            && telemetryRecordHasType(record, type);
    });
    
const rangeLabel = minutes < 1440
    ? `${minutes / 60}h`
    : `${minutes / 1440}d`;

if (filteredRecords.length === 0) {
    container.innerHTML = `<div class="loading">📊 ${escapeHtml(window.I18N.t('nodes.no_data_for_period', { range: rangeLabel }))}</div>`;
    if (recordsCount) recordsCount.textContent = `📊 ${window.I18N.plural('nodes.records_count', 0, { count: 0 })}`;
    return;
}

if (recordsCount) {
    recordsCount.textContent = `📊 ${window.I18N.plural('nodes.records_count', filteredRecords.length, { count: filteredRecords.length })} (${rangeLabel})`;
}

    renderTelemetryChart(container, filteredRecords, type);
    updateTelemetryCards(filteredRecords, type);
}

function renderTelemetryChart(container, records, type) {
    container.innerHTML = '<canvas id="telemetryChartCanvas"></canvas>';

    const canvas = document.getElementById('telemetryChartCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');

    // Dark theme: only axis/grid/legend/tooltip chrome changes here - the
    // per-series line/fill colors (SENSOR_COLORS/SENSOR_BG_COLORS) already
    // work on a dark background and are left untouched.
    const isDark = document.documentElement.dataset.theme === 'dark';
    const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)';
    const tickColor = isDark ? '#a0aec0' : '#666';

    const labels = records.map(r => {
        const t = new Date(r.timestamp * 1000);
        return TimeFormatter.formatTime(t);
    });

    let datasets = [];
    let hasPressure = false;
    let hasCurrent = false;
    let hasPower = false;

    if (type === 'environment') {
        const tempData = records.map(r => r.temperature).filter(v => v !== null && v !== undefined);
        if (tempData.length > 0 && telemetryVisibleSeries.environment.temperature) {
            datasets.push({
                label: window.I18N.t('node_panel.temperature') + ' ' + temperatureChartUnit(),
                metricType: 'temperature',
                data: records.map(r => telemetryValuePresent(r.temperature) ? temperatureChartValue(r.temperature) : null),
                borderColor: SENSOR_COLORS.temperature,
                backgroundColor: SENSOR_BG_COLORS.temperature,
                fill: true,
                tension: 0.3,
                spanGaps: false,
                yAxisID: 'y'
            });
        }

        const humData = records.map(r => r.humidity).filter(v => v !== null && v !== undefined);
        if (humData.length > 0 && telemetryVisibleSeries.environment.humidity) {
            datasets.push({
                label: window.I18N.t('node_panel.humidity') + ' %',
                metricType: 'humidity',
                data: records.map(r => telemetryValuePresent(r.humidity) ? Number(r.humidity) : null),
                borderColor: SENSOR_COLORS.humidity,
                backgroundColor: SENSOR_BG_COLORS.humidity,
                fill: true,
                tension: 0.3,
                spanGaps: false,
                yAxisID: 'y'
            });
        }

        const pressData = records.map(r => r.pressure).filter(v => v !== null && v !== undefined && !isNaN(v));
        if (pressData.length > 0 && telemetryVisibleSeries.environment.pressure) {
            hasPressure = true;
            datasets.push({
                label: window.I18N.t('node_panel.pressure') + ' ' + pressureChartUnit(),
                metricType: 'pressure',
                data: records.map(r => telemetryValuePresent(r.pressure) ? pressureChartValue(r.pressure) : null),
                borderColor: SENSOR_COLORS.pressure,
                backgroundColor: SENSOR_BG_COLORS.pressure,
                fill: true,
                tension: 0.3,
                spanGaps: false,
                yAxisID: 'y1'
            });
        }

    } else if (type === 'power') {
        const voltData = records.map(r => r.voltage).filter(v => v !== null && v !== undefined);
        if (voltData.length > 0 && telemetryVisibleSeries.power.voltage) {
            datasets.push({
                label: window.I18N.t('node_panel.voltage') + ' V',
                metricType: 'voltage',
                data: records.map(r => telemetryValuePresent(r.voltage) ? Number(r.voltage) : null),
                borderColor: SENSOR_COLORS.voltage,
                backgroundColor: SENSOR_BG_COLORS.voltage,
                fill: true,
                tension: 0.3,
                spanGaps: false,
                yAxisID: 'y'
            });
        }

        const currData = records.map(r => r.current).filter(v => v !== null && v !== undefined);
        if (currData.length > 0 && telemetryVisibleSeries.power.current) {
            hasCurrent = true;
            datasets.push({
                label: window.I18N.t('node_panel.current') + ' mA',
                metricType: 'current',
                data: records.map(r => telemetryValuePresent(r.current) ? Number(r.current) : null),
                borderColor: SENSOR_COLORS.current,
                backgroundColor: SENSOR_BG_COLORS.current,
                fill: true,
                tension: 0.3,
                spanGaps: false,
                yAxisID: 'y1'
            });
        }

        const powerSeries = records.map(r => {
            if (telemetryValuePresent(r.power)) return Number(r.power) / 1000;
            if (telemetryValuePresent(r.voltage) && telemetryValuePresent(r.current)) {
                return (Number(r.voltage) * Number(r.current)) / 1000;
            }
            return null;
        });

        const powerData = powerSeries.filter(v => v !== null && v !== undefined);
        if (powerData.length > 0 && telemetryVisibleSeries.power.power) {
            hasPower = true;
            datasets.push({
                label: window.I18N.t('node_panel.power') + ' W',
                metricType: 'power',
                data: powerSeries,
                borderColor: SENSOR_COLORS.power,
                backgroundColor: SENSOR_BG_COLORS.power,
                fill: true,
                tension: 0.3,
                spanGaps: false,
                yAxisID: 'y2'
            });
        }
    }

    if (telemetryChart) {
        telemetryChart.destroy();
        telemetryChart = null;
    }

    if (datasets.length === 0) {
        container.innerHTML = `<div class="loading">📊 ${escapeHtml(window.I18N.t('nodes.no_data_for_sensor_type'))}</div>`;
        return;
    }

    let yConfig = {
        position: 'left',
        grid: { color: gridColor, drawBorder: true },
        ticks: { font: { size: 9 }, color: tickColor }
    };

    let y1Config = {
        position: 'right',
        grid: { drawOnChartArea: false, drawBorder: true },
        ticks: { font: { size: 9 }, color: tickColor }
    };

    let y2Config = {
        position: 'right',
        offset: true,
        grid: { drawOnChartArea: false, drawBorder: true },
        ticks: {
            font: { size: 9 },
            color: tickColor,
            callback: value => value.toFixed(1)
        }
    };

    if (type === 'environment') {
        const tempUnit = appSettings?.units?.temperature || "c";
        yConfig.min = tempUnit === "f" ? 20 : -5;
        yConfig.max = tempUnit === "f" ? 120 : 100;

        if (hasPressure) {
            const pressureUnit = appSettings?.units?.pressure || "hpa";
            y1Config.min = pressureUnit === "mmhg" ? 675 : 900;
            y1Config.max = pressureUnit === "mmhg" ? 900 : 1200;
        } else {
            y1Config.min = -10;
            y1Config.max = 100;
        }
    }

    if (type === 'power') {
        yConfig.min = 3.40;
        yConfig.max = 4.30;

        y1Config.min = 250;
        y1Config.max = 1000;

        if (hasPower) {
            const powerValues = datasets
                .filter(d => d.metricType === 'power')
                .flatMap(d => d.data)
                .filter(v => v !== null && v !== undefined && !isNaN(v));

            const maxPower = powerValues.length > 0 ? Math.max(...powerValues) : 2;
            y2Config.min = 0;
            y2Config.max = Math.max(2, Math.ceil(maxPower * 1.2));
        }
    }

    try {
        telemetryChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        position: 'top',
                        labels: { usePointStyle: true, padding: 15, font: { size: 11 }, color: isDark ? '#cbd5e0' : '#333' }
                    },
                    tooltip: {
                        backgroundColor: isDark ? '#0f1923' : 'rgba(255,255,255,0.95)',
                        titleColor: isDark ? '#e2e8f0' : '#333',
                        bodyColor: isDark ? '#cbd5e0' : '#666',
                        borderColor: isDark ? '#263c52' : 'rgba(0,0,0,0.1)',
                        borderWidth: 1,
                        callbacks: {
                            title: function(context) {
                                if (!context || !context.length) return '';
                                const record = records[context[0].dataIndex];
                                if (!record || !record.timestamp) return '';
                                return TimeFormatter.formatDateTime(record.timestamp);
                            },
                            label: function(context) {
                                const label = context.dataset.label || '';
                                const metricType = context.dataset.metricType || '';
                                const value = context.parsed.y;

                                if (value === null || value === undefined || isNaN(value)) return label + ': —';
                                if (metricType === 'temperature') return label + ': ' + value.toFixed(1) + temperatureChartUnit();
                                if (metricType === 'humidity') return label + ': ' + value.toFixed(1) + '%';
                                if (metricType === 'pressure') return label + ': ' + value.toFixed(1) + ' ' + pressureChartUnit();
                                if (metricType === 'voltage') return label + ': ' + value.toFixed(3) + ' V';
                                if (metricType === 'current') return label + ': ' + value.toFixed(1) + ' mA';
                                if (metricType === 'power') return label + ': ' + value.toFixed(3) + ' W';

                                return label + ': ' + value.toFixed(2);
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: gridColor, drawBorder: true },
                        ticks: { maxTicksLimit: 20, font: { size: 9 }, color: tickColor }
                    },
                    y: yConfig,
                    y1: y1Config,
                    ...(hasPower ? { y2: y2Config } : {})
                }
            }
        });

    } catch (error) {
        console.error('Chart creation error:', error);
        container.innerHTML = '<div class="loading">⚠️ ' + escapeHtml(window.I18N.t('nodes.error_creating_chart', { message: error.message })) + '</div>';
    }
}

function updateTelemetryCards(records, type) {
    const modal = document.getElementById('telemetryModal');
    if (!modal || modal.dataset.type !== type) return;

    const remoteNodeHistory = Boolean(modal.dataset.nodeId);

    if (type === 'environment') {
        const lastTemperature = remoteNodeHistory
            ? latestTelemetryValue(records, 'temperature')
            : (telemetryData.temperature ?? latestTelemetryValue(records, 'temperature'));
        const lastHumidity = remoteNodeHistory
            ? latestTelemetryValue(records, 'humidity')
            : (telemetryData.humidity ?? latestTelemetryValue(records, 'humidity'));
        const lastPressure = remoteNodeHistory
            ? latestTelemetryValue(records, 'pressure')
            : (telemetryData.pressure ?? latestTelemetryValue(records, 'pressure'));

        const tempValues = records.map(r => Number(r.temperature)).filter(Number.isFinite);
        const humValues = records.map(r => Number(r.humidity)).filter(Number.isFinite);
        const pressValues = records.map(r => Number(r.pressure)).filter(Number.isFinite);

        const tempCard = document.getElementById('environmentTemperatureCard');
        const humCard = document.getElementById('environmentHumidityCard');
        const pressureCard = document.getElementById('environmentPressureCard');
        if (!tempCard || !humCard || !pressureCard) return;

        tempCard.onclick = () => toggleTelemetrySeries('temperature');
        tempCard.classList.toggle('inactive', !telemetryVisibleSeries.environment.temperature);
        document.getElementById('environmentTemperatureValue').textContent = formatTemperature(lastTemperature);
        document.getElementById('environmentTemperatureValue').style.color = SENSOR_COLORS.temperature;
        document.getElementById('environmentTemperatureMin').textContent = tempValues.length ? formatTemperature(Math.min(...tempValues)) : '--';
        document.getElementById('environmentTemperatureMax').textContent = tempValues.length ? formatTemperature(Math.max(...tempValues)) : '--';

        humCard.onclick = () => toggleTelemetrySeries('humidity');
        humCard.classList.toggle('inactive', !telemetryVisibleSeries.environment.humidity);
        document.getElementById('environmentHumidityValue').textContent = lastHumidity !== null ? lastHumidity.toFixed(1) + '%' : '--';
        document.getElementById('environmentHumidityValue').style.color = SENSOR_COLORS.humidity;
        document.getElementById('environmentHumidityMin').textContent = humValues.length ? Math.min(...humValues).toFixed(1) + '%' : '--';
        document.getElementById('environmentHumidityMax').textContent = humValues.length ? Math.max(...humValues).toFixed(1) + '%' : '--';

        pressureCard.onclick = () => toggleTelemetrySeries('pressure');
        pressureCard.classList.toggle('inactive', !telemetryVisibleSeries.environment.pressure);
        document.getElementById('environmentPressureValue').textContent = formatPressure(lastPressure);
        document.getElementById('environmentPressureValue').style.color = SENSOR_COLORS.pressure;
        document.getElementById('environmentPressureMin').textContent = pressValues.length ? formatPressure(Math.min(...pressValues)) : '--';
        document.getElementById('environmentPressureMax').textContent = pressValues.length ? formatPressure(Math.max(...pressValues)) : '--';
        return;
    }

    if (type === 'power') {
        const lastVoltage = remoteNodeHistory
            ? latestTelemetryValue(records, 'voltage')
            : (telemetryData.voltage ?? latestTelemetryValue(records, 'voltage'));
        const lastCurrent = remoteNodeHistory
            ? latestTelemetryValue(records, 'current')
            : (telemetryData.current ?? latestTelemetryValue(records, 'current'));
        let lastPower = remoteNodeHistory
            ? latestTelemetryValue(records, 'power')
            : (telemetryData.power ?? latestTelemetryValue(records, 'power'));

        if (lastPower === null && lastVoltage !== null && lastCurrent !== null) {
            lastPower = lastVoltage * lastCurrent;
        }

        const voltValues = records.map(r => Number(r.voltage)).filter(Number.isFinite);
        const currValues = records.map(r => Number(r.current)).filter(Number.isFinite);
        const powerValues = records.map(r => {
            const power = Number(r.power);
            if (Number.isFinite(power)) return power;
            const voltage = Number(r.voltage);
            const current = Number(r.current);
            return Number.isFinite(voltage) && Number.isFinite(current) ? voltage * current : null;
        }).filter(Number.isFinite);

        const voltageCard = document.getElementById('powerVoltageCard');
        const currentCard = document.getElementById('powerCurrentCard');
        const powerCard = document.getElementById('powerPowerCard');
        if (!voltageCard || !currentCard || !powerCard) return;

        voltageCard.onclick = () => toggleTelemetrySeries('voltage');
        voltageCard.classList.toggle('inactive', !telemetryVisibleSeries.power.voltage);
        document.getElementById('powerVoltageValue').textContent = lastVoltage !== null ? lastVoltage.toFixed(3) + ' V' : '--';
        document.getElementById('powerVoltageValue').style.color = SENSOR_COLORS.voltage;
        document.getElementById('powerVoltageMin').textContent = voltValues.length ? Math.min(...voltValues).toFixed(3) + ' V' : '--';
        document.getElementById('powerVoltageMax').textContent = voltValues.length ? Math.max(...voltValues).toFixed(3) + ' V' : '--';

        currentCard.onclick = () => toggleTelemetrySeries('current');
        currentCard.classList.toggle('inactive', !telemetryVisibleSeries.power.current);
        document.getElementById('powerCurrentValue').textContent = lastCurrent !== null ? lastCurrent.toFixed(1) + ' mA' : '--';
        document.getElementById('powerCurrentValue').style.color = SENSOR_COLORS.current;
        document.getElementById('powerCurrentMin').textContent = currValues.length ? Math.min(...currValues).toFixed(1) + ' mA' : '--';
        document.getElementById('powerCurrentMax').textContent = currValues.length ? Math.max(...currValues).toFixed(1) + ' mA' : '--';

        powerCard.onclick = () => toggleTelemetrySeries('power');
        powerCard.classList.toggle('inactive', !telemetryVisibleSeries.power.power);
        document.getElementById('powerPowerValue').textContent = lastPower !== null ? (lastPower / 1000).toFixed(3) + ' W' : '--';
        document.getElementById('powerPowerValue').style.color = SENSOR_COLORS.power;
        document.getElementById('powerPowerMin').textContent = powerValues.length ? (Math.min(...powerValues) / 1000).toFixed(3) + ' W' : '--';
        document.getElementById('powerPowerMax').textContent = powerValues.length ? (Math.max(...powerValues) / 1000).toFixed(3) + ' W' : '--';
    }
}

function closeTelemetryModal() {
    telemetryModalRequestId += 1;
    const modal = document.getElementById('telemetryModal');
    if (modal) {
        modal.style.display = 'none';
        modal.dataset.type = '';
    }
    if (telemetryChart) {
        telemetryChart.destroy();
        telemetryChart = null;
    }
    telemetryTimeRange = 60;
}

// ============================================================
// CAMERA CONTROL
// ============================================================

function isCameraTabVisible() {
    return currentMainTab === 'video';
}

function setCameraFeedLoading(loading, message = window.I18N.t('camera.connecting')) {
    const img = document.getElementById('videoFeed');
    const placeholder = document.getElementById('cameraLoadingPlaceholder');
    const title = placeholder?.querySelector('.camera-loading-title');

    if (title && message) {
        title.textContent = message;
    }

    if (placeholder) {
        placeholder.classList.toggle('camera-loading-visible', Boolean(loading));
        placeholder.setAttribute('aria-hidden', loading ? 'false' : 'true');
    }

    if (img) {
        img.classList.toggle('camera-stream-hidden', Boolean(loading));
    }
}

function showCameraFeed() {
    const img = document.getElementById('videoFeed');
    if (img) {
        img.classList.remove('camera-stream-hidden');
    }
    setCameraFeedLoading(false);
}

function hideCameraFeed(message = window.I18N.t('camera.connecting')) {
    setCameraFeedLoading(true, message);
}

function setCameraControlsDisabled(disabled) {
    const controls = document.getElementById('videoControls');

    if (!controls) {
        return;
    }

    controls.classList.toggle(
        'camera-controls-disabled',
        disabled
    );

    controls.querySelectorAll(
        'input, select, button'
    ).forEach(element => {
        if (element.id === 'cameraGalleryBtn') {
            return;
        }

        element.disabled = disabled;
    });
}

function renderCameraPowerState() {
    const button =
        document.getElementById('cameraPowerBtn');

    const buttonText =
        document.getElementById('cameraPowerBtnText');

    const placeholder =
        document.getElementById('cameraOffPlaceholder');

    const feed =
        document.getElementById('videoFeed');

    const status =
        document.getElementById('videoStatus');

    const liveInfo =
        document.getElementById('videoLiveInfo');

    const transitioning =
        cameraPowerStatus === 'starting'
        || cameraPowerStatus === 'stopping';

    if (button) {
        button.disabled =
            cameraPowerRequestInProgress || transitioning;

        button.classList.toggle(
            'camera-power-off',
            !cameraPowerEnabled
        );
    }

    if (cameraPowerStatus === 'starting') {
        if (buttonText) {
            buttonText.textContent = window.I18N.t('camera.starting');
        }

        if (status) {
            status.textContent = `🟡 ${window.I18N.t('camera.starting_camera')}`;
            status.style.color = '#d97706';
        }

        return;
    }

    if (cameraPowerStatus === 'stopping') {
        if (buttonText) {
            buttonText.textContent = window.I18N.t('camera.stopping');
        }

        if (status) {
            status.textContent = `🟠 ${window.I18N.t('camera.stopping_camera')}`;
            status.style.color = '#ea580c';
        }

        return;
    }

    if (cameraPowerStatus === 'error') {
        if (buttonText) {
            buttonText.textContent =
                cameraPowerEnabled ? window.I18N.t('camera.turn_off') : window.I18N.t('camera.try_again');
        }

        if (status) {
            status.textContent = `🔴 ${window.I18N.t('camera.camera_error')}`;
            status.style.color = '#c62828';
        }

        return;
    }

    if (!cameraPowerEnabled) {
        cameraActive = false;

        if (feed) {
            feed.removeAttribute('src');
            feed.classList.add('camera-stream-hidden');
        }
        setCameraFeedLoading(false);

        if (placeholder) {
            placeholder.style.display = 'flex';
        }

        if (buttonText) {
            buttonText.textContent = window.I18N.t('camera.turn_on');
        }

        if (status) {
            status.textContent = `⚫ ${window.I18N.t('notifications.camera_off')}`;
            status.style.color = '#64748b';
        }

        if (liveInfo) {
            liveInfo.textContent = window.I18N.t('camera.power_saving_mode');
        }

        setCameraControlsDisabled(true);
        updateStatusDock('video');
        return;
    }

    if (placeholder) {
        placeholder.style.display = 'none';
    }

    if (feed && cameraActive && feed.naturalWidth > 0) {
        showCameraFeed();
    }

    if (buttonText) {
        buttonText.textContent = window.I18N.t('camera.turn_off');
    }

    setCameraControlsDisabled(false);

    if (status) {
        status.textContent =
            cameraActive && isCameraTabVisible()
                ? `🟢 ${window.I18N.t('nodes.status_online')}`
                : `⏸️ ${window.I18N.t('camera.paused')}`;

        status.style.color =
            cameraActive && isCameraTabVisible()
                ? '#4caf50'
                : '#888';
    }

    updateStatusDock('video');
}

async function loadCameraPowerState() {
    try {
        const response =
            await fetch('/api/camera/power');

        const data = await response.json();

        if (!response.ok || !data.ok) {
            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            requestError.params = data.error_params || undefined;
            throw requestError;
        }

        cameraPowerEnabled = Boolean(data.enabled);
        cameraPowerStatus = data.status || (
            cameraPowerEnabled ? 'ready' : 'off'
        );

        renderCameraPowerState();
        return data;

    } catch (error) {
        cameraPowerStatus = 'error';
        renderCameraPowerState();

        console.error(
            '[CAMERA POWER] State load failed:',
            error
        );

        return null;
    }
}

async function setCameraPower(enabled) {
    if (cameraPowerRequestInProgress) {
        return;
    }

    cameraPowerRequestInProgress = true;
    cameraPowerStatus = enabled
        ? 'starting'
        : 'stopping';

    if (!enabled) {
        stopVideoFeed();
        cameraActive = false;
    }

    renderCameraPowerState();

    try {
        const response =
            await fetch('/api/camera/power', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    enabled: Boolean(enabled)
                })
            });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            requestError.params = data.error_params || undefined;
            throw requestError;
        }

        cameraPowerEnabled = Boolean(data.enabled);
        cameraPowerStatus = data.status || (
            cameraPowerEnabled ? 'ready' : 'off'
        );

        if (
            cameraPowerEnabled
            && isCameraTabVisible()
        ) {
            await switchCameraMode('video');
            await loadVideoSettings();
            await loadPhotoSettings();
            await reconnectCameraFeed();
            cameraActive = true;
        }

        renderCameraPowerState();

        showToast(
            cameraPowerEnabled
                ? `✅ ${window.I18N.t('camera.camera_turned_on')}`
                : `✅ ${window.I18N.t('camera.camera_turned_off')}`,
            'success'
        );

    } catch (error) {
        cameraPowerStatus = 'error';

        showToast(
            `❌ ${window.I18N.t('camera.camera_power_error', { message: translateRequestError(error) })}`,
            'error'
        );

        console.error(
            '[CAMERA POWER] Change failed:',
            error
        );

        await loadCameraPowerState();

    } finally {
        cameraPowerRequestInProgress = false;
        renderCameraPowerState();
    }
}

function toggleCameraPower() {
    setCameraPower(!cameraPowerEnabled);
}

async function startCameraStream() {
    if (!cameraPowerEnabled || cameraActive) return;
    cameraActive = true;

    console.log('[CAMERA] Starting stream...');
    hideCameraFeed(window.I18N.t('camera.connecting'));

    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = `🔄 ${window.I18N.t('camera.starting')}`;
        status.style.color = '#ff9800';
    }

    await reconnectCameraFeed();
}

function stopCameraStream() {
    if (!cameraActive) return;
    cameraActive = false;

    console.log('[CAMERA] Stopping stream...');
    const img = document.getElementById('videoFeed');
    if (img) {
        img.removeAttribute('src');
        img.classList.add('camera-stream-hidden');
    }
    setCameraFeedLoading(false);

    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = `⏸️ ${window.I18N.t('camera.paused')}`;
        status.style.color = '#888';
    }
}

// ============================================================
// VIDEO FUNCTIONS
// ============================================================
let currentVideoSettings = {};
let currentCameraControls = {};

let cameraControlRequestInProgress = false;
let cameraControlPending = false;
let cameraControlShowMessage = false;
let cameraFeedRefreshTimer = null;
let cameraFeedRefreshSequence = 0;
let videoSettingsRequestInProgress = false;
let videoSettingsPending = false;

async function loadVideoSettings() {
    try {
        const response = await fetch('/api/camera/settings');
        const data = await response.json();
        
        if (data.ok) {
            currentVideoSettings = data.config;
            currentCameraControls = data.controls || {};
            
            const resSelect = document.getElementById('videoResolution');
            const fpsSelect = document.getElementById('videoFps');
            const qualitySlider = document.getElementById('videoQuality');
            const qualityLabel = document.getElementById('videoQualityLabel');
            
            if (resSelect) resSelect.value = data.config.resolution || '640x480';
            if (fpsSelect) fpsSelect.value = data.config.fps || 12;
            const cameraControlValues = {
                cameraBrightness: currentCameraControls.brightness ?? 0.0,
                cameraContrast: currentCameraControls.contrast ?? 1.0,
                cameraSaturation: currentCameraControls.saturation ?? 1.0,
                cameraSharpness: currentCameraControls.sharpness ?? 1.0,
                cameraExposure: currentCameraControls.exposure_compensation ?? 0.0
            };

            Object.entries(cameraControlValues).forEach(([id, value]) => {
                const element = document.getElementById(id);
                if (element) {
                    element.value = value;
                }
            });

            updateCameraControlLabels();

            const presetSelect = document.getElementById('cameraImagePreset');
            if (presetSelect) {
                presetSelect.value = 'custom';
            }

            const whiteBalanceSelect = document.getElementById('cameraWhiteBalance');
            if (whiteBalanceSelect) {
                const savedAwbMode = String(currentCameraControls.awb_mode || 'auto').toLowerCase();
                const availableMode = Array.from(whiteBalanceSelect.options)
                    .some(option => option.value === savedAwbMode);
                whiteBalanceSelect.value = availableMode ? savedAwbMode : 'auto';
            }

            const liveInfo = document.getElementById('videoLiveInfo');
            if (liveInfo) {
                liveInfo.textContent = window.I18N.t('camera.live_info', { resolution: data.config.resolution || '640×480', fps: data.config.fps || 12 });
            }

            const statusEl = document.getElementById('videoStatus');
            if (statusEl) {
                statusEl.textContent = cameraActive ? `🟢 ${window.I18N.t('nodes.status_online')}` : `⏸️ ${window.I18N.t('camera.paused')}`;
                statusEl.style.color = cameraActive ? '#4caf50' : '#888';
            }
            
            const controls = document.getElementById('videoControls');
            if (controls) {
                controls.style.display = 'block';
                controls.style.visibility = 'visible';
                controls.style.opacity = '1';
            }
        }
    } catch (error) {
        console.error('Error loading video settings:', error);
    }
}

function updateVideoQualityLabel(value) {
    const qualityLabel = document.getElementById('videoQualityLabel');
    if (qualityLabel) {
        qualityLabel.textContent = `${value}%`;
    }
}

function readVideoSettingsFromUi() {
    const resolution = document.getElementById('videoResolution')?.value || '640x480';
    const fps = parseInt(document.getElementById('videoFps')?.value || '12', 10);
    const quality = parseInt(document.getElementById('videoQuality')?.value || '75', 10);

    return { resolution, fps, quality };
}

function sameVideoSettings(left, right) {
    if (!left || !right) {
        return false;
    }

    return String(left.resolution) === String(right.resolution)
        && Number(left.fps) === Number(right.fps)
        && Number(left.quality) === Number(right.quality);
}

async function updateVideoSettings() {
    updateVideoQualityLabel(
        document.getElementById('videoQuality')?.value || 75
    );

    /*
     * Camera reconfiguration is expensive on Raspberry Pi Zero 2 W.
     * Serialize requests and keep only the latest pending UI state so a
     * quick resolution/FPS change cannot start overlapping pipelines.
     */
    if (videoSettingsRequestInProgress) {
        videoSettingsPending = true;
        return;
    }

    const requestedSettings = readVideoSettingsFromUi();

    if (sameVideoSettings(requestedSettings, currentVideoSettings)) {
        return;
    }

    videoSettingsRequestInProgress = true;
    videoSettingsPending = false;

    const liveInfo = document.getElementById('videoLiveInfo');
    const status = document.getElementById('videoStatus');

    if (status) {
        status.textContent = `🔄 ${window.I18N.t('camera.applying_settings')}`;
        status.style.color = '#ff9800';
    }

    try {
        const response = await fetch('/api/camera/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestedSettings)
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        currentVideoSettings = data.config || requestedSettings;

        if (liveInfo) {
            const appliedResolution = currentVideoSettings.resolution || requestedSettings.resolution;
            const appliedFps = currentVideoSettings.fps || requestedSettings.fps;
            liveInfo.textContent = window.I18N.t('camera.live_info', { resolution: appliedResolution.replace('x', '×'), fps: appliedFps });
        }

        if (cameraActive) {
            await reconnectCameraFeed();
        } else if (status) {
            status.textContent = `⏸️ ${window.I18N.t('camera.paused')}`;
            status.style.color = '#888';
        }

        showToast(`✅ ${window.I18N.t('camera.video_settings_updated')}`, 'success');

    } catch (error) {
        console.error('Error updating video settings:', error);
        showToast(`❌ ${window.I18N.t('camera.settings_failed', { message: error.message })}`, 'error');
        await loadVideoSettings();

    } finally {
        videoSettingsRequestInProgress = false;

        if (videoSettingsPending) {
            videoSettingsPending = false;
            queueMicrotask(updateVideoSettings);
        }
    }
}


const CAMERA_IMAGE_PRESETS = Object.freeze({
    neutral: {
        brightness: 0.0,
        contrast: 1.0,
        saturation: 1.0,
        sharpness: 1.0,
        exposure_compensation: 0.0
    },
    indoor: {
        brightness: 0.2,
        contrast: 1.1,
        saturation: 1.0,
        sharpness: 1.1,
        exposure_compensation: 0.5
    },
    night: {
        brightness: 0.5,
        contrast: 1.15,
        saturation: 0.0,
        sharpness: 1.0,
        exposure_compensation: 1.0
    },
    outdoor: {
        brightness: -0.1,
        contrast: 1.1,
        saturation: 1.15,
        sharpness: 1.2,
        exposure_compensation: -0.5
    },
    monochrome: {
        brightness: 0.1,
        contrast: 1.2,
        saturation: 0.0,
        sharpness: 1.2,
        exposure_compensation: 0.0
    },
    highContrast: {
        brightness: 0.0,
        contrast: 1.5,
        saturation: 0.9,
        sharpness: 1.4,
        exposure_compensation: 0.0
    }
});

function switchCameraControlTab(tabName) {
    const showImage = tabName === 'image';

    const cameraTab = document.getElementById('cameraControlTabCamera');
    const imageTab = document.getElementById('cameraControlTabImage');
    const cameraPanel = document.getElementById('cameraControlsPanel');
    const imagePanel = document.getElementById('imageControlsPanel');

    cameraTab?.classList.toggle('active', !showImage);
    imageTab?.classList.toggle('active', showImage);

    cameraTab?.setAttribute('aria-selected', String(!showImage));
    imageTab?.setAttribute('aria-selected', String(showImage));

    if (cameraPanel) {
        cameraPanel.hidden = showImage;
        cameraPanel.classList.toggle('active', !showImage);
    }

    if (imagePanel) {
        imagePanel.hidden = !showImage;
        imagePanel.classList.toggle('active', showImage);
    }
}

function markCameraImagePresetCustom() {
    const presetSelect = document.getElementById('cameraImagePreset');

    if (presetSelect) {
        presetSelect.value = 'custom';
    }
}

function writeCameraImageControls(values) {
    const mapping = {
        cameraBrightness: values.brightness,
        cameraContrast: values.contrast,
        cameraSaturation: values.saturation,
        cameraSharpness: values.sharpness,
        cameraExposure: values.exposure_compensation
    };

    Object.entries(mapping).forEach(([id, value]) => {
        const element = document.getElementById(id);

        if (element && Number.isFinite(Number(value))) {
            element.value = String(value);
        }
    });

    updateCameraControlLabels();
}

async function applyCameraImagePreset(presetName) {
    if (!presetName || presetName === 'custom') {
        return;
    }

    const preset = CAMERA_IMAGE_PRESETS[presetName];

    if (!preset) {
        return;
    }

    writeCameraImageControls(preset);
    await updateCameraImageControls(false);

    showToast(
        `✅ ${window.I18N.t('camera.image_preset_applied', { name: document.getElementById('cameraImagePreset')?.selectedOptions[0]?.textContent || presetName })}`,
        'success'
    );
}

function formatCameraControlValue(value) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
        return '0.0';
    }

    return number.toFixed(1);
}


function updateCameraControlLabels() {
    const controls = [
        ['cameraBrightness', 'cameraBrightnessValue'],
        ['cameraContrast', 'cameraContrastValue'],
        ['cameraSaturation', 'cameraSaturationValue'],
        ['cameraSharpness', 'cameraSharpnessValue'],
        ['cameraExposure', 'cameraExposureValue']
    ];

    controls.forEach(([inputId, labelId]) => {
        const input = document.getElementById(inputId);
        const label = document.getElementById(labelId);

        if (input && label) {
            label.textContent = formatCameraControlValue(input.value);
        }
    });
}


function readCameraImageControls() {
    return {
        brightness: parseFloat(
            document.getElementById('cameraBrightness')?.value ?? 0
        ),

        contrast: parseFloat(
            document.getElementById('cameraContrast')?.value ?? 1
        ),

        saturation: parseFloat(
            document.getElementById('cameraSaturation')?.value ?? 1
        ),

        sharpness: parseFloat(
            document.getElementById('cameraSharpness')?.value ?? 1
        ),

        exposure_compensation: parseFloat(
            document.getElementById('cameraExposure')?.value ?? 0
        ),

        awb_mode: document.getElementById('cameraWhiteBalance')?.value || 'auto'
    };
}


async function updateCameraImageControls(showMessage = false) {
    cameraControlPending = true;

    if (showMessage) {
        cameraControlShowMessage = true;
    }

    if (cameraControlRequestInProgress) {
        return;
    }

    cameraControlRequestInProgress = true;

    try {
        while (cameraControlPending) {
            cameraControlPending = false;

            const controls = readCameraImageControls();

            const response = await fetch('/api/camera/settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    controls: controls
                })
            });

            const data = await response.json();

            if (!response.ok || !data.ok) {
                throw new Error(
                    data.error || `HTTP ${response.status}`
                );
            }

            currentCameraControls = data.controls || controls;
            updateCameraControlLabels();

            /*
             * Image controls restart the Picamera2 pipeline.
             * The browser must always reconnect to the new MJPEG stream.
             */
            if (data.restarted) {
                await reconnectCameraFeed();
            }
        }

        if (cameraControlShowMessage) {
            showToast(`✅ ${window.I18N.t('camera.image_settings_updated')}`, 'success');
        }

    } catch (error) {
        console.error(
            'Error updating camera image controls:',
            error
        );

        showToast(
            `❌ ${window.I18N.t('camera.image_settings_failed', { message: error.message })}`,
            'error'
        );

    } finally {
        cameraControlRequestInProgress = false;
        cameraControlShowMessage = false;

        if (cameraControlPending) {
            updateCameraImageControls(false);
        }
    }
}

async function restoreCameraImageDefaults() {
    writeCameraImageControls(CAMERA_IMAGE_PRESETS.neutral);

    const presetSelect = document.getElementById('cameraImagePreset');
    if (presetSelect) {
        presetSelect.value = 'neutral';
    }

    const whiteBalanceSelect = document.getElementById('cameraWhiteBalance');
    if (whiteBalanceSelect) {
        whiteBalanceSelect.value = 'auto';
    }

    await updateCameraImageControls(false);

    showToast(
        `✅ ${window.I18N.t('camera.neutral_settings_restored')}`,
        'success'
    );
}

async function takeScreenshot(source = 'video') {
    const btn = document.querySelector('.screenshot-btn');
    const originalText = btn.textContent;
    
    try {
        btn.disabled = true;
        btn.textContent = `⏳ ${window.I18N.t('camera.capturing')}`;

        const response = await fetch('/api/camera/screenshot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: source })
        });

        const data = await response.json();

        if (data.ok) {
            showToast(`✅ ${window.I18N.t('camera.screenshot_saved')}`, 'success');
        } else {
            showToast(`❌ ${window.I18N.t('camera.failed_prefix', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
        }
    } catch (error) {
        console.error('Error taking screenshot:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

function stopVideoFeed() {
    const img = document.getElementById('videoFeed');
    if (img) {
        img.onload = null;
        img.onerror = null;
        img.removeAttribute('src');
        img.classList.add('camera-stream-hidden');
    }

    setCameraFeedLoading(false);
}

function reconnectCameraFeed() {
    if (!cameraPowerEnabled) {
        renderCameraPowerState();
        return Promise.resolve(false);
    }

    return new Promise((resolve) => {
        const img = document.getElementById('videoFeed');
        const status = document.getElementById('videoStatus');
        const frameWrap = img?.closest('.video-frame-wrap');

        if (!img) {
            resolve();
            return;
        }

        const sequence = ++cameraFeedRefreshSequence;

        if (cameraFeedRefreshTimer) {
            clearTimeout(cameraFeedRefreshTimer);
            cameraFeedRefreshTimer = null;
        }

        hideCameraFeed(window.I18N.t('camera.connecting'));

        if (status) {
            status.textContent = `🔄 ${window.I18N.t('camera.connecting_short')}`;
            status.style.color = '#ff9800';
        }

        let freezeFrame = null;

        /* A stale overlay from an interrupted reconnect must never survive. */
        frameWrap?.querySelectorAll('.camera-freeze-frame').forEach(element => element.remove());
        frameWrap?.classList.add('camera-feed-reconfiguring');

        /*
         * Preserve the latest visible camera frame while the
         * Picamera2 pipeline and MJPEG connection are restarting.
         */
        if (
            frameWrap &&
            img.complete &&
            img.naturalWidth > 0 &&
            img.naturalHeight > 0
        ) {
            try {
                const canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth;
                canvas.height = img.naturalHeight;

                const context = canvas.getContext('2d');

                if (context) {
                    context.drawImage(
                        img,
                        0,
                        0,
                        canvas.width,
                        canvas.height
                    );

                freezeFrame = document.createElement('img');
                freezeFrame.className = 'camera-freeze-frame';
                freezeFrame.src = canvas.toDataURL(
                    'image/jpeg',
                    0.85
                );

                frameWrap.appendChild(freezeFrame);
                }

            } catch (error) {
                console.warn(
                    '[CAMERA] Could not preserve current frame:',
                    error
                );
            }
        }

        img.onload = null;
        img.onerror = null;
        img.removeAttribute('src');

        cameraFeedRefreshTimer = setTimeout(() => {
            cameraFeedRefreshTimer = null;

            if (sequence !== cameraFeedRefreshSequence) {
                freezeFrame?.remove();
                frameWrap?.classList.remove('camera-feed-reconfiguring');
                resolve();
                return;
            }

            const finishReconnect = (online) => {
                if (sequence !== cameraFeedRefreshSequence) {
                    return;
                }

                frameWrap?.classList.remove('camera-feed-reconfiguring');

                if (online) {
                    showCameraFeed();
                } else {
                    hideCameraFeed(window.I18N.t('camera.stream_unavailable'));
                }

                if (status) {
                    status.textContent = online
                        ? `🟢 ${window.I18N.t('nodes.status_online')}`
                        : `🔴 ${window.I18N.t('camera.camera_unavailable')}`;

                    status.style.color = online
                        ? '#4caf50'
                        : '#c62828';
                }

                if (freezeFrame) {
                    freezeFrame.classList.add(
                        'camera-freeze-frame-hide'
                    );

                    setTimeout(() => {
                        freezeFrame.remove();
                    }, 250);
                }

                resolve();
            };

            img.onload = function() {
                finishReconnect(true);
            };

            img.onerror = function() {
                finishReconnect(false);
            };

            img.src = '/video_feed?t=' + Date.now();

            /*
             * MJPEG load events can behave differently between
             * browsers. Use a fallback after the new connection
             * has had enough time to produce its first frame.
             */
            setTimeout(() => {
                if (
                    sequence === cameraFeedRefreshSequence &&
                    img.naturalWidth > 0
                ) {
                    finishReconnect(true);
                }
            }, 1600);

        }, 500);
    });
}

function refreshVideoFeed() {
    if (!cameraPowerEnabled) {
        renderCameraPowerState();
        return;
    }

    reconnectCameraFeed();
}

// ============================================================
// SWITCH CAMERA MODE
// ============================================================
async function switchCameraMode(mode) {
    if (!cameraPowerEnabled) {
        renderCameraPowerState();
        return false;
    }

    try {
        console.log(`[CAMERA] Switching to ${mode} mode...`);
        const response = await fetch('/api/camera/switch_mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode })
        });
        
        const data = await response.json();
        if (data.ok) {
            console.log(`[CAMERA] Switched to ${mode} mode: ${data.resolution}`);
            return true;
        } else {
            console.error(`[CAMERA] Failed to switch to ${mode}:`, data.error);
            return false;
        }
    } catch (error) {
        console.error(`[CAMERA] Error switching to ${mode}:`, error);
        return false;
    }
}

// ============================================================
// PHOTO FUNCTIONS
// ============================================================
async function loadPhotoSettings() {
    try {
        const response = await fetch('/api/photo/settings');
        const data = await response.json();
        
        if (data.ok) {
            photoPreviewResolution = data.config.resolution || '640x480';
            photoSaveResolution = data.save_resolution || '3280x2464';
            currentPhotoQuality = data.config.quality || 85;
            
            const resSelect = document.getElementById('photoResolution');
            const qualitySlider = document.getElementById('photoQuality');
            const qualityLabel = document.getElementById('photoQualityLabel');
            
            if (resSelect) resSelect.value = photoPreviewResolution;
            if (qualitySlider) {
                qualitySlider.value = currentPhotoQuality;
                if (qualityLabel) qualityLabel.textContent = currentPhotoQuality + '%';
            }
            
            const photoInfo = document.getElementById('photoInfo');
            if (photoInfo) {
                const res = photoPreviewResolution.replace('x', '×');
                photoInfo.textContent = window.I18N.t('camera.photo_info', {
                    resolution: res,
                    quality: currentPhotoQuality,
                    saveResolution: photoSaveResolution.replace('x', '×')
                });
            }
            
            console.log('[PHOTO] Settings loaded:', { preview: photoPreviewResolution, quality: currentPhotoQuality });
        }
    } catch (error) {
        console.error('Error loading photo settings:', error);
    }
}

async function updatePhotoSettings(showMessage = false) {
    const resolution = document.getElementById('photoResolution')?.value;
    const quality = parseInt(document.getElementById('photoQuality')?.value || '95');

    const label = document.getElementById('photoQualityLabel');
    if (label) label.textContent = `${quality}%`;

    try {
        const response = await fetch('/api/photo/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                resolution: resolution,
                quality: quality
            })
        });

        const data = await response.json();

        if (!data.ok) {
            showToast(`❌ ${window.I18N.t('camera.update_photo_settings_failed')}`, 'error');
            return;
        }

        if (showMessage) {
            showToast(`✅ ${window.I18N.t('camera.photo_quality_set', { quality })}`, 'success');
        }

    } catch (error) {
        console.error('Error updating photo settings:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
    }
}

async function capturePhotoPreview() {
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    
    try {
        if (status) {
            status.textContent = `⏳ ${window.I18N.t('camera.capturing_preview')}`;
            status.style.color = '#ff9800';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = '⏳...';
        }
        if (display) display.style.display = 'none';
        if (placeholder) placeholder.style.display = 'flex';
        
        console.log('[PHOTO] Capturing preview with quality:', currentPhotoQuality);
        const response = await fetch('/api/photo/capture', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        console.log('[PHOTO] Response:', data);
        
        if (data.ok && data.image_data) {
            if (display) {
                display.src = 'data:image/jpeg;base64,' + data.image_data;
                display.style.display = 'block';
            }
            if (placeholder) placeholder.style.display = 'none';
            if (status) {
                const res = data.preview_resolution || photoPreviewResolution;
                const quality = data.quality || currentPhotoQuality;
                status.textContent = `📷 ${window.I18N.t('camera.preview_ready', { resolution: res.replace('x', '×'), quality })}`;
                status.style.color = '#2e7d32';
            }
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
            }
            currentPhotoData = data.image_data;
        } else {
            console.error('[PHOTO] Failed:', data.error);
            if (status) {
                status.textContent = `❌ ${window.I18N.t('camera.failed_prefix', { reason: data.error || window.I18N.t('errors.unknown_error') })}`;
                status.style.color = '#c62828';
            }
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
            }
            if (placeholder) placeholder.style.display = 'flex';
        }
    } catch (error) {
        console.error('[PHOTO] Error:', error);
        if (status) {
            status.textContent = `❌ ${window.I18N.t('errors.network_error')}`;
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
        }
        if (placeholder) placeholder.style.display = 'flex';
    }
}

async function captureCameraPhoto() {
    if (!cameraPowerEnabled) {
        showToast(
            `⚫ ${window.I18N.t('camera.turn_camera_on_first')}`,
            'error'
        );
        return;
    }

    const btn = document.querySelector('.camera-actions-block .screenshot-btn');
    const videoFeed = document.getElementById('videoFeed');

    try {
        if (btn) {
            btn.disabled = true;
            btn.textContent = `⏳ ${window.I18N.t('camera.saving')}`;
        }

        if (videoFeed) {
            videoFeed.classList.add('camera-capturing');
        }


        await updatePhotoSettings(false);

        const response = await fetch('/api/photo/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.ok) {
            showToast(`✅ ${window.I18N.t('camera.screenshot_saved_named', { name: data.display_name || data.filename })}`, 'success');
        } else {
            showToast(`❌ ${window.I18N.t('camera.screenshot_failed', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
        }

    } catch (error) {
        console.error('Screenshot error:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');

    } finally {

    if (videoFeed) {
        videoFeed.classList.remove('camera-capturing');
    }

    setTimeout(() => {
        refreshVideoFeed();
    }, 1200);

    if (btn) {
        btn.disabled = false;
        btn.textContent = `📸 ${window.I18N.t('camera.screenshot')}`;
        }
    }
}

async function savePhoto() {
    const display = document.getElementById('photoDisplay');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    
    if (!display || display.style.display === 'none' || !currentPhotoData) {
        showToast(`❌ ${window.I18N.t('camera.no_photo_to_save')}`, 'error');
        return;
    }

    try {
        if (status) {
            status.textContent = `⏳ ${window.I18N.t('camera.capturing_high_res')}`;
            status.style.color = '#ff9800';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = '⏳...';
        }

        const response = await fetch('/api/photo/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.ok) {
            if (data.preview_data && display) {
                display.src = 'data:image/jpeg;base64,' + data.preview_data;
            }

            if (status) {
                status.textContent = `✅ ${window.I18N.t('camera.saved_exclaim')}`;
                status.style.color = '#2e7d32';
            }
            showToast(`✅ ${window.I18N.t('camera.photo_saved', { filename: data.filename, size: (data.size / 1024).toFixed(1) })}`, 'success');

            setTimeout(() => {
                if (status) {
                    const res = photoPreviewResolution.replace('x', '×');
                    status.textContent = `📷 ${window.I18N.t('camera.preview_ready', { resolution: res, quality: currentPhotoQuality })}`;
                    status.style.color = '#2e7d32';
                }
                if (saveBtn) {
                    saveBtn.disabled = false;
                    saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
                }
            }, 2000);
        } else {
            if (status) {
                status.textContent = `❌ ${window.I18N.t('camera.save_failed')}`;
                status.style.color = '#c62828';
            }
            showToast(`❌ ${window.I18N.t('camera.failed_to_save', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
            }
        }
    } catch (error) {
        console.error('Error saving photo:', error);
        if (status) {
            status.textContent = `❌ ${window.I18N.t('errors.network_error')}`;
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
        }
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
    }
}

function refreshPhoto() {
    const status = document.getElementById('photoStatus');
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    
    if (status) {
        status.textContent = `⏳ ${window.I18N.t('camera.capturing')}`;
        status.style.color = '#ff9800';
    }
    if (display) display.style.display = 'none';
    if (placeholder) placeholder.style.display = 'flex';

    switchCameraMode('photo').then(() => {
        setTimeout(() => capturePhotoPreview(), 300);
    });
}

// ============================================================
// SWITCH MAIN TAB (MODIFIED)
// ============================================================

function deviceDashboardValue(value, fallback = '—') {
    if (value === null || value === undefined || value === '') return fallback;
    return escapeHtml(String(value));
}

function formatDeviceDashboardDate(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return escapeHtml(String(value));
    return TimeFormatter.formatDateTime(parsed);
}

function deviceStatusClass(mode, identityStatus) {
    if (identityStatus === 'MISMATCH') return 'device-status-danger';
    if (mode === 'connected') return 'device-status-ok';
    if (mode === 'released' || mode === 'releasing' || mode === 'reconnecting') return 'device-status-warning';
    return 'device-status-danger';
}

function deviceConnectionLabel(mode, listenerRunning) {
    if (mode === 'released') return window.I18N.t('settings.radio_status_released');
    if (mode === 'releasing') return window.I18N.t('settings.radio_status_releasing');
    if (mode === 'reconnecting') return window.I18N.t('settings.radio_status_reconnecting');
    if (mode === 'error') return window.I18N.t('node_manager.connection_error');
    return listenerRunning ? window.I18N.t('settings.radio_status_connected') : window.I18N.t('node_manager.listener_stopped');
}

async function loadNodeManagerDashboard(showFeedback = false) {
    const container = document.getElementById('nodeManagerDashboard');
    if (!container) return;

    if (!container.dataset.loaded) {
        container.innerHTML = `<div class="device-dashboard-loading">${escapeHtml(window.I18N.t('node_manager.loading'))}</div>`;
    }

    try {
        const response = await fetch('/api/node-manager/dashboard', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || window.I18N.t('node_manager.unable_to_load'));

        const radio = data.radio || {};
        const connection = data.connection || {};
        const profile = data.profile || {};
        const profiles = Array.isArray(data.profiles) && data.profiles.length
            ? data.profiles
            : [{ profile_id: profile.profile_id, radio, active: true, connection }];
        const counts = profile.counts || {};
        const storage = profile.storage || {};
        const statusClass = deviceStatusClass(connection.mode, radio.identity_status);
        const connectionLabel = deviceConnectionLabel(connection.mode, connection.listener_running);
        const canRelease = connection.mode === 'connected' && connection.listener_running;
        const canReconnect = connection.mode === 'released' || connection.mode === 'error' || (!connection.listener_running && connection.mode !== 'reconnecting');
        const iconSrc = window.MeshCenterNodeAvatar?.current?.() || '/static/meshcenter_logo.png';

        const profileCards = profiles.map(item => {
            const itemRadio = item.radio || {};
            const itemConnection = item.connection || {};
            const active = Boolean(item.active || item.profile_id === profile.profile_id);
            const connected = Boolean(itemConnection.listener_running && itemConnection.mode === 'connected');
            const identity = itemRadio.identity_status || 'NOT_CHECKED';
            return `
                <button type="button" class="node-profile-card ${active ? 'is-active' : ''}"
                        data-profile-id="${deviceDashboardValue(item.profile_id)}"
                        ${active ? 'aria-current="true" disabled' : `onclick="activateNodeManagerProfile('${String(item.profile_id || '').replace(/'/g, "\\'")}')"`}>
                    <span class="node-profile-icon">📻</span>
                    <span class="node-profile-main">
                        <strong>${deviceDashboardValue(itemRadio.long_name, item.profile_id || 'Radio profile')}</strong>
                        <small>${deviceDashboardValue(itemRadio.hardware)} · ${deviceDashboardValue(itemRadio.node_id)}</small>
                    </span>
                    <span class="node-profile-badges">
                        ${active ? `<span class="node-profile-badge active">${escapeHtml(window.I18N.t('node_manager.badge_active'))}</span>` : `<span class="node-profile-badge saved">${escapeHtml(window.I18N.t('node_manager.badge_saved'))}</span>`}
                        ${active ? `<span class="node-profile-badge ${connected ? 'connected' : 'offline'}">${connected ? escapeHtml(window.I18N.t('settings.radio_status_connected')) : escapeHtml(window.I18N.t('node_manager.badge_offline'))}</span>` : ''}
                        <span class="node-profile-badge identity">${deviceDashboardValue(identity)}</span>
                    </span>
                </button>`;
        }).join('');

        container.dataset.loaded = '1';
        container.innerHTML = `
            <section class="node-profile-selector-section">
                <div class="node-manager-section-heading">
                    <div>
                        <h3>${escapeHtml(window.I18N.t('node_manager.radios_and_profiles'))}</h3>
                        <p>${escapeHtml(window.I18N.t('node_manager.select_profile_hint'))}</p>
                    </div>
                    <div class="node-manager-profile-heading-actions">
                        <span class="node-manager-profile-count">${profiles.length}</span>
                        <button type="button"
                            class="node-manager-detect-radio-btn"
                            onclick="detectAndAddNodeManagerRadio()">
                            ${escapeHtml(window.I18N.t('node_manager.detect_radio'))}
                        </button>
                    </div>
                </div>
                <div class="node-profile-list">${profileCards}</div>
            </section>

            <section class="device-hero-card node-manager-hero-card">
                <div class="node-manager-avatar-wrap">
                    <img id="nodeManagerAvatar" class="node-manager-avatar" src="${escapeHtml(iconSrc)}" alt="">
                    <button type="button" class="node-manager-change-image-btn" id="nodeManagerChangeImageBtn">${escapeHtml(window.I18N.t('node_manager.change_image'))}</button>
                </div>
                <div class="device-hero-main">
                    <div class="device-card-eyebrow">${escapeHtml(window.I18N.t('node_manager.selected_radio'))}</div>
                    <h3>${deviceDashboardValue(radio.long_name, window.I18N.t('node_manager.meshtastic_radio_fallback'))}</h3>
                    <div class="device-hero-meta">
                        <span>${deviceDashboardValue(radio.short_name)}</span>
                        <span>${deviceDashboardValue(radio.hardware)}</span>
                        <span>${deviceDashboardValue(radio.node_id)}</span>
                    </div>
                </div>
                <div class="node-manager-status-stack">
                    <div class="device-status-pill ${statusClass}"><span class="device-status-dot"></span>${escapeHtml(connectionLabel)}</div>
                    <span class="node-manager-active-label">${escapeHtml(window.I18N.t('node_manager.active_profile'))}</span>
                </div>
            </section>

            <div class="device-dashboard-grid">
                <section class="device-info-card">
                    <div class="device-card-title">📡 ${escapeHtml(window.I18N.t('node_manager.radio_label'))}</div>
                    <dl class="device-detail-list">
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.long_name'))}</dt><dd>${deviceDashboardValue(radio.long_name)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.short_name'))}</dt><dd>${deviceDashboardValue(radio.short_name)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('chat.node_id_label'))}</dt><dd class="device-monospace copyable-value" title="${escapeHtml(window.I18N.t('node_manager.click_to_copy'))}" onclick="copyTextToClipboard('${String(radio.node_id || '').replace(/'/g, "\\'")}', '${escapeHtml(window.I18N.t('node_manager.node_id_copied'))}')">${deviceDashboardValue(radio.node_id)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.hardware'))}</dt><dd>${deviceDashboardValue(radio.hardware)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.firmware_version'))}</dt><dd>${deviceDashboardValue(radio.firmware_version)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.role'))}</dt><dd>${deviceDashboardValue(radio.role)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.identity'))}</dt><dd>${deviceDashboardValue(radio.identity_status)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.last_verified'))}</dt><dd>${formatDeviceDashboardDate(radio.identity_checked_at)}</dd></div>
                    </dl>
                </section>

                <section class="device-info-card">
                    <div class="device-card-title">🔌 ${escapeHtml(window.I18N.t('node_manager.connection_label'))}</div>
                    <dl class="device-detail-list">
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.usb_port'))}</dt><dd class="device-monospace">${deviceDashboardValue(radio.port)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('waypoints.status'))}</dt><dd>${escapeHtml(connectionLabel)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.listener_label'))}</dt><dd>${connection.listener_running ? escapeHtml(window.I18N.t('node_manager.running')) : escapeHtml(window.I18N.t('node_manager.stopped'))}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.listener_pid'))}</dt><dd>${deviceDashboardValue(connection.listener_pid)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.connected_since'))}</dt><dd>${formatDeviceDashboardDate(connection.connected_since)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.message_label'))}</dt><dd>${deviceDashboardValue(connection.message)}</dd></div>
                    </dl>
                    <div class="device-action-row">
                        <button type="button" class="device-action-btn device-action-secondary"
                            onclick="releaseRadioConnection(); setTimeout(() => loadNodeManagerDashboard(), 1200);"
                            ${canRelease ? '' : 'disabled'}>${escapeHtml(window.I18N.t('settings.release_radio'))}</button>
                        <button type="button" class="device-action-btn device-action-primary"
                            onclick="reconnectRadioConnection(); setTimeout(() => loadNodeManagerDashboard(), 1800);"
                            ${canReconnect ? '' : 'disabled'}>${escapeHtml(window.I18N.t('node_manager.reconnect'))}</button>
                    </div>
                </section>

                <section class="device-info-card">
                    <div class="device-card-title">🗂 ${escapeHtml(window.I18N.t('node_manager.profile_label'))}</div>
                    <dl class="device-detail-list">
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.profile_id'))}</dt><dd class="device-monospace">${deviceDashboardValue(profile.profile_id)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.created'))}</dt><dd>${formatDeviceDashboardDate(profile.created_at)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.last_used'))}</dt><dd>${formatDeviceDashboardDate(profile.last_used_at)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('nodes.messages'))}</dt><dd>${deviceDashboardValue(counts.messages, '0')}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('nav.chats'))}</dt><dd>${deviceDashboardValue(counts.chats, '0')}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('nodes.sidebar_tab'))}</dt><dd>${deviceDashboardValue(counts.nodes, '0')}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('waypoints.title'))}</dt><dd>${deviceDashboardValue(counts.waypoints, '0')}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.telemetry_records'))}</dt><dd>${deviceDashboardValue(counts.telemetry_records, '0')}</dd></div>
                    </dl>
                </section>

                <section class="device-info-card">
                    <div class="device-card-title">💾 ${escapeHtml(window.I18N.t('node_manager.profile_storage'))}</div>
                    <dl class="device-detail-list">
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.total'))}</dt><dd>${deviceDashboardValue(storage.total)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('nodes.messages'))}</dt><dd>${deviceDashboardValue(storage.messages)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('nodes.telemetry_short'))}</dt><dd>${deviceDashboardValue(storage.telemetry)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('waypoints.title'))}</dt><dd>${deviceDashboardValue(storage.waypoints)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.node_icons'))}</dt><dd>${deviceDashboardValue(storage.icons)}</dd></div>
                        <div><dt>${escapeHtml(window.I18N.t('node_manager.path'))}</dt><dd class="device-path-value copyable-value" title="${deviceDashboardValue(profile.path)}">${deviceDashboardValue(profile.path)}</dd></div>
                    </dl>
                </section>
            </div>`;

        if (showFeedback) showToast(window.I18N.t('node_manager.info_refreshed'), 'success');
    } catch (error) {
        console.error('[NODE MANAGER] Dashboard load failed:', error);
        container.innerHTML = `<div class="device-dashboard-error"><strong>${escapeHtml(window.I18N.t('node_manager.unable_to_load'))}</strong><span>${escapeHtml(error.message || String(error))}</span><button type="button" class="mc-refresh-btn" onclick="loadNodeManagerDashboard(true)">${escapeHtml(window.I18N.t('node_manager.try_again'))}</button></div>`;
        if (showFeedback) showToast(window.I18N.t('node_manager.info_load_failed'), 'error');
    }
}



async function waitForNodeManagerProfile(profileId, timeoutMs = 60000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        try {
            const response = await fetch('/api/node-manager/dashboard', {
                cache: 'no-store'
            });
            if (!response.ok) continue;
            const dashboard = await response.json();
            if (
                dashboard.ok &&
                String(dashboard.profile?.profile_id || '') === String(profileId)
            ) {
                window.location.reload();
                return;
            }
        } catch (_) {
            // The service is expected to be unavailable briefly during restart.
        }
    }
    window.location.reload();
}


async function detectAndAddNodeManagerRadio() {
    const confirmed = window.confirm(window.I18N.t('node_manager.detect_radio_confirm'));
    if (!confirmed) return;

    showToast(window.I18N.t('node_manager.releasing_and_scanning'), 'info');

    try {
        const response = await fetch('/api/node-manager/radio/detect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            cache: 'no-store'
        });
        const data = await response.json().catch(() => ({}));

        if (!response.ok || !data.ok) {
            const attempts = Array.isArray(data.attempts)
                ? data.attempts.map(item =>
                    `${item.port}: ${item.error || item.status || window.I18N.t('node_manager.no_response')}`
                ).join('\n')
                : '';
            throw new Error(
                (data.error || window.I18N.t('node_manager.radio_detection_failed_http', { status: response.status })) +
                (attempts ? `\n\n${window.I18N.t('node_manager.probe_results')}:\n${attempts}` : '')
            );
        }

        const radio = data.detected || {};
        const label = radio.long_name || radio.node_id || window.I18N.t('node_manager.meshtastic_radio_fallback');
        const details = [
            radio.short_name,
            radio.hardware,
            radio.node_id,
            radio.port
        ].filter(Boolean).join(' · ');

        const action = data.profile_exists
            ? window.I18N.t('node_manager.use_saved_profile')
            : window.I18N.t('node_manager.create_clean_profile');

        const accept = window.confirm(
            window.I18N.t('node_manager.radio_detected_confirm', {
                knownOrNew: data.profile_exists ? window.I18N.t('node_manager.known') : window.I18N.t('node_manager.new'),
                label,
                details,
                action
            })
        );
        if (!accept) {
            showToast(window.I18N.t('node_manager.radio_detected_released'), 'info');
            await loadNodeManagerDashboard();
            return;
        }

        showToast(
            data.profile_exists
                ? window.I18N.t('node_manager.selecting_profile_for', { label })
                : window.I18N.t('node_manager.creating_clean_profile_for', { label }),
            'info'
        );

        const acceptResponse = await fetch('/api/node-manager/radio/accept', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            cache: 'no-store',
            body: JSON.stringify({
                node_id: radio.node_id,
                port: radio.port
            })
        });
        const accepted = await acceptResponse.json().catch(() => ({}));
        if (!acceptResponse.ok || !accepted.ok) {
            throw new Error(
                accepted.error ||
                window.I18N.t('node_manager.profile_creation_failed_http', { status: acceptResponse.status })
            );
        }

        showToast(
            accepted.message || window.I18N.t('node_manager.radio_accepted_restarting'),
            'success'
        );
        waitForNodeManagerProfile(accepted.profile_id);
    } catch (error) {
        console.error('[NODE MANAGER] Radio detection failed:', error);
        window.alert(error.message || String(error));
        showToast(window.I18N.t('node_manager.radio_not_added'), 'error');
        await loadNodeManagerDashboard();
    }
}

async function activateNodeManagerProfile(profileId) {
    const cleanProfileId = String(profileId || '').trim();
    if (!cleanProfileId) return;

    const card = document.querySelector(
        `.node-profile-card[data-profile-id="${CSS.escape(cleanProfileId)}"]`
    );
    const radioName = card?.querySelector('.node-profile-main strong')?.textContent?.trim()
        || cleanProfileId;

    const confirmed = window.confirm(window.I18N.t('node_manager.switch_profile_confirm', { name: radioName }));
    if (!confirmed) return;

    document.querySelectorAll('.node-profile-card').forEach(button => {
        button.disabled = true;
    });
    card?.classList.add('is-switching');
    showToast(window.I18N.t('node_manager.checking_connected_radio_for', { name: radioName }), 'info');

    try {
        const response = await fetch(
            `/api/node-manager/profiles/${encodeURIComponent(cleanProfileId)}/activate`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                cache: 'no-store'
            }
        );
        const data = await response.json().catch(() => ({}));

        if (!response.ok || !data.ok) {
            throw new Error(data.error || window.I18N.t('node_manager.profile_activation_failed_http', { status: response.status }));
        }

        if (data.already_active) {
            showToast(data.message || window.I18N.t('node_manager.profile_already_active'), 'info');
            await loadNodeManagerDashboard();
            return;
        }

        showToast(data.message || window.I18N.t('node_manager.profile_activated_restarting'), 'success');

        waitForNodeManagerProfile(cleanProfileId, 60000);
    } catch (error) {
        console.error('[NODE MANAGER] Profile activation failed:', error);
        window.alert(error.message || String(error));
        showToast(window.I18N.t('node_manager.radio_profile_not_changed'), 'error');
        await loadNodeManagerDashboard();
    } finally {
        card?.classList.remove('is-switching');
        document.querySelectorAll('.node-profile-card').forEach(button => {
            if (!button.classList.contains('is-active')) button.disabled = false;
        });
    }
}

async function copyTextToClipboard(text, successMessage = window.I18N.t('node_manager.copied')) {
    if (!text) return;
    try {
        if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
        else {
            const area = document.createElement('textarea');
            area.value = text;
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            document.execCommand('copy');
            area.remove();
        }
        showToast(successMessage, 'success');
    } catch (error) {
        showToast(window.I18N.t('node_manager.copy_failed'), 'error');
    }
}


function openNodeManager(event) {
    event?.preventDefault?.();
    switchMainTab('node-manager');
}

function handleNodeManagerHeaderKey(event) {
    if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openNodeManager(event);
    }
}

function peripheralStatusLabel(device) {
    if (!device.assigned) return window.I18N.t('devices.not_assigned');
    if (!device.enabled) return window.I18N.t('devices.disabled');
    if (device.status === 'active') return window.I18N.t('waypoints.active');
    if (device.status === 'available') return window.I18N.t('devices.available');
    if (device.status === 'data') return window.I18N.t('settings.radio_status_connected');
    if (device.status === 'no_data') return window.I18N.t('devices.no_data');
    return window.I18N.t('devices.unavailable');
}

function peripheralStatusClass(device) {
    if (!device.assigned || !device.enabled) return 'device-status-warning';
    if (device.status === 'active' || device.status === 'available' || device.status === 'data') return 'device-status-ok';
    return 'device-status-warning';
}

function formatPeripheralMetric(value, unit = '') {
    if (value === null || value === undefined || value === '') return '—';
    const number = Number(value);
    const text = Number.isFinite(number) ? (Math.round(number * 100) / 100).toString() : String(value);
    return `${escapeHtml(text)}${unit}`;
}

function renderDisplayCard(hardwareDisplayData, profileId) {
    // Read-only for now (e-Paper Stage 1 plan Phase 6) - no enable/disable
    // or settings here yet, that's Phase 7. Nothing rendered at all when
    // the feature isn't enabled server-side (EPAPER_ENABLED in config.py),
    // matching how this card simply doesn't exist on installs without the
    // hardware, rather than showing a permanently "disabled" placeholder.
    if (!hardwareDisplayData || !hardwareDisplayData.enabled) return '';

    const eyebrow = escapeHtml(window.I18N.t('devices.active_profile', { id: deviceDashboardValue(profileId) }));
    const isOnline = hardwareDisplayData.status === 'online';
    const statusClass = isOnline ? 'device-status-ok' : 'device-status-warning';
    const statusLabel = isOnline
        ? window.I18N.t('devices.display_online')
        : window.I18N.t('devices.display_offline');
    const lastRefresh = hardwareDisplayData.last_successful_refresh
        ? TimeFormatter.formatTime(hardwareDisplayData.last_successful_refresh)
        : '—';
    const avgDuration = typeof hardwareDisplayData.average_duration === 'number'
        ? `${hardwareDisplayData.average_duration.toFixed(1)}s`
        : '—';

    return `
        <section class="peripheral-card">
            <div class="peripheral-card-header">
                <div>
                    <div class="device-card-eyebrow">${eyebrow}</div>
                    <h3>${deviceDashboardValue(hardwareDisplayData.model)}</h3>
                </div>
                <div class="device-status-pill ${statusClass}">
                    <span class="device-status-dot"></span>${statusLabel}
                </div>
            </div>
            <dl class="device-detail-list">
                <div><dt>${escapeHtml(window.I18N.t('devices.display_refreshes'))}</dt><dd>${deviceDashboardValue(hardwareDisplayData.refresh_count)}</dd></div>
                <div><dt>${escapeHtml(window.I18N.t('devices.display_errors'))}</dt><dd>${deviceDashboardValue(hardwareDisplayData.error_count)}</dd></div>
                <div><dt>${escapeHtml(window.I18N.t('devices.display_avg_duration'))}</dt><dd>${avgDuration}</dd></div>
                <div><dt>${escapeHtml(window.I18N.t('devices.display_last_refresh'))}</dt><dd>${lastRefresh}</dd></div>
            </dl>
        </section>`;
}

function renderCameraManagerCards(cameraManagerData, profileId) {
    const eyebrow = escapeHtml(window.I18N.t('devices.active_profile', { id: deviceDashboardValue(profileId) }));

    if (!cameraManagerData || !cameraManagerData.scanned) {
        return `
            <section class="peripheral-card">
                <div class="peripheral-card-header">
                    <div>
                        <div class="device-card-eyebrow">${eyebrow}</div>
                        <h3>${escapeHtml(window.I18N.t('devices.cameras_not_scanned'))}</h3>
                    </div>
                </div>
                <div class="device-action-row device-action-row-single">
                    <button type="button" class="mc-refresh-btn" onclick="rescanCameras(this)">${escapeHtml(window.I18N.t('devices.scan_cameras'))}</button>
                </div>
            </section>`;
    }

    const cameras = Array.isArray(cameraManagerData.cameras) ? cameraManagerData.cameras : [];
    if (cameras.length === 0) {
        return `
            <section class="peripheral-card">
                <div class="peripheral-card-header">
                    <div>
                        <div class="device-card-eyebrow">${eyebrow}</div>
                        <h3>${escapeHtml(window.I18N.t('devices.no_cameras_found'))}</h3>
                    </div>
                </div>
                <div class="device-action-row device-action-row-single">
                    <button type="button" class="mc-refresh-btn" onclick="rescanCameras(this)">${escapeHtml(window.I18N.t('devices.rescan_cameras'))}</button>
                </div>
            </section>`;
    }

    return cameras.map(camera => {
        const isActive = !!camera.active;
        const statusClass = isActive ? 'device-status-ok' : 'device-status-warning';
        const statusLabel = isActive ? window.I18N.t('waypoints.active') : window.I18N.t('devices.available');
        const action = isActive
            ? ''
            : `<div class="device-action-row device-action-row-single"><button type="button" class="mc-refresh-btn" onclick="setActiveCamera('${escapeHtml(camera.id)}')">${escapeHtml(window.I18N.t('devices.make_active'))}</button></div>`;
        return `
            <section class="peripheral-card">
                <div class="peripheral-card-header">
                    <div>
                        <div class="device-card-eyebrow">${eyebrow}</div>
                        <h3>${deviceDashboardValue(camera.model || camera.display_name)}</h3>
                    </div>
                    <div class="device-status-pill ${statusClass}">
                        <span class="device-status-dot"></span>${escapeHtml(statusLabel)}
                    </div>
                </div>
                <dl class="device-detail-list">
                    <div><dt>${escapeHtml(window.I18N.t('devices.camera_id'))}</dt><dd>${deviceDashboardValue(camera.id)}</dd></div>
                    <div><dt>${escapeHtml(window.I18N.t('devices.driver'))}</dt><dd>${deviceDashboardValue(camera.display_name)}</dd></div>
                </dl>
                ${action}
            </section>`;
    }).join('');
}

async function rescanCameras(button) {
    if (button) button.disabled = true;
    try {
        const response = await fetch('/api/devices/cameras/rescan', { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || window.I18N.t('devices.unable_to_load'));
        await loadPeripheralDevices(false);
    } catch (error) {
        console.error('[DEVICES] Camera rescan failed:', error);
        showToast(window.I18N.t('devices.unable_to_load'), 'error');
        if (button) button.disabled = false;
    }
}

async function setActiveCamera(driverId) {
    try {
        const response = await fetch('/api/camera/active', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ driver_id: driverId }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'unknown error');
        const activeCamera = (data.cameras || []).find(camera => camera.id === driverId);
        const name = activeCamera ? activeCamera.display_name : driverId;
        showToast(window.I18N.t('devices.camera_switched', { name }), 'success');
        await loadPeripheralDevices(false);
        await loadCameraSourceSelector();
    } catch (error) {
        console.error('[DEVICES] Camera switch failed:', error);
        showToast(window.I18N.t('devices.switch_camera_failed', { reason: error.message || String(error) }), 'error');
    }
}

// Camera tab's source dropdown - a second entry point to the same
// camera_manager.py registry the Devices tab cards use (setActiveCamera()
// above), shown inline next to the power button for convenience. Same
// caveat as the Devices cards: switching here only changes what
// camera_manager.py considers active, not the live stream, until the
// cutover described in server.py's CUTOVER TODO comment happens.
async function loadCameraSourceSelector() {
    const select = document.getElementById('cameraSourceSelect');
    if (!select) return;

    try {
        const response = await fetch('/api/devices/cameras', { cache: 'no-store' });
        const data = await response.json();
        const cameras = (response.ok && data.ok && data.scanned && Array.isArray(data.cameras))
            ? data.cameras
            : [];

        if (cameras.length < 2) {
            select.style.display = 'none';
            select.innerHTML = '';
            return;
        }

        select.innerHTML = cameras.map(camera => {
            const label = camera.model || camera.display_name || camera.id;
            const selected = camera.active ? ' selected' : '';
            return `<option value="${escapeHtml(camera.id)}"${selected}>${escapeHtml(label)}</option>`;
        }).join('');
        select.style.display = '';
    } catch (error) {
        console.error('[CAMERA] Failed to load camera source list:', error);
        select.style.display = 'none';
        select.innerHTML = '';
    }
}

async function onCameraSourceSelectChange(driverId) {
    const select = document.getElementById('cameraSourceSelect');
    if (select) select.disabled = true;
    try {
        const response = await fetch('/api/camera/active', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ driver_id: driverId }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'unknown error');
        const activeCamera = (data.cameras || []).find(camera => camera.id === driverId);
        const name = activeCamera ? (activeCamera.model || activeCamera.display_name) : driverId;
        showToast(window.I18N.t('camera.source_switched', { name }), 'success');
        if (document.getElementById('devicesDashboard')?.dataset.loaded) {
            await loadPeripheralDevices(false);
        }
        await loadCameraSourceSelector();
    } catch (error) {
        console.error('[CAMERA] Camera source switch failed:', error);
        showToast(window.I18N.t('camera.source_switch_failed', { reason: error.message || String(error) }), 'error');
        await loadCameraSourceSelector();
    } finally {
        if (select) select.disabled = false;
    }
}

async function loadPeripheralDevices(showFeedback = false) {
    const container = document.getElementById('devicesDashboard');
    if (!container) return;
    if (!container.dataset.loaded) {
        container.innerHTML = `<div class="device-dashboard-loading">${escapeHtml(window.I18N.t('devices.loading'))}</div>`;
    }

    try {
        const [response, cameraManagerResponse, hardwareDisplayResponse] = await Promise.all([
            fetch('/api/devices', { cache: 'no-store' }),
            fetch('/api/devices/cameras', { cache: 'no-store' }).catch(() => null),
            fetch('/api/hardware/display', { cache: 'no-store' }).catch(() => null),
        ]);
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || window.I18N.t('devices.unable_to_load'));
        const devices = Array.isArray(data.devices) ? data.devices : [];
        const cameraManagerData = cameraManagerResponse && cameraManagerResponse.ok
            ? await cameraManagerResponse.json().catch(() => null)
            : null;
        const hardwareDisplayData = hardwareDisplayResponse && hardwareDisplayResponse.ok
            ? await hardwareDisplayResponse.json().catch(() => null)
            : null;

        // The new camera-driver framework (camera_manager.py) replaces the
        // single hardcoded "camera" entry from /api/devices with however
        // many real cameras it found - CSI and/or USB. /video_feed itself
        // isn't wired to it yet (see the project's usb-camera-plan notes),
        // so "Make active" here only affects what camera_manager.py
        // thinks is selected, not the live stream, until that cutover.
        const cards = devices
            .filter(device => device.id !== 'camera')
            .map(device => {
            const values = device.values || {};
            let details = '';
            let action = '';
            if (device.id === 'environment') {
                details = `
                    <div><dt>${escapeHtml(window.I18N.t('devices.driver'))}</dt><dd>${deviceDashboardValue(device.driver)}</dd></div>
                    <div><dt>${escapeHtml(window.I18N.t('node_panel.temperature'))}</dt><dd>${formatPeripheralMetric(values.temperature, '°')}</dd></div>
                    <div><dt>${escapeHtml(window.I18N.t('node_panel.humidity'))}</dt><dd>${formatPeripheralMetric(values.humidity, '%')}</dd></div>
                    <div><dt>${escapeHtml(window.I18N.t('node_panel.pressure'))}</dt><dd>${formatPeripheralMetric(values.pressure, ' hPa')}</dd></div>`;
            } else if (device.id === 'power') {
                details = `
                    <div><dt>${escapeHtml(window.I18N.t('devices.driver'))}</dt><dd>${deviceDashboardValue(device.driver)}</dd></div>
                    <div><dt>${escapeHtml(window.I18N.t('node_panel.voltage'))}</dt><dd>${formatPeripheralMetric(values.voltage, ' V')}</dd></div>
                    <div><dt>${escapeHtml(window.I18N.t('node_panel.current'))}</dt><dd>${formatPeripheralMetric(values.current, ' mA')}</dd></div>
                    <div><dt>${escapeHtml(window.I18N.t('node_panel.power'))}</dt><dd>${formatPeripheralMetric(values.power, ' mW')}</dd></div>`;
            }
            return `
                <section class="peripheral-card">
                    <div class="peripheral-card-header">
                        <div>
                            <div class="device-card-eyebrow">${escapeHtml(window.I18N.t('devices.active_profile', { id: deviceDashboardValue(data.profile_id) }))}</div>
                            <h3>${deviceDashboardValue(device.name)}</h3>
                        </div>
                        <div class="device-status-pill ${peripheralStatusClass(device)}">
                            <span class="device-status-dot"></span>${escapeHtml(peripheralStatusLabel(device))}
                        </div>
                    </div>
                    <dl class="device-detail-list">${details}</dl>
                    ${action ? `<div class="device-action-row device-action-row-single">${action}</div>` : ''}
                </section>`;
        }).join('');

        const cameraCards = renderCameraManagerCards(cameraManagerData, data.profile_id);
        const displayCard = renderDisplayCard(hardwareDisplayData, data.profile_id);

        container.dataset.loaded = '1';
        container.innerHTML = `
            <div class="peripheral-grid">${cameraCards}${displayCard}${cards}</div>
            <section class="peripheral-card peripheral-add-card" aria-disabled="true">
                <div class="peripheral-add-icon">＋</div>
                <h3>${escapeHtml(window.I18N.t('devices.add_device'))}</h3>
                <p>${escapeHtml(window.I18N.t('devices.add_device_planned'))}</p>
            </section>`;
        if (showFeedback) showToast(window.I18N.t('devices.info_refreshed'), 'success');
    } catch (error) {
        console.error('[DEVICES] Peripheral load failed:', error);
        container.innerHTML = `<div class="device-dashboard-error"><strong>${escapeHtml(window.I18N.t('devices.unable_to_load'))}</strong><span>${escapeHtml(error.message || String(error))}</span><button type="button" class="mc-refresh-btn" onclick="loadPeripheralDevices(true)">${escapeHtml(window.I18N.t('node_manager.try_again'))}</button></div>`;
        if (showFeedback) showToast(window.I18N.t('devices.info_load_failed'), 'error');
    }
}


function setChatWorkspaceChromeVisible(visible) {
    const chatHeader = document.getElementById('chatHeader');
    const chatListContainer = document.getElementById('chatListContainer');
    const messagesView = document.getElementById('messagesView');
    const chatPanels = document.querySelector('.chat-panels');

    [chatHeader, chatListContainer, messagesView].forEach(element => {
        if (!element) return;
        element.hidden = !visible;
        element.setAttribute('aria-hidden', visible ? 'false' : 'true');
    });

    if (chatPanels && !visible) {
        chatPanels.scrollTop = 0;
        chatPanels.scrollLeft = 0;
    }
}

function switchMainTab(tab) {
    const transitionSequence = ++mainTabTransitionSequence;
    const operationalTabs = new Set(['chats', 'video', 'media', 'devices']);

    if (operationalTabs.has(tab)) {
        lastOperationalMainTab = tab;
    }

//    if (radioHealthTimer) {
//        clearInterval(radioHealthTimer);
//        radioHealthTimer = null;
//    }

    if (tab === 'chats' && contextChatMode) {
        contextChatMode = false;
        contextBaseTab = null;
        document.body.classList.remove('context-chat-mode');

        document.getElementById('videoView').style.display = 'none';
        document.getElementById('mediaView').style.display = 'none';
        document.getElementById('systemView').style.display = 'none';
        document.getElementById('settingsView').style.display = 'none';
        document.getElementById('aboutView').style.display = 'none';
        document.getElementById('mapView').style.display = 'none';
        document.getElementById('devicesView').style.display = 'none';
        document.getElementById('nodeManagerView')?.style && (document.getElementById('nodeManagerView').style.display = 'none');

        document.getElementById('chatListContainer').style.display = currentChatId ? 'none' : 'block';
        document.getElementById('messagesView').style.display = currentChatId ? 'flex' : 'none';
        document.getElementById('chatHeader').style.display = currentChatId ? 'flex' : 'none';

        document.querySelectorAll('.main-content-tab').forEach(t => t.classList.remove('active'));
        document.getElementById('mainTabChats')?.classList.add('active');

        updateDockForTab?.('chats');
        return;
    }

    currentMainTab = tab;
    const nodeManagerOpen = tab === 'node-manager';
    document.body.classList.toggle('node-manager-open', nodeManagerOpen);

    document.querySelectorAll('.main-content-tab').forEach(btn => {
        btn.classList.toggle(
            'active',
            btn.id === 'mainTab' + tab.charAt(0).toUpperCase() + tab.slice(1)
        );
    });

    document.querySelectorAll('.workspace-nav-btn[data-workspace-page]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.workspacePage === tab);
    });

    const messagesView = document.getElementById('messagesView');
    const videoView = document.getElementById('videoView');
    const mediaView = document.getElementById('mediaView');
    const devicesView = document.getElementById('devicesView');
    const nodeManagerView = document.getElementById('nodeManagerView');
    const photoView = document.getElementById('photoView');
    const chatHeader = document.getElementById('chatHeader');
    const chatListContainer = document.getElementById('chatListContainer');
    const systemView = document.getElementById('systemView');    
    const settingsView = document.getElementById('settingsView');
    const aboutView = document.getElementById('aboutView');
    const mapView = document.getElementById('mapView');

    if (tab !== 'chats') setChatWorkspaceChromeVisible(false);

    if (messagesView) messagesView.style.display = 'none';
    if (videoView) videoView.style.display = 'none';
    if (mediaView) mediaView.style.display = 'none';
    if (devicesView) devicesView.style.display = 'none';
    if (nodeManagerView) nodeManagerView.style.display = 'none';
    if (photoView) photoView.style.display = 'none';
    if (systemView) systemView.style.display = 'none';
    if (settingsView) settingsView.style.display = 'none';
    if (aboutView) aboutView.style.display = 'none';
    if (mapView) mapView.style.display = 'none';

    if (tab !== 'video') {
        stopCameraStream();
        stopVideoFeed();
    }

    if (tab === 'chats') {
        setChatWorkspaceChromeVisible(true);
        const chatHeader = document.getElementById('chatHeader');
        const chatListContainer = document.getElementById('chatListContainer');
        const messagesView = document.getElementById('messagesView');

        // Панели всегда видны
        if (chatHeader) chatHeader.style.display = 'flex';
        if (chatListContainer) chatListContainer.style.display = 'block';
        if (messagesView) messagesView.style.display = 'flex';

        // Если чат не выбран, выбрать первый канал (или первый DM)
        if (!currentChatId) {
            const channelChat = chatListCache.find(c => c.is_channel);
            if (channelChat) {
                openChat(channelChat.id, channelChat.name, channelChat.type);
            } else if (chatListCache.length > 0) {
                const firstChat = chatListCache[0];
                openChat(firstChat.id, firstChat.name, firstChat.type);
            } else {
                showChatList();
            }
        } else {
            // Если чат уже выбран, обновляем сообщения и подсветку
            // Сбрасываем сигнатуру, чтобы принудительно обновить сообщения
            lastRenderedSignature[currentChatId] = null;
            loadChatMessages(currentChatId);
            startMessagePolling(currentChatId);
            updateChatHeader();
            loadChatList();
        }

        loadMessages();
        updateStatusDock('chats');

        if (!isInitialized) {
            switchCameraMode('video');
        }

    } else if (tab === 'video') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (videoView) videoView.style.display = 'flex';

        updateStatusDock('video');
        stopMessagePolling();

        hideCameraFeed('Connecting to camera…');
        loadCameraSourceSelector();

        loadCameraPowerState().then(async () => {
            if (transitionSequence !== mainTabTransitionSequence || currentMainTab !== 'video') return;

            if (!cameraPowerEnabled) {
                setCameraFeedLoading(false);
                renderCameraPowerState();
                return;
            }

            await switchCameraMode('video');
            if (transitionSequence !== mainTabTransitionSequence || currentMainTab !== 'video') return;

            await Promise.allSettled([
                loadVideoSettings(),
                loadPhotoSettings()
            ]);
            if (transitionSequence !== mainTabTransitionSequence || currentMainTab !== 'video') return;

            cameraActive = true;
            await reconnectCameraFeed();

            if (transitionSequence !== mainTabTransitionSequence || currentMainTab !== 'video') {
                stopCameraStream();
                stopVideoFeed();
                return;
            }

            renderCameraPowerState();
        }).catch(error => {
            if (transitionSequence !== mainTabTransitionSequence || currentMainTab !== 'video') return;
            console.error('[CAMERA] Failed to open camera workspace:', error);
            cameraActive = false;
            stopVideoFeed();
            setCameraFeedLoading(false);
        });

    } else if (tab === 'media') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (mediaView) mediaView.style.display = 'flex';

        updateStatusDock('media');
        stopMessagePolling();

        if (typeof loadMediaGallery === 'function') {
            loadMediaGallery();
        }

    } else if (tab === 'devices') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (devicesView) devicesView.style.display = 'flex';

        updateStatusDock('devices');
        stopMessagePolling();
        loadPeripheralDevices();

    } else if (tab === 'node-manager') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (nodeManagerView) nodeManagerView.style.display = 'flex';

        updateStatusDock('node-manager');
        stopMessagePolling();
        loadNodeManagerDashboard();

    } else if (tab === 'photo') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (photoView) photoView.style.display = 'flex';

        updateStatusDock('photo');
        stopMessagePolling();

        switchCameraMode('photo').then(() => {
            setTimeout(() => loadPhotoSettings(), 100);
            setTimeout(() => capturePhotoPreview(), 300);
        });

    } else if (tab === 'system') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (systemView) systemView.style.display = 'flex';

        updateStatusDock('system');
        loadSystemNetwork();
        loadSystemInfo();
        loadInstanceInfo();
        loadRadioHealth();

    } else if (tab === 'map') {
        MapLayout.state.mode = 'full';
        MapLayout.save();
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (mapView) mapView.style.display = 'flex';

        stopMessagePolling();
        updateStatusDock('map');
        requestAnimationFrame(() => renderMeshMap(meshMapTargetNodeId));

    } else if (tab === 'settings') {
        if (settingsView) settingsView.style.display = 'flex';

        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';

        updateStatusDock('settings');
        loadEpaperSettings();
    } else if (tab === 'about') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (aboutView) aboutView.style.display = 'flex';

        stopMessagePolling();
        switchAboutTab(window.meshcenterAboutTab || 'overview');
        updateStatusDock('about');
    }

    if (['chats', 'video', 'media', 'devices', 'map'].includes(tab)) {
        applyMapLayout();
    } else {
        document.querySelector('.chat-area')?.classList.remove('map-layout-full', 'map-layout-split', 'map-on-top', 'map-on-bottom');
    }
}

function switchAboutTab(tab) {
    const normalized = ['overview', 'help', 'license', 'links', 'changelog'].includes(tab)
        ? tab
        : 'overview';

    window.meshcenterAboutTab = normalized;

    document.querySelectorAll('.about-tab').forEach(button => {
        const active = button.id === 'aboutTab' + normalized.charAt(0).toUpperCase() + normalized.slice(1);
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
    });

    document.querySelectorAll('.about-section').forEach(section => {
        const active = section.id === 'aboutSection' + normalized.charAt(0).toUpperCase() + normalized.slice(1);
        section.classList.toggle('active', active);
        section.hidden = !active;
    });
}

function updateStatusDock(tab) {
    const workspaceLabel = document.getElementById('dockWorkspaceLabel');
    const centerText = document.getElementById('dockStatusText');
    const right = document.getElementById('dockContextText');

    if (!workspaceLabel || !centerText || !right) return;

    if (tab === 'chats') {
        workspaceLabel.textContent = window.I18N.t('nav.chats');
        setDockStatusBaseline(window.I18N.t('notifications.mesh_online'), 'online');
        setStatusDockContext('Nodes');
    } else if (tab === 'video') {
        workspaceLabel.textContent = window.I18N.t('nav.camera');

        setDockStatusBaseline(
            cameraPowerEnabled
                ? (cameraActive ? window.I18N.t('notifications.camera_online') : window.I18N.t('notifications.camera_ready'))
                : window.I18N.t('notifications.camera_off'),
            cameraPowerEnabled ? 'online' : 'warning'
        );

        setStatusDockContext(cameraPowerEnabled
            ? getCurrentVideoInfoText()
            : 'Power-saving mode');
    } else if (tab === 'media') {
        workspaceLabel.textContent = window.I18N.t('nav.media');
        setDockStatusBaseline(window.I18N.t('notifications.local_gallery'), 'online');
        setStatusDockContext('Images');
    } else if (tab === 'devices') {
        workspaceLabel.textContent = window.I18N.t('nav.devices');
        setDockStatusBaseline(window.I18N.t('notifications.peripherals'), 'online');
        setStatusDockContext('Active profile');
    } else if (tab === 'node-manager') {
        workspaceLabel.textContent = window.I18N.t('node_manager.title');
        setDockStatusBaseline(window.I18N.t('notifications.active_radio'), 'online');
        setStatusDockContext('Profile');
    } else if (tab === 'system') {
        workspaceLabel.textContent = window.I18N.t('nav.system');
        setDockStatusBaseline(window.I18N.t('notifications.system_monitor'), 'online');
        setStatusDockContext('MeshCenter');
    } else if (tab === 'map') {
        workspaceLabel.textContent = window.I18N.t('settings.map');
        setDockStatusBaseline(window.I18N.t('notifications.node_positions'), 'online');
        setStatusDockContext('OpenStreetMap');
    } else if (tab === 'settings') {
        workspaceLabel.textContent = window.I18N.t('nav.settings');
        setDockStatusBaseline(window.I18N.t('notifications.ready'), 'online');
        setStatusDockContext('MeshCenter');
    } else if (tab === 'about') {
        workspaceLabel.textContent = window.I18N.t('notifications.about');
        setDockStatusBaseline('MeshCenter', 'online');
        setStatusDockContext('v' + (document.querySelector('meta[name="app-version"]')?.content || '?'));
    } else {
        workspaceLabel.textContent = window.I18N.t('notifications.workspace');
        setDockStatusBaseline(window.I18N.t('notifications.ready'), 'online');
        setStatusDockContext('MeshCenter');
    }
}

function getCurrentVideoInfoText() {
    const info = document.getElementById('videoLiveInfo');
    return info ? info.textContent.replace('Live: ', '') : window.I18N.t('nav.camera');
}

function syncVideoControlsToDock() {
    const srcRes = document.getElementById('videoResolution');
    const srcFps = document.getElementById('videoFps');
    const srcQuality = document.getElementById('videoQuality');

    const dockRes = document.getElementById('dockVideoResolution');
    const dockFps = document.getElementById('dockVideoFps');
    const dockQuality = document.getElementById('dockVideoQuality');
    const dockQualityLabel = document.getElementById('dockVideoQualityLabel');

    if (srcRes && dockRes) dockRes.value = srcRes.value;
    if (srcFps && dockFps) dockFps.value = srcFps.value;
    if (srcQuality && dockQuality) {
        dockQuality.value = srcQuality.value;
        if (dockQualityLabel) dockQualityLabel.textContent = srcQuality.value + '%';
    }
}

function syncDockVideoSettings() {
    const dockRes = document.getElementById('dockVideoResolution');
    const dockFps = document.getElementById('dockVideoFps');
    const dockQuality = document.getElementById('dockVideoQuality');
    const dockQualityLabel = document.getElementById('dockVideoQualityLabel');

    const srcRes = document.getElementById('videoResolution');
    const srcFps = document.getElementById('videoFps');
    const srcQuality = document.getElementById('videoQuality');

    if (dockRes && srcRes) srcRes.value = dockRes.value;
    if (dockFps && srcFps) srcFps.value = dockFps.value;
    if (dockQuality && srcQuality) {
        srcQuality.value = dockQuality.value;
        if (dockQualityLabel) dockQualityLabel.textContent = dockQuality.value + '%';
    }

    updateVideoSettings();
}

async function loadSystemNetwork() {
    try {
        const response = await fetch('/api/system/network');
        const data = await response.json();

        const ssidEl = document.getElementById('systemWifiSsid');
        if (ssidEl) ssidEl.textContent = data.ssid || '--';

        const signalEl = document.getElementById('systemWifiSignal');
        if (signalEl) {
            signalEl.textContent = data.signal_percent !== null && data.signal_percent !== undefined
                ? `${data.signal_percent}%`
                : '--';
        }

        const rssiEl = document.getElementById('systemWifiRssi');
        if (rssiEl) {
            rssiEl.textContent = data.rssi_dbm !== null && data.rssi_dbm !== undefined
                ? `${data.rssi_dbm} dBm`
                : '--';
        }

        const rxRateEl = document.getElementById('systemRxRate');
        if (rxRateEl) rxRateEl.textContent = data.rx_bitrate || '--';

        const txRateEl = document.getElementById('systemTxRate');
        if (txRateEl) txRateEl.textContent = data.tx_bitrate || '--';

        const ipEl = document.getElementById('systemWifiIp');
        if (ipEl) ipEl.textContent = data.ip || '--';

        const gatewayEl = document.getElementById('systemWifiGateway');
        if (gatewayEl) gatewayEl.textContent = data.gateway || '--';

        const internetEl = document.getElementById('systemInternet');
        if (internetEl) {
            internetEl.textContent = data.internet ? `🟢 ${window.I18N.t('settings.radio_status_connected')}` : `🔴 ${window.I18N.t('nodes.status_radio_offline')}`;
        }

    } catch (error) {
        console.error('System network load error:', error);
        showToast(`❌ ${window.I18N.t('system.failed_to_load_network_info')}`, 'error');
    }
}

async function toggleWifiNetworks() {
    const panel = document.getElementById("wifiNetworksPanel");

    if (!panel) return;

    if (panel.style.display === "none") {
        panel.style.display = "block";
        await loadWifiNetworks();
    } else {
        panel.style.display = "none";
    }
}

async function loadWifiNetworks() {

    const list = document.getElementById("wifiNetworksList");

    list.innerHTML = window.I18N.t('system.scanning_ellipsis');

    try {

        const response = await fetch("/api/system/wifi/scan");

        const data = await response.json();

        if (!data.ok) {
            list.innerHTML = escapeHtml(window.I18N.t('system.scan_failed'));
            return;
        }

        if (data.networks.length === 0) {
            list.innerHTML = escapeHtml(window.I18N.t('system.no_networks_found'));
            return;
        }

        list.innerHTML = "";

        data.networks.forEach(net => {

            const div = document.createElement("div");

            div.className = "wifi-network-item";

        const actionHtml = net.connected
            ? `<span class="wifi-connected">${escapeHtml(window.I18N.t('settings.radio_status_connected'))}</span>`
            : `
                <div class="wifi-actions">
                    ${net.saved ? `<button class="wifi-forget-btn" data-ssid="${escapeHtml(net.ssid)}">${escapeHtml(window.I18N.t('system.forget'))}</button>` : ''}
                    <button class="wifi-connect-btn" data-ssid="${escapeHtml(net.ssid)}" data-saved="${net.saved ? '1' : '0'}">
                        ${escapeHtml(window.I18N.t('system.connect'))}
                    </button>
                </div>
            `;

        div.innerHTML = `
            <div class="wifi-name">
                ${net.connected ? "🟢" : "⚪"} ${net.ssid}
                ${net.saved && !net.connected ? `<span class="wifi-saved-badge">${escapeHtml(window.I18N.t('node_manager.badge_saved'))}</span>` : ''}
            </div>

            <div class="wifi-info">
                <span>${net.signal ?? '--'}%</span>
                <span>${net.signal_dbm ?? '--'} dBm</span>
                <span>${net.security || window.I18N.t('system.open_security')}</span>
                ${actionHtml}
            </div>
        `;

            list.appendChild(div);

        });

        document.querySelectorAll('.wifi-connect-btn').forEach(btn => {
            btn.onclick = () => {
                const ssid = btn.dataset.ssid;
                const saved = btn.dataset.saved === '1';

                if (saved) {
                    connectWifi(ssid, '');
                } else {
                    openWifiConnectModal(ssid);
                }
            };
        });

        document.querySelectorAll('.wifi-forget-btn').forEach(btn => {
            btn.onclick = () => {
                forgetWifi(btn.dataset.ssid);
            };
        });

    } catch(e){

        console.error(e);

        list.innerHTML=escapeHtml(window.I18N.t('system.scan_error'));

    }

}

async function connectWifi(ssid, password) {
    try {
        showToast(`📶 ${window.I18N.t('system.connecting_to', { ssid })}`, 'success');

        const response = await fetch('/api/system/wifi/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid, password })
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            showToast(`✅ ${window.I18N.t('system.connected_to', { ssid })}`, 'success');

            setTimeout(() => {
                loadSystemNetwork();
                loadWifiNetworks();
            }, 2500);
        } else {
            showToast(`❌ ${window.I18N.t('system.wifi_connect_failed', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
        }

    } catch (error) {
        console.error('Wi-Fi connect error:', error);
        showToast(`❌ ${window.I18N.t('system.wifi_connect_network_error')}`, 'error');
    }
}

async function forgetWifi(ssid) {
    if (!confirm(window.I18N.t('system.forget_wifi_confirm', { ssid }))) return;

    try {
        const response = await fetch('/api/system/wifi/forget', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid })
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            showToast(`🗑️ ${window.I18N.t('system.forgotten', { ssid })}`, 'success');
            loadWifiNetworks();
        } else {
            showToast(`❌ ${window.I18N.t('system.forget_failed', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
        }

    } catch (error) {
        console.error('Wi-Fi forget error:', error);
        showToast(`❌ ${window.I18N.t('system.wifi_forget_network_error')}`, 'error');
    }
}

function exitSplitView() {
    const chatList = document.getElementById('chatListContainer');
    const messagesView = document.getElementById('messagesView');
    const videoView = document.getElementById('videoView');
    const mediaView = document.getElementById('mediaView');
    const systemView = document.getElementById('systemView');
    const settingsView = document.getElementById('settingsView');
    const nodeManagerView = document.getElementById('nodeManagerView');

    if (chatList) chatList.style.display = 'flex';
    if (messagesView) messagesView.style.display = 'none';
    if (videoView) videoView.style.display = 'none';
    if (mediaView) mediaView.style.display = 'none';
    if (systemView) systemView.style.display = 'none';
    if (settingsView) settingsView.style.display = 'none';
    if (nodeManagerView) nodeManagerView.style.display = 'none';

    document.querySelectorAll('.main-content-tab').forEach(tab => {
        tab.classList.remove('active');
    });

    document.getElementById('mainTabChats')?.classList.add('active');

    updateDockForTab?.('chats');
}

// ============================================================
// INIT
// ============================================================
async function init() {
    if (isInitialized) return;
    isInitialized = true;
    
    console.log('[INIT] Starting application...');
    
    await loadSettings();
    
    const statusEl = document.getElementById('statusText');
    if (statusEl) statusEl.innerHTML = `⏳ ${escapeHtml(window.I18N.t('common.loading'))}`;
    
    try {
        // Загружаем настройки из localStorage
        const savedShowIgnored = localStorage.getItem('mesh_show_ignored');
        if (savedShowIgnored === 'true') {
            showIgnored = true;
            const checkbox = document.getElementById('showIgnoredToggle');
            if (checkbox) checkbox.checked = true;
        }
        
        const savedShowFavorites = localStorage.getItem('mesh_show_favorites');
        if (savedShowFavorites === 'true') {
            showFavorites = true;
            const checkbox = document.getElementById('showFavoritesToggle');
            if (checkbox) checkbox.checked = true;
        }
        
        // Загружаем все данные параллельно с таймаутами
        console.log('[INIT] Loading data in parallel...');
        
        // Загружаем чаты в первую очередь (самое важное)
        await loadChatList();
        
        // Остальное загружаем параллельно
        await Promise.allSettled([
            loadTelemetry(),
            loadBaseStatus(),
            loadSensors(),
            loadMessages()
        ]).then(results => {
            const names = ['Telemetry', 'BaseStatus', 'Sensors', 'Messages'];
            results.forEach((result, index) => {
                if (result.status === 'fulfilled') {
                    console.log(`[INIT] ${names[index]} loaded`);
                } else {
                    console.warn(`[INIT] ${names[index]} failed:`, result.reason);
                }
            });
        });
        
        // Переключаемся на вкладку чатов
        console.log('[INIT] Switching to chats tab...');
        switchMainTab('chats');

        if (statusEl) statusEl.innerHTML = `🟢 ${window.I18N.t('notifications.mesh_online')}`;

        await loadRadioHealth();

        if (!radioHealthTimer) {
            radioHealthTimer = setInterval(loadRadioHealth, 5000);
        }

        console.log('[INIT] Application ready');

    } catch (error) {
        console.error('[INIT] Critical error:', error);
        const statusEl = document.getElementById('statusText');
        if (statusEl) statusEl.innerHTML = `🔴 ${window.I18N.t('nodes.error_loading_refresh')}`;

        const chatList = document.getElementById('chatList');
        if (chatList) {
            chatList.innerHTML = `
                <div class="loading" style="color:#c62828;">
                    ⚠️ ${escapeHtml(window.I18N.t('chat.failed_to_load_data'))}<br>
                    <small style="font-size:12px;color:#999;">${escapeHtml(error.message || window.I18N.t('errors.unknown_error'))}</small>
                    <br><br>
                    <button onclick="window.location.reload()" style="padding:8px 20px;border:none;border-radius:8px;background:#1a73e8;color:white;cursor:pointer;">
                        ↻ ${escapeHtml(window.I18N.t('chat.refresh_page'))}
                    </button>
                </div>
            `;
        }
    }
    
    // Периодические обновления
    setInterval(async () => {
        try {
            await Promise.all([
                loadMessages(),
                loadChatList(),
                loadSensors(),
                loadBaseStatus()
            ]);
        } catch (e) {
            console.error('Polling error:', e);
        }
    }, 10000);
    
    setInterval(loadTelemetry, 30000);

    const input = document.getElementById('messageInput');
    if (input) input.focus();
}

let selectedWifiSsid = null;

function openWifiConnectModal(ssid) {
    selectedWifiSsid = ssid;

    const modal = document.getElementById('wifiConnectModal');
    const ssidEl = document.getElementById('wifiConnectSsid');
    const passEl = document.getElementById('wifiConnectPassword');

    if (ssidEl) ssidEl.textContent = ssid;
    if (passEl) passEl.value = '';

    if (modal) modal.style.display = 'flex';
}

function closeWifiConnectModal() {
    const modal = document.getElementById('wifiConnectModal');
    if (modal) modal.style.display = 'none';
}

function toggleWifiPasswordVisible(cb) {
    const input = document.getElementById('wifiConnectPassword');
    if (input) input.type = cb.checked ? 'text' : 'password';
}

async function connectSelectedWifi() {
    const password = document.getElementById('wifiConnectPassword')?.value || '';

    if (!selectedWifiSsid) {
        showToast(`❌ ${window.I18N.t('system.no_wifi_selected')}`, 'error');
        return;
    }

    await connectWifi(selectedWifiSsid, password);
    closeWifiConnectModal();
}


function ensureCpuHistoryPanel() {
    if (document.getElementById('cpuUsageHistoryPanel')) return;

    const kernelValue = document.getElementById('systemKernel');
    if (!kernelValue) return;

    const systemCard = kernelValue.closest('.system-card, .info-card, .card')
        || kernelValue.parentElement?.parentElement;
    if (!systemCard) return;

    const panel = document.createElement('div');
    panel.id = 'cpuUsageHistoryPanel';
    panel.className = 'cpu-history-panel';
    panel.innerHTML = `
        <div class="cpu-history-header">
            <div>
                <strong>CPU Usage</strong>
                <span id="cpuHistoryCurrent" class="cpu-history-current">--%</span>
            </div>
            <div class="cpu-history-ranges" role="group" aria-label="CPU history range">
                ${['30m', '1h', '6h', '12h', '24h'].map(range => `
                    <button type="button"
                            class="cpu-range-btn ${range === cpuHistoryRange ? 'active' : ''}"
                            data-range="${range}">${range}</button>
                `).join('')}
            </div>
        </div>
        <div class="cpu-history-chart-wrap">
            <canvas id="cpuUsageHistoryCanvas"></canvas>
            <div id="cpuHistoryEmpty" class="cpu-history-empty">Collecting CPU data…</div>
        </div>
    `;
    systemCard.appendChild(panel);

    panel.addEventListener('click', event => {
        const button = event.target.closest('.cpu-range-btn');
        if (!button) return;
        cpuHistoryRange = button.dataset.range || '30m';
        panel.querySelectorAll('.cpu-range-btn').forEach(item => {
            item.classList.toggle('active', item === button);
        });
        loadCpuHistory(true);
    });
}

function cpuMetricClass(value, warning = 50, danger = 80) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '';
    if (number >= danger) return 'danger';
    if (number >= warning) return 'warning';
    return 'normal';
}

function formatDockPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--.-';

    // Figure space keeps one-digit values aligned without changing the dock width.
    const formatted = number.toFixed(1);
    return number < 10 ? `\u2007${formatted}` : formatted;
}


let dockCpuState = {
    usage: null,
    ram: null,
    temp: null
};
function ensureStatusDockMetrics() {
    const right = document.getElementById('dockContextText');
    if (!right) return null;

    let metrics = right.querySelector('.dock-system-metrics');
    if (!metrics) {
        right.innerHTML = '';
        metrics = document.createElement('span');
        metrics.className = 'dock-system-metrics';
        metrics.innerHTML = `
            <span class="dock-separator" aria-hidden="true">•</span>
            <span id="dockCpuMetric">CPU --%</span>
            <span class="dock-separator" aria-hidden="true">•</span>
            <span id="dockRamMetric">RAM --%</span>
            <span class="dock-separator" aria-hidden="true">•</span>
            <span id="dockTempMetric">TEMP --°C</span>
        `;
        right.append(metrics);
    }
    return { right, metrics };
}

function setStatusDockContext(_text) {
    ensureStatusDockMetrics();
}

function updateCpuStatus(data) {
    const usage = Number(data?.current);
    const ram = Number(data?.ram_percent);

    const rawTemp =
        data?.cpu_temp
        ?? data?.cpu_temperature
        ?? data?.temperature;

    const temp = rawTemp === null || rawTemp === undefined
        ? NaN
        : Number(rawTemp);

    if (Number.isFinite(usage)) dockCpuState.usage = usage;
    if (Number.isFinite(ram)) dockCpuState.ram = ram;
    if (Number.isFinite(temp)) dockCpuState.temp = temp;

    const cpuEl = document.getElementById('dockCpuMetric');
    const ramEl = document.getElementById('dockRamMetric');
    const tempEl = document.getElementById('dockTempMetric');
    const currentEl = document.getElementById('cpuHistoryCurrent');

    if (cpuEl) {
        cpuEl.textContent = Number.isFinite(dockCpuState.usage) ? `CPU ${formatDockPercent(dockCpuState.usage)}%` : 'CPU --%';
        cpuEl.className = cpuMetricClass(dockCpuState.usage);
    }
    if (ramEl) {
        ramEl.textContent = Number.isFinite(dockCpuState.ram) ? `RAM ${formatDockPercent(dockCpuState.ram)}%` : 'RAM --%';
        ramEl.className = cpuMetricClass(dockCpuState.ram, 70, 90);
    }
    if (tempEl) {
        tempEl.textContent = Number.isFinite(dockCpuState.temp)
            ? `TEMP ${formatTemperature(dockCpuState.temp)}`
            : '--';

        tempEl.className = cpuMetricClass(dockCpuState.temp, 65, 75);
    }
    if (currentEl) {
        currentEl.textContent = Number.isFinite(dockCpuState.usage) ? `${dockCpuState.usage.toFixed(1)}%` : '--%';
        currentEl.className = `cpu-history-current ${cpuMetricClass(dockCpuState.usage)}`;
    }
}

async function loadCpuStatus() {
    try {
        ensureStatusDockMetrics();

        const [cpuResponse, systemResponse] = await Promise.all([
            fetch('/api/system/cpu-history?range=30m', { cache: 'no-store' }),
            fetch('/api/system/info', { cache: 'no-store' })
        ]);

        if (!cpuResponse.ok) return;

        const cpuData = await cpuResponse.json();
        let systemData = {};

        if (systemResponse.ok) {
            systemData = await systemResponse.json();
        }

        updateCpuStatus({
            ...cpuData,
            cpu_temp:
                systemData?.cpu_temp
                ?? cpuData?.cpu_temp
                ?? cpuData?.cpu_temperature
                ?? cpuData?.temperature
        });
    } catch (error) {
        console.debug('CPU status update failed:', error);
    }
}

function getCpuHistoryRangeMs(range) {
    const ranges = {
        '30m': 30 * 60 * 1000,
        '1h': 60 * 60 * 1000,
        '6h': 6 * 60 * 60 * 1000,
        '12h': 12 * 60 * 60 * 1000,
        '24h': 24 * 60 * 60 * 1000
    };

    return ranges[range] || ranges['30m'];
}

async function loadCpuHistory(force = false) {
    if (currentMainTab !== 'system' && !force) return;
    ensureCpuHistoryPanel();
    const canvas = document.getElementById('cpuUsageHistoryCanvas');
    if (!canvas || typeof Chart === 'undefined') return;

    try {
        const response = await fetch(`/api/system/cpu-history?range=${encodeURIComponent(cpuHistoryRange)}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        const records = Array.isArray(data.records) ? data.records : [];
        updateCpuStatus(data);

        const empty = document.getElementById('cpuHistoryEmpty');
        if (empty) empty.style.display = records.length ? 'none' : 'flex';

        const now = Date.now();
        const rangeMs = getCpuHistoryRangeMs(cpuHistoryRange);
        const rangeStart = now - rangeMs;

        const chartData = records
            .map(item => ({
                x: Number(item.timestamp) * 1000,
                y: Number(item.usage)
            }))
            .filter(item =>
                Number.isFinite(item.x)
                && Number.isFinite(item.y)
                && item.x >= rangeStart
                && item.x <= now
            );
        
        const currentUsage = Number(data.current);

        if (Number.isFinite(currentUsage)) {
            const lastPoint = chartData[chartData.length - 1];

            if (!lastPoint || now - lastPoint.x > 1000) {
                chartData.push({
                    x: now,
                    y: currentUsage
                });
            }
        }    

        if (cpuUsageChart) {
            cpuUsageChart.data.datasets[0].data = chartData;

            cpuUsageChart.options.scales.x.min = rangeStart;
            cpuUsageChart.options.scales.x.max = now;

            cpuUsageChart.resize();
            cpuUsageChart.update('none');
            return;
        }

        cpuUsageChart = new Chart(canvas.getContext('2d'), {
            type: 'line',
            data: {
                datasets: [{
                    label: 'CPU %',
                    data: chartData,
                    borderWidth: 1.5,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    tension: 0.18,
                    fill: true
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                parsing: false,
                normalized: true,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title(items) {
                                const value = items?.[0]?.parsed?.x;
                                return value ? TimeFormatter.formatDateTime(new Date(value)) : '';
                            },
                            label(item) { return `CPU: ${Number(item.parsed.y).toFixed(1)}%`; }
                        }
                    }
                },
                scales: {
                    x: {
                        type: 'linear',
                        min: rangeStart,
                        max: now,
                        ticks: {
                            maxTicksLimit: 6,
                            callback(value) {
                                return TimeFormatter.formatTime(new Date(Number(value)));
                            }
                        },
                        grid: { display: false }
                    },
                    y: {
                        min: 0,
                        max: 100,
                        ticks: { stepSize: 25, callback: value => `${value}%` }
                    }
                }
            }
        });
    } catch (error) {
        console.error('CPU history load error:', error);
    }
}

function startCpuMonitoringUi() {
    ensureStatusDockMetrics();
    loadCpuStatus();
    clearInterval(cpuStatusTimer);
    clearInterval(cpuChartTimer);
    cpuStatusTimer = setInterval(loadCpuStatus, 2000);
    cpuChartTimer = setInterval(() => loadCpuHistory(false), 5000);
}

function formatIdentityRadio(radio) {
    if (!radio || typeof radio !== 'object') return '--';
    const name = radio.long_name || radio.short_name || window.I18N.t('nodes.unknown_node');
    const nodeId = radio.node_id || '';
    return nodeId ? `${name} (${nodeId})` : name;
}

function formatIdentityCheckedAt(value) {
    if (!value) return '--';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return TimeFormatter.formatDateTime(date);
}

async function loadInstanceInfo() {
    try {
        const response = await fetch('/api/instance', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || window.I18N.t('system.identity_request_failed'));

        const setText = (id, value) => {
            const element = document.getElementById(id);
            if (element) element.textContent = value || '--';
        };
        setText('instanceName', data.instance_name);
        setText('instanceHostname', data.hostname);
        setText('instanceConfiguredRadio', formatIdentityRadio(data.configured));
        setText('instanceDetectedRadio', formatIdentityRadio(data.detected));
        setText('instanceLastCheck', formatIdentityCheckedAt(data.checked_at));

        const status = String(data.status || 'NOT_CHECKED').toUpperCase();
        const labels = {
            MATCH: window.I18N.t('system.identity_verified'),
            MISMATCH: window.I18N.t('system.identity_mismatch'),
            NOT_FOUND: window.I18N.t('system.identity_not_found'),
            DETECTION_ERROR: window.I18N.t('system.identity_detection_error'),
            NOT_CHECKED: window.I18N.t('system.identity_not_checked'),
        };
        const statusElement = document.getElementById('instanceIdentityStatus');
        if (statusElement) {
            statusElement.textContent = labels[status] || status;
            statusElement.className = `identity-status identity-${status.toLowerCase().replaceAll('_', '-')}`;
        }

        const errorElement = document.getElementById('instanceIdentityError');
        if (errorElement) {
            errorElement.textContent = data.error || '';
            errorElement.hidden = !data.error;
        }
    } catch (error) {
        console.error('Instance identity load error:', error);
        const statusElement = document.getElementById('instanceIdentityStatus');
        if (statusElement) {
            statusElement.textContent = window.I18N.t('devices.unavailable');
            statusElement.className = 'identity-status identity-detection-error';
        }
    }
}

async function loadSystemInfo() {
    try {
        const response = await fetch('/api/system/info');
        const data = await response.json();

        const hostnameEl = document.getElementById('systemHostname');
        if (hostnameEl) hostnameEl.textContent = data.hostname || '--';

        const uptimeEl = document.getElementById('systemUptime');
        if (uptimeEl) uptimeEl.textContent = data.uptime || '--';

        const cpuTempEl = document.getElementById('systemCpuTemp');
        if (cpuTempEl) {
            cpuTempEl.textContent = data.cpu_temp !== null && data.cpu_temp !== undefined
                ? formatTemperature(data.cpu_temp)
                : '--';
        }

        const cpuLoadEl = document.getElementById('systemCpuLoad');
        if (cpuLoadEl) {
            cpuLoadEl.textContent = data.load_avg !== null && data.load_avg !== undefined
                ? data.load_avg.toFixed(2)
                : '--';
        }

        const ramEl = document.getElementById('systemRam');
        if (ramEl) {
            ramEl.textContent = data.ram_used_mb !== null && data.ram_total_mb !== null
                ? `${data.ram_used_mb} / ${data.ram_total_mb} MB`
                : '--';
        }

        const diskEl = document.getElementById('systemDisk');
        if (diskEl) {
            diskEl.textContent = data.disk_used_gb !== null && data.disk_total_gb !== null
                ? `${data.disk_used_gb} / ${data.disk_total_gb} GB`
                : '--';
        }

        const modelEl = document.getElementById('systemModel');
        if (modelEl) modelEl.textContent = data.model || '--';

        const osEl = document.getElementById('systemOs');
        if (osEl) osEl.textContent = data.os || '--';

        const kernelEl = document.getElementById('systemKernel');
        if (kernelEl) kernelEl.textContent = data.kernel || '--';

        ensureCpuHistoryPanel();
        loadCpuHistory(true);

    } catch (error) {
        console.error('System info load error:', error);
        showToast('❌ Failed to load system info', 'error');
    }
}

function updateHeaderNodeStatus(data, reachable = true) {
    const headerStatus = document.getElementById('headerStatusText');
    if (!headerStatus) return;

    const labelEl = headerStatus.querySelector('.status-label');

    const status = String(data?.status || '').toUpperCase();
    const level = String(data?.level || '').toUpperCase();
    const listenerRunning = Boolean(data?.listener_running);

    let label = 'Disconnected';
    let stateClass = 'status-offline';

    if (!reachable) {
        label = 'Disconnected';
        stateClass = 'status-offline';

    } else if (status === 'PAUSED') {
        label = 'Radio Busy';
        stateClass = 'status-warning';

    } else if (status === 'STARTING') {
        label = 'Starting';
        stateClass = 'status-warning';

    } else if (!listenerRunning || status === 'LISTENER_DOWN') {
        label = 'Offline';
        stateClass = 'status-error';

    } else if (status === 'NO_PACKETS') {
        label = 'No Signal';
        stateClass = 'status-error';

    } else if (status === 'IDLE' || level === 'WARNING') {
        label = status === 'IDLE' ? 'Idle' : 'Warning';
        stateClass = 'status-warning';
    } else if (level === 'ERROR') {
        label = 'Error';
        stateClass = 'status-error';

    } else if (level === 'OK' || status === 'OK') {
        label = 'Online';
        stateClass = 'status-ok';

    } else {
        label = status ? status.replaceAll('_', ' ') : 'Unknown';
        stateClass = 'status-warning';
    }

    headerStatus.classList.remove(
        'status-connecting',
        'status-ok',
        'status-warning',
        'status-error',
        'status-offline'
    );

    headerStatus.classList.add(stateClass);

    if (labelEl) {
        labelEl.textContent = label;
    }

    const packetText =
        data?.packet_age == null
            ? 'never'
            : `${data.packet_age} s ago`;

    const listenerText =
        listenerRunning
            ? 'running'
            : 'stopped';

    const reason =
        data?.status_reason ||
        data?.recommendation ||
        '';

    headerStatus.title = reachable
        ? `Radio: ${label} | Listener: ${listenerText} | Last packet: ${packetText}${reason ? ` | ${reason}` : ''} | Click to open System`
        : 'MeshCenter status API is unavailable. Click to open System';

    headerStatus.setAttribute(
        'aria-label',
        `${label}. Open System status`
    );
}

async function loadRadioHealth() {
    try {
        const healthResponse = await fetch('/api/radio_health', {
            cache: 'no-store'
        });

        if (!healthResponse.ok) {
            throw new Error(`Radio health HTTP ${healthResponse.status}`);
        }

        const data = await healthResponse.json();

        updateHeaderNodeStatus(data, true);

        let logData = {
            events: []
        };

        try {
            const logResponse = await fetch(
                '/api/system/log?limit=100',
                {
                    cache: 'no-store'
                }
            );

            if (logResponse.ok) {
                logData = await logResponse.json();
            }

        } catch (logError) {
            console.warn(
                'System log load error:',
                logError
            );
        }

        const statusEl = document.getElementById('radioHealthStatus');
        const levelEl = document.getElementById('radioHealthLevel');
        const listenerEl = document.getElementById('radioHealthListener');
        const packetEl = document.getElementById('radioHealthPacket');
        const telemetryEl = document.getElementById('radioHealthTelemetry');
        const sendEl = document.getElementById('radioHealthSend');
        const restartEl = document.getElementById('radioHealthRestart');
        const recommendationEl = document.getElementById('radioHealthRecommendation');
        const restartBtn = document.getElementById('restartListenerBtn');
        const historyEl = document.getElementById('radioHealthHistory');

        if (statusEl) statusEl.textContent = data.status || '--';

        const level = String(data.level || 'UNKNOWN').toUpperCase();
        let levelIcon = '⚪';
        let levelColor = '#777';

        if (level === 'OK') {
            levelIcon = '🟢';
            levelColor = '#249448';
        } else if (level === 'WARNING') {
            levelIcon = '🟡';
            levelColor = '#a66d00';
        } else if (level === 'ERROR') {
            levelIcon = '🔴';
            levelColor = '#c62828';
        }

        if (levelEl) {
            levelEl.textContent = `${levelIcon} ${level}`;
            levelEl.style.color = levelColor;
        }

        if (listenerEl) {
            listenerEl.textContent = data.listener_running ? `🟢 ${window.I18N.t('node_manager.running')}` : `🔴 ${window.I18N.t('node_manager.stopped')}`;
        }

        if (packetEl) packetEl.textContent = data.packet_age == null ? window.I18N.t('nodes.never_seen') : window.I18N.t('system.seconds_ago', { seconds: data.packet_age });
        if (telemetryEl) telemetryEl.textContent = data.telemetry_age == null ? window.I18N.t('nodes.never_seen') : window.I18N.t('system.seconds_ago', { seconds: data.telemetry_age });
        if (sendEl) sendEl.textContent = data.send_age == null ? window.I18N.t('nodes.never_seen') : window.I18N.t('system.seconds_ago', { seconds: data.send_age });
        if (restartEl) restartEl.textContent = data.restart_count ?? 0;

        if (recommendationEl) {
            recommendationEl.textContent = data.recommendation || data.status_reason || '--';
            recommendationEl.style.color = levelColor;
        }

        if (restartBtn) {
            restartBtn.disabled = false;
            restartBtn.textContent = `🔄 ${window.I18N.t('system.restart_listener_button_label')}`;
        }

        if (historyEl) {
            const history = Array.isArray(logData.events) ? logData.events.slice().reverse() : [];

            if (!history.length) {
                historyEl.innerHTML = `<div class="radio-history-empty">${escapeHtml(window.I18N.t('system.no_events_yet'))}</div>`;
            } else {
                historyEl.innerHTML = history.map(item => {
                    const itemLevel = String(item.level || 'INFO').toUpperCase();
                    let icon = '🔵';
                    let color = '#3974b9';

                    if (itemLevel === 'OK') {
                        icon = '🟢';
                        color = '#249448';
                    } else if (itemLevel === 'WARNING') {
                        icon = '🟡';
                        color = '#a66d00';
                    } else if (itemLevel === 'ERROR') {
                        icon = '🔴';
                        color = '#c62828';
                    } else if (itemLevel === 'ACTION') {
                        icon = '🟣';
                        color = '#7652a8';
                    }

                    const dateTime = item.date && item.time
                        ? `${item.date} ${item.time}`
                        : (item.datetime || item.time || '--');

                    const details = item.details
                        ? `<div class="radio-history-details">${escapeHtml(item.details)}</div>`
                        : '';

                    return `
                        <div class="radio-history-item">
                            <div class="radio-history-line">
                                <span class="radio-history-time">${escapeHtml(dateTime)}</span>
                                <span>${icon}</span>
                                <span class="radio-history-event" style="color:${color};">
                                    ${escapeHtml(item.event || window.I18N.t('system.event_fallback'))}
                                </span>
                                <span class="radio-history-source">${escapeHtml(item.source || 'system')}</span>
                            </div>
                            ${details}
                        </div>
                    `;
                }).join('');
            }
        }
    } catch (error) {
        updateHeaderNodeStatus(null, false);
        console.error('Radio health load error:', error);
    }
}

async function runSystemAction(action, button) {
    const config = {
        restart_meshcenter: {
            confirm: window.I18N.t('system.restart_meshcenter_confirm'),
            pending: window.I18N.t('system.restarting_meshcenter'),
            success: window.I18N.t('system.meshcenter_restart_requested')
        },
        reboot: {
            confirm: window.I18N.t('system.reboot_pi_confirm'),
            pending: window.I18N.t('system.restarting_pi'),
            success: window.I18N.t('system.pi_restart_requested')
        },
        shutdown: {
            confirm: window.I18N.t('system.shutdown_pi_confirm'),
            pending: window.I18N.t('system.shutting_down_pi'),
            success: window.I18N.t('system.pi_shutdown_requested')
        }
    };

    const selected = config[action];
    if (!selected || !confirm(selected.confirm)) return;

    const originalText = button?.textContent || '';
    if (button) {
        button.disabled = true;
        button.textContent = selected.pending;
    }

    try {
        const response = await fetch('/api/system/action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action })
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        showToast(`✅ ${selected.success}`, 'success');
        setTimeout(loadRadioHealth, 500);
    } catch (error) {
        showToast(`❌ ${error.message}`, 'error');
        if (button) {
            button.disabled = false;
            button.textContent = originalText;
        }
    }
}

function toggleRadioHealthHistory() {
    const panel = document.getElementById('radioHealthHistoryPanel');
    const arrow = document.getElementById('radioHistoryArrow');
    const button = document.getElementById('radioHistoryToggle');

    if (!panel) return;

    const opening = panel.style.display === 'none';

    panel.style.display = opening ? 'block' : 'none';

    if (arrow) {
        arrow.textContent = opening ? '▴' : '▾';
    }

    if (button) {
        button.setAttribute('aria-expanded', opening ? 'true' : 'false');
    }
}

// ============================================================
// ЭКСПОРТ В ГЛОБАЛЬНУЮ ОБЛАСТЬ
// ============================================================
window.loadChatList = loadChatList;
window.loadMessages = loadMessages;
window.openChat = openChat;
window.showChatList = showChatList;
window.toggleNotificationCenter = toggleNotificationCenter;
window.closeNotificationCenter = closeNotificationCenter;
window.clearNotifications = clearNotifications;
window.markNotificationRead = markNotificationRead;
window.toggleIgnore = toggleIgnore;
window.toggleFavorite = toggleFavorite;
window.selectNode = selectNode;
window.clearNodeSearch = clearNodeSearch;
window.rescanNodes = rescanNodes;
window.restartListener = restartListener;
window.showExportOptions = showExportOptions;
window.showImportOptions = showImportOptions;
window.closeFormatMenus = closeFormatMenus;
window.exportNodesCSV = exportNodesCSV;
window.exportNodesJSON = exportNodesJSON;
window.importNodesCSV = importNodesCSV;
window.importNodesJSON = importNodesJSON;
window.switchMainTab = switchMainTab;
window.switchSidebarTab = switchSidebarTab;
window.refreshVideoFeed = refreshVideoFeed;
window.updateVideoSettings = updateVideoSettings;
window.takeScreenshot = takeScreenshot;
window.loadPhotoSettings = loadPhotoSettings;
window.updatePhotoSettings = updatePhotoSettings;
window.capturePhotoPreview = capturePhotoPreview;
window.savePhoto = savePhoto;
window.refreshPhoto = refreshPhoto;
window.showChatActions = showChatActions;
window.deleteCurrentChat = deleteCurrentChat;
window.clearCurrentChat = clearCurrentChat;
window.deleteAllDmChats = deleteAllDmChats;
window.executeDeleteChat = executeDeleteChat;
window.executeClearChat = executeClearChat;
window.executeDeleteAllDm = executeDeleteAllDm;
window.closeChatActions = closeChatActions;
window.closeConfirmDelete = closeConfirmDelete;
window.closeConfirmClear = closeConfirmClear;
window.closeDeleteAllDmModal = closeDeleteAllDmModal;
window.openTelemetryModal = openTelemetryModal;
window.closeTelemetryModal = closeTelemetryModal;
window.setTelemetryRange = setTelemetryRange;
window.updateTelemetryConfig = updateTelemetryConfig;
window.switchCameraMode = switchCameraMode;
window.startCameraStream = startCameraStream;
window.stopCameraStream = stopCameraStream;
window.loadSensors = loadSensors;
window.loadBaseStatus = loadBaseStatus;
window.loadTelemetry = loadTelemetry;
window.loadSettings = loadSettings;
window.setUnitSetting = setUnitSetting;
window.exportTelemetryData = exportTelemetryData;
window.closeTelemetryExportMenu = closeTelemetryExportMenu;
window.downloadTelemetryExport = downloadTelemetryExport;
window.toggleTelemetrySeries = toggleTelemetrySeries;
window.openCustomTelemetryExport = openCustomTelemetryExport;
window.closeCustomTelemetryExport = closeCustomTelemetryExport;
window.updateCustomExportMode = updateCustomExportMode;
window.runCustomTelemetryExport = runCustomTelemetryExport;
window.updateStatusDock = updateStatusDock;
window.syncDockVideoSettings = syncDockVideoSettings;
window.loadSystemNetwork = loadSystemNetwork;
window.openWifiConnectModal = openWifiConnectModal;
window.closeWifiConnectModal = closeWifiConnectModal;
window.toggleWifiPasswordVisible = toggleWifiPasswordVisible;
window.connectSelectedWifi = connectSelectedWifi;
window.loadSystemInfo = loadSystemInfo;
window.exitSplitView = exitSplitView;
window.toggleRadioHealthHistory = toggleRadioHealthHistory;
window.runSystemAction = runSystemAction;
window.restartListener = restartListener;
window.updateCameraControlLabels = updateCameraControlLabels;
window.updateCameraImageControls = updateCameraImageControls;
window.restoreCameraImageDefaults = restoreCameraImageDefaults;
window.updateListenerRecoverySettings = updateListenerRecoverySettings;
window.toggleNodeToolsMenu = toggleNodeToolsMenu;
window.closeNodeToolsMenu = closeNodeToolsMenu;
window.runNodeTool = runNodeTool;
window.closeNodeToolResult = closeNodeToolResult;
window.openNodeMap = openNodeMap;
window.setMapProvider = setMapProvider;
window.setWeatherProvider = setWeatherProvider;
window.updateReferenceLocationFields =
    updateReferenceLocationFields;
window.saveReferenceLocation =
    saveReferenceLocation;
window.openReferenceSettings =
    openReferenceSettings;
window.openWeatherProviderSettings =
    openWeatherProviderSettings;
window.setBasePanelHidden = setBasePanelHidden;
window.setNodesPanelHidden = setNodesPanelHidden;
window.getAppSettings = function() {
    return appSettings;
};

// Экспортируем переменные
window.chatListCache = chatListCache;
window.currentChatId = currentChatId;
window.nodeCache = nodeCache;

console.log('[EXPORT] Все функции экспортированы в window');

// ============================================================
// ЗАПУСК
// ============================================================
console.log('[CHAT] Script loaded, calling init()...');
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeNotificationCenter, { once: true });
} else {
    initializeNotificationCenter();
}
init();


window.setCameraPower = setCameraPower;
window.toggleCameraPower = toggleCameraPower;


// ============================================================
// ЭКСПОРТ В ГЛОБАЛЬНУЮ ОБЛАСТЬ
// ============================================================
window.loadChatList = loadChatList;
window.loadMessages = loadMessages;
window.openChat = openChat;
window.showChatList = showChatList;
window.toggleNotificationCenter = toggleNotificationCenter;
window.closeNotificationCenter = closeNotificationCenter;
window.clearNotifications = clearNotifications;
window.markNotificationRead = markNotificationRead;
window.toggleIgnore = toggleIgnore;
window.toggleFavorite = toggleFavorite;
window.selectNode = selectNode;
window.clearNodeSearch = clearNodeSearch;
window.rescanNodes = rescanNodes;
window.restartListener = restartListener;
window.showExportOptions = showExportOptions;
window.showImportOptions = showImportOptions;
window.closeFormatMenus = closeFormatMenus;
window.exportNodesCSV = exportNodesCSV;
window.exportNodesJSON = exportNodesJSON;
window.importNodesCSV = importNodesCSV;
window.importNodesJSON = importNodesJSON;
window.switchMainTab = switchMainTab;
window.switchSidebarTab = switchSidebarTab;
window.refreshVideoFeed = refreshVideoFeed;
window.updateVideoSettings = updateVideoSettings;
window.takeScreenshot = takeScreenshot;
window.loadPhotoSettings = loadPhotoSettings;
window.updatePhotoSettings = updatePhotoSettings;
window.capturePhotoPreview = capturePhotoPreview;
window.savePhoto = savePhoto;
window.refreshPhoto = refreshPhoto;
window.showChatActions = showChatActions;
window.deleteCurrentChat = deleteCurrentChat;
window.clearCurrentChat = clearCurrentChat;
window.deleteAllDmChats = deleteAllDmChats;
window.executeDeleteChat = executeDeleteChat;
window.executeClearChat = executeClearChat;
window.executeDeleteAllDm = executeDeleteAllDm;
window.closeChatActions = closeChatActions;
window.closeConfirmDelete = closeConfirmDelete;
window.closeConfirmClear = closeConfirmClear;
window.closeDeleteAllDmModal = closeDeleteAllDmModal;
window.openTelemetryModal = openTelemetryModal;
window.closeTelemetryModal = closeTelemetryModal;
window.setTelemetryRange = setTelemetryRange;
window.updateTelemetryConfig = updateTelemetryConfig;
window.switchCameraMode = switchCameraMode;
window.startCameraStream = startCameraStream;
window.stopCameraStream = stopCameraStream;
window.loadSensors = loadSensors;
window.loadBaseStatus = loadBaseStatus;
window.loadTelemetry = loadTelemetry;
window.loadSettings = loadSettings;
window.setUnitSetting = setUnitSetting;
window.exportTelemetryData = exportTelemetryData;
window.closeTelemetryExportMenu = closeTelemetryExportMenu;
window.downloadTelemetryExport = downloadTelemetryExport;
window.toggleTelemetrySeries = toggleTelemetrySeries;
window.openCustomTelemetryExport = openCustomTelemetryExport;
window.closeCustomTelemetryExport = closeCustomTelemetryExport;
window.updateCustomExportMode = updateCustomExportMode;
window.runCustomTelemetryExport = runCustomTelemetryExport;
window.updateStatusDock = updateStatusDock;
window.syncDockVideoSettings = syncDockVideoSettings;
window.loadSystemNetwork = loadSystemNetwork;
window.openWifiConnectModal = openWifiConnectModal;
window.closeWifiConnectModal = closeWifiConnectModal;
window.toggleWifiPasswordVisible = toggleWifiPasswordVisible;
window.connectSelectedWifi = connectSelectedWifi;
window.loadSystemInfo = loadSystemInfo;
window.exitSplitView = exitSplitView;
window.toggleRadioHealthHistory = toggleRadioHealthHistory;
window.runSystemAction = runSystemAction;
window.updateCameraControlLabels = updateCameraControlLabels;
window.updateCameraImageControls = updateCameraImageControls;
window.restoreCameraImageDefaults = restoreCameraImageDefaults;
window.updateListenerRecoverySettings = updateListenerRecoverySettings;
window.toggleNodeToolsMenu = toggleNodeToolsMenu;
window.closeNodeToolsMenu = closeNodeToolsMenu;
window.runNodeTool = runNodeTool;
window.closeNodeToolResult = closeNodeToolResult;
window.openNodeMap = openNodeMap;
window.setMapProvider = setMapProvider;
window.setWeatherProvider = setWeatherProvider;
window.updateReferenceLocationFields = updateReferenceLocationFields;
window.saveReferenceLocation = saveReferenceLocation;
window.openReferenceSettings = openReferenceSettings;
window.openWeatherProviderSettings = openWeatherProviderSettings;
window.setBasePanelHidden = setBasePanelHidden;
window.setNodesPanelHidden = setNodesPanelHidden;
window.getAppSettings = function() { return appSettings; };
window.setCameraPower = setCameraPower;
window.toggleCameraPower = toggleCameraPower;

// ===== НОВЫЕ ФУНКЦИИ ДЛЯ ДЕТАЛЬНОЙ КАРТОЧКИ =====
window.renderNodeDetails = renderNodeDetails;
window.registerNodeDetailTab = registerNodeDetailTab;
window.switchNodeDetailTab = switchNodeDetailTab;
window.toggleNodeActionsMenu = toggleNodeActionsMenu;
window.copyNodeId = copyNodeId;
window.copyCoordinates = copyCoordinates;
window.setNodeAsReference = setNodeAsReference;
window.refreshNodeMetrics = refreshNodeMetrics;
window.viewTelemetryHistory = viewTelemetryHistory;

// Экспортируем переменные (если нужно)
window.chatListCache = chatListCache;
window.currentChatId = currentChatId;
window.nodeCache = nodeCache;

console.log('[EXPORT] Все функции экспортированы в window');

document.addEventListener('input', event => {
    if (event.target.closest('.reference-location-card')) {
        updateReferenceLocationSaveButton();
    }
});

document.addEventListener('change', event => {
    if (event.target.closest('.reference-location-card')) {
        updateReferenceLocationSaveButton();
    }
});
// Install delegated node-card selection after the DOM is available.
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installNodeCardClickHandler, { once: true });
} else {
    installNodeCardClickHandler();
}

window.updateBatteryCapacitySetting = updateBatteryCapacitySetting;

window.openEmbeddedNodeMap = openEmbeddedNodeMap;
window.fitMeshMapToNodes = fitMeshMapToNodes;
window.renderMeshMap = renderMeshMap;

// ============================================================
// RADIO CONNECTION ACTIONS (Node Manager -> Connection card)
// ============================================================

async function releaseRadioConnection() {
    const confirmed = window.confirm(window.I18N.t('settings.release_radio_confirm'));

    if (!confirmed) return;

    try {
        const response = await fetch('/api/radio_connection/release', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || data.message || window.I18N.t('settings.unable_to_release_radio'));
        }

        showToast(window.I18N.t('settings.radio_released_success'), 'success');
    } catch (error) {
        showToast(window.I18N.t('settings.unable_to_release_radio_reason', { reason: error.message }), 'error');
    }
}

async function reconnectRadioConnection() {
    try {
        const response = await fetch('/api/radio_connection/reconnect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || data.message || window.I18N.t('settings.unable_to_reconnect_radio'));
        }

        showToast(window.I18N.t('settings.radio_reconnect_requested'), 'success');

        // Give the existing listener loop time to reopen the serial port, then
        // force the normal chat/channel refresh to pick up configuration changes.
        window.setTimeout(async () => {
            try {
                lastForcedChannelRefreshAt = 0;
                await loadChatList();
            } catch (error) {
                console.warn('[RADIO MODE] Channel refresh after reconnect failed:', error);
            }
        }, 1800);
    } catch (error) {
        showToast(window.I18N.t('settings.unable_to_reconnect_radio_reason', { reason: error.message }), 'error');
    }
}

window.releaseRadioConnection = releaseRadioConnection;
window.reconnectRadioConnection = reconnectRadioConnection;
