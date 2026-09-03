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

    const seriesKeys = TELEMETRY_SERIES_KEYS[type] || [];
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
                            ${buildExportDateTimeFieldHtml('exportStart', from)}
                        </label>

                        <label>
                            <span>${escapeHtml(window.I18N.t('nodes.date_to'))}</span>
                            ${buildExportDateTimeFieldHtml('exportEnd', now)}
                        </label>
                    </div>
                </div>

                <div class="export-section">
                    <div class="export-section-title">${escapeHtml(window.I18N.t('nodes.series'))}</div>
                    <div class="export-series-checks" id="exportSeriesChecks">
                        ${seriesKeys.map(key => `
                            <label class="export-series-check">
                                <input type="checkbox" value="${key}" ${telemetryVisibleSeries[type]?.[key] ? 'checked' : ''}>
                                <span>${escapeHtml(telemetrySeriesLabel(key))}</span>
                            </label>
                        `).join('')}
                    </div>
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

function getExportAmPmLabels() {
    const fmt = new Intl.DateTimeFormat(TimeFormatter._getLocale(), { hour: 'numeric', hour12: true });
    const partAt = hour => fmt.formatToParts(new Date(2000, 0, 1, hour, 0)).find(p => p.type === 'dayPeriod')?.value;

    return { am: partAt(1) || 'AM', pm: partAt(13) || 'PM' };
}

// Native <input type="datetime-local">/<input type="time"> always render their
// picker in the browser/OS locale's hour format, ignoring appSettings.units.time_format
// entirely - there is no attribute to force 12h/24h on them. So the export
// range fields are built from a plain date input plus hand-rolled hour/minute
// (+ AM/PM when the app is in 12h mode) inputs instead, kept in sync with
// TimeFormatter._is12h() like every other time display in this file.
function buildExportDateTimeFieldHtml(idPrefix, date) {
    const pad = n => String(n).padStart(2, '0');
    const dateValue = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    const hour24 = date.getHours();
    const minute = date.getMinutes();

    if (TimeFormatter._is12h()) {
        const { am, pm } = getExportAmPmLabels();
        const isPm = hour24 >= 12;
        let hour12 = hour24 % 12;
        if (hour12 === 0) hour12 = 12;

        return `
            <div class="export-datetime-row">
                <input type="date" id="${idPrefix}Date" value="${dateValue}" class="export-date-input">
                <input type="number" id="${idPrefix}Hour" min="1" max="12" value="${hour12}" class="export-time-input" aria-label="${escapeHtml(window.I18N.t('nodes.hour'))}">
                <span class="export-time-sep">:</span>
                <input type="number" id="${idPrefix}Minute" min="0" max="59" value="${pad(minute)}" class="export-time-input" aria-label="${escapeHtml(window.I18N.t('nodes.minute'))}">
                <select id="${idPrefix}Ampm" class="export-ampm-select">
                    <option value="${escapeHtml(am)}" ${!isPm ? 'selected' : ''}>${escapeHtml(am)}</option>
                    <option value="${escapeHtml(pm)}" ${isPm ? 'selected' : ''}>${escapeHtml(pm)}</option>
                </select>
            </div>
        `;
    }

    return `
        <div class="export-datetime-row">
            <input type="date" id="${idPrefix}Date" value="${dateValue}" class="export-date-input">
            <input type="number" id="${idPrefix}Hour" min="0" max="23" value="${pad(hour24)}" class="export-time-input" aria-label="${escapeHtml(window.I18N.t('nodes.hour'))}">
            <span class="export-time-sep">:</span>
            <input type="number" id="${idPrefix}Minute" min="0" max="59" value="${pad(minute)}" class="export-time-input" aria-label="${escapeHtml(window.I18N.t('nodes.minute'))}">
        </div>
    `;
}

// Reads an idPrefix's date + hour/minute(+AM/PM) fields back into a Date,
// mirroring the encoding buildExportDateTimeFieldHtml() rendered them in.
function readExportDateTime(idPrefix) {
    const dateStr = document.getElementById(`${idPrefix}Date`)?.value;
    const hourInput = document.getElementById(`${idPrefix}Hour`);
    const minuteInput = document.getElementById(`${idPrefix}Minute`);
    const ampmSelect = document.getElementById(`${idPrefix}Ampm`);

    if (!dateStr || !hourInput || !minuteInput) return null;

    const [year, month, day] = dateStr.split('-').map(Number);
    let hour = parseInt(hourInput.value, 10);
    const minute = parseInt(minuteInput.value, 10);

    if (![year, month, day, hour, minute].every(Number.isFinite)) return null;

    if (ampmSelect) {
        const { pm } = getExportAmPmLabels();
        const isPm = ampmSelect.value === pm;
        hour = hour % 12;
        if (isPm) hour += 12;
    }

    return new Date(year, month - 1, day, hour, minute);
}

