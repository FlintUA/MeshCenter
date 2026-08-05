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

function registerNodeDetailTab(tab) {
    if (!tab || !tab.id || !tab.label || typeof tab.render !== 'function') return false;
    if (NODE_DETAIL_TABS.some(item => item.id === tab.id)) return false;
    NODE_DETAIL_TABS.push(tab);
    resetNodeRenderCache();
    return true;
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
        ? 'Saving…'
        : changed
            ? '💾 Save reference location'
            : '✓ Reference location saved';
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
    placeholder.textContent = 'Select node';
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
                    || 'Manual position'
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
        nameElement.textContent = '📍 Location not configured';
        coordinatesElement.textContent = 'Click to configure';
        locationButton?.classList.add('reference-is-disabled');
        return;
    }

    locationButton?.classList.remove('reference-is-disabled');
    const hasCoordinates = Number.isFinite(reference.latitude) && Number.isFinite(reference.longitude);
    const placeName =
        String(appSettings?.reference_location?.place_name || '').trim()
        || String(reference.name || '').trim()
        || 'Reference location';
    nameElement.textContent = `📍 ${placeName}`;
    coordinatesElement.textContent = hasCoordinates
        ? `${reference.latitude.toFixed(5)} • ${reference.longitude.toFixed(5)}`
        : 'Position unavailable';
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
    const units = appSettings?.units || {};

    document.getElementById('unitTempC')?.classList.toggle('active', units.temperature === 'c');
    document.getElementById('unitTempF')?.classList.toggle('active', units.temperature === 'f');

    document.getElementById('unitPressureHpa')?.classList.toggle('active', units.pressure === 'hpa');
    document.getElementById('unitPressureMmhg')?.classList.toggle('active', units.pressure === 'mmhg');

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
            bearingText: 'Reference'
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
    let bearingText = 'No reference';
    let mapTitle = 'Open node position on map';
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
            bearingText = 'Reference';
            mapTitle = 'Reference position';
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
                  title="Distance from reference location: ${escapeHtml(distanceText)}">${escapeHtml(distanceText)}</span>
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
            showToast('❌ Enter both reference coordinates', 'error');
            return;
        }

        const latitude = Number.parseFloat(latitudeValue);
        const longitude = Number.parseFloat(longitudeValue);

        if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) {
            showToast('❌ Invalid reference latitude', 'error');
            return;
        }

        if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
            showToast('❌ Invalid reference longitude', 'error');
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
            showToast('❌ Select a reference node', 'error');
            return;
        }

        const referenceNode = nodeCache.find(
            node => node.node_id === nodeId
        );

        activeLatitude = Number(referenceNode?.position?.latitude);
        activeLongitude = Number(referenceNode?.position?.longitude);

        if (!Number.isFinite(activeLatitude) || !Number.isFinite(activeLongitude)) {
            showToast('❌ Selected node has no valid position', 'error');
            return;
        }

        referenceLocation.node_id = nodeId;
    }

    referenceLocationSaving = true;
    updateReferenceLocationSaveButton();

    if (statusElement) {
        statusElement.textContent = 'Saving reference location…';
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
            statusElement.textContent = 'Reference location saved';
        }

        showToast('✅ Reference location saved', 'success');
    } catch (error) {
        if (statusElement) {
            statusElement.textContent = `Save failed: ${error.message}`;
        }

        showToast(
            `❌ Unable to save reference location: ${error.message}`,
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
            `✅ Map provider: ${providerName}`,
            'success'
        );

    } catch (error) {
        showToast(
            `❌ Unable to save map provider: ${error.message}`,
            'error'
        );
    }
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
            alert('Unable to save settings: ' + (data.error || 'Unknown error'));
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
        alert('Unable to save settings: ' + error.message);
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

            showToast("Unable to save settings", "error");
            return;

        }

        appSettings = data.settings;

        updateSettingsUi();

        showToast(
            "Listener Auto Recovery updated",
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
        ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : `${date.toLocaleDateString([], { day: '2-digit', month: '2-digit' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
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
        message: String(message || 'Ready'),
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

    text.textContent = displayed.message || 'Ready';
    button.dataset.notificationType = type;
    button.title = notificationCurrent
        ? `${displayed.message} - open notifications`
        : 'Open notifications';

    state.className = `dock-notification-state dock-state-${type}`;

    const hasAction = Boolean(notificationCurrent?.action);
    if (actionButton) {
        actionButton.hidden = !hasAction;
        actionButton.textContent = notificationCurrent?.actionLabel || 'Retry';
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
            showToast('Please choose a PNG, JPEG or WebP image.', 'error');
            input.value = '';
            return;
        }
        if (file.size > 2 * 1024 * 1024) {
            showToast('The node image must be smaller than 2 MB.', 'error');
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
            if (!response.ok || !result.ok) throw new Error(result.error || 'Upload failed.');
            applyIcon(`${result.icon_url}&t=${Date.now()}`);
            localStorage.removeItem('meshcenter.baseNodeAvatar');
            showToast('Node image saved', 'success');
        } catch (error) {
            console.warn('Unable to save node image:', error);
            showToast(error.message || 'Unable to save node image', 'error');
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

    const title = document.getElementById('appTitle');
    if (title) {
        title.addEventListener('click', function() {
            if (this.classList.contains('is-reloading')) return;

            const appName = this.querySelector('.app-name');
            if (appName) {
                appName.classList.add('brand-fade-out');
                setTimeout(() => {
                    appName.textContent = 'Reloading…';
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

// ============================================================
// RENDER CHAT ITEM
// ============================================================
function renderChatItem(chat) {
    const isDemo = Boolean(chat.is_demo);
    const isSelected = (chat.id === currentChatId);
    const selectedClass = isSelected ? 'selected' : '';
    const icon = chat.is_channel ? (isDemo ? '🔒' : '📡') : getChatNodeShortName(chat);
    const iconClass = chat.is_channel ? `channel${isDemo ? ' demo' : ''}` : 'dm node-short-name';
    const lastMsg = chat.last_message || 'No messages yet';
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
        ? `showToast('This channel is not configured on the radio yet', 'info')`
        : `openChat('${escapeHtml(chat.id)}', '${escapeHtml(chat.name)}', '${escapeHtml(chat.type)}', 'chat')`;
    const demoClass = isDemo ? 'demo-channel' : '';

    return `
        <div class="chat-item ${hasUnread} ${selectedClass} ${demoClass}" data-chat-id="${escapeHtml(chat.id)}" onclick="${clickHandler}" ${isDemo ? 'title="Channel is not configured on the radio"' : ''}>
            <div class="chat-icon ${iconClass}">${icon}</div>
            <div class="chat-info">
                <div class="chat-name">${ignored}${favorite}${escapeHtml(chat.name)}</div>
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
            if (chatTitle) chatTitle.textContent = totalUnreadCount > 0 ? `💬 Chats (${totalUnreadCount})` : '💬 Chats';
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
            : '<div class="loading">📡 No configured channels</div>';

        // If the active radio channel was removed externally, leave the stale
        // conversation and switch to the first channel that still exists.
        if (currentChatType === 'channel' && currentChatId) {
            const activeChannelStillExists = channels.some(channel => channel.id === currentChatId);
            if (!activeChannelStillExists) {
                const fallbackChannel = channels[0] || null;
                showToast('The selected channel was removed from the radio', 'info');

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
            : '<div class="loading">💬 No direct messages yet</div>';

        flushPendingSynchronizedScroll();
    } catch (error) {
        console.error('[CHAT] Error:', error);
        const message = error.name === 'AbortError' ? 'Request timeout' : error.message;
        channelContainer.innerHTML = `<div class="loading" style="color:#ff9800;">⚠️ ${escapeHtml(message)}</div>`;
        dmContainer.innerHTML = '<div class="loading">Direct messages unavailable</div>';
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

        if (statusEl && statusEl.innerHTML !== '🔴 Error loading - refresh page') {
            statusEl.innerHTML = '🟢 Mesh online';
        }
        
        const allNodes = nodeCache;
        const ignoredNodes = allNodes.filter(n => n.ignored);
        const favoriteNodes = allNodes.filter(n => n.favorite);
        
        const ignoredCountEl = document.getElementById('ignoredCount');
        if (ignoredCountEl) {
            ignoredCountEl.textContent = ignoredNodes.length + ' ignored';
        }
        
        const favoritesCountEl = document.getElementById('favoritesCount');
        if (favoritesCountEl) {
            favoritesCountEl.textContent = favoriteNodes.length + ' favorites';
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
            nodeCountEl.innerHTML = '🖥️ Nodes [' + totalDisplay + ']';
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
            let message = '🔍 No nodes found';
            if (showFavorites && showIgnored) {
                message = '⚑ No favorite ignored nodes found';
            } else if (showFavorites) {
                message = '⚑ No favorite nodes found';
            } else if (showIgnored) {
                message = '🚫 No ignored nodes found';
            }
            nodesList.innerHTML = `<div class="loading" style="padding: 16px;">${message}</div>`;
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
        || 'Unknown'
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
        || 'Unknown'
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
              title="Signal quality: ${level}/7"
              aria-label="Signal quality ${level} of 7">
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
    let statusText = 'Online';
    
    if (node && node.age) {
        const age = node.age;
        if (age.includes('h') || age.includes('day') || (age.includes('min') && parseInt(age) > 10)) {
            statusIcon = '🟡';
            statusText = 'Away';
        }
        if (age.includes('day') || (age.includes('h') && parseInt(age) > 24)) {
            statusIcon = '🔴';
            statusText = 'Radio Offline';
        }
    }
    
    const shortId = currentChatId ? currentChatId.slice(-4) : '';
    titleEl.innerHTML = `${statusIcon} ${currentChatName} <span style="font-size:12px;font-weight:400;color:#888;margin-left:6px;">${shortId}</span>`;
    subtitleEl.textContent = `Direct Message • ${statusText}`;
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

// ============================================================
// UPDATE CHAT HEADER (NEW)
// ============================================================
function updateChatHeader() {
    const titleEl = document.getElementById('chatTitle');
    const subtitleEl = document.getElementById('chatSubtitle');
    if (!titleEl) return;

    if (!currentChatId) {
        titleEl.textContent = '💬 Chats';
        if (subtitleEl) {
            subtitleEl.textContent = 'Select a chat to view messages';
            subtitleEl.style.color = '';
        }
        return;
    }

    if (currentChatType === 'channel') {
        titleEl.textContent = '📡 ' + currentChatName;
        if (subtitleEl) {
            subtitleEl.textContent = 'Channel • All messages are broadcast';
            subtitleEl.style.color = '#1a73e8';
        }
    } else {
        titleEl.textContent = '💬 ' + currentChatName;
        if (subtitleEl) {
            subtitleEl.textContent = 'Direct Message';
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
        container.innerHTML = '<div class="loading">⏳ Loading messages...</div>';
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
        input.placeholder = chatType === 'channel' ? 'Type a message to channel...' : `Message ${chatName}...`;
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
        container.innerHTML = '<div class="loading">💬 Select a chat from the list</div>';
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
        showMessageActionStatus('Message copied', 'success');
    } catch (error) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();

        try {
            document.execCommand('copy');
            showMessageActionStatus('Message copied', 'success');
        } catch (fallbackError) {
            console.error('[MESSAGE ACTIONS] Copy failed:', fallbackError);
            showMessageActionStatus('Could not copy message', 'error');
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

    sender.textContent = `Reply to ${activeReply.sender || 'Unknown'}`;
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
        return 'System';
    }
    return messageBelongsToActiveRadio(message) ? 'Sent' : 'Received';
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
        ['Sender', message.sender || 'Unknown'],
        ['Node ID', message.node_id || '—'],
        ['Chat', message.chat_name || currentChatName || message.chat_id || '—'],
        ['Chat type', message.chat_type || currentChatType || '—'],
        ['Direction', messageDirectionLabel(message)],
        ['Time', message.time || '—'],
        ['Message ID', message.id || '—'],
        ['Packet ID', message.packet_id ?? '—']
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
            ? `Delete this message locally?\n\n“${preview.slice(0, 140)}${preview.length > 140 ? '…' : ''}”`
            : 'Delete this message locally?';
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
        button.textContent = 'Deleting…';
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
            throw new Error(data.error || `Delete failed (HTTP ${response.status})`);
        }

        invalidateCache(currentChatId);
        lastRenderedSignature[currentChatId] = null;
        closeConfirmDeleteMessage();
        messageActionTarget = null;
        await loadChatMessages(currentChatId);
        loadChatList();
        showMessageActionStatus('Message deleted locally', 'success');
    } catch (error) {
        console.error('[MESSAGE ACTIONS] Delete failed:', error);
        // The modal stays open on failure by design (so the user can see
        // what went wrong and decide whether to retry or cancel), so the
        // error has to be shown *inside* it - the status dock this used to
        // rely on exclusively sits behind the modal overlay and is
        // invisible while it's open, which made failures look like the
        // dialog had simply frozen.
        if (errorEl) {
            errorEl.textContent = error.message || 'Could not delete message';
            errorEl.style.display = 'block';
        }
        showMessageActionStatus(error.message || 'Could not delete message', 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = 'Delete';
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
            showMessageActionStatus('Original message is not available in this chat', 'error');
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
        const chatName = currentChatName || chatId;
        container.innerHTML = `<div class="loading">💬 No messages yet with ${escapeHtml(chatName)}. Send the first one!</div>`;
    } else {
        container.innerHTML = messages.map(msg => {
            // A transmitted record is outgoing only for the radio profile
            // that actually sent it.  Messages from another saved local radio
            // are rendered as received after a profile switch.
            const isMe = messageBelongsToActiveRadio(msg);
            const isSystem = msg.kind === 'system' || msg.sender === 'SYSTEM ERROR';
            const sender = escapeHtml(msg.sender || 'Unknown');
            const text = escapeHtml(msg.text || '');
            const time = escapeHtml(msg.time || '');

            const messageId = escapeHtml(String(msg.id || ''));
            const reply = msg.reply_to && typeof msg.reply_to === 'object' ? msg.reply_to : null;
            const replyBlock = reply ? `
                <button type="button" class="message-reply-quote" data-reply-message-id="${escapeHtml(String(reply.id || ''))}" title="Referenced message">
                    <span class="message-reply-label">↪ ${escapeHtml(String(reply.sender || 'Unknown'))}</span>
                    <span class="message-reply-text">${escapeHtml(String(reply.text || ''))}</span>
                </button>
            ` : '';
            const actionsButton = msg.id ? `
                <button type="button"
                        class="message-actions-trigger"
                        data-message-id="${messageId}"
                        title="Message actions"
                        aria-label="Message actions"
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
            // (pending -> sent / failed) independently of the request.
            let statusBadge = '';
            if (isMe) {
                if (msg.status === 'pending') {
                    statusBadge = '<span class="message-status pending" title="Sending…">⏳</span>';
                } else if (msg.status === 'failed') {
                    const errText = escapeHtml(String(msg.error || 'Send failed'));
                    statusBadge = `
                        <span class="message-status failed" title="${errText}">⚠️
                            <button type="button" class="message-retry-btn" data-retry-message-id="${messageId}">Retry</button>
                        </span>
                    `;
                } else {
                    statusBadge = '<span class="message-status sent" title="Sent">✓</span>';
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
                '<div class="loading">⚠️ Error loading messages</div>';
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

        const where = msg.chat_name || (msg.chat_type === 'dm' ? 'direct message' : 'channel');
        const preview = String(msg.text || '').trim().slice(0, 60);
        const reason = msg.error || 'send failed';

        if (typeof showToast === 'function') {
            showToast(
                `Message not delivered in ${where}: ${reason}${preview ? ` — "${preview}"` : ''}`,
                'error'
            );
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
    return text || 'Me';
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
        time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
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
        tempMsg.error = (error && error.message) ? error.message : 'Network error';
        renderChatIfActive(chatId);

        if (typeof showToast === 'function') {
            const preview = String(text || '').trim().slice(0, 60);
            showToast(
                `Message not delivered in ${chatName || (chatType === 'dm' ? 'direct message' : 'channel')}: ${tempMsg.error}${preview ? ` — "${preview}"` : ''}`,
                'error'
            );
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
    // timeout, etc). Resend using the stored copy.
    const chatId = currentChatId;
    const cachedMessages = (messageCache[chatId] && messageCache[chatId].messages) || [];
    const serverMsg = cachedMessages.find(m => String(m.id) === String(messageId));
    if (serverMsg) {
        submitOutgoingMessage(
            chatId,
            serverMsg.chat_type,
            serverMsg.chat_name,
            serverMsg.text,
            serverMsg.reply_to || null
        );
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
                if (!confirm(`⚠️ Node "${currentChatName}" is ignored. Send message anyway?`)) {
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
        text.textContent = `Delete chat with "${chatName}"? This action cannot be undone.`;
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
            alert('Failed to delete chat: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error deleting chat:', error);
        alert('Network error');
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
        text.textContent = `Clear all messages in "${chatName}"? This action cannot be undone.`;
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
            alert('Failed to clear chat: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error clearing chat:', error);
        alert('Network error');
    }
}

function clearCurrentChat() {
    if (!currentChatId) return;
    closeChatActions();
    showConfirmClear(currentChatName, currentChatId);
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
            showToast(data.ignored ? 'Node ignored' : 'Node restored', data.ignored ? 'warning' : 'success');
            
            if (currentChatId === nodeId) {
                // Avoid duplicating the bottom notification with a chat banner.
                hideIgnoredBanner();
                invalidateCache(nodeId);
                lastMessagesSignature = '';
                await loadChatMessages(nodeId);
            }
        } else {
            const error = await response.json();
            alert('Failed to toggle ignore: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error toggling ignore:', error);
        alert('Network error');
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
            showToast(data.favorite ? 'Node added to favorites' : 'Node removed from favorites', 'success');
        } else {
            const error = await response.json();
            alert('Failed to toggle favorite: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error toggling favorite:', error);
        alert('Network error');
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

        return updatedDate.toLocaleString([], {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
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
    return new Date(seconds * 1000).toLocaleString();
}

function formatWaypointExpiryDetails(expireAt) {
    const seconds = Number(expireAt);
    if (!Number.isFinite(seconds) || seconds <= 0) {
        return { relative:'No expiration', absolute:'', expired:false };
    }

    let remaining = Math.floor(seconds - Date.now() / 1000);
    const expiresDate = new Date(seconds * 1000);
    const absoluteTime = expiresDate.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
    const absoluteDateTime = expiresDate.toLocaleString([], {
        day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'
    });

    if (remaining <= 0) {
        return { relative:'Expired', absolute:absoluteDateTime, expired:true };
    }

    const days = Math.floor(remaining / 86400);
    remaining %= 86400;
    const hours = Math.floor(remaining / 3600);
    remaining %= 3600;
    const minutes = Math.floor(remaining / 60);

    if (days > 0) {
        const tail = hours > 0 ? ` ${hours} h` : (minutes > 0 ? ` ${minutes} min` : '');
        return { relative:`in ${days} d${tail}`, absolute:absoluteDateTime, expired:false };
    }
    if (hours > 0) {
        return {
            relative:`in ${hours} h${minutes > 0 ? ` ${minutes} min` : ''}`,
            absolute:absoluteTime,
            expired:false
        };
    }
    return { relative:`in ${Math.max(1, minutes)} min`, absolute:absoluteTime, expired:false };
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
    const sender = waypoint?.sender_name || waypoint?.sender_id || 'Unknown';
    const description = waypoint?.description || 'No description';
    const channel = Number.isFinite(Number(waypoint?.channel_index)) ? Number(waypoint.channel_index) : '--';
    const expired = waypoint?.is_active === false || formatWaypointExpiryDetails(waypoint?.expire_at).expired;
    return `
        <div class="map-popup-name waypoint-popup-name">${escapeHtml(waypointIconCharacter(waypoint?.icon))} ${escapeHtml(name)}</div>
        <div class="map-popup-subtitle">${escapeHtml(description)}</div>
        <div class="map-popup-grid">
            <span>Status</span><strong class="${expired ? 'waypoint-status-expired' : 'waypoint-status-active'}">${expired ? 'Expired' : 'Active'}</strong>
            <span>Sender</span><strong>${escapeHtml(sender)}</strong>
            <span>Distance</span><strong>${escapeHtml(nav.distanceText)}</strong>
            <span>Bearing</span><strong>${escapeHtml(nav.bearingText)}</strong>
            <span>Channel</span><strong>${escapeHtml(channel)}</strong>
            <span>Received</span><strong>${escapeHtml(formatWaypointTime(waypoint?.received_at))}</strong>
            <span>Expires</span><strong>${waypointExpiryHtml(waypoint?.expire_at)}</strong>
            <span>Coordinates</span><strong>${Number.isFinite(lat) ? lat.toFixed(6) : '--'}, ${Number.isFinite(lon) ? lon.toFixed(6) : '--'}</strong>
        </div>
        <div class="map-popup-actions">
            <button class="map-popup-primary-btn" type="button" onclick="centerMapOnWaypoint('${escapeJsString(waypoint?.waypoint_id)}')">⌖ Center</button>
            <button class="map-popup-action-btn" type="button" onclick="openExternalNodeMap('${Number.isFinite(lat) ? lat : ''}', '${Number.isFinite(lon) ? lon : ''}')">↗ Navigate</button>
            <button class="map-popup-action-btn" type="button" title="Copy coordinates to clipboard" onclick="copyCoordinates('${Number.isFinite(lat) ? lat : ''}', '${Number.isFinite(lon) ? lon : ''}')">📋 Coordinates</button>
            <button class="map-popup-action-btn danger" type="button" onclick="setWaypointHidden('${escapeJsString(waypoint?.waypoint_id)}', true)">🙈 Hide</button>
            <button class="map-popup-action-btn map-popup-close-btn" type="button" onclick="closeWaypointPopup()">✕ Close</button>
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
        container.innerHTML = `<div class="waypoint-tools-empty">${waypointToolsShowExpired ? 'No saved waypoints' : 'No active waypoints'}</div>`;
        updateWaypointBulkControls();
        return;
    }
    container.innerHTML = items.map(item => {
        const id = String(item?.waypoint_id);
        const name = item?.name || `Waypoint ${id}`;
        const sender = item?.sender_name || item?.sender_id || 'Unknown';
        const expiry = formatWaypointExpiryDetails(item?.expire_at);
        const hidden = Boolean(item?.is_hidden);
        const expired = item?.is_active === false || expiry.expired;
        const pending = waypointVisibilityPending.has(id);
        const selected = id === String(selectedWaypointId || '');
        return `<div class="waypoint-tools-item ${expired ? 'is-expired' : ''} ${hidden ? 'is-hidden' : ''} ${pending ? 'is-pending' : ''} ${selected ? 'is-selected' : ''}" data-waypoint-id="${escapeHtml(id)}">` +
            `<label class="waypoint-tools-select" title="Select"><input type="checkbox" ${waypointToolsSelectedIds.has(id) ? 'checked' : ''} onchange="toggleWaypointSelection('${escapeJsString(id)}', this.checked)"></label>` +
            `<button type="button" class="waypoint-tools-main" onclick="showWaypointOnMap('${escapeJsString(id)}')" title="Open waypoint on map">` +
            `<span class="waypoint-tools-icon">${escapeHtml(waypointIconCharacter(item?.icon))}</span>` +
            `<span class="waypoint-tools-copy"><strong>${escapeHtml(name)}</strong>` +
            `<small>${escapeHtml(sender)} · ${escapeHtml(expired ? 'Expired' : expiry.relative)}</small></span></button>` +
            `<button type="button" class="waypoint-tools-visibility" title="${hidden ? 'Show waypoint' : 'Hide waypoint'}" ` +
            `onclick="setWaypointHidden('${escapeJsString(id)}', ${hidden ? 'false' : 'true'})" ${pending ? 'disabled' : ''}>${pending ? '…' : (hidden ? '👁' : '🙈')}</button>` +
            `<button type="button" class="waypoint-tools-delete" title="Delete locally" onclick="deleteWaypoint('${escapeJsString(id)}')">🗑</button></div>`;
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
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || 'Could not load waypoints');
        waypointToolsItems = Array.isArray(payload.waypoints) ? payload.waypoints : [];
        renderWaypointToolsList();
    } catch (error) {
        showToast(error.message || 'Could not load waypoints', 'error');
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
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || 'Waypoint update failed');
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
        if (!options.silent) showToast(hidden ? 'Waypoint hidden locally' : 'Waypoint is visible again', 'success');
        return true;
    } catch (error) {
        if (toolsIndex >= 0 && previousTools) waypointToolsItems[toolsIndex] = previousTools;
        if (previousMap) meshMapWaypoints = [previousMap, ...meshMapWaypoints.filter(item => String(item?.waypoint_id) !== id)];
        else meshMapWaypoints = meshMapWaypoints.filter(item => String(item?.waypoint_id) !== id);
        showToast(error.message || 'Waypoint update failed', 'error');
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
    if (!window.confirm(`Delete "${name}" from MeshCenter local storage?`)) return;
    try {
        const response = await fetch(`/api/waypoints/${encodeURIComponent(id)}`, { method:'DELETE' });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || 'Waypoint delete failed');
        waypointToolsItems = waypointToolsItems.filter(row => String(row?.waypoint_id) !== id);
        meshMapWaypoints = meshMapWaypoints.filter(row => String(row?.waypoint_id) !== id);
        waypointToolsSelectedIds.delete(id);
        renderWaypointToolsList();
        if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        showToast('Waypoint deleted locally', 'success');
    } catch (error) {
        showToast(error.message || 'Waypoint delete failed', 'error');
    }
}

async function deleteSelectedWaypoints() {
    const ids = [...waypointToolsSelectedIds];
    if (!ids.length) return;
    if (!window.confirm(`Delete ${ids.length} selected waypoint${ids.length === 1 ? '' : 's'} from local storage?`)) return;
    try {
        const response = await fetch('/api/waypoints/delete', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({ waypoint_ids: ids.map(Number) })
        });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || 'Waypoint delete failed');
        const idSet = new Set(ids);
        waypointToolsItems = waypointToolsItems.filter(row => !idSet.has(String(row?.waypoint_id)));
        meshMapWaypoints = meshMapWaypoints.filter(row => !idSet.has(String(row?.waypoint_id)));
        waypointToolsSelectedIds.clear();
        renderWaypointToolsList();
        if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        showToast(`${payload.deleted || ids.length} waypoint(s) deleted locally`, 'success');
    } catch (error) {
        showToast(error.message || 'Waypoint delete failed', 'error');
    }
}

async function deleteAllWaypoints() {
    if (!window.confirm('Delete ALL saved waypoints from MeshCenter local storage? This cannot be undone.')) return;
    try {
        const response = await fetch('/api/waypoints', { method:'DELETE' });
        const payload = await response.json();
        if (!response.ok || !payload?.ok) throw new Error(payload?.error || 'Waypoint cleanup failed');
        waypointToolsItems = [];
        meshMapWaypoints = [];
        waypointToolsSelectedIds.clear();
        renderWaypointToolsList();
        if (meshMap) renderMeshMap(meshMapTargetNodeId, { preserveViewport:true, openPopup:false });
        showToast(`${payload.deleted || 0} waypoint(s) deleted locally`, 'success');
    } catch (error) {
        showToast(error.message || 'Waypoint cleanup failed', 'error');
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
        const label = `${channel.name} [${channel.index}]`;
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
            actionLabel: 'Retry',
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
        `Waypoint sent: ${operation.payload.name}`,
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
            ? `Retrying waypoint: ${operation.payload.name}`
            : `Sending waypoint: ${operation.payload.name}`,
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
            throw new Error(result?.error || 'Waypoint send failed');
        }

        await applySuccessfulWaypointResult(result, operation);
    } catch (error) {
        console.error('[WAYPOINT] Send failed:', error);
        showWaypointActionStatus(
            error?.message || 'Waypoint was not sent',
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
        showToast('Enter a waypoint name', 'warning');
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
        showToast('Waypoint is no longer available', 'error');
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
                    const sender = item?.sender_name || item?.sender_id || 'Unknown';
                    showToast(`📍 Waypoint received: ${item?.name || 'Unnamed'} · ${sender}`, 'info');
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
        showToast('Waypoint coordinates are unavailable', 'error');
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
    const source = node?.position?.source || 'Radio';
    const updated = formatNodePositionUpdated(node?.position);
    const age = node?.age || node?.last_seen || '--';
    const nodeId = escapeJsString(node?.node_id || '');
    const nodeName = escapeJsString(getNodeDisplayName(node));

    return `
        <div class="map-popup-name">${escapeHtml(getNodeDisplayName(node))}</div>
        <div class="map-popup-grid">
            <span>Distance</span><strong>${escapeHtml(navigation.distanceText)}</strong>
            <span>Bearing</span><strong>${escapeHtml(navigation.bearingText)}</strong>
            <span>Source</span><strong>${escapeHtml(source)}</strong>
            <span>Last update</span><strong>${escapeHtml(updated || age)}</strong>
        </div>
        <div class="map-popup-actions">
            <button class="map-popup-primary-btn" onclick="openChat('${nodeId}', '${nodeName}', 'dm')">💬 Message</button>
            <button class="map-popup-action-btn" onclick="runNodeTool('request_telemetry', '${nodeId}', '${nodeName}', this)">📊 Telemetry</button>
            <button class="map-popup-action-btn" onclick="runNodeTool('request_position', '${nodeId}', '${nodeName}', this)">📍 Position</button>
            <button class="map-popup-action-btn" onclick="setNodeAsReference('${nodeId}')">📌 Reference</button>
            <button class="map-popup-action-btn" onclick="copyCoordinates('${pos ? pos.latitude : ''}', '${pos ? pos.longitude : ''}')">📋 Coordinates</button>
        </div>
    `;
}

function renderMeshMap(targetNodeId = null, options = {}) {
    const map = ensureMeshMap();
    if (!map) {
        showToast('Map library could not be loaded', 'error');
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
        meshMapReferenceMarker = L.marker([reference.latitude, reference.longitude], {
            icon: createMeshMapIcon('reference'),
            title: reference.name || 'Reference location',
            zIndexOffset: 700
        }).addTo(map).bindPopup(`<div class="map-popup-name">${escapeHtml(reference.name || 'Reference location')}</div><div class="map-popup-grid"><span>Type</span><strong>Reference</strong><span>Latitude</span><strong>${reference.latitude.toFixed(6)}</strong><span>Longitude</span><strong>${reference.longitude.toFixed(6)}</strong></div>`, { className:'meshcenter-map-popup' });
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
        const waypointSummary = expiredWaypointCount > 0
            ? `${visibleWaypoints.length} waypoints (${activeWaypointCount} active, ${expiredWaypointCount} expired)`
            : `${visibleWaypoints.length} waypoint${visibleWaypoints.length === 1 ? '' : 's'}`;
        countEl.textContent = `${positionedNodes.length} node${positionedNodes.length === 1 ? '' : 's'} · ${waypointSummary}`;
    }

const targetNode = positionedNodes.find(
    node => String(node.node_id) === String(meshMapTargetNodeId)
);

const targetPos = getNodePosition(targetNode);

const title = document.getElementById("mapViewTitle");
const subtitle = document.getElementById("mapViewSubtitle");

if (targetNode && targetPos) {
    if (title)
        title.textContent = `🗺 Map — ${getNodeDisplayName(targetNode)}`;
    if (subtitle) {
        const nav = getNodeDistanceAndBearing(
            targetPos.latitude,
            targetPos.longitude
        );
        subtitle.textContent =
            `${nav.distanceText} · ${nav.bearingText} · ` +
            `${targetNode.position?.source || "Radio"}`;
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
        if (title) title.textContent = '🗺 Map';
        if (subtitle) subtitle.textContent = 'Known Meshtastic nodes and waypoints';

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
        showToast('Position coordinates are unavailable', 'error');
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
        showToast('Position coordinates are unavailable', 'error');
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
        details.innerHTML = 'Select a node below';
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
    const lastSeen = node.age || 'Never';
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
                    <span class="node-detail-activity ${getNodeActivityPresentation(node).activityClass}" title="Activity status" aria-hidden="true"></span>
                    <span class="node-detail-name">${escapeHtml(displayName)}</span>
                </div>
                <button type="button" class="node-detail-close" onclick="closeNodeDetails()" title="Close node details" aria-label="Close node details">×</button>
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
                    <button type="button" class="node-detail-id" onclick="copyNodeId('${escapeHtml(nodeId)}')" title="Click to copy Node ID" aria-label="Copy Node ID">${escapeHtml(truncateText(nodeId, 12))}</button>
                </div>
            </div>

            <!-- Третья строка: статус + segmented actions -->
            <div class="node-detail-status-row">
                <div class="node-detail-status-copy">
                    <span class="node-detail-last-seen">🕒 ${escapeHtml(lastSeen)}</span>
                    <span class="node-detail-hops">Hops: ${escapeHtml(hops)}</span>
                </div>
                <div class="node-detail-header-actions node-detail-action-group">
                    <button type="button"
                            class="node-detail-state-btn node-detail-favorite-btn ${isFavorite ? 'active' : ''}"
                            onclick="toggleFavorite('${escapeHtml(nodeId)}')"
                            title="${isFavorite ? 'Remove from favorites' : 'Add to favorites'}"
                            aria-label="${isFavorite ? 'Remove node from favorites' : 'Add node to favorites'}"
                            aria-pressed="${isFavorite ? 'true' : 'false'}">
                        <span aria-hidden="true">⚑</span>
                    </button>

                    <button type="button"
                            class="node-detail-state-btn node-detail-ignore-btn ${isIgnored ? 'active' : ''}"
                            onclick="toggleIgnore('${escapeHtml(nodeId)}')"
                            title="${isIgnored ? 'Stop ignoring node' : 'Ignore node'}"
                            aria-label="${isIgnored ? 'Stop ignoring node' : 'Ignore node'}"
                            aria-pressed="${isIgnored ? 'true' : 'false'}">
                        <span aria-hidden="true">🚫</span>
                    </button>

                    <button class="node-detail-actions-btn"
                            onclick="toggleNodeActionsMenu(event)"
                            aria-label="More node actions"
                            title="More actions">
                        ⋮
                    </button>
                </div>
            </div>

            <!-- Вкладки -->
            <div class="node-detail-tabs" role="tablist" aria-label="Node details">
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
            <button onclick="openChat('${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', 'dm')">📨 Send message</button>
            <button onclick="runNodeTool('request_position', '${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', this)">📍 Request position</button>
            <button onclick="runNodeTool('request_telemetry', '${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', this)">📊 Request telemetry</button>
            <button onclick="runNodeTool('traceroute', '${escapeHtml(nodeId)}', '${escapeHtml(displayName)}', this)">🔍 Traceroute</button>
            <button onclick="setNodeAsReference('${escapeHtml(nodeId)}')">📍 Set as reference</button>
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
                    <span class="tile-label">Hops</span>
                    <span class="tile-value">${escapeHtml(hops)}</span>
                </div>
                <div class="tile">
                    <span class="tile-label">Distance</span>
                    <span class="tile-value">${escapeHtml(distanceText)}</span>
                </div>
                <div class="tile">
                    <span class="tile-label">Bearing</span>
                    <span class="tile-value">${escapeHtml(bearingText)}</span>
                </div>
                ${battery !== '--' ? `
                <div class="tile">
                    <span class="tile-label">Battery</span>
                    <span class="tile-value">${escapeHtml(battery)}%</span>
                </div>` : ''}
                ${voltage !== '--' ? `
                <div class="tile">
                    <span class="tile-label">Voltage</span>
                    <span class="tile-value">${escapeHtml(voltage)} V</span>
                </div>` : ''}
            </div>
            ${lastText ? `
            <div class="node-detail-last-msg">
                <span class="last-msg-label">Last message</span>
                <span class="last-msg-text">${escapeHtml(truncateText(lastText, 80))}</span>
                <span class="last-msg-time">${escapeHtml(node.last_time || '')}</span>
            </div>` : ''}
            <div class="node-detail-quick-actions">
                <button class="quick-action" onclick="openChat('${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', 'dm')">💬 Message</button>
                <button class="quick-action" onclick="openExternalNodeMap(${node.position?.latitude || 0}, ${node.position?.longitude || 0})" ${!hasPosition ? 'disabled' : ''}>🗺 External Map</button>
                <button class="quick-action" onclick="toggleNodeActionsMenu(event)">⚡ More</button>
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
    const lastSeen = node.age || 'Never';
    const relay = node.relay_node || '--';
    const signalQuality = formatSignalQualityLabel(node.signal_quality);

    // Простая история (заглушка)
    let historyHtml = '<div class="radio-history-placeholder">Signal history is not available yet</div>';

    return `
        <div class="node-detail-radio">
            <div class="radio-params">
                <div class="radio-param"><span class="label">Signal quality</span><span class="value">${escapeHtml(signalQuality)}</span></div>
                <div class="radio-param"><span class="label">RSSI</span><span class="value">${escapeHtml(rssi)} dBm</span></div>
                <div class="radio-param"><span class="label">SNR</span><span class="value">${escapeHtml(snr)} dB</span></div>
                <div class="radio-param"><span class="label">Hops</span><span class="value">${escapeHtml(hops)}</span></div>
                <div class="radio-param"><span class="label">Last relay</span><span class="value">${escapeHtml(relay)}</span></div>
                <div class="radio-param"><span class="label">Last heard</span><span class="value">${escapeHtml(lastSeen)}</span></div>
            </div>
            <div class="radio-history">
                <div class="radio-history-header">
                    <span>Signal history</span>
                    <span class="radio-history-range" title="Time ranges will be enabled when history storage is added">30m · 1h · 6h · 24h</span>
                </div>
                ${historyHtml}
            </div>
            <div class="radio-actions">
                <button class="radio-action" onclick="runNodeTool('traceroute', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">🔍 Run traceroute</button>
                <button class="radio-action" onclick="refreshNodeMetrics('${escapeHtml(node.node_id)}')">↻ Refresh</button>
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
    const source = pos.source || 'Radio';
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
        return ref ? ref.name : 'Not set';
    })();

    return `
        <div class="node-detail-position">
            ${hasPosition ? `
            <div class="position-coords">
                <div class="coord"><span class="label">Latitude</span><span class="value">${escapeHtml(lat)}</span></div>
                <div class="coord"><span class="label">Longitude</span><span class="value">${escapeHtml(lon)}</span></div>
                <div class="coord"><span class="label">Altitude</span><span class="value">${escapeHtml(alt)}</span></div>
                <div class="coord"><span class="label">Distance</span><span class="value">${escapeHtml(distanceText)}</span></div>
                <div class="coord"><span class="label">Bearing</span><span class="value">${escapeHtml(bearingText)}</span></div>
                <div class="coord"><span class="label">Position age</span><span class="value">${escapeHtml(age)}</span></div>
                <div class="coord"><span class="label">Source</span><span class="value">${escapeHtml(source)}</span></div>
                <div class="coord"><span class="label">Precision</span><span class="value">${escapeHtml(precision)}</span></div>
            </div>
            <div class="position-actions">
                <button onclick='openNodeMap(${pos.latitude}, ${pos.longitude}, ${JSON.stringify(String(node.node_id || ""))})'>🗺 Locate on Map</button>
                <button onclick="copyCoordinates('${pos.latitude}', '${pos.longitude}')">📋 Copy coordinates</button>
                <button onclick="setNodeAsReference('${escapeHtml(node.node_id)}')">📍 Set as reference</button>
                <button onclick="runNodeTool('request_position', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">📡 Request new position</button>
            </div>
            <div class="position-reference">Reference: ${escapeHtml(referenceName)}</div>
            ` : `
            <div class="position-no-data">
                <span>📍 No known position</span>
                <button onclick="runNodeTool('request_position', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">Request position</button>
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
        hasBattery ? `<div><span class="label">Battery</span><span class="value">${escapeHtml(formatBatteryPercent(node.battery_level))}%</span></div>` : '',
        hasDeviceVoltage ? `<div><span class="label">Voltage</span><span class="value">${escapeHtml(formatTelemetryNumber(node.voltage, 3))} V</span></div>` : '',
        hasChannelUtil ? `<div><span class="label">Channel utilization</span><span class="value">${escapeHtml(formatTelemetryNumber(node.channel_utilization, 2))}%</span></div>` : '',
        hasAirUtil ? `<div><span class="label">Air utilization TX</span><span class="value">${escapeHtml(formatTelemetryNumber(node.air_util_tx, 2))}%</span></div>` : '',
        hasUptime ? `<div><span class="label">Uptime</span><span class="value">${escapeHtml(formatUptime(node.uptime_seconds))}</span></div>` : ''
    ].filter(Boolean).join('');

    const environmentRows = [
        hasTemperature ? `<div><span class="label">Temperature</span><span class="value">${formatTemperature(node.temperature)}</span></div>` : '',
        hasHumidity ? `<div><span class="label">Humidity</span><span class="value">${escapeHtml(formatTelemetryNumber(node.humidity, 1))}%</span></div>` : '',
        hasPressure ? `<div><span class="label">Pressure</span><span class="value">${formatPressure(node.pressure)}</span></div>` : ''
    ].filter(Boolean).join('');

    const powerRows = [
        hasPowerVoltage ? `<div><span class="label">Voltage</span><span class="value">${escapeHtml(formatTelemetryNumber(node.voltage, 3))} V</span></div>` : '',
        hasCurrent ? `<div><span class="label">Current</span><span class="value">${escapeHtml(formatTelemetryNumber(node.current, 1))} mA</span></div>` : '',
        hasPowerValue ? `<div><span class="label">Power</span><span class="value">${escapeHtml(formatPowerWattsFromMilliwatts(node.power))} W</span></div>` : ''
    ].filter(Boolean).join('');

    return `
        <div class="node-detail-data">
            <div class="data-group">
                <div class="data-group-title">📟 Device</div>
                ${deviceRows ? `<div class="data-grid">${deviceRows}</div>` : '<div class="data-no-data">No device metrics received</div>'}
            </div>
            <div class="data-group">
                <div class="data-group-title">🌡️ Environment</div>
                ${hasEnv ? `<div class="data-grid">${environmentRows}</div>` : '<div class="data-no-data">No environment metrics received</div>'}
            </div>
            <div class="data-group">
                <div class="data-group-title">⚡ Power</div>
                ${hasPower ? `<div class="data-grid">${powerRows}</div>` : '<div class="data-no-data">No power metrics received</div>'}
            </div>
            <div class="data-actions">
                <button onclick="runNodeTool('request_telemetry', '${nodeId}', '${nodeName}', this)">📊 Request telemetry</button>
                ${hasPower ? `<button onclick="viewTelemetryHistory('${nodeId}', 'power')">⚡ Power history</button>` : ''}
                ${hasEnv ? `<button onclick="viewTelemetryHistory('${nodeId}', 'environment')">🌡️ Environment history</button>` : ''}
            </div>
        </div>
    `;
}

function renderLogPane(node) {
    // Сводка
    const summary = {
        first_seen: node.first_seen || '--',
        last_heard: node.age || 'Never',
        last_text: node.last_time || 'Never',
        last_position: node.position?.updated_time || 'Never',
        last_telemetry: node.telemetry_time || 'Never',
        packets: node.packets_received ?? '--',
        messages: node.messages_received ?? '--'
    };


    return `
        <div class="node-detail-log">
            <div class="log-summary">
                <div class="log-summary-item"><span class="label">First seen</span><span class="value">${escapeHtml(summary.first_seen)}</span></div>
                <div class="log-summary-item"><span class="label">Last heard</span><span class="value">${escapeHtml(summary.last_heard)}</span></div>
                <div class="log-summary-item"><span class="label">Last text</span><span class="value">${escapeHtml(summary.last_text)}</span></div>
                <div class="log-summary-item"><span class="label">Last position</span><span class="value">${escapeHtml(summary.last_position)}</span></div>
                <div class="log-summary-item"><span class="label">Last telemetry</span><span class="value">${escapeHtml(summary.last_telemetry)}</span></div>
                <div class="log-summary-item"><span class="label">Packets</span><span class="value">${escapeHtml(summary.packets)}</span></div>
                <div class="log-summary-item"><span class="label">Messages</span><span class="value">${escapeHtml(summary.messages)}</span></div>
            </div>
            <div class="log-events">
                <div class="log-events-title">Event history</div>
                <div class="log-history-placeholder">Detailed node event history is not available yet</div>
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
        showToast('✅ Node ID copied', 'success');
    } catch (error) {
        console.warn('Unable to copy Node ID:', error);
        showToast('❌ Failed to copy Node ID', 'error');
    }
}

async function copyCoordinates(lat, lon) {
    const latitude = Number(lat);
    const longitude = Number(lon);

    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
        showToast('❌ Coordinates unavailable', 'error');
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

        showToast('✅ Coordinates copied', 'success');
    } catch (error) {
        console.warn('[WAYPOINT] Failed to copy coordinates:', error);
        showToast('❌ Failed to copy coordinates', 'error');
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
            showToast('✅ Reference node set', 'success');
            // Перерисовать карточку
            const node = nodeCache.find(n => n.node_id === nodeId);
            if (node) renderNodeDetails(node);
        } else {
            showToast('❌ Failed to set reference', 'error');
        }
    })
    .catch(() => showToast('❌ Network error', 'error'));
}

function refreshNodeMetrics(nodeId) {
    // Просто обновляем данные
    loadMessages();
    showToast('↻ Refreshing local node data', 'info');
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
            ? '<span>⏳ Working...</span>'
            : '<span>🛠 Tools</span><span id="nodeToolsArrow">▾</span>';
    }

    if (radioCommandRunning && toolsMenu) {
        toolsMenu.style.display = 'none';
    }
}

function toggleNodeToolsMenu(forceOpen = null) {
    if (radioCommandRunning && forceOpen !== false) {
        showToast('A radio command is already running', 'info');
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
            ? 'Unknown'
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
                Route information unavailable
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
            ? 'SOURCE'
            : (isLast ? 'DESTINATION' : '');

        const connector = !isLast
            ? `
                <div class="route-chain-connector">
                    <span class="route-chain-line"></span>

                    <span class="route-snr-badge">
                        ${escapeHtml(
                            route.nodes[index + 1].snr || '? dB'
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
                        ? `<span class="route-endpoint-label">${nodeLabel}</span>`
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

    const hopWord = route.hopCount === 1 ? 'hop' : 'hops';

    return `
        <div class="route-chain-meta">
            ${route.hopCount} ${hopWord}
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
                ? `Uptime: ${formatted}`
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
                    title="Close">
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
            'Another radio command is already running',
            'info'
        );
        return;
    }

    closeNodeToolsMenu();
    setNodeToolsBusy(true);

    const originalText = button?.innerHTML || '';

    const toolConfig = {
        traceroute: {
            pendingTitle: '🛰 Traceroute',
            pendingMessage: `Checking route to ${nodeName}...`,
            successToast: `✅ Traceroute completed: ${nodeName}`,
            errorTitle: '❌ Traceroute failed',
            errorToastPrefix: '❌ Traceroute failed'
        },

        request_telemetry: {
            pendingTitle: '📊 Request telemetry',
            pendingMessage: `Requesting telemetry from ${nodeName}...`,
            successToast: `✅ Telemetry request completed: ${nodeName}`,
            errorTitle: '❌ Telemetry request failed',
            errorToastPrefix: '❌ Telemetry request failed'
        },

        request_position: {
            pendingTitle: '📍 Request position',
            pendingMessage: `Requesting position from ${nodeName}...`,
            successToast: `✅ Position request completed: ${nodeName}`,
            errorTitle: '❌ Position request failed',
            errorToastPrefix: '❌ Position request failed'
        }
    };

    const currentTool = toolConfig[action];

    if (!currentTool) {
        showToast('Unsupported Node Tool action', 'error');
        setNodeToolsBusy(false);
        return;
    }    

    if (button) {
        button.disabled = true;
        button.innerHTML = `
            <span>⏳</span>
            <span>Running...</span>
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
                    'Another radio command is already running'
                );
            }

            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            throw requestError;
        }

        if (action === 'traceroute') {
            const route = parseTracerouteOutput(data.output);

            const routeDetails = `
            <div class="route-grid">

                <div class="route-card route-card-forward">
                    <div class="route-card-header">
                        <span class="route-badge route-forward">
                            FORWARD
                        </span>
                    </div>

                    ${renderTracerouteChain(route.forward)}
                </div>

                <div class="route-card route-card-return">
                    <div class="route-card-header">
                        <span class="route-badge route-return">
                            RETURN
                        </span>
                    </div>

                    ${renderTracerouteChain(route.returnRoute)}
                </div>

            </div>
        `;

            renderNodeToolResult(
                nodeId,
                "success",
                `🛰 Traceroute to ${data.node_name || nodeName}`,
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
                `📊 Telemetry request sent to ${data.node_name || nodeName}`,
                'The radio listener is active again and is waiting for the node response.'
            );

            showToast(
                `📡 Telemetry request sent: ${data.node_name || nodeName}`,
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
                            Response received through the MeshCenter listener
                        </div>
                        <div class="telemetry-request-note">
                            Battery: ${formatBatteryPercent(device.battery_level)}% ·
                            Voltage: ${device.voltage ?? '--'} V ·
                            Updated: ${refreshedNode.last_telemetry_time_text || 'just now'}
                        </div>
                    </div>
                `;

                renderNodeToolResult(
                    nodeId,
                    'success',
                    `📊 Telemetry received from ${data.node_name || nodeName}`,
                    '',
                    details
                );
                showToast(
                    `✅ Telemetry received: ${data.node_name || nodeName}`,
                    'success'
                );
            } else {
                renderNodeToolResult(
                    nodeId,
                    'error',
                    '⚠️ No fresh telemetry response',
                    'The request was transmitted, but the node did not provide fresh telemetry before the waiting period ended.'
                );
                showToast(
                    `⚠️ Telemetry request sent, but no fresh response: ${data.node_name || nodeName}`,
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
                            Request completed by Meshtastic CLI
                        </div>

                        <pre>${escapeHtml(rawOutput)}</pre>
                    </div>
                `
                : `
                    <div class="telemetry-request-output">
                        <div class="telemetry-request-status">
                            Position request sent successfully
                        </div>

                        <div class="telemetry-request-note">
                            The response may arrive asynchronously through the listener.
                        </div>
                    </div>
                `;

            renderNodeToolResult(
                nodeId,
                'success',
                `📍 Position from ${data.node_name || nodeName}`,
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

        let successMessage = `✅ Command completed: ${completedName}`;

        if (action === 'traceroute') {
            successMessage = `✅ Traceroute completed: ${completedName}`;
        } else if (action === 'request_telemetry') {
            successMessage = '';
        } else if (action === 'request_position') {
            successMessage = `✅ Position request completed: ${completedName}`;
        }

        if (successMessage) {
            showToast(successMessage, 'success');
        }

    } catch (error) {
        console.error('[NODE TOOLS] Error:', error);

        renderNodeToolResult(
            nodeId,
            'error',
            currentTool.errorTitle,
            error.message
        );

        showToast(error.message, 'error');
        
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
        runtimeEl.textContent = 'Waiting for charge data';
        runtimeEl.title = 'Battery percentage is not available yet';
        return;
    }

    if (!Number.isFinite(averageCurrent) || averageCurrent <= 5) {
        runtimeEl.textContent = 'Waiting for current data';
        runtimeEl.title = 'Current consumption is not available yet';
        return;
    }

    const remainingMah = capacityMah * (percent / 100);
    const runtimeHours = remainingMah / averageCurrent;
    runtimeEl.textContent = formatEstimatedRuntime(runtimeHours);
    runtimeEl.title = `Approximate estimate using ${Math.round(capacityMah)} mAh and ${Math.round(averageCurrent)} mA average current`;
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
        if (!data.ok) throw new Error(data.error || 'Unable to save battery capacity');
        appSettings = data.settings || appSettings;
        updateBatteryRuntime(null, latestBatteryPercent);
        showToast(`Battery capacity set to ${capacity} mAh`, 'success');
    } catch (error) {
        console.error('Error updating battery capacity:', error);
        showToast('Unable to save battery capacity', 'error');
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

            document.getElementById('sensorUpdate').textContent = `Updated ${data.last_update || '--'}`;
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
            uptimeEl.textContent = `Uptime ${uptime}`;
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
        document.documentElement.dataset.theme = resolvedTheme;
        document.documentElement.dataset.themePreference = this.state.theme;
        document.documentElement.style.colorScheme = resolvedTheme;
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
            container.innerHTML = '<div class="loading">No nodes found</div>';
            return;
        }
        
        container.innerHTML = data.nodes.map(node => {
            const statusClass = node.ignored ? 'ignored' : 'normal';
            const statusText = node.ignored ? '🚫 Ignored' : '✅ Normal';

            return `
                <div class="nodes-management-item">
                    <div class="name-wrapper">
                        <span class="name">${escapeHtml(node.name)}</span>
                        <span class="id">${escapeHtml(node.node_id)}</span>
                    </div>
                    <span class="status ${statusClass}">${statusText}</span>
                </div>
            `;
        }).join('');
    } catch (error) {
        console.error('Error loading nodes management:', error);
        container.innerHTML = '<div class="loading">⚠️ Error loading nodes</div>';
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
            showToast('❌ No nodes to export', 'error');
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
        
        showToast(`✅ Exported ${data.nodes.length} nodes to CSV`, 'success');
    } catch (error) {
        console.error('Export CSV error:', error);
        showToast('❌ Export failed', 'error');
    }
}

async function exportNodesJSON() {
    try {
        const response = await fetch('/api/nodes_export');
        const data = await response.json();
        
        if (!data.nodes || data.nodes.length === 0) {
            showToast('❌ No nodes to export', 'error');
            return;
        }
        
        const blob = new Blob([JSON.stringify(data.nodes, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `meshtastic_nodes_${new Date().toISOString().slice(0,10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
        
        showToast(`✅ Exported ${data.nodes.length} nodes to JSON`, 'success');
    } catch (error) {
        console.error('Export JSON error:', error);
        showToast('❌ Export failed', 'error');
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
                showToast('❌ Invalid CSV file', 'error');
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
                showToast('❌ No valid nodes found in CSV', 'error');
                return;
            }
            
            const response = await fetch('/api/nodes_import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes })
            });
            
            const result = await response.json();
            if (result.ok) {
                showToast(`✅ Imported ${result.imported_count} nodes from CSV`, 'success');
                loadMessages();
                loadNodesManagement();
            } else {
                showToast('❌ Import failed: ' + result.error, 'error');
            }
        } catch (error) {
            console.error('Import CSV error:', error);
            showToast('❌ Import failed', 'error');
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
                showToast('❌ Invalid JSON file', 'error');
                return;
            }
            
            const response = await fetch('/api/nodes_import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes })
            });
            
            const result = await response.json();
            if (result.ok) {
                showToast(`✅ Imported ${result.imported_count} nodes from JSON`, 'success');
                loadMessages();
                loadNodesManagement();
            } else {
                showToast('❌ Import failed: ' + result.error, 'error');
            }
        } catch (error) {
            console.error('Import JSON error:', error);
            showToast('❌ Import failed', 'error');
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
    if (!confirm("Restart MeshCenter listener?\n\nCurrent reception will be interrupted for a few seconds.")) {
        return;
    }

    const button = document.getElementById('restartListenerBtn');
    const originalText = button?.textContent || '🔄 Restart Listener';

    if (button) {
        button.disabled = true;
        button.textContent = 'Restarting Listener...';
    }

    try {
        const response = await fetch('/api/restart_listener', { method: 'POST' });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        showToast('✅ Meshtastic listener restart requested', 'success');
        setTimeout(loadRadioHealth, 1000);
    } catch (error) {
        showToast('❌ Restart failed: ' + error.message, 'error');
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
        btn.textContent = '⏳ Scanning...';
        
        const response = await fetch('/api/rescan_nodes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.ok) {
            btn.textContent = '⏳ Waiting for nodes...';
            await new Promise(resolve => setTimeout(resolve, 5000));
            
            await loadMessages();
            await loadChatList();
            
            btn.textContent = '✅ Done!';
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 2000);
            
            showToast('✅ Network rescanned', 'success');
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
            btn.textContent = originalText;
            btn.disabled = false;
        }
    } catch (error) {
        console.error('Rescan error:', error);
        showToast('❌ Network error', 'error');
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
        text.textContent = '⚠️ Delete ALL Direct Message chats?\n\nThis will delete all DM chats and their messages.\nThe LongFast channel will remain.\n\nThis action cannot be undone!';
        btn.textContent = 'Delete All';
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
        text.textContent = '⚠️ Are you sure?\n\nAll DM chats and messages will be permanently deleted!\n\nThis action cannot be undone!';
        btn.textContent = 'Yes, Delete Everything!';
        btn.style.background = '#c62828';
        return;
    }
    
    btn.disabled = true;
    btn.textContent = '⏳ Deleting...';
    
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
            showToast(`✅ Deleted ${data.deleted_count} DM chats`, 'success');
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
            btn.disabled = false;
            btn.textContent = 'Delete All';
            btn.style.background = '';
        }
    })
    .catch(error => {
        console.error('Delete all DM error:', error);
        showToast('❌ Network error', 'error');
        btn.disabled = false;
        btn.textContent = 'Delete All';
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
                <div class="card-label">⚡ Voltage</div>
                <div class="card-value" id="powerVoltageValue">--</div>
                <div class="card-range">
                    <span class="range-min" id="powerVoltageMin">--</span>
                    <span class="range-sep">—</span>
                    <span class="range-max" id="powerVoltageMax">--</span>
                </div>
            </div>
            <div class="telemetry-card" id="powerCurrentCard">
                <div class="card-label">🔌 Current</div>
                <div class="card-value" id="powerCurrentValue">--</div>
                <div class="card-range">
                    <span class="range-min" id="powerCurrentMin">--</span>
                    <span class="range-sep">—</span>
                    <span class="range-max" id="powerCurrentMax">--</span>
                </div>
            </div>
            <div class="telemetry-card" id="powerPowerCard">
                <div class="card-label">⚡ Power</div>
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
            <div class="card-label">🌡️ Temperature</div>
            <div class="card-value" id="environmentTemperatureValue">--</div>
            <div class="card-range">
                <span class="range-min" id="environmentTemperatureMin">--</span>
                <span class="range-sep">—</span>
                <span class="range-max" id="environmentTemperatureMax">--</span>
            </div>
        </div>
        <div class="telemetry-card" id="environmentHumidityCard">
            <div class="card-label">💧 Humidity</div>
            <div class="card-value" id="environmentHumidityValue">--</div>
            <div class="card-range">
                <span class="range-min" id="environmentHumidityMin">--</span>
                <span class="range-sep">—</span>
                <span class="range-max" id="environmentHumidityMax">--</span>
            </div>
        </div>
        <div class="telemetry-card" id="environmentPressureCard">
            <div class="card-label">📊 Pressure</div>
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
            statusEl.textContent = '⚪ No data';
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
            showToast(`✅ Interval set to ${interval/60} minutes`, 'success');
        } else {
            showToast('❌ Failed to update interval', 'error');
        }
    } catch (error) {
        console.error('Error updating telemetry config:', error);
        showToast('❌ Network error', 'error');
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
    container.innerHTML = '<div class="loading">⏳ Loading telemetry data...</div>';

    const labels = {
        'environment': '🌡️ Environment Sensors',
        'power': '⚡ Power Sensors'
    };
    title.textContent = nodeName
        ? `${labels[type] || '📊 Telemetry'} - ${nodeName}`
        : (labels[type] || '📊 Telemetry');

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
            <button class="telemetry-export-btn" onclick="exportTelemetryData()">⬇ Export</button>
            <span class="telemetry-records-count" id="telemetryRecordsCount">📊 0 records</span>
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
        container.innerHTML = '<div class="loading">⚠️ Error loading telemetry data</div>';
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
                    <div class="custom-export-title">📤 Export telemetry</div>
                    <div class="custom-export-subtitle">Export selected telemetry data</div>
                </div>
                <button class="custom-export-close" onclick="closeCustomTelemetryExport()">×</button>
            </div>

            <div class="custom-export-body">

                <div class="export-section">
                    <div class="export-section-title">Export source</div>

                    <label class="export-radio-row">
                        <input type="radio" name="exportRangeMode" value="visible" checked onchange="updateCustomExportMode()">
                        <span>Current visible range (${rangeLabel})</span>
                    </label>

                    <label class="export-radio-row">
                        <input type="radio" name="exportRangeMode" value="custom" onchange="updateCustomExportMode()">
                        <span>Custom range</span>
                    </label>
                </div>

                <div class="export-section custom-export-range" id="customExportRangeFields" style="display:none;">
                    <div class="export-date-grid">
                        <label>
                            <span>From</span>
                            <input type="datetime-local" id="exportStartDate" value="${datetimeLocalValue(from)}">
                        </label>

                        <label>
                            <span>To</span>
                            <input type="datetime-local" id="exportEndDate" value="${datetimeLocalValue(now)}">
                        </label>
                    </div>
                </div>

                <div class="export-section">
                    <div class="export-section-title">Series</div>
                    <div class="export-series-summary">${seriesText}</div>
                </div>

                <div class="export-section">
                    <div class="export-section-title">Format</div>

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
                <button class="custom-export-cancel" onclick="closeCustomTelemetryExport()">Cancel</button>
                <button class="custom-export-primary" onclick="runCustomTelemetryExport()">⬇ Export</button>
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
        temperature: 'Temperature',
        humidity: 'Humidity',
        pressure: 'Pressure',
        voltage: 'Voltage',
        current: 'Current',
        power: 'Power'
    };

    const active = Object.keys(visible)
        .filter(key => visible[key])
        .map(key => labels[key] || key);

    return active.length > 0 ? active.join(' • ') : 'No series selected';
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
            alert('Please select start and end date/time.');
            return;
        }

        const startTs = Math.floor(new Date(startValue).getTime() / 1000);
        const endTs = Math.floor(new Date(endValue).getTime() / 1000);

        if (!startTs || !endTs || startTs >= endTs) {
            alert('Invalid date range.');
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
    container.innerHTML = `<div class="loading">📊 No data for this period (${rangeLabel}). Try a longer range.</div>`;
    if (recordsCount) recordsCount.textContent = '📊 0 records';
    return;
}

