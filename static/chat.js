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
let showDuplicatesOnly = false;
let currentMainTab = 'chats';
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
let telemetryInterval = 900;
let telemetryUpdateInterval = null;
let telemetryTimeRange = 60;
let telemetryFullHistory = [];
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
                name: 'Manual position'
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
    const element =
        document.getElementById('baseReferenceLocation');

    if (!element) {
        return;
    }

    const nameElement =
        element.querySelector('.reference-card-name');

    const coordinatesElement =
        element.querySelector('.reference-card-coordinates');

    const reference =
        getReferenceLocation();

    element.style.display = 'flex';

    if (!reference) {
        if (nameElement) {
            nameElement.textContent = 'Disabled';
        }

        if (coordinatesElement) {
            coordinatesElement.textContent =
                'Click to configure';
        }

        element.classList.add('reference-is-disabled');
        element.classList.remove('reference-has-position');
        return;
    }

    element.classList.remove('reference-is-disabled');

    const hasCoordinates =
        Number.isFinite(reference.latitude)
        && Number.isFinite(reference.longitude);

    if (nameElement) {
        nameElement.textContent =
            reference.mode === 'manual'
                ? (appSettings?.reference_location?.place_name || 'Manual coordinates')
                : (appSettings?.reference_location?.place_name || reference.name);
    }

    if (coordinatesElement) {
        coordinatesElement.textContent =
            hasCoordinates
                ? `${reference.latitude.toFixed(5)}, ${reference.longitude.toFixed(5)}`
                : 'No saved position';
    }

    element.classList.toggle(
        'reference-has-position',
        hasCoordinates
    );
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
            throw new Error(
                data.error || `HTTP ${response.status}`
            );
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
const CACHE_TTL = 30000;

// ===== RENDER SIGNATURES =====
let lastRenderedSignature = {};

