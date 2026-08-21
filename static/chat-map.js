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