if (recordsCount) {
    recordsCount.textContent = `📊 ${filteredRecords.length} records (${rangeLabel})`;
}

    renderTelemetryChart(container, filteredRecords, type);
    updateTelemetryCards(filteredRecords, type);
}

function renderTelemetryChart(container, records, type) {
    container.innerHTML = '<canvas id="telemetryChartCanvas"></canvas>';

    const canvas = document.getElementById('telemetryChartCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');

    const labels = records.map(r => {
        const t = new Date(r.timestamp * 1000);
        return t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    });

    let datasets = [];
    let hasPressure = false;
    let hasCurrent = false;
    let hasPower = false;

    if (type === 'environment') {
        const tempData = records.map(r => r.temperature).filter(v => v !== null && v !== undefined);
        if (tempData.length > 0 && telemetryVisibleSeries.environment.temperature) {
            datasets.push({
                label: 'Temperature ' + temperatureChartUnit(),
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
                label: 'Humidity %',
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
                label: 'Pressure ' + pressureChartUnit(),
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
                label: 'Voltage V',
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
                label: 'Current mA',
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
                label: 'Power W',
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
        container.innerHTML = '<div class="loading">📊 No data available for this sensor type</div>';
        return;
    }

    let yConfig = {
        position: 'left',
        grid: { color: 'rgba(0,0,0,0.08)', drawBorder: true },
        ticks: { font: { size: 9 }, color: '#666' }
    };

    let y1Config = {
        position: 'right',
        grid: { drawOnChartArea: false, drawBorder: true },
        ticks: { font: { size: 9 }, color: '#666' }
    };

    let y2Config = {
        position: 'right',
        offset: true,
        grid: { drawOnChartArea: false, drawBorder: true },
        ticks: {
            font: { size: 9 },
            color: '#666',
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
                .filter(d => d.label === 'Power W')
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
                        labels: { usePointStyle: true, padding: 15, font: { size: 11 }, color: '#333' }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(255,255,255,0.95)',
                        titleColor: '#333',
                        bodyColor: '#666',
                        borderColor: 'rgba(0,0,0,0.1)',
                        borderWidth: 1,
                        callbacks: {
                            title: function(context) {
                                if (!context || !context.length) return '';
                                const record = records[context[0].dataIndex];
                                if (!record || !record.timestamp) return '';
                                return new Date(record.timestamp * 1000).toLocaleString([], {
                                    year: 'numeric',
                                    month: '2-digit',
                                    day: '2-digit',
                                    hour: '2-digit',
                                    minute: '2-digit'
                                });
                            },
                            label: function(context) {
                                const label = context.dataset.label || '';
                                const value = context.parsed.y;

                                if (value === null || value === undefined || isNaN(value)) return label + ': —';
                                if (label.startsWith('Temperature')) return label + ': ' + value.toFixed(1) + temperatureChartUnit();
                                if (label.startsWith('Humidity')) return label + ': ' + value.toFixed(1) + '%';
                                if (label.startsWith('Pressure')) return label + ': ' + value.toFixed(1) + ' ' + pressureChartUnit();
                                if (label.startsWith('Voltage')) return label + ': ' + value.toFixed(3) + ' V';
                                if (label.startsWith('Current')) return label + ': ' + value.toFixed(1) + ' mA';
                                if (label.startsWith('Power')) return label + ': ' + value.toFixed(3) + ' W';

                                return label + ': ' + value.toFixed(2);
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: 'rgba(0,0,0,0.06)', drawBorder: true },
                        ticks: { maxTicksLimit: 20, font: { size: 9 }, color: '#666' }
                    },
                    y: yConfig,
                    y1: y1Config,
                    ...(hasPower ? { y2: y2Config } : {})
                }
            }
        });

    } catch (error) {
        console.error('Chart creation error:', error);
        container.innerHTML = '<div class="loading">⚠️ Error creating chart: ' + error.message + '</div>';
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

function setCameraFeedLoading(loading, message = 'Connecting to camera…') {
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

function hideCameraFeed(message = 'Connecting to camera…') {
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
            buttonText.textContent = 'Starting...';
        }

        if (status) {
            status.textContent = '🟡 Starting camera...';
            status.style.color = '#d97706';
        }

        return;
    }

    if (cameraPowerStatus === 'stopping') {
        if (buttonText) {
            buttonText.textContent = 'Stopping...';
        }

        if (status) {
            status.textContent = '🟠 Stopping camera...';
            status.style.color = '#ea580c';
        }

        return;
    }

    if (cameraPowerStatus === 'error') {
        if (buttonText) {
            buttonText.textContent =
                cameraPowerEnabled ? 'Turn Off' : 'Try Again';
        }

        if (status) {
            status.textContent = '🔴 Camera error';
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
            buttonText.textContent = 'Turn On';
        }

        if (status) {
            status.textContent = '⚫ Camera Off';
            status.style.color = '#64748b';
        }

        if (liveInfo) {
            liveInfo.textContent = 'Power-saving mode';
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
        buttonText.textContent = 'Turn Off';
    }

    setCameraControlsDisabled(false);

    if (status) {
        status.textContent =
            cameraActive && isCameraTabVisible()
                ? '🟢 Online'
                : '⏸️ Paused';

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
                ? '✅ Camera turned on'
                : '✅ Camera turned off',
            'success'
        );

    } catch (error) {
        cameraPowerStatus = 'error';

        showToast(
            `❌ Camera power error: ${error.message}`,
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
    hideCameraFeed('Connecting to camera…');

    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = '🔄 Starting...';
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
        status.textContent = '⏸️ Paused';
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
                liveInfo.textContent = `Live: ${data.config.resolution || '640×480'} @ ${data.config.fps || 12} FPS`;
            }
            
            const statusEl = document.getElementById('videoStatus');
            if (statusEl) {
                statusEl.textContent = cameraActive ? '🟢 Online' : '⏸️ Paused';
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
        status.textContent = '🔄 Applying settings...';
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
            liveInfo.textContent = `Live: ${appliedResolution.replace('x', '×')} @ ${appliedFps} FPS`;
        }

        if (cameraActive) {
            await reconnectCameraFeed();
        } else if (status) {
            status.textContent = '⏸️ Paused';
            status.style.color = '#888';
        }

        showToast('✅ Video settings updated', 'success');

    } catch (error) {
        console.error('Error updating video settings:', error);
        showToast(`❌ Camera settings failed: ${error.message}`, 'error');
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
        `✅ Image preset applied: ${document.getElementById('cameraImagePreset')?.selectedOptions[0]?.textContent || presetName}`,
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
            showToast('✅ Image settings updated', 'success');
        }

    } catch (error) {
        console.error(
            'Error updating camera image controls:',
            error
        );

        showToast(
            '❌ Image settings failed: ' + error.message,
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
        '✅ Neutral image settings restored',
        'success'
    );
}

async function takeScreenshot(source = 'video') {
    const btn = document.querySelector('.screenshot-btn');
    const originalText = btn.textContent;
    
    try {
        btn.disabled = true;
        btn.textContent = '⏳ Capturing...';
        
        const response = await fetch('/api/camera/screenshot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: source })
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast('✅ Screenshot saved', 'success');
        } else {
            showToast('❌ Failed: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error taking screenshot:', error);
        showToast('❌ Network error', 'error');
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

        hideCameraFeed('Connecting to camera…');

        if (status) {
            status.textContent = '🔄 Connecting...';
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
                    hideCameraFeed('Camera stream unavailable');
                }

                if (status) {
                    status.textContent = online
                        ? '🟢 Online'
                        : '🔴 Camera unavailable';

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
                photoInfo.textContent = `Preview: ${res} (${currentPhotoQuality}%) • Save: ${photoSaveResolution.replace('x', '×')}`;
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
            showToast('❌ Failed to update photo settings', 'error');
            return;
        }

        if (showMessage) {
            showToast(`✅ Photo quality set to ${quality}%`, 'success');
        }

    } catch (error) {
        console.error('Error updating photo settings:', error);
        showToast('❌ Network error', 'error');
    }
}

async function capturePhotoPreview() {
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    
    try {
        if (status) {
            status.textContent = '⏳ Capturing preview...';
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
                status.textContent = `📷 Preview ready (${res.replace('x', '×')}, ${quality}%)`;
                status.style.color = '#2e7d32';
            }
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = '💾 Save';
            }
            currentPhotoData = data.image_data;
        } else {
            console.error('[PHOTO] Failed:', data.error);
            if (status) {
                status.textContent = '❌ Failed: ' + (data.error || 'Unknown error');
                status.style.color = '#c62828';
            }
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.textContent = '💾 Save';
            }
            if (placeholder) placeholder.style.display = 'flex';
        }
    } catch (error) {
        console.error('[PHOTO] Error:', error);
        if (status) {
            status.textContent = '❌ Network error';
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = '💾 Save';
        }
        if (placeholder) placeholder.style.display = 'flex';
    }
}