// ============================================================
// TOAST FUNCTION
// ============================================================
function showToast(message, type = 'info') {
    const oldToast = document.getElementById('toast');
    if (oldToast) oldToast.remove();
    
    const toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    
    setTimeout(() => toast.classList.add('show'), 10);
    
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============================================================
// UTILITY FUNCTIONS
// ============================================================
document.addEventListener('DOMContentLoaded', async function() {
    await loadSettings();
    await loadCameraPowerState();
    startCpuMonitoringUi();

    const title = document.getElementById('appTitle');
    if (title) {
        title.addEventListener('click', function(e) {
            const icon = this.querySelector('span');
            if (icon) {
                icon.style.display = 'inline-block';
                icon.style.transition = 'transform 0.4s ease';
                icon.style.transform = 'rotate(720deg) scale(1.3)';
                setTimeout(() => {
                    icon.style.transform = 'rotate(0deg) scale(1)';
                }, 400);
            }

            this.innerHTML = '🔄 Reloading...';
            this.style.opacity = '0.6';
            this.style.cursor = 'default';

            setTimeout(() => {
                window.location.reload(true);
            }, 500);
        });
    }

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
    const isSelected = (chat.id === currentChatId);
    const selectedClass = isSelected ? 'selected' : '';
    const icon = chat.is_channel ? '📡' : getChatNodeShortName(chat);
    const iconClass = chat.is_channel ? 'channel' : 'dm node-short-name';
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

    return `
        <div class="chat-item ${hasUnread} ${selectedClass}" data-chat-id="${escapeHtml(chat.id)}" onclick="openChat('${escapeHtml(chat.id)}', '${escapeHtml(chat.name)}', '${escapeHtml(chat.type)}', 'chat')">
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
async function loadChatList() {
    console.log('[CHAT] loadChatList called');
    
    // Скрываем индикатор загрузки
    const initialLoading = document.getElementById('initialLoading');
    if (initialLoading) {
        initialLoading.style.display = 'none';
        console.log('[CHAT] Hidden loading indicator');
    }
    
    try {
        console.log('[CHAT] Fetching /api/chats...');
        
        const controller = new AbortController();
        const timeoutId = setTimeout(() => {
            console.log('[CHAT] Request timeout, aborting...');
            controller.abort();
        }, 10000);
        
        const response = await fetch('/api/chats', {
            signal: controller.signal,
            headers: { 'Cache-Control': 'no-cache' }
        });
        clearTimeout(timeoutId);
        
        console.log('[CHAT] Response status:', response.status);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const data = await response.json();
        console.log('[CHAT] Received data:', data.chats ? data.chats.length : 0, 'chats');
        
        chatListCache = data.chats || [];
        totalUnreadCount = data.total_unread || 0;

        const container = document.getElementById('chatList');
        if (!container) {
            console.error('[CHAT] Container #chatList not found');
            return;
        }

        if (!currentChatId) {
            const chatTitle = document.getElementById('chatTitle');
            if (chatTitle) {
                if (totalUnreadCount > 0) {
                    chatTitle.textContent = `💬 Chats (${totalUnreadCount})`;
                } else {
                    chatTitle.textContent = '💬 Chats';
                }
            }
            const subtitleEl = document.getElementById('chatSubtitle');
            if (subtitleEl) subtitleEl.textContent = '';
        }

        if (chatListCache.length === 0) {
            container.innerHTML = '<div class="loading">💬 No chats yet</div>';
            console.log('[CHAT] No chats found');
            return;
        }

        const channelChat = chatListCache.find(c => c.is_channel);
        const dmChats = chatListCache.filter(c => !c.is_channel);

        let html = '';

        if (channelChat) {
            html += renderChatItem(channelChat);
        }

        if (dmChats.length > 0) {
            html += `<div class="chat-section-title">💬 Direct Messages</div>`;
            html += dmChats.map(chat => renderChatItem(chat)).join('');
        } else if (!channelChat) {
            html = '<div class="loading">💬 No chats yet</div>';
        }

        container.innerHTML = html;
        console.log('[CHAT] Chat list rendered successfully');
        flushPendingSynchronizedScroll();

    } catch (error) {
        console.error('[CHAT] Error:', error);
        const container = document.getElementById('chatList');
        
        if (container) {
            if (error.name === 'AbortError') {
                container.innerHTML = `
                    <div class="loading" style="color:#ff9800;">
                        ⏳ Request timeout - retrying...<br>
                        <button onclick="loadChatList()" style="margin-top:8px;padding:4px 12px;border:none;border-radius:4px;background:#1a73e8;color:white;cursor:pointer;">
                            🔄 Retry
                        </button>
                    </div>
                `;
                setTimeout(() => loadChatList(), 3000);
            } else {
                container.innerHTML = `
                    <div class="loading" style="color:#c62828;">
                        ⚠️ Error loading chats<br>
                        <small style="font-size:12px;color:#999;">${error.message}</small>
                        <br><br>
                        <button onclick="loadChatList()" style="padding:6px 16px;border:none;border-radius:6px;background:#1a73e8;color:white;cursor:pointer;">
                            🔄 Retry
                        </button>
                    </div>
                `;
            }
        }
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

function showIgnoredBanner(nodeId, nodeName) {
    hideIgnoredBanner();
    
    const container = document.getElementById('messagesContainer');
    if (!container) return;
    
    const banner = document.createElement('div');
    banner.id = 'ignoreBanner';
    banner.className = 'ignore-banner';
    banner.innerHTML = `
        <div class="ignore-banner-content">
            <span>🚫 Node "${escapeHtml(nodeName)}" is ignored</span>
            <button class="unignore-btn" onclick="toggleIgnore('${escapeHtml(nodeId)}')">
                Unignore
            </button>
        </div>
    `;
    
    container.prepend(banner);
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

function centerElementInContainerIfNeeded(element, container) {
    if (!element || !container) return false;

    if (!isElementFullyVisibleInContainer(element, container)) {
        element.scrollIntoView({
            behavior: 'smooth',
            block: 'center',
            inline: 'nearest'
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
    const chatItem = findElementByDataValue(
        '#chatList .chat-item',
        'chatId',
        nodeId
    );
    const chatContainer = document.getElementById('chatListContainer');

    return centerElementInContainerIfNeeded(chatItem, chatContainer);
}

function scrollNodeCardIntoView(nodeId) {
    const nodeCard = findElementByDataValue(
        '#nodesList .node-card',
        'nodeId',
        nodeId
    );
    const nodesContainer = document.querySelector('.nodes-scroll');

    return centerElementInContainerIfNeeded(nodeCard, nodesContainer);
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
                if (scrollNodeCardIntoView(pendingNodeScrollNodeId)) {
                    pendingNodeScrollNodeId = null;
                }
            }
        });
    });
}

function requestSynchronizedListScroll(nodeId, source) {
    if (!nodeId) return;

    if (source === 'chat') {
        pendingNodeScrollNodeId = String(nodeId);
    } else if (source === 'nodes') {
        pendingChatScrollNodeId = String(nodeId);
    } else {
        pendingChatScrollNodeId = String(nodeId);
        pendingNodeScrollNodeId = String(nodeId);
    }

    flushPendingSynchronizedScroll();
}

function syncSelectedNodeCard() {
    const selectedNodeId =
        currentChatType === 'dm' && currentChatId ? String(currentChatId) : '';

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
    loadChatMessages(chatId);
    startMessagePolling(chatId);

    // Если это DM, обновить детали ноды
    if (chatType === 'dm' && chatId !== 'channel') {
        updateNodeDetails(chatId);
        checkNodeIgnored(chatId).then(isIgnored => {
            if (isIgnored) {
                showIgnoredBanner(chatId, chatName);
            } else {
                hideIgnoredBanner();
            }
        });
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
// RENDER MESSAGES (with force update when container shows loading)
// ============================================================
function renderMessages(container, messages, chatId) {
    if (!container) return;
    
    // Принудительно обновляем, если контейнер показывает загрузку
    const isLoading = container.innerHTML.includes('loading') || container.innerHTML.includes('Loading');
    
    const signature = messages.map(m => 
        [m.kind, m.sender, m.text, m.time].join('|')
    ).join('||');
    
    if (!isLoading && lastRenderedSignature[chatId] === signature) {
        console.log(`[RENDER] No changes for chat: ${chatId}, skipping render`);
        return;
    }
    
    lastRenderedSignature[chatId] = signature;
    
    if (messages.length === 0) {
        const chatName = currentChatName || chatId;
        container.innerHTML = `<div class="loading">💬 No messages yet with ${escapeHtml(chatName)}. Send the first one!</div>`;
    } else {
        container.innerHTML = messages.map(msg => {
            const isMe = msg.kind === 'me';
            const isSystem = msg.kind === 'system' || msg.sender === 'SYSTEM ERROR';
            const sender = escapeHtml(msg.sender || 'Unknown');
            const text = escapeHtml(msg.text || '');
            const time = escapeHtml(msg.time || '');

            if (isSystem) {
                return `
                    <div class="message system">
                        <div class="bubble">
                            <div class="text">${text}</div>
                            <div class="time">${time}</div>
                        </div>
                    </div>
                `;
            }

            return `
                <div class="message ${isMe ? 'me' : 'rx'}">
                    <div class="bubble">
                        <div class="sender">${sender}</div>
                        <div class="text">${text}</div>
                        <div class="time">${time}</div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
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

async function loadChatMessages(chatId) {
    if (!chatId) return;

    const container = document.getElementById('messagesContainer');
    if (!container) return;
    
    if (currentLoadRequest) {
        currentLoadRequest = null;
    }
    
    const requestId = Date.now() + '_' + chatId;
    currentLoadRequest = requestId;
    
    if (messageCache[chatId]) {
        const cached = messageCache[chatId];
        const isFresh = (Date.now() - cached.timestamp) < CACHE_TTL;
        
        if (isFresh && currentChatId === chatId) {
            console.log(`[CACHE] Using cached messages for: ${chatId} (${cached.messages.length} messages)`);
            renderMessages(container, cached.messages, chatId);
            currentLoadRequest = null;
            return;
        }
    }

    try {
        const response = await fetch(`/api/messages?chat_id=${encodeURIComponent(chatId)}`);
        const data = await response.json();

        if (currentChatId !== chatId || currentLoadRequest !== requestId) {
            console.log(`[DEBUG] Chat changed or request cancelled: ${chatId} → ${currentChatId}`);
            currentLoadRequest = null;
            return;
        }

        const messages = data.messages || [];
        
        messageCache[chatId] = {
            messages: messages,
            timestamp: Date.now()
        };
        
        const keys = Object.keys(messageCache);
        if (keys.length > 20) {
            const sortedKeys = keys.sort((a, b) => {
                return (messageCache[a].timestamp || 0) - (messageCache[b].timestamp || 0);
            });
            delete messageCache[sortedKeys[0]];
            console.log(`[CACHE] Removed oldest entry: ${sortedKeys[0]}`);
        }

        renderMessages(container, messages, chatId);
        currentLoadRequest = null;

    } catch (error) {
        console.error('Error loading messages:', error);
        if (currentChatId === chatId && currentLoadRequest === requestId) {
            container.innerHTML = '<div class="loading">⚠️ Error loading messages</div>';
        }
        currentLoadRequest = null;
    }
}

let messagePollingInterval = null;

function startMessagePolling(chatId) {
    stopMessagePolling();
    messagePollingInterval = setInterval(() => {
        if (currentChatId === chatId) {
            loadChatMessages(chatId);
        }
    }, 5000);
}

function stopMessagePolling() {
    if (messagePollingInterval) {
        clearInterval(messagePollingInterval);
        messagePollingInterval = null;
    }
}

// ============================================================
// SEND FORM
// ============================================================
const sendForm = document.getElementById('sendForm');
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

        const button = e.target.querySelector('button[type="submit"]');
        const originalHtml = button ? button.innerHTML : 'Send';

        if (button) {
            button.disabled = true;
            const currentWidth = button.offsetWidth;
            button.style.minWidth = '100px';
            button.style.width = currentWidth + 'px';
            button.classList.add('sending');
            button.innerHTML = `<span style="display:inline-block;min-width:80px;text-align:left;">Sending<span class="dots"></span></span>`;
            button.style.animation = 'pulse 1s ease-in-out infinite';
        }

        try {
            const payload = {
                text: text,
                chat_id: currentChatId
            };

            if (currentChatType === 'dm') {
                payload.target_node = currentChatId;
            }

            const response = await fetch('/api/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });

            if (response.ok) {
                if (input) input.value = '';
                
                invalidateCache(currentChatId);
                lastMessagesSignature = '';
                await loadChatMessages(currentChatId);
                loadChatList();
                
                if (button) {
                    button.innerHTML = '✓ Sent!';
                    button.style.background = '#4caf50';
                    button.style.borderColor = '#4caf50';
                    button.style.animation = '';
                    button.classList.remove('sending');
                    setTimeout(() => {
                        button.disabled = false;
                        button.style.width = '';
                        button.style.minWidth = '';
                        button.style.background = '';
                        button.style.borderColor = '';
                        button.innerHTML = originalHtml;
                        button.classList.remove('sending');
                    }, 1200);
                }
            } else {
                const error = await response.json();
                alert('Failed to send: ' + (error.error || 'Unknown error'));
                if (button) {
                    button.disabled = false;
                    button.style.width = '';
                    button.style.minWidth = '';
                    button.style.animation = '';
                    button.classList.remove('sending');
                    button.innerHTML = originalHtml;
                }
            }

        } catch (error) {
            console.error('Error sending message:', error);
            alert('Network error');
            if (button) {
                button.disabled = false;
                button.style.width = '';
                button.style.minWidth = '';
                button.style.animation = '';
                button.classList.remove('sending');
                button.innerHTML = originalHtml;
            }
        } finally {
            if (input) input.focus();
        }
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
            
            if (currentChatId === nodeId) {
                if (data.ignored) {
                    showIgnoredBanner(nodeId, currentChatName);
                } else {
                    hideIgnoredBanner();
                }
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

function openNodeMap(latitude, longitude) {
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
                        onclick="openNodeMap(${latitude}, ${longitude})">
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

function renderNodeDetails(node) {
    const details = document.getElementById('nodeDetails');
    if (!details) return;

    if (!node || typeof node !== 'object') {
        details.className = 'node-details-placeholder';
        details.innerHTML = 'Select a node below';
        return;
    }

    const nodeId = node.node_id;
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
                    <span class="node-detail-favorite" title="${isFavorite ? 'Favorite node' : ''}">${isFavorite ? '⚑' : ''}</span>
                    <span class="node-detail-activity ${getNodeActivityPresentation(node).activityClass}" title="Activity status" aria-hidden="true"></span>
                    <span class="node-detail-name">${escapeHtml(displayName)}</span>
                    <span class="node-detail-short-id">${escapeHtml(shortName)}</span>
                </div>
                <!-- [NEW] Header actions: Favorite and Ignore buttons -->
                <div class="node-detail-header-actions">
                    <button type="button"
                            class="node-detail-state-btn node-detail-favorite-btn ${isFavorite ? 'active' : ''}"
                            onclick="toggleFavorite('${escapeHtml(nodeId)}')"
                            title="${isFavorite ? 'Remove from favorites' : 'Add to favorites'}"
                            aria-label="${isFavorite ? 'Remove node from favorites' : 'Add node to favorites'}"
                            aria-pressed="${isFavorite ? 'true' : 'false'}">
                        <span aria-hidden="true">${isFavorite ? '⚑' : '⚐'}</span>
                    </button>

                    <button type="button"
                            class="node-detail-state-btn node-detail-ignore-btn ${isIgnored ? 'active' : ''}"
                            onclick="toggleIgnore('${escapeHtml(nodeId)}')"
                            title="${isIgnored ? 'Stop ignoring node' : 'Ignore node'}"
                            aria-label="${isIgnored ? 'Stop ignoring node' : 'Ignore node'}"
                            aria-pressed="${isIgnored ? 'true' : 'false'}">
                        <span aria-hidden="true">${isIgnored ? '🔓' : '🚫'}</span>
                    </button>

                    <button class="node-detail-actions-btn"
                            onclick="toggleNodeActionsMenu(event)"
                            aria-label="More node actions"
                            title="More actions">
                        ⋮
                    </button>
                </div>
            </div>

            <!-- Вторая строка: ID, модель, роль -->
            <div class="node-detail-subheader">
                <span class="node-detail-role">${escapeHtml(role)}</span>
                <span class="node-detail-separator">•</span>
                <span class="node-detail-hw">${escapeHtml(hwModel)}</span>
                <span class="node-detail-separator">•</span>
                <span class="node-detail-id" title="${escapeHtml(nodeId)}">${escapeHtml(truncateText(nodeId, 12))}</span>
                <button class="node-detail-copy-id" onclick="copyNodeId('${escapeHtml(nodeId)}')" title="Copy ID">📋</button>
            </div>

            <!-- Третья строка: статус -->
            <div class="node-detail-status-row">
                <span class="node-detail-last-seen">🕒 ${escapeHtml(lastSeen)}</span>
                <span class="node-detail-hops">Hops: ${escapeHtml(hops)}</span>
                <span class="node-detail-ignored">${isIgnored ? '🚫 Ignored' : ''}</span>
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
            <button onclick="copyNodeId('${escapeHtml(nodeId)}')">📋 Copy ID</button>
            <button onclick="setNodeAsReference('${escapeHtml(nodeId)}')">📍 Set as reference</button>
            <button onclick="toggleFavorite('${escapeHtml(nodeId)}')">${isFavorite ? '⚑ Unfavorite' : '⚐ Favorite'}</button>
            <button onclick="toggleIgnore('${escapeHtml(nodeId)}')">${isIgnored ? '🔓 Unignore' : '🚫 Ignore'}</button>
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
    const battery = node.battery_level ?? '--';
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
                <button class="quick-action" onclick="openNodeMap(${node.position?.latitude || 0}, ${node.position?.longitude || 0})" ${!hasPosition ? 'disabled' : ''}>🗺️ Map</button>
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
                <button onclick="openNodeMap(${pos.latitude}, ${pos.longitude})">🗺 Show on map</button>
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

function renderDataPane(node) {
    // Группировка данных
    const device = {
        battery: node.battery_level ?? '--',
        voltage: node.voltage ?? '--',
        channel_util: node.channel_utilization ?? '--',
        air_util_tx: node.air_util_tx ?? '--',
        uptime: node.uptime_seconds ? formatUptime(node.uptime_seconds) : '--'
    };

    const environment = {
        temperature: node.temperature ?? '--',
        humidity: node.humidity ?? '--',
        pressure: node.pressure ?? '--'
    };

    const power = {
        voltage: node.voltage ?? '--',
        current: node.current ?? '--',
        power: node.power ?? '--'
    };

    const hasEnv = environment.temperature !== '--' || environment.humidity !== '--' || environment.pressure !== '--';
    const hasPower = power.voltage !== '--' || power.current !== '--' || power.power !== '--';

    return `
        <div class="node-detail-data">
            <div class="data-group">
                <div class="data-group-title">📟 Device</div>
                <div class="data-grid">
                    <div><span class="label">Battery</span><span class="value">${escapeHtml(device.battery)}%</span></div>
                    <div><span class="label">Voltage</span><span class="value">${escapeHtml(device.voltage)} V</span></div>
                    <div><span class="label">Channel utilization</span><span class="value">${escapeHtml(device.channel_util)}%</span></div>
                    <div><span class="label">Air utilization TX</span><span class="value">${escapeHtml(device.air_util_tx)}%</span></div>
                    <div><span class="label">Uptime</span><span class="value">${escapeHtml(device.uptime)}</span></div>
                </div>
            </div>
            ${hasEnv ? `
            <div class="data-group">
                <div class="data-group-title">🌡️ Environment</div>
                <div class="data-grid">
                    <div><span class="label">Temperature</span><span class="value">${formatTemperature(environment.temperature)}</span></div>
                    <div><span class="label">Humidity</span><span class="value">${environment.humidity !== '--' ? environment.humidity + '%' : '--'}</span></div>
                    <div><span class="label">Pressure</span><span class="value">${formatPressure(environment.pressure)}</span></div>
                </div>
            </div>` : `
            <div class="data-group">
                <div class="data-group-title">🌡️ Environment</div>
                <div class="data-no-data">No environment data</div>
                <button class="data-request" onclick="runNodeTool('request_telemetry', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">Request telemetry</button>
            </div>`}
            ${hasPower ? `
            <div class="data-group">
                <div class="data-group-title">⚡ Power</div>
                <div class="data-grid">
                    <div><span class="label">Voltage</span><span class="value">${escapeHtml(power.voltage)} V</span></div>
                    <div><span class="label">Current</span><span class="value">${escapeHtml(power.current)} mA</span></div>
                    <div><span class="label">Power</span><span class="value">${escapeHtml(power.power)} mW</span></div>
                </div>
            </div>` : `
            <div class="data-group">
                <div class="data-group-title">⚡ Power</div>
                <div class="data-no-data">No power data</div>
                <button class="data-request" onclick="runNodeTool('request_telemetry', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">Request telemetry</button>
            </div>`}
            <div class="data-actions">
                <button onclick="runNodeTool('request_telemetry', '${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name || node.name || node.node_id)}', this)">📊 Request telemetry</button>
                <button onclick="viewTelemetryHistory('${escapeHtml(node.node_id)}')">📈 View history</button>
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

function copyNodeId(nodeId) {
    navigator.clipboard.writeText(nodeId).then(() => {
        showToast('✅ Node ID copied', 'success');
    }).catch(() => {
        showToast('❌ Failed to copy', 'error');
    });
}

function copyCoordinates(lat, lon) {
    const coords = `${lat}, ${lon}`;
    navigator.clipboard.writeText(coords).then(() => {
        showToast('✅ Coordinates copied', 'success');
    }).catch(() => {
        showToast('❌ Failed to copy', 'error');
    });
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

function viewTelemetryHistory(nodeId) {
    // Открыть модалку телеметрии с фильтром по ноде
    openTelemetryModal('environment');
    showToast('History currently shows all telemetry records; node filtering is planned', 'info');
    // TODO(plugin): add node_id filtering to the telemetry history API and modal
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

            throw new Error(
                data.error || `HTTP ${response.status}`
            );
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
            const rawOutput = String(data.output || '').trim();
            const displayOutput = formatTelemetryCliOutput(rawOutput);

            const telemetryDetails = rawOutput
                ? `
                    <div class="telemetry-request-output">
                        <div class="telemetry-request-status">
                            Request completed by Meshtastic CLI
                        </div>

                        <pre>${escapeHtml(displayOutput)}</pre>
                    </div>
                `
                : `
                    <div class="telemetry-request-output">
                        <div class="telemetry-request-status">
                            Request sent successfully
                        </div>

                        <div class="telemetry-request-note">
                            The response may arrive asynchronously through the listener.
                        </div>
                    </div>
                `;

            renderNodeToolResult(
                nodeId,
                'success',
                `📊 Telemetry from ${data.node_name || nodeName}`,
                '',
                telemetryDetails
            );

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
            successMessage = `✅ Telemetry request completed: ${completedName}`;
        } else if (action === 'request_position') {
            successMessage = `✅ Position request completed: ${completedName}`;
        }

        showToast(successMessage, 'success');

    } catch (error) {
        console.error('[NODE TOOLS] Error:', error);

        renderNodeToolResult(
            nodeId,
            'error',
            currentTool.errorTitle,
            error.message
        );

        showToast(
            `${currentTool.errorToastPrefix}: ${error.message}`,
            'error'
        );
        
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
            background: #eaf3fb !important;
            border-color: #9bbfda !important;
            box-shadow: 0 0 0 1px rgba(75, 132, 175, 0.14), 0 3px 10px rgba(54, 92, 120, 0.08) !important;
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

        selectNode(nodeId, nodeName);
    });
}

function selectNode(nodeId, nodeName) {
    // Даже если эта нода уже выбрана сверху, восстанавливаем выделение
    // компактной карточки. Раньше ранний return оставлял карточку без фона.
    if (currentChatId === nodeId && currentChatType === 'dm') {
        syncSelectedNodeCard();
        requestSynchronizedListScroll(nodeId, 'nodes');
        updateNodeDetails(nodeId);
        return;
    }

    openChat(nodeId, nodeName, 'dm', 'nodes');
    updateNodeDetails(nodeId);
}

// ============================================================
// SENSORS & BASE STATUS
// ============================================================
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
                document.getElementById('batteryFill').style.width = percent + '%';
                document.getElementById('batteryPercent').textContent = percent + '%';
            }

            document.getElementById('sensorUpdate').textContent = `Last update: ${data.last_update || '--'}`;
        }
    } catch (error) {
        console.error('Error loading sensors:', error);
    }
}

async function loadBaseStatus() {
    try {
        const response = await fetch('/api/base_status');
        const data = await response.json();

        const card = document.getElementById('baseCard');
        if (!card) return;
        
        const nodeName = data.node_name || 'Flint Base';

        const battery = data.real_battery !== null ? '~' + data.real_battery + '%' :
                       data.battery_level !== null ? data.battery_level + '%' : '--%';
        const voltage = data.voltage !== null ? Number(data.voltage).toFixed(3) + ' V' : '-- V';
        const channel = data.channel_utilization !== null ? Number(data.channel_utilization).toFixed(2) + '%' : '--%';
        const airTx = data.air_util_tx !== null ? Number(data.air_util_tx).toFixed(2) + '%' : '--%';
        const uptime = data.uptime_seconds !== null ? formatUptime(data.uptime_seconds) : '--';

        card.innerHTML = `
            <div class="base-card-title">
                <span class="base-card-name">
                    <span class="base-card-icon" aria-hidden="true">📡</span>
                    <span>${escapeHtml(nodeName)}</span>
                </span>
            </div>
            <div class="base-status-line">
                <span class="base-status-metrics">
                    <span class="base-status-item">⚡ ${escapeHtml(voltage)}</span>
                    <span class="base-status-item">🔋 ${escapeHtml(battery)}</span>
                    <span class="base-status-item">📶 ${escapeHtml(channel)}</span>
                    <span class="base-status-item">📡 ${escapeHtml(airTx)}</span>
                </span>
                <span class="base-status-uptime">⏱ ${escapeHtml(uptime)}</span>
            </div>
        `;

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
const PANEL_STORAGE_KEYS = {
    base: 'meshcenter.basePanelHidden',
    nodes: 'meshcenter.nodesPanelHidden'
};

function readPanelHidden(storageKey) {
    try {
        return localStorage.getItem(storageKey) === '1';
    } catch (error) {
        console.warn('[LAYOUT] Unable to read panel state:', error);
        return false;
    }
}

function savePanelHidden(storageKey, isHidden) {
    try {
        localStorage.setItem(storageKey, isHidden ? '1' : '0');
    } catch (error) {
        console.warn('[LAYOUT] Unable to save panel state:', error);
    }
}

function applyPanelState(panel, button, isHidden, panelName) {
    if (!panel || !button) return;

    panel.classList.toggle('panel-hidden', isHidden);
    button.classList.toggle('panel-is-hidden', isHidden);
    button.setAttribute('aria-pressed', String(isHidden));

    const action = isHidden ? 'Show' : 'Hide';
    button.title = `${action} ${panelName} panel`;
    button.setAttribute('aria-label', `${action} ${panelName} panel`);

    /*
     * Remove the old right-sidebar hidden class if it remains from an
     * earlier interface version. Stage 2 uses panel-hidden for both sides.
     */
    panel.classList.remove('hidden');
}

function setBasePanelHidden(isHidden, persist = true) {
    const panel = document.getElementById('baseSidebar');
    const button = document.getElementById('toggleBaseSidebarBtn');

    applyPanelState(panel, button, Boolean(isHidden), 'Base');

    if (persist) {
        savePanelHidden(PANEL_STORAGE_KEYS.base, Boolean(isHidden));
    }
}

function setNodesPanelHidden(isHidden, persist = true) {
    const panel = document.getElementById('sidebar');
    const button = document.getElementById('toggleSidebarBtn');

    applyPanelState(panel, button, Boolean(isHidden), 'Nodes');

    if (persist) {
        savePanelHidden(PANEL_STORAGE_KEYS.nodes, Boolean(isHidden));
    }
}

function restorePanelStates() {
    setBasePanelHidden(
        readPanelHidden(PANEL_STORAGE_KEYS.base),
        false
    );

    setNodesPanelHidden(
        readPanelHidden(PANEL_STORAGE_KEYS.nodes),
        false
    );
}

document.getElementById('toggleBaseSidebarBtn')?.addEventListener('click', () => {
    const panel = document.getElementById('baseSidebar');
    setBasePanelHidden(
        !panel?.classList.contains('panel-hidden')
    );
});

document.getElementById('toggleSidebarBtn')?.addEventListener('click', () => {
    const panel = document.getElementById('sidebar');
    setNodesPanelHidden(
        !panel?.classList.contains('panel-hidden')
    );
});

restorePanelStates();

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
        '🎉', '🎊', '🎁', '🎈', '🎀', '🎂', '🎆', '🎇',
        '✨', '🌟', '⭐', '🌈', '☀️', '🌙', '🌟', '💫',
        '🎵', '🎶', '🎤', '🎧', '🎼', '🎹', '🥁', '🎸',
        '🎺', '🎻', '🪕', '🎯', '🎳', '🎮', '🎲', '♟️',
        '🏆', '🏅', '🥇', '🥈', '🥉', '⚽', '🏀', '🏈',
        '⚾', '🥎', '🎾', '🏐', '🏉', '🥏', '🎱', '🪀'
    ],
    travel: [
        '🚗', '🚕', '🚙', '🚌', '🚎', '🏎️', '🚓', '🚑',
        '🚒', '🚐', '🛻', '🚚', '🚛', '🚜', '🏍️', '🛵',
        '🚲', '🛴', '🛹', '🛼', '🚁', '✈️', '🛩️', '🛫',
        '🛬', '🪂', '💺', '🚀', '🛸', '🚢', '🛳️', '⛵',
        '🚤', '🛥️', '🛶', '🚂', '🚆', '🚇', '🚉', '🚊',
        '🚝', '🚞', '🚋', '🚃', '🚄', '🚅', '🚈', '🚍'
    ],
    objects: [
        '💡', '🔦', '🕯️', '🧯', '🪣', '🧹', '🧺', '🪥',
        '🧽', '🪒', '💈', '🧴', '🧵', '🧶', '👓', '🕶️',
        '🥽', '🥼', '🦺', '👔', '👕', '👖', '🧣', '🧤',
        '🧥', '🧦', '👗', '👘', '🥻', '🩱', '🩲', '🩳',
        '👙', '👚', '👛', '👜', '👝', '🛍️', '🎒', '👞',
        '👟', '🥾', '🥿', '👠', '👡', '👢', '👑', '🎩'
    ],
    symbols: [
        '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍',
        '🤎', '💔', '❤️‍🔥', '❤️‍🩹', '💕', '💞', '💓', '💗',
        '💖', '💘', '💝', '💟', '☮️', '✝️', '☪️', '🕉️',
        '☸️', '✡️', '🔯', '🕎', '☯️', '☦️', '🛐', '⛎',
        '♈', '♉', '♊', '♋', '♌', '♍', '♎', '♏',
        '♐', '♑', '♒', '♓', '🆔', '⚛️', '🉑', '☢️'
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
        
        const nameMap = {};
        const duplicates = new Set();
        data.nodes.forEach(n => {
            if (nameMap[n.name]) {
                duplicates.add(n.name);
            } else {
                nameMap[n.name] = true;
            }
        });
        
        let filteredNodes = data.nodes;
        if (showDuplicatesOnly) {
            filteredNodes = data.nodes.filter(n => duplicates.has(n.name));
        }
        
        container.innerHTML = filteredNodes.map(node => {
            const isDuplicate = duplicates.has(node.name);
            const statusClass = node.ignored ? 'ignored' : (isDuplicate ? 'duplicate' : 'normal');
            const statusText = node.ignored ? '🚫 Ignored' : (isDuplicate ? '⚠️ Duplicate' : '✅ Normal');
            
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

async function mergeDuplicates() {
    if (!confirm('⚠️ Merge duplicate nodes?\n\nThis will merge nodes with the same name, keeping the most recent one.\n\nThis action cannot be undone!')) {
        return;
    }
    
    try {
        const response = await fetch('/api/nodes_merge_duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast(`✅ Merged ${data.merged_count} duplicates`, 'success');
            loadMessages();
            loadNodesManagement();
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Merge duplicates error:', error);
        showToast('❌ Network error', 'error');
    }
}

function toggleShowDuplicates() {
    showDuplicatesOnly = !showDuplicatesOnly;
    const btn = document.querySelector('.nodes-tool-btn.show');
    if (btn) {
        btn.style.background = showDuplicatesOnly ? '#d0c0e0' : '';
        btn.textContent = showDuplicatesOnly ? '📋 Hide Duplicates' : '📋 Show Duplicates';
    }
    loadNodesManagement();
}

async function cleanupAllNodes() {
    if (!confirm('⚠️ Delete ALL nodes?\n\nThis will delete all nodes and their chats.\nThe LongFast channel will remain.\n\nThis action cannot be undone!')) {
        return;
    }
    
    if (!confirm('Are you sure? All nodes and DM chats will be permanently deleted!')) {
        return;
    }
    
    try {
        const response = await fetch('/api/cleanup_all_nodes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast(`✅ Deleted ${data.deleted_count} nodes`, 'success');
            loadMessages();
            loadChatList();
            loadNodesManagement();
            if (currentChatType === 'dm') {
                showChatList();
            }
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Cleanup all nodes error:', error);
        showToast('❌ Network error', 'error');
    }
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

async function loadTelemetryHistory() {
    try {
        const historyResponse = await fetch('/api/telemetry/history?limit=5000');
        const historyData = await historyResponse.json();

        telemetryFullHistory = historyData.history || [];
        telemetryHistory = telemetryFullHistory;

        if (historyData.config) {
            telemetryInterval = historyData.config.interval || 900;
            const select = document.getElementById('telemetryInterval');
            if (select) {
                select.value = telemetryInterval;
            }
        }

        console.log('[TELEMETRY] History records:', telemetryHistory.length);
    } catch (error) {
        console.error('[TELEMETRY] Error loading telemetry history:', error);
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
            parts.push(`${data.current.toFixed(0)}mA`);
        }
        powerValue.textContent = parts.length > 0 ? parts.join('  ') : '—';
    }
    if (powerUpdate) {
        powerUpdate.textContent = data.last_update ? `⏱${data.last_update}` : '';
    }
    
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
    const interval = parseInt(select.value);
    
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

async function openTelemetryModal(type) {
    const modal = document.getElementById('telemetryModal');
    const title = document.getElementById('telemetryModalTitle');
    const container = document.getElementById('telemetryChartContainer');

    if (!modal || !title || !container) {
        console.error('Modal elements not found');
        return;
    }

    modal.dataset.type = type;
    modal.style.display = 'flex';
    container.innerHTML = '<div class="loading">⏳ Loading telemetry data...</div>';

    const labels = {
        'environment': '🌡️ Environment Sensors',
        'power': '⚡ Power Sensors'
    };
    title.textContent = labels[type] || '📊 Telemetry';

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
        await loadTelemetry();
        await loadTelemetryHistory();
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

    let url = `/api/export/telemetry?type=${encodeURIComponent(type)}&format=${encodeURIComponent(format)}&series=${encodeURIComponent(series)}`;

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

function renderTelemetryWithRange(type, minutes) {
    const container = document.getElementById('telemetryChartContainer');
    const recordsCount = document.getElementById('telemetryRecordsCount');
    
    if (!container) return;
    
    const now = Date.now() / 1000;
    const cutoff = now - (minutes * 60);
    
    const filteredRecords = telemetryFullHistory.filter(r => r.timestamp >= cutoff);
    
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
                data: records.map(r => temperatureChartValue(r.temperature)),
                borderColor: SENSOR_COLORS.temperature,
                backgroundColor: SENSOR_BG_COLORS.temperature,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y'
            });
        }

        const humData = records.map(r => r.humidity).filter(v => v !== null && v !== undefined);
        if (humData.length > 0 && telemetryVisibleSeries.environment.humidity) {
            datasets.push({
                label: 'Humidity %',
                data: records.map(r => r.humidity),
                borderColor: SENSOR_COLORS.humidity,
                backgroundColor: SENSOR_BG_COLORS.humidity,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y'
            });
        }

        const pressData = records.map(r => r.pressure).filter(v => v !== null && v !== undefined && !isNaN(v));
        if (pressData.length > 0 && telemetryVisibleSeries.environment.pressure) {
            hasPressure = true;
            datasets.push({
                label: 'Pressure ' + pressureChartUnit(),
                data: records.map(r => pressureChartValue(r.pressure)),
                borderColor: SENSOR_COLORS.pressure,
                backgroundColor: SENSOR_BG_COLORS.pressure,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y1'
            });
        }

    } else if (type === 'power') {
        const voltData = records.map(r => r.voltage).filter(v => v !== null && v !== undefined);
        if (voltData.length > 0 && telemetryVisibleSeries.power.voltage) {
            datasets.push({
                label: 'Voltage V',
                data: records.map(r => r.voltage),
                borderColor: SENSOR_COLORS.voltage,
                backgroundColor: SENSOR_BG_COLORS.voltage,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y'
            });
        }

        const currData = records.map(r => r.current).filter(v => v !== null && v !== undefined);
        if (currData.length > 0 && telemetryVisibleSeries.power.current) {
            hasCurrent = true;
            datasets.push({
                label: 'Current mA',
                data: records.map(r => r.current),
                borderColor: SENSOR_COLORS.current,
                backgroundColor: SENSOR_BG_COLORS.current,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y1'
            });
        }

        const powerSeries = records.map(r => {
            if (r.power !== null && r.power !== undefined) return r.power / 1000;
            if (r.voltage !== null && r.voltage !== undefined && r.current !== null && r.current !== undefined) {
                return (r.voltage * r.current) / 1000;
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
                spanGaps: true,
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
    if (type === 'environment') {
        const historyLast = records[records.length - 1] || {};
        const last = {
            temperature: telemetryData.temperature ?? historyLast.temperature,
            humidity: telemetryData.humidity ?? historyLast.humidity,
            pressure: telemetryData.pressure ?? historyLast.pressure
        };

        const tempValues = records.map(r => r.temperature).filter(v => v !== null && v !== undefined);
        const humValues = records.map(r => r.humidity).filter(v => v !== null && v !== undefined);
        const pressValues = records.map(r => r.pressure).filter(v => v !== null && v !== undefined);

        const card1 = document.getElementById('cardTemp').parentElement;
        card1.onclick = () => toggleTelemetrySeries('temperature');
        card1.classList.toggle('inactive', !telemetryVisibleSeries.environment.temperature);
        card1.querySelector('.card-label').textContent = '🌡️ Temperature';
        document.getElementById('cardTemp').textContent = formatTemperature(last.temperature);
        document.getElementById('cardTemp').style.color = SENSOR_COLORS.temperature;

        if (tempValues.length > 0) {
            document.getElementById('cardTempMin').textContent = formatTemperature(Math.min(...tempValues));
            document.getElementById('cardTempMax').textContent = formatTemperature(Math.max(...tempValues));
        }
        card1.querySelector('.card-range').style.display = 'flex';

        const card2 = document.getElementById('cardHum').parentElement;
        card2.onclick = () => toggleTelemetrySeries('humidity');
        card2.classList.toggle('inactive', !telemetryVisibleSeries.environment.humidity);
        card2.querySelector('.card-label').textContent = '💧 Humidity';
        document.getElementById('cardHum').textContent =
            last.humidity !== null && last.humidity !== undefined ? last.humidity.toFixed(1) + '%' : '--';
        document.getElementById('cardHum').style.color = SENSOR_COLORS.humidity;

        if (humValues.length > 0) {
            document.getElementById('cardHumMin').textContent = Math.min(...humValues).toFixed(1) + '%';
            document.getElementById('cardHumMax').textContent = Math.max(...humValues).toFixed(1) + '%';
        }
        card2.querySelector('.card-range').style.display = 'flex';

        const card3 = document.getElementById('cardPress').parentElement;
        card3.onclick = () => toggleTelemetrySeries('pressure');
        card3.classList.toggle('inactive', !telemetryVisibleSeries.environment.pressure);
        card3.querySelector('.card-label').textContent = '📊 Pressure';
        document.getElementById('cardPress').textContent = formatPressure(last.pressure);
        document.getElementById('cardPress').style.color = SENSOR_COLORS.pressure;

        if (pressValues.length > 0) {
            document.getElementById('cardPressMin').textContent = formatPressure(Math.min(...pressValues));
            document.getElementById('cardPressMax').textContent = formatPressure(Math.max(...pressValues));
        }
        card3.querySelector('.card-range').style.display = 'flex';

    } else if (type === 'power') {
        const historyLast = records[records.length - 1] || {};
        const last = {
            voltage: telemetryData.voltage ?? historyLast.voltage,
            current: telemetryData.current ?? historyLast.current,
            power: telemetryData.power ?? historyLast.power
        };

        const voltValues = records.map(r => r.voltage).filter(v => v !== null && v !== undefined);
        const currValues = records.map(r => r.current).filter(v => v !== null && v !== undefined);
        const powerValues = records.map(r => {
            if (r.power !== null && r.power !== undefined) return r.power;
            if (r.voltage !== null && r.voltage !== undefined && r.current !== null && r.current !== undefined) {
                return r.voltage * r.current;
            }
            return null;
        }).filter(v => v !== null && v !== undefined);

        const card1 = document.getElementById('cardTemp').parentElement;
        card1.onclick = () => toggleTelemetrySeries('voltage');
        card1.classList.toggle('inactive', !telemetryVisibleSeries.power.voltage);
        card1.querySelector('.card-label').textContent = '⚡ Voltage';
        document.getElementById('cardTemp').textContent =
            last.voltage !== null && last.voltage !== undefined ? last.voltage.toFixed(3) + ' V' : '--';
        document.getElementById('cardTemp').style.color = SENSOR_COLORS.voltage;

        if (voltValues.length > 0) {
            document.getElementById('cardTempMin').textContent = Math.min(...voltValues).toFixed(3) + ' V';
            document.getElementById('cardTempMax').textContent = Math.max(...voltValues).toFixed(3) + ' V';
        }
        card1.querySelector('.card-range').style.display = 'flex';

        const card2 = document.getElementById('cardHum').parentElement;
        card2.onclick = () => toggleTelemetrySeries('current');
        card2.classList.toggle('inactive', !telemetryVisibleSeries.power.current);
        card2.querySelector('.card-label').textContent = '🔌 Current';
        document.getElementById('cardHum').textContent =
            last.current !== null && last.current !== undefined ? last.current.toFixed(1) + ' mA' : '--';
        document.getElementById('cardHum').style.color = SENSOR_COLORS.current;

        if (currValues.length > 0) {
            document.getElementById('cardHumMin').textContent = Math.min(...currValues).toFixed(1) + ' mA';
            document.getElementById('cardHumMax').textContent = Math.max(...currValues).toFixed(1) + ' mA';
        }
        card2.querySelector('.card-range').style.display = 'flex';

        const card3 = document.getElementById('cardPress').parentElement;
        card3.onclick = () => toggleTelemetrySeries('power');
        card3.classList.toggle('inactive', !telemetryVisibleSeries.power.power);
        card3.querySelector('.card-label').textContent = '⚡ Power';

        const powerValue =
            last.power !== null && last.power !== undefined
                ? last.power
                : (last.voltage !== null && last.voltage !== undefined && last.current !== null && last.current !== undefined
                    ? last.voltage * last.current
                    : null);

        document.getElementById('cardPress').textContent =
            powerValue !== null && powerValue !== undefined ? (powerValue / 1000).toFixed(3) + ' W' : '--';
        document.getElementById('cardPress').style.color = SENSOR_COLORS.power;

        if (powerValues.length > 0) {
            document.getElementById('cardPressMin').textContent = (Math.min(...powerValues) / 1000).toFixed(3) + ' W';
            document.getElementById('cardPressMax').textContent = (Math.max(...powerValues) / 1000).toFixed(3) + ' W';
            card3.querySelector('.card-range').style.display = 'flex';
        } else {
            document.getElementById('cardPressMin').textContent = '--';
            document.getElementById('cardPressMax').textContent = '--';
            card3.querySelector('.card-range').style.display = 'none';
        }
    }
}

function closeTelemetryModal() {
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
            feed.style.display = 'none';
        }

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

    if (feed) {
        feed.style.display = 'block';
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
            throw new Error(
                data.error || `HTTP ${response.status}`
            );
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
            throw new Error(
                data.error || `HTTP ${response.status}`
            );
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
    const img = document.getElementById('videoFeed');
    if (img) {
        img.src = '/video_feed?t=' + Date.now();
        img.style.display = 'block';
    }
    
    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = '🔄 Starting...';
        status.style.color = '#ff9800';
    }
}

function stopCameraStream() {
    if (!cameraActive) return;
    cameraActive = false;
    
    console.log('[CAMERA] Stopping stream...');
    const img = document.getElementById('videoFeed');
    if (img) {
        img.src = '';
        img.style.display = 'none';
    }
    
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

async function updateVideoSettings() {
    const resolution = document.getElementById('videoResolution').value;
    const fps = parseInt(document.getElementById('videoFps').value);
    const quality = parseInt(document.getElementById('videoQuality').value);
    
    const qualityLabel = document.getElementById('videoQualityLabel');
    const liveInfo = document.getElementById('videoLiveInfo');
    
    if (qualityLabel) qualityLabel.textContent = quality + '%';
    if (liveInfo) liveInfo.textContent = `Live: ${resolution.replace('x', '×')} @ ${fps} FPS`;
    
    try {
        const response = await fetch('/api/camera/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resolution, fps, quality })
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast('✅ Video settings updated', 'success');
            if (cameraActive) {
                refreshVideoFeed();
            }
        } else {
            showToast('❌ Failed: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error updating video settings:', error);
        showToast('❌ Network error', 'error');
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
        )
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
        img.removeAttribute('src');
    }
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

        if (status) {
            status.textContent = '🔄 Applying settings...';
            status.style.color = '#ff9800';
        }

        let freezeFrame = null;

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

                const imageRect = img.getBoundingClientRect();
                const wrapRect = frameWrap.getBoundingClientRect();

                freezeFrame.style.left =
                    `${imageRect.left - wrapRect.left}px`;

                freezeFrame.style.top =
                    `${imageRect.top - wrapRect.top}px`;

                freezeFrame.style.width =
                    `${imageRect.width}px`;

                freezeFrame.style.height =
                    `${imageRect.height}px`;

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
        img.style.visibility = 'hidden';
        img.removeAttribute('src');

        cameraFeedRefreshTimer = setTimeout(() => {
            cameraFeedRefreshTimer = null;

            if (sequence !== cameraFeedRefreshSequence) {
                freezeFrame?.remove();
                resolve();
                return;
            }

            const finishReconnect = (online) => {
                if (sequence !== cameraFeedRefreshSequence) {
                    return;
                }

                img.style.visibility = 'visible';

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
function switchMainTab(tab) {

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

        document.getElementById('chatListContainer').style.display = currentChatId ? 'none' : 'block';
        document.getElementById('messagesView').style.display = currentChatId ? 'flex' : 'none';
        document.getElementById('chatHeader').style.display = currentChatId ? 'flex' : 'none';

        document.querySelectorAll('.main-content-tab').forEach(t => t.classList.remove('active'));
        document.getElementById('mainTabChats')?.classList.add('active');

        updateDockForTab?.('chats');
        return;
    }

    currentMainTab = tab;

    document.querySelectorAll('.main-content-tab').forEach(btn => {
        btn.classList.toggle(
            'active',
            btn.id === 'mainTab' + tab.charAt(0).toUpperCase() + tab.slice(1)
        );
    });

    const messagesView = document.getElementById('messagesView');
    const videoView = document.getElementById('videoView');
    const mediaView = document.getElementById('mediaView');
    const photoView = document.getElementById('photoView');
    const chatHeader = document.getElementById('chatHeader');
    const chatListContainer = document.getElementById('chatListContainer');
    const systemView = document.getElementById('systemView');    
    const settingsView = document.getElementById('settingsView');

    if (messagesView) messagesView.style.display = 'none';
    if (videoView) videoView.style.display = 'none';
    if (mediaView) mediaView.style.display = 'none';
    if (photoView) photoView.style.display = 'none';
    if (systemView) systemView.style.display = 'none';
    if (settingsView) settingsView.style.display = 'none';

    if (tab !== 'video') {
        stopVideoFeed();
    }

    if (tab === 'chats') {
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

        loadCameraPowerState().then(async () => {
            if (!cameraPowerEnabled) {
                renderCameraPowerState();
                return;
            }

            await switchCameraMode('video');

            setTimeout(() => loadVideoSettings(), 100);
            setTimeout(() => loadPhotoSettings(), 150);
            setTimeout(() => {
                refreshVideoFeed();
                cameraActive = true;
            }, 200);
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
        loadRadioHealth();

    } else if (tab === 'system') {
        left.innerHTML = '🖥️ System';
        centerText.textContent = 'System Monitor';
        setStatusDockContext('MeshCenter');
    } else if (tab === 'settings') {
        const btn = document.getElementById('mainTabSettings');
        if (btn) btn.classList.add('active');

        if (settingsView) settingsView.style.display = 'flex';

        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';

        updateStatusDock('settings');
    }
}

function updateStatusDock(tab) {
    const left = document.getElementById('dockLeft');
    const centerText = document.getElementById('dockStatusText');
    const right = document.getElementById('dockContextText');

    if (!left || !centerText || !right) return;

    if (tab === 'chats') {
        left.innerHTML = '💬 Chats';
        centerText.textContent = 'Mesh Online';
        setStatusDockContext('Nodes');
    } else if (tab === 'video') {
        left.innerHTML = '📷 Camera';

        centerText.textContent = cameraPowerEnabled
            ? (cameraActive ? 'Camera Online' : 'Camera Ready')
            : 'Camera Off';

        setStatusDockContext(cameraPowerEnabled
            ? getCurrentVideoInfoText()
            : 'Power-saving mode');
    } else if (tab === 'media') {
        left.innerHTML = '🖼️ Media';
        centerText.textContent = 'Local Gallery';
        setStatusDockContext('Images');
    } else if (tab === 'settings') {
        left.innerHTML = '⚙️ Settings';
        centerText.textContent = 'Ready';
        setStatusDockContext('MeshCenter');
    } else {
        left.innerHTML = 'Workspace';
        centerText.textContent = 'Ready';
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
                    <button class="wifi-connect-btn" data-ssid="${escapeHtml(net.ssid)}" data-saved="${net.saved ? '1' : '0'}">
                        Connect
                    </button>
                    ${net.saved ? `<button class="wifi-forget-btn" data-ssid="${escapeHtml(net.ssid)}">Forget</button>` : ''}
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

    if (chatList) chatList.style.display = 'flex';
    if (messagesView) messagesView.style.display = 'none';
    if (videoView) videoView.style.display = 'none';
    if (mediaView) mediaView.style.display = 'none';
    if (systemView) systemView.style.display = 'none';
    if (settingsView) settingsView.style.display = 'none';

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


let dockCpuState = {
    usage: null,
    ram: null,
    temp: null
};
function ensureStatusDockMetrics() {
    const right = document.getElementById('dockContextText');
    if (!right) return null;

    let context = right.querySelector('.dock-context-label');
    let metrics = right.querySelector('.dock-system-metrics');

    if (!context || !metrics) {
        const previous = right.textContent.trim();
        right.innerHTML = '';
        context = document.createElement('span');
        context.className = 'dock-context-label';
        context.textContent = previous || 'MeshCenter';
        metrics = document.createElement('span');
        metrics.className = 'dock-system-metrics';
        metrics.innerHTML = `
            <span id="dockCpuMetric">CPU --%</span>
            <span id="dockRamMetric">RAM --%</span>
            <span id="dockTempMetric">--°C</span>
        `;
        right.append(context, metrics);
    }
    return { right, context, metrics };
}

function setStatusDockContext(text) {
    const dock = ensureStatusDockMetrics();
    if (dock) dock.context.textContent = text;
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
        cpuEl.textContent = Number.isFinite(dockCpuState.usage) ? `CPU ${dockCpuState.usage.toFixed(1)}%` : 'CPU --%';
        cpuEl.className = cpuMetricClass(dockCpuState.usage);
    }
    if (ramEl) {
        ramEl.textContent = Number.isFinite(dockCpuState.ram) ? `RAM ${dockCpuState.ram.toFixed(1)}%` : 'RAM --%';
        ramEl.className = cpuMetricClass(dockCpuState.ram, 70, 90);
    }
    if (tempEl) {
        tempEl.textContent = Number.isFinite(dockCpuState.temp)
            ? formatTemperature(dockCpuState.temp)
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
window.toggleIgnore = toggleIgnore;
window.toggleFavorite = toggleFavorite;
window.selectNode = selectNode;
window.clearNodeSearch = clearNodeSearch;
window.rescanNodes = rescanNodes;
window.restartListener = restartListener;
window.mergeDuplicates = mergeDuplicates;
window.cleanupAllNodes = cleanupAllNodes;
window.toggleShowDuplicates = toggleShowDuplicates;
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
window.toggleIgnore = toggleIgnore;
window.toggleFavorite = toggleFavorite;
window.selectNode = selectNode;
window.clearNodeSearch = clearNodeSearch;
window.rescanNodes = rescanNodes;
window.restartListener = restartListener;
window.mergeDuplicates = mergeDuplicates;
window.cleanupAllNodes = cleanupAllNodes;
window.toggleShowDuplicates = toggleShowDuplicates;
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