function getTelemetryRangeLabel(minutes) {
    if (minutes < 1440) return `${minutes / 60}h`;
    return `${minutes / 1440}d`;
}

function getTelemetryVisibleSeriesText(type) {
    const visible = telemetryVisibleSeries[type] || {};

    const active = Object.keys(visible)
        .filter(key => visible[key])
        .map(telemetrySeriesLabel);

    return active.length > 0 ? active.join(' • ') : window.I18N.t('nodes.no_series_selected');
}

function runCustomTelemetryExport() {
    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';

    const format = document.querySelector('input[name="exportFormat"]:checked')?.value || 'csv';
    const mode = document.querySelector('input[name="exportRangeMode"]:checked')?.value || 'visible';

    // Read from the dialog's own checkboxes, not telemetryVisibleSeries -
    // these are seeded from the chart's current toggle state when the
    // dialog opens, but editable here without touching the chart itself.
    const series = Array.from(document.querySelectorAll('#exportSeriesChecks input[type="checkbox"]:checked'))
        .map(checkbox => checkbox.value)
        .join(',');

    const nodeId = modal?.dataset?.nodeId || '';
    let url = `/api/export/telemetry?type=${encodeURIComponent(type)}&format=${encodeURIComponent(format)}&series=${encodeURIComponent(series)}`;
    if (nodeId) {
        url += `&node_id=${encodeURIComponent(nodeId)}`;
    }

    if (mode === 'custom') {
        const startDate = readExportDateTime('exportStart');
        const endDate = readExportDateTime('exportEnd');

        if (!startDate || !endDate) {
            alert(window.I18N.t('nodes.select_start_end_date'));
            return;
        }

        const startTs = Math.floor(startDate.getTime() / 1000);
        const endTs = Math.floor(endDate.getTime() / 1000);

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
    //
    // theme-registry Stage 1.5b: every dark-mode literal below (tickColor,
    // the legend label color, and the four tooltip colors further down)
    // was checked individually against the full --mc-* dark token table in
    // ui-kit.css's html[data-theme="dark"] block - none of them are an
    // exact match for any token (closest misses: tickColor #a0aec0 vs
    // --mc-chat-muted #9fb0c3 off by 1-3 per channel, tooltip background
    // #0f1923 vs --mc-bg-media-stage #101923 off by 1 on red only), so
    // they stay hardcoded literals rather than being wired through
    // getComputedStyle(document.documentElement).getPropertyValue('--mc-*')
    // - there's no existing token to read. Candidates for a dedicated
    // chart-chrome token set in Stage 3.1, same category as the raw
    // telemetry-card/battery colors from Stage 1.5a. (No getComputedStyle
    // precedent exists anywhere else in this codebase for reading --mc-*
    // from JS either, for what it's worth - moot here since nothing
    // matched, but noted in case Stage 3.1 wants to establish one.)
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
        const voltageValues = datasets
            .filter(d => d.metricType === 'voltage')
            .flatMap(d => d.data)
            .filter(v => v !== null && v !== undefined && !isNaN(v));

        if (voltageValues.length > 0) {
            const minVoltage = Math.min(...voltageValues);
            const maxVoltage = Math.max(...voltageValues);
            const padding = Math.max(0.05, (maxVoltage - minVoltage) * 0.1);
            yConfig.min = Math.floor((minVoltage - padding) * 100) / 100;
            yConfig.max = Math.ceil((maxVoltage + padding) * 100) / 100;
        } else {
            yConfig.min = 3.40;
            yConfig.max = 4.30;
        }

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
                        // raw: no exact --mc-* match, see the Stage 1.5b
                        // comment near isDark/tickColor above
                        labels: { usePointStyle: true, padding: 15, font: { size: 11 }, color: isDark ? '#cbd5e0' : '#333' }
                    },
                    tooltip: {
                        // raw: no exact --mc-* match for any of these four
                        // (bodyColor intentionally reuses the same literal
                        // as the legend label color above - both were
                        // '#cbd5e0' already, kept as one shared shade for
                        // now rather than split into two independent
                        // copies); see the Stage 1.5b comment near
                        // isDark/tickColor above
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