async function captureCameraPhoto() {
    if (!cameraPowerEnabled) {
        showToast(
            '⚫ Turn the camera on first',
            'error'
        );
        return;
    }

    const btn = document.querySelector('.camera-actions-block .screenshot-btn');
    const videoFeed = document.getElementById('videoFeed');

    try {
        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ Saving...';
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
            showToast(`✅ Screenshot saved: ${data.display_name || data.filename}`, 'success');
        } else {
            showToast('❌ Screenshot failed: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        console.error('Screenshot error:', error);
        showToast('❌ Network error', 'error');

    } finally {

    if (videoFeed) {
        videoFeed.classList.remove('camera-capturing');
    }

    setTimeout(() => {
        refreshVideoFeed();
    }, 1200);

    if (btn) {
        btn.disabled = false;
        btn.textContent = '📸 Screenshot';
        }
    }
}

async function savePhoto() {
    const display = document.getElementById('photoDisplay');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    
    if (!display || display.style.display === 'none' || !currentPhotoData) {
        showToast('❌ No photo to save. Create a screenshot first!', 'error');
        return;
    }
    
    try {
        if (status) {
            status.textContent = '⏳ Capturing high-res photo...';
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
                status.textContent = '✅ Saved!';
                status.style.color = '#2e7d32';
            }
            showToast(`✅ Photo saved: ${data.filename} (${(data.size/1024).toFixed(1)} KB)`, 'success');
            
            setTimeout(() => {
                if (status) {
                    const res = photoPreviewResolution.replace('x', '×');
                    status.textContent = `📷 Preview ready (${res}, ${currentPhotoQuality}%)`;
                    status.style.color = '#2e7d32';
                }
                if (saveBtn) {
                    saveBtn.disabled = false;
                    saveBtn.textContent = '💾 Save';
                }
            }, 2000);
        } else {
            if (status) {
                status.textContent = '❌ Save failed';
                status.style.color = '#c62828';
            }
            showToast('❌ Failed to save: ' + (data.error || 'Unknown error'), 'error');
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = '💾 Save';
            }
        }
    } catch (error) {
        console.error('Error saving photo:', error);
        if (status) {
            status.textContent = '❌ Network error';
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = '💾 Save';
        }
        showToast('❌ Network error', 'error');
    }
}

function refreshPhoto() {
    const status = document.getElementById('photoStatus');
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    
    if (status) {
        status.textContent = '⏳ Capturing...';
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
    return parsed.toLocaleString();
}

function deviceStatusClass(mode, identityStatus) {
    if (identityStatus === 'MISMATCH') return 'device-status-danger';
    if (mode === 'connected') return 'device-status-ok';
    if (mode === 'released' || mode === 'releasing' || mode === 'reconnecting') return 'device-status-warning';
    return 'device-status-danger';
}

function deviceConnectionLabel(mode, listenerRunning) {
    if (mode === 'released') return 'Released';
    if (mode === 'releasing') return 'Releasing';
    if (mode === 'reconnecting') return 'Reconnecting';
    if (mode === 'error') return 'Connection error';
    return listenerRunning ? 'Connected' : 'Listener stopped';
}

async function loadNodeManagerDashboard(showFeedback = false) {
    const container = document.getElementById('nodeManagerDashboard');
    if (!container) return;

    if (!container.dataset.loaded) {
        container.innerHTML = '<div class="device-dashboard-loading">Loading node information...</div>';
    }

    try {
        const response = await fetch('/api/node-manager/dashboard', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Unable to load node information');

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
                        ${active ? '<span class="node-profile-badge active">Active</span>' : '<span class="node-profile-badge saved">Saved</span>'}
                        ${active ? `<span class="node-profile-badge ${connected ? 'connected' : 'offline'}">${connected ? 'Connected' : 'Offline'}</span>` : ''}
                        <span class="node-profile-badge identity">${deviceDashboardValue(identity)}</span>
                    </span>
                </button>`;
        }).join('');

        container.dataset.loaded = '1';
        container.innerHTML = `
            <section class="node-profile-selector-section">
                <div class="node-manager-section-heading">
                    <div>
                        <h3>Radios and profiles</h3>
                        <p>Select the radio profile to inspect or activate.</p>
                    </div>
                    <div class="node-manager-profile-heading-actions">
                        <span class="node-manager-profile-count">${profiles.length}</span>
                        <button type="button"
                            class="node-manager-detect-radio-btn"
                            onclick="detectAndAddNodeManagerRadio()">
                            Detect radio
                        </button>
                    </div>
                </div>
                <div class="node-profile-list">${profileCards}</div>
            </section>

            <section class="device-hero-card node-manager-hero-card">
                <div class="node-manager-avatar-wrap">
                    <img id="nodeManagerAvatar" class="node-manager-avatar" src="${escapeHtml(iconSrc)}" alt="">
                    <button type="button" class="node-manager-change-image-btn" id="nodeManagerChangeImageBtn">Change image</button>
                </div>
                <div class="device-hero-main">
                    <div class="device-card-eyebrow">Selected radio</div>
                    <h3>${deviceDashboardValue(radio.long_name, 'Meshtastic radio')}</h3>
                    <div class="device-hero-meta">
                        <span>${deviceDashboardValue(radio.short_name)}</span>
                        <span>${deviceDashboardValue(radio.hardware)}</span>
                        <span>${deviceDashboardValue(radio.node_id)}</span>
                    </div>
                </div>
                <div class="node-manager-status-stack">
                    <div class="device-status-pill ${statusClass}"><span class="device-status-dot"></span>${escapeHtml(connectionLabel)}</div>
                    <span class="node-manager-active-label">Active profile</span>
                </div>
            </section>

            <div class="device-dashboard-grid">
                <section class="device-info-card">
                    <div class="device-card-title">📡 Radio</div>
                    <dl class="device-detail-list">
                        <div><dt>Long name</dt><dd>${deviceDashboardValue(radio.long_name)}</dd></div>
                        <div><dt>Short name</dt><dd>${deviceDashboardValue(radio.short_name)}</dd></div>
                        <div><dt>Node ID</dt><dd class="device-monospace copyable-value" title="Click to copy" onclick="copyTextToClipboard('${String(radio.node_id || '').replace(/'/g, "\\'")}', 'Node ID copied')">${deviceDashboardValue(radio.node_id)}</dd></div>
                        <div><dt>Hardware</dt><dd>${deviceDashboardValue(radio.hardware)}</dd></div>
                        <div><dt>Role</dt><dd>${deviceDashboardValue(radio.role)}</dd></div>
                        <div><dt>Identity</dt><dd>${deviceDashboardValue(radio.identity_status)}</dd></div>
                        <div><dt>Last verified</dt><dd>${formatDeviceDashboardDate(radio.identity_checked_at)}</dd></div>
                    </dl>
                </section>

                <section class="device-info-card">
                    <div class="device-card-title">🔌 Connection</div>
                    <dl class="device-detail-list">
                        <div><dt>USB port</dt><dd class="device-monospace">${deviceDashboardValue(radio.port)}</dd></div>
                        <div><dt>Status</dt><dd>${escapeHtml(connectionLabel)}</dd></div>
                        <div><dt>Listener</dt><dd>${connection.listener_running ? 'Running' : 'Stopped'}</dd></div>
                        <div><dt>Listener PID</dt><dd>${deviceDashboardValue(connection.listener_pid)}</dd></div>
                        <div><dt>Connected since</dt><dd>${formatDeviceDashboardDate(connection.connected_since)}</dd></div>
                        <div><dt>Message</dt><dd>${deviceDashboardValue(connection.message)}</dd></div>
                    </dl>
                    <div class="device-action-row">
                        <button type="button" class="device-action-btn device-action-secondary"
                            onclick="releaseRadioConnection(); setTimeout(() => loadNodeManagerDashboard(), 1200);"
                            ${canRelease ? '' : 'disabled'}>Release Radio</button>
                        <button type="button" class="device-action-btn device-action-primary"
                            onclick="reconnectRadioConnection(); setTimeout(() => loadNodeManagerDashboard(), 1800);"
                            ${canReconnect ? '' : 'disabled'}>Reconnect</button>
                    </div>
                </section>

                <section class="device-info-card">
                    <div class="device-card-title">🗂 Profile</div>
                    <dl class="device-detail-list">
                        <div><dt>Profile ID</dt><dd class="device-monospace">${deviceDashboardValue(profile.profile_id)}</dd></div>
                        <div><dt>Created</dt><dd>${formatDeviceDashboardDate(profile.created_at)}</dd></div>
                        <div><dt>Last used</dt><dd>${formatDeviceDashboardDate(profile.last_used_at)}</dd></div>
                        <div><dt>Messages</dt><dd>${deviceDashboardValue(counts.messages, '0')}</dd></div>
                        <div><dt>Chats</dt><dd>${deviceDashboardValue(counts.chats, '0')}</dd></div>
                        <div><dt>Nodes</dt><dd>${deviceDashboardValue(counts.nodes, '0')}</dd></div>
                        <div><dt>Waypoints</dt><dd>${deviceDashboardValue(counts.waypoints, '0')}</dd></div>
                        <div><dt>Telemetry records</dt><dd>${deviceDashboardValue(counts.telemetry_records, '0')}</dd></div>
                    </dl>
                </section>

                <section class="device-info-card">
                    <div class="device-card-title">💾 Profile storage</div>
                    <dl class="device-detail-list">
                        <div><dt>Total</dt><dd>${deviceDashboardValue(storage.total)}</dd></div>
                        <div><dt>Messages</dt><dd>${deviceDashboardValue(storage.messages)}</dd></div>
                        <div><dt>Telemetry</dt><dd>${deviceDashboardValue(storage.telemetry)}</dd></div>
                        <div><dt>Waypoints</dt><dd>${deviceDashboardValue(storage.waypoints)}</dd></div>
                        <div><dt>Node icons</dt><dd>${deviceDashboardValue(storage.icons)}</dd></div>
                        <div><dt>Path</dt><dd class="device-path-value copyable-value" title="${deviceDashboardValue(profile.path)}">${deviceDashboardValue(profile.path)}</dd></div>
                    </dl>
                </section>
            </div>`;

        if (showFeedback) showToast('Node information refreshed', 'success');
    } catch (error) {
        console.error('[NODE MANAGER] Dashboard load failed:', error);
        container.innerHTML = `<div class="device-dashboard-error"><strong>Unable to load node information</strong><span>${escapeHtml(error.message || String(error))}</span><button type="button" class="mc-refresh-btn" onclick="loadNodeManagerDashboard(true)">Try again</button></div>`;
        if (showFeedback) showToast('Node information could not be loaded', 'error');
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
    const confirmed = window.confirm(
        'Detect the currently connected Meshtastic radio?\n\n' +
        'MeshCenter will stop the listener and release the USB connection. ' +
        'Connect the replacement radio before continuing.\n\n' +
        'No existing profile data will be deleted or merged.'
    );
    if (!confirmed) return;

    showToast('Releasing listener and scanning serial ports...', 'info');

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
                    `${item.port}: ${item.error || item.status || 'no response'}`
                ).join('\n')
                : '';
            throw new Error(
                (data.error || `Radio detection failed (HTTP ${response.status})`) +
                (attempts ? `\n\nProbe results:\n${attempts}` : '')
            );
        }

        const radio = data.detected || {};
        const label = radio.long_name || radio.node_id || 'Meshtastic radio';
        const details = [
            radio.short_name,
            radio.hardware,
            radio.node_id,
            radio.port
        ].filter(Boolean).join(' · ');

        const action = data.profile_exists
            ? 'use its saved profile'
            : 'create a new clean profile';

        const accept = window.confirm(
            `${data.profile_exists ? 'Known' : 'New'} radio detected:\n\n` +
            `${label}\n${details}\n\n` +
            `Do you want to ${action} and restart MeshCenter?`
        );
        if (!accept) {
            showToast('Radio detected. Listener remains released.', 'info');
            await loadNodeManagerDashboard();
            return;
        }

        showToast(
            data.profile_exists
                ? `Selecting profile for ${label}...`
                : `Creating a clean profile for ${label}...`,
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
                `Radio profile creation failed (HTTP ${acceptResponse.status})`
            );
        }

        showToast(
            accepted.message || 'Radio accepted. MeshCenter is restarting...',
            'success'
        );
        waitForNodeManagerProfile(accepted.profile_id);
    } catch (error) {
        console.error('[NODE MANAGER] Radio detection failed:', error);
        window.alert(error.message || String(error));
        showToast('Radio was not added', 'error');
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

    const confirmed = window.confirm(
        `Switch MeshCenter to "${radioName}"?\n\n` +
        `Connect that Meshtastic radio to the configured USB port. ` +
        `MeshCenter will release the current radio, verify the connected node, ` +
        `activate its saved profile and restart the service.\n\n` +
        `No profile data will be deleted or merged.`
    );
    if (!confirmed) return;

    document.querySelectorAll('.node-profile-card').forEach(button => {
        button.disabled = true;
    });
    card?.classList.add('is-switching');
    showToast(`Checking connected radio for ${radioName}...`, 'info');

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
            throw new Error(data.error || `Profile activation failed (HTTP ${response.status})`);
        }

        if (data.already_active) {
            showToast(data.message || 'This profile is already active', 'info');
            await loadNodeManagerDashboard();
            return;
        }

        showToast(data.message || 'Profile activated. MeshCenter is restarting...', 'success');

        waitForNodeManagerProfile(cleanProfileId, 60000);
    } catch (error) {
        console.error('[NODE MANAGER] Profile activation failed:', error);
        window.alert(error.message || String(error));
        showToast('Radio profile was not changed', 'error');
        await loadNodeManagerDashboard();
    } finally {
        card?.classList.remove('is-switching');
        document.querySelectorAll('.node-profile-card').forEach(button => {
            if (!button.classList.contains('is-active')) button.disabled = false;
        });
    }
}

async function copyTextToClipboard(text, successMessage = 'Copied') {
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
        showToast('Copy failed', 'error');
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
    if (!device.assigned) return 'Not assigned';
    if (!device.enabled) return 'Disabled';
    if (device.status === 'active') return 'Active';
    if (device.status === 'available') return 'Available';
    if (device.status === 'data') return 'Connected';
    if (device.status === 'no_data') return 'No data';
    return 'Unavailable';
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

async function loadPeripheralDevices(showFeedback = false) {
    const container = document.getElementById('devicesDashboard');
    if (!container) return;
    if (!container.dataset.loaded) {
        container.innerHTML = '<div class="device-dashboard-loading">Loading peripheral devices...</div>';
    }

    try {
        const response = await fetch('/api/devices', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Unable to load peripheral devices');
        const devices = Array.isArray(data.devices) ? data.devices : [];

        const cards = devices.map(device => {
            const values = device.values || {};
            let details = '';
            let action = '';
            if (device.id === 'camera') {
                details = `
                    <div><dt>Source</dt><dd>${deviceDashboardValue(device.source)}</dd></div>
                    <div><dt>Model</dt><dd>${deviceDashboardValue(device.model)}</dd></div>
                    <div><dt>Assigned</dt><dd>${device.assigned ? 'Yes' : 'No'}</dd></div>`;
                action = `<button type="button" class="device-action-btn device-action-primary" onclick="switchMainTab('video')">Open Camera</button>`;
            } else if (device.id === 'environment') {
                details = `
                    <div><dt>Driver</dt><dd>${deviceDashboardValue(device.driver)}</dd></div>
                    <div><dt>Temperature</dt><dd>${formatPeripheralMetric(values.temperature, '°')}</dd></div>
                    <div><dt>Humidity</dt><dd>${formatPeripheralMetric(values.humidity, '%')}</dd></div>
                    <div><dt>Pressure</dt><dd>${formatPeripheralMetric(values.pressure, ' hPa')}</dd></div>`;
            } else if (device.id === 'power') {
                details = `
                    <div><dt>Driver</dt><dd>${deviceDashboardValue(device.driver)}</dd></div>
                    <div><dt>Voltage</dt><dd>${formatPeripheralMetric(values.voltage, ' V')}</dd></div>
                    <div><dt>Current</dt><dd>${formatPeripheralMetric(values.current, ' mA')}</dd></div>
                    <div><dt>Power</dt><dd>${formatPeripheralMetric(values.power, ' mW')}</dd></div>`;
            }
            return `
                <section class="peripheral-card">
                    <div class="peripheral-card-header">
                        <div>
                            <div class="device-card-eyebrow">Active profile ${deviceDashboardValue(data.profile_id)}</div>
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

        container.dataset.loaded = '1';
        container.innerHTML = `
            <div class="peripheral-grid">${cards}</div>
            <section class="peripheral-card peripheral-add-card" aria-disabled="true">
                <div class="peripheral-add-icon">＋</div>
                <h3>Add device</h3>
                <p>Support for additional modules and actuators is planned.</p>
            </section>`;
        if (showFeedback) showToast('Device information refreshed', 'success');
    } catch (error) {
        console.error('[DEVICES] Peripheral load failed:', error);
        container.innerHTML = `<div class="device-dashboard-error"><strong>Unable to load devices</strong><span>${escapeHtml(error.message || String(error))}</span><button type="button" class="mc-refresh-btn" onclick="loadPeripheralDevices(true)">Try again</button></div>`;
        if (showFeedback) showToast('Device information could not be loaded', 'error');
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
        workspaceLabel.textContent = 'Chats';
        setDockStatusBaseline('Mesh Online', 'online');
        setStatusDockContext('Nodes');
    } else if (tab === 'video') {
        workspaceLabel.textContent = 'Camera';

        setDockStatusBaseline(
            cameraPowerEnabled
                ? (cameraActive ? 'Camera Online' : 'Camera Ready')
                : 'Camera Off',
            cameraPowerEnabled ? 'online' : 'warning'
        );

        setStatusDockContext(cameraPowerEnabled
            ? getCurrentVideoInfoText()
            : 'Power-saving mode');
    } else if (tab === 'media') {
        workspaceLabel.textContent = 'Media';
        setDockStatusBaseline('Local Gallery', 'online');
        setStatusDockContext('Images');
    } else if (tab === 'devices') {
        workspaceLabel.textContent = 'Devices';
        setDockStatusBaseline('Peripherals', 'online');
        setStatusDockContext('Active profile');
    } else if (tab === 'node-manager') {
        workspaceLabel.textContent = 'Node Manager';
        setDockStatusBaseline('Active Radio', 'online');
        setStatusDockContext('Profile');
    } else if (tab === 'system') {
        workspaceLabel.textContent = 'System';
        setDockStatusBaseline('System Monitor', 'online');
        setStatusDockContext('MeshCenter');
    } else if (tab === 'map') {
        workspaceLabel.textContent = 'Map';
        setDockStatusBaseline('Node Positions', 'online');
        setStatusDockContext('OpenStreetMap');
    } else if (tab === 'settings') {
        workspaceLabel.textContent = 'Settings';
        setDockStatusBaseline('Ready', 'online');
        setStatusDockContext('MeshCenter');
    } else if (tab === 'about') {
        workspaceLabel.textContent = 'About';
        setDockStatusBaseline('MeshCenter', 'online');
        setStatusDockContext('v1.1.0');
    } else {
        workspaceLabel.textContent = 'Workspace';
        setDockStatusBaseline('Ready', 'online');
        setStatusDockContext('MeshCenter');
    }
}

function getCurrentVideoInfoText() {
    const info = document.getElementById('videoLiveInfo');
    return info ? info.textContent.replace('Live: ', '') : 'Camera';
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
            internetEl.textContent = data.internet ? '🟢 Connected' : '🔴 Radio Offline';
        }

    } catch (error) {
        console.error('System network load error:', error);
        showToast('❌ Failed to load system network info', 'error');
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

    list.innerHTML = "Scanning...";

    try {

        const response = await fetch("/api/system/wifi/scan");

        const data = await response.json();

        if (!data.ok) {
            list.innerHTML = "Scan failed";
            return;
        }

        if (data.networks.length === 0) {
            list.innerHTML = "No networks found";
            return;
        }

        list.innerHTML = "";

        data.networks.forEach(net => {

            const div = document.createElement("div");

            div.className = "wifi-network-item";

        const actionHtml = net.connected
            ? '<span class="wifi-connected">Connected</span>'
            : `
                <div class="wifi-actions">
                    ${net.saved ? `<button class="wifi-forget-btn" data-ssid="${escapeHtml(net.ssid)}">Forget</button>` : ''}
                    <button class="wifi-connect-btn" data-ssid="${escapeHtml(net.ssid)}" data-saved="${net.saved ? '1' : '0'}">
                        Connect
                    </button>
                </div>
            `;

        div.innerHTML = `
            <div class="wifi-name">
                ${net.connected ? "🟢" : "⚪"} ${net.ssid}
                ${net.saved && !net.connected ? '<span class="wifi-saved-badge">Saved</span>' : ''}
            </div>

            <div class="wifi-info">
                <span>${net.signal ?? '--'}%</span>
                <span>${net.signal_dbm ?? '--'} dBm</span>
                <span>${net.security || 'Open'}</span>
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

        list.innerHTML="Scan error";

    }

}

async function connectWifi(ssid, password) {
    try {
        showToast(`📶 Connecting to ${ssid}...`, 'success');

        const response = await fetch('/api/system/wifi/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid, password })
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            showToast(`✅ Connected to ${ssid}`, 'success');

            setTimeout(() => {
                loadSystemNetwork();
                loadWifiNetworks();
            }, 2500);
        } else {
            showToast('❌ Wi-Fi connect failed: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        console.error('Wi-Fi connect error:', error);
        showToast('❌ Wi-Fi connect network error', 'error');
    }
}

async function forgetWifi(ssid) {
    if (!confirm(`Forget Wi-Fi network "${ssid}"?`)) return;

    try {
        const response = await fetch('/api/system/wifi/forget', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid })
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            showToast(`🗑️ Forgotten: ${ssid}`, 'success');
            loadWifiNetworks();
        } else {
            showToast('❌ Forget failed: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        console.error('Wi-Fi forget error:', error);
        showToast('❌ Wi-Fi forget network error', 'error');
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
    if (statusEl) statusEl.innerHTML = '⏳ Loading...';
    
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
        
        if (statusEl) statusEl.innerHTML = '🟢 Mesh online';

        await loadRadioHealth();

        if (!radioHealthTimer) {
            radioHealthTimer = setInterval(loadRadioHealth, 5000);
        }

        console.log('[INIT] Application ready');
        
    } catch (error) {
        console.error('[INIT] Critical error:', error);
        const statusEl = document.getElementById('statusText');
        if (statusEl) statusEl.innerHTML = '🔴 Error loading - refresh page';
        
        const chatList = document.getElementById('chatList');
        if (chatList) {
            chatList.innerHTML = `
                <div class="loading" style="color:#c62828;">
                    ⚠️ Failed to load data<br>
                    <small style="font-size:12px;color:#999;">${error.message || 'Unknown error'}</small>
                    <br><br>
                    <button onclick="window.location.reload()" style="padding:8px 20px;border:none;border-radius:8px;background:#1a73e8;color:white;cursor:pointer;">
                        ↻ Refresh Page
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
        showToast('❌ No Wi-Fi selected', 'error');
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
                                return value ? new Date(value).toLocaleString() : '';
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
                                return new Date(Number(value)).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
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
    const name = radio.long_name || radio.short_name || 'Unknown';
    const nodeId = radio.node_id || '';
    return nodeId ? `${name} (${nodeId})` : name;
}

function formatIdentityCheckedAt(value) {
    if (!value) return '--';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
}

async function loadInstanceInfo() {
    try {
        const response = await fetch('/api/instance', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || 'Identity request failed');

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
            MATCH: 'Verified',
            MISMATCH: 'Different radio detected',
            NOT_FOUND: 'Radio identity not found',
            DETECTION_ERROR: 'Detection error',
            NOT_CHECKED: 'Not checked',
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
            statusElement.textContent = 'Unavailable';
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
            listenerEl.textContent = data.listener_running ? '🟢 Running' : '🔴 Stopped';
        }

        if (packetEl) packetEl.textContent = data.packet_age == null ? 'Never' : `${data.packet_age} s ago`;
        if (telemetryEl) telemetryEl.textContent = data.telemetry_age == null ? 'Never' : `${data.telemetry_age} s ago`;
        if (sendEl) sendEl.textContent = data.send_age == null ? 'Never' : `${data.send_age} s ago`;
        if (restartEl) restartEl.textContent = data.restart_count ?? 0;

        if (recommendationEl) {
            recommendationEl.textContent = data.recommendation || data.status_reason || '--';
            recommendationEl.style.color = levelColor;
        }

        if (restartBtn) {
            restartBtn.disabled = false;
            restartBtn.textContent = '🔄 Restart Listener';
        }

        if (historyEl) {
            const history = Array.isArray(logData.events) ? logData.events.slice().reverse() : [];

            if (!history.length) {
                historyEl.innerHTML = '<div class="radio-history-empty">No events yet</div>';
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
                                    ${escapeHtml(item.event || 'Event')}
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
            confirm: 'Restart MeshCenter service?\n\nThe web interface will be unavailable for a few seconds.',
            pending: 'Restarting MeshCenter...',
            success: 'MeshCenter restart requested.'
        },
        reboot: {
            confirm: 'Restart Raspberry Pi?\n\nMeshCenter and the radio connection will be temporarily unavailable.',
            pending: 'Restarting Raspberry Pi...',
            success: 'Raspberry Pi restart requested.'
        },
        shutdown: {
            confirm: 'Shut down Raspberry Pi?\n\nThe device must be powered on manually afterwards.',
            pending: 'Shutting down Raspberry Pi...',
            success: 'Raspberry Pi shutdown requested.'
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
window.updateReferenceLocationFields =
    updateReferenceLocationFields;
window.saveReferenceLocation =
    saveReferenceLocation;
window.openReferenceSettings =
    openReferenceSettings;
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
window.updateReferenceLocationFields = updateReferenceLocationFields;
window.saveReferenceLocation = saveReferenceLocation;
window.openReferenceSettings = openReferenceSettings;
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
// RADIO CONFIGURATION MODE
// ============================================================
let radioConnectionState = null;
let radioConnectionPollTimer = null;
let radioConnectionActionBusy = false;

function radioConnectionLabel(mode) {
    const labels = {
        connected: 'Connected',
        releasing: 'Releasing',
        released: 'Released',
        reconnecting: 'Reconnecting',
        error: 'Error'
    };
    return labels[mode] || 'Checking';
}

function renderRadioConnectionState(radio) {
    radioConnectionState = radio || {};

    const badge = document.getElementById('radioConnectionBadge');
    const status = document.getElementById('radioConnectionStatus');
    const note = document.getElementById('radioConnectionNote');
    const action = document.getElementById('radioConnectionAction');

    if (!badge || !status || !action) return;

    const mode = String(radioConnectionState.mode || 'error').toLowerCase();
    badge.className = `radio-connection-badge is-${mode}`;
    badge.textContent = radioConnectionLabel(mode);

    let message = radioConnectionState.message || 'Radio connection status is unavailable.';
    if (radioConnectionState.last_error) {
        message += ` ${radioConnectionState.last_error}`;
    }
    status.textContent = message;

    if (note) {
        note.textContent = mode === 'released'
            ? 'Open the official Meshtastic application now. When configuration is complete, return here and reconnect the radio.'
            : 'Messaging, telemetry and Node Tools are unavailable while the radio is released.';
    }

    const transitional = mode === 'releasing' || mode === 'reconnecting';
    action.disabled = transitional || radioConnectionActionBusy;
    action.classList.toggle('is-reconnect', mode === 'released' || mode === 'error');
    action.textContent = mode === 'released' || mode === 'error'
        ? 'Reconnect Radio'
        : transitional
            ? radioConnectionLabel(mode) + '...'
            : 'Release Radio';

    document.documentElement.dataset.radioMode = mode;
}

async function loadRadioConnectionStatus({ silent = false } = {}) {
    try {
        const response = await fetch('/api/radio_connection/status', {
            cache: 'no-store'
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || 'Unable to read radio status');
        }

        renderRadioConnectionState(data.radio);
        return data.radio;
    } catch (error) {
        console.warn('[RADIO MODE] Status error:', error);
        renderRadioConnectionState({
            mode: 'error',
            message: 'Unable to read the radio connection status.',
            last_error: error.message
        });
        if (!silent) showToast('Unable to read radio status', 'error');
        return null;
    }
}

async function releaseRadioConnection() {
    const confirmed = window.confirm(
        'Release the Meshtastic radio?\n\n' +
        'Messaging, telemetry, node discovery and Node Tools will be temporarily unavailable. ' +
        'MeshCenter itself will continue running.\n\n' +
        'After the radio is released, connect to it using the official Meshtastic application.'
    );

    if (!confirmed) return;

    radioConnectionActionBusy = true;
    renderRadioConnectionState({
        ...(radioConnectionState || {}),
        mode: 'releasing',
        message: 'Stopping the listener and releasing the serial port...'
    });

    try {
        const response = await fetch('/api/radio_connection/release', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || data.message || 'Unable to release the radio');
        }

        renderRadioConnectionState(data.radio);
        showToast('Radio released for external configuration', 'success');
    } catch (error) {
        showToast(`Unable to release radio: ${error.message}`, 'error');
        await loadRadioConnectionStatus({ silent: true });
    } finally {
        radioConnectionActionBusy = false;
        await loadRadioConnectionStatus({ silent: true });
    }
}

async function reconnectRadioConnection() {
    radioConnectionActionBusy = true;
    renderRadioConnectionState({
        ...(radioConnectionState || {}),
        mode: 'reconnecting',
        message: 'Reconnecting MeshCenter to the Meshtastic radio...'
    });

    try {
        const response = await fetch('/api/radio_connection/reconnect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || data.message || 'Unable to reconnect the radio');
        }

        renderRadioConnectionState(data.radio);
        showToast('Radio reconnect requested', 'success');

        // Give the existing listener loop time to reopen the serial port, then
        // force the normal chat/channel refresh to pick up configuration changes.
        window.setTimeout(async () => {
            await loadRadioConnectionStatus({ silent: true });
            try {
                lastForcedChannelRefreshAt = 0;
                await loadChatList();
            } catch (error) {
                console.warn('[RADIO MODE] Channel refresh after reconnect failed:', error);
            }
        }, 1800);
    } catch (error) {
        showToast(`Unable to reconnect radio: ${error.message}`, 'error');
        await loadRadioConnectionStatus({ silent: true });
    } finally {
        radioConnectionActionBusy = false;
        window.setTimeout(() => loadRadioConnectionStatus({ silent: true }), 2200);
    }
}

function toggleRadioConnectionMode() {
    const mode = String(radioConnectionState?.mode || '').toLowerCase();
    if (mode === 'released' || mode === 'error') {
        reconnectRadioConnection();
    } else if (mode === 'connected') {
        releaseRadioConnection();
    }
}

function initializeRadioConnectionMode() {
    loadRadioConnectionStatus({ silent: true });

    if (radioConnectionPollTimer) {
        window.clearInterval(radioConnectionPollTimer);
    }

    radioConnectionPollTimer = window.setInterval(() => {
        if (document.visibilityState === 'visible') {
            loadRadioConnectionStatus({ silent: true });
        }
    }, 5000);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeRadioConnectionMode, { once: true });
} else {
    initializeRadioConnectionMode();
}

window.loadRadioConnectionStatus = loadRadioConnectionStatus;
window.releaseRadioConnection = releaseRadioConnection;
window.reconnectRadioConnection = reconnectRadioConnection;
window.toggleRadioConnectionMode = toggleRadioConnectionMode;
