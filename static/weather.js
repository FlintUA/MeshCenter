// ============================================================
// WEATHER MODULE
// Weather data is fetched only through the MeshCenter backend, from
// whichever provider is active (Settings -> Weather Provider). The API
// key never reaches the browser.
// ============================================================

let weatherRefreshTimer = null;
let weatherLoading = false;
let weatherLastData = null;
let weatherReferenceSignature = null;

function weatherText(id, value, fallback = '--') {
    const element = document.getElementById(id);
    if (element) element.textContent = value ?? fallback;
}

// Keyed on the provider-neutral condition_key every WeatherProvider backend
// normalizes into (see weather/providers/base.py CONDITION_KEYS) - not on any
// provider's own icon vocabulary, so this never needs to change when a new
// provider is added.
const WEATHER_CONDITION_EMOJI = {
    'clear-day': '☀️',
    'clear-night': '🌙',
    'partly-cloudy-day': '🌤️',
    'partly-cloudy-night': '☁️',
    'cloudy': '☁️',
    'fog': '🌫️',
    'drizzle': '🌦️',
    'rain': '🌧️',
    'thunderstorm': '⛈️',
    'snow': '🌨️',
};

function weatherEmoji(conditionKey) {
    return WEATHER_CONDITION_EMOJI[conditionKey] || '🌤️';
}

function formatWeatherNumber(value, digits = 0) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--';
    return number.toFixed(digits);
}

function weatherTemperatureUnit() {
    return appSettings?.units?.temperature === 'f' ? '°F' : '°C';
}

function weatherPressureUnit() {
    return appSettings?.units?.pressure === 'mmhg' ? 'mmHg' : 'hPa';
}

function formatWeatherTemperature(value, digits = 0) {
    const number = Number(value);
    if (!Number.isFinite(number)) return `--${weatherTemperatureUnit()}`;

    const unit = appSettings?.units?.temperature || 'c';
    const converted = unit === 'f' ? celsiusToFahrenheit(number) : number;
    return `${converted.toFixed(digits)}${unit === 'f' ? '°F' : '°C'}`;
}

function formatWeatherPressure(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return `-- ${weatherPressureUnit()}`;

    const unit = appSettings?.units?.pressure || 'hpa';
    if (unit === 'mmhg') {
        return `${hPaToMmHg(number).toFixed(1)} mmHg`;
    }

    return `${number.toFixed(0)} hPa`;
}

function formatWeatherWind(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '-- m/s';

    const unit = appSettings?.units?.wind || 'ms';
    if (unit === 'kmh') return `${(number * 3.6).toFixed(1)} km/h`;
    if (unit === 'mph') return `${(number * 2.236936).toFixed(1)} mph`;
    return `${number.toFixed(1)} m/s`;
}

function formatWeatherDate(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
    if (!match) return value || '--';
    return `${match[3]}.${match[2]}.${match[1]}`;
}

function weatherDayLabel(item, index) {
    return index === 0 ? window.I18N.t('weather.tomorrow') : formatWeatherDate(item?.date);
}

function weatherShortTemperature(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--°';
    const unit = appSettings?.units?.temperature || 'c';
    const converted = unit === 'f' ? celsiusToFahrenheit(number) : number;
    return `${Math.round(converted)}°`;
}

function renderWeatherForecast(items) {
    const grid = document.getElementById('weatherForecastGrid');
    if (!grid) return;

    const forecast = Array.isArray(items) ? items.slice(0, 3) : [];
    if (!forecast.length) {
        grid.innerHTML = `<div class="weather-forecast-placeholder">${escapeHtml(window.I18N.t('weather.forecast_unavailable'))}</div>`;
        return;
    }

    grid.innerHTML = forecast.map((item, index) => {
        const label = weatherDayLabel(item, index);
        const dateLine = index === 0 ? formatWeatherDate(item?.date) : '';
        const icon = weatherEmoji(item.condition_key);
        const high = weatherShortTemperature(item.temp_max);
        const low = weatherShortTemperature(item.temp_min);
        const description = item.description || item.condition || '';
        const rainChance = Number(item.precipitation_probability);
        const titleParts = [description];
        if (Number.isFinite(rainChance) && rainChance > 0) {
            titleParts.push(window.I18N.t('weather.precipitation_chance', { percent: Math.round(rainChance) }));
        }
        const tooltip = titleParts.filter(Boolean).join(' • ');

        return `
            <div class="weather-forecast-day" title="${escapeHtml(tooltip)}">
                <div class="weather-forecast-label">${escapeHtml(label)}</div>
                ${dateLine ? `<div class="weather-forecast-date">${escapeHtml(dateLine)}</div>` : ''}
                <div class="weather-forecast-icon" aria-hidden="true">${icon}</div>
                <div class="weather-forecast-temperature">${high}/${low}</div>
                <div class="weather-forecast-description">${escapeHtml(description)}</div>
            </div>
        `;
    }).join('');
}

function weatherUpdatedText(data) {
    const fetchedAt = Number(data?.fetched_at);
    if (!Number.isFinite(fetchedAt)) return window.I18N.t('weather.updated_at', { time: data?.updated_local || '--:--' });

    const ageMinutes = Math.max(0, Math.floor((Date.now() / 1000 - fetchedAt) / 60));
    if (ageMinutes < 1) return window.I18N.t('weather.updated_just_now');
    // Uses plural() rather than a hardcoded "1 min ago" / "N min ago" split -
    // Russian/Ukrainian "минута/минуты/минут" needs the full one/few/many
    // distinction, not just a singular/plural one.
    if (ageMinutes < 60) return window.I18N.plural('weather.updated_min_ago', ageMinutes);
    return window.I18N.t('weather.updated_at', { time: data?.updated_local || '--:--' });
}

function setWeatherState(state, label) {
    const card = document.getElementById('weatherCard');
    const badge = document.getElementById('weatherStateBadge');
    if (card) {
        card.classList.remove('weather-loading', 'weather-online', 'weather-error', 'weather-stale');
        card.classList.add(`weather-${state}`);
    }
    if (badge) badge.textContent = label;
}

function renderWeather(data) {
    weatherLastData = data;
    weatherSetupRequired = false;

    weatherText('weatherLocation', [data.location, data.country].filter(Boolean).join(', '));
    weatherText('weatherIcon', weatherEmoji(data.condition_key));
    weatherText('weatherTemperature', formatWeatherTemperature(data.temperature));
    weatherText('weatherCondition', data.description || data.condition || window.I18N.t('weather.current_weather_fallback'));
    weatherText('weatherHumidity', `${formatWeatherNumber(data.humidity)}%`);
    weatherText('weatherPressure', formatWeatherPressure(data.pressure));
    weatherText('weatherWind', formatWeatherWind(data.wind_speed));
    weatherText('weatherFeelsLike', window.I18N.t('weather.feels_like', { temperature: formatWeatherTemperature(data.feels_like) }));
    weatherText('weatherUpdated', weatherUpdatedText(data));
    renderWeatherForecast(data.forecast);

    if (data.stale) {
        setWeatherState('stale', window.I18N.t('weather.status_cached'));
    } else if (data.cached) {
        setWeatherState('online', window.I18N.t('weather.status_cached'));
    } else {
        setWeatherState('online', window.I18N.t('weather.status_live'));
    }
}

function renderWeatherError(data) {
    weatherLastData = null;
    weatherSetupRequired = data?.configured === false;
    setWeatherState('error', weatherSetupRequired ? window.I18N.t('weather.status_setup') : window.I18N.t('weather.status_offline'));
    weatherText('weatherTemperature', `--${weatherTemperatureUnit()}`);
    weatherText('weatherCondition', data?.error || window.I18N.t('weather.data_unavailable'));
    weatherText('weatherHumidity', '--%');
    weatherText('weatherPressure', `-- ${weatherPressureUnit()}`);
    weatherText('weatherWind', '-- m/s');
    weatherText('weatherFeelsLike', 'OpenWeather');
    weatherText('weatherUpdated', weatherSetupRequired ? window.I18N.t('weather.click_setup') : window.I18N.t('weather.retry_later'));
    renderWeatherForecast([]);
}

function currentWeatherReferenceSignature() {
    const reference = appSettings?.reference_location || {};
    return JSON.stringify({
        mode: reference.mode || 'disabled',
        manual: reference.manual || {},
        node_id: reference.node_id || '',
    });
}

function handleWeatherSettingsUpdated() {
    const signature = currentWeatherReferenceSignature();
    const referenceChanged = weatherReferenceSignature !== null
        && weatherReferenceSignature !== signature;
    weatherReferenceSignature = signature;

    if (weatherLastData) renderWeather(weatherLastData);
    if (referenceChanged) loadWeather(true);
}

async function loadWeather(force = false) {
    if (weatherLoading) return;
    weatherLoading = true;
    setWeatherState('loading', window.I18N.t('common.loading_short'));

    try {
        const endpoint = force ? '/api/weather/current?refresh=1' : '/api/weather/current';
        const response = await fetch(endpoint, { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || !data.ok) throw data;
        renderWeather(data);
    } catch (error) {
        console.error('Weather load failed:', error);
        renderWeatherError(error);
    } finally {
        weatherLoading = false;
    }
}

function startWeatherModule() {
    weatherReferenceSignature = currentWeatherReferenceSignature();
    loadWeather(false);
    if (weatherRefreshTimer) clearInterval(weatherRefreshTimer);
    weatherRefreshTimer = setInterval(() => loadWeather(false), 10 * 60 * 1000);
}

document.addEventListener('meshcenter:settings-updated', handleWeatherSettingsUpdated);
document.addEventListener('DOMContentLoaded', startWeatherModule);

// ============================================================
// WEATHER PROVIDER SETTINGS (Settings -> Weather Provider card)
// The API key is sent only to the MeshCenter backend and is never
// returned to the browser after it has been saved.
// ============================================================

let weatherSetupRequired = false;
let weatherProviderBusy = false;

function handleWeatherBadgeClick() {
    if (weatherSetupRequired) {
        window.openWeatherProviderSettings?.();
        return;
    }
    loadWeather(true);
}

function setWeatherProviderStatus(message = '', state = '') {
    const status = document.getElementById('weatherProviderStatus');
    if (!status) return;
    status.textContent = message;
    status.className = 'reference-location-status weather-provider-status';
    if (state) status.classList.add(`is-${state}`);
}

async function loadWeatherProviderStatus() {
    const select = document.getElementById('weatherProviderSelect');
    try {
        const response = await fetch('/api/weather/config', { cache: 'no-store' });
        const data = await response.json();
        if (!data.ok) throw data;

        if (select && data.provider) select.value = data.provider;
        setWeatherProviderStatus(
            data.configured
                ? `✓ ${window.I18N.t('settings.weather_configured')}`
                : `▲ ${window.I18N.t('settings.weather_key_required')}`,
            data.configured ? 'configured' : 'required'
        );
    } catch (error) {
        console.warn('[WEATHER] Failed to load provider status:', error);
    }
}

function setWeatherProviderBusy(busy) {
    weatherProviderBusy = Boolean(busy);
    const input = document.getElementById('weatherProviderApiKeyInput');
    const saveButton = document.getElementById('weatherProviderSaveButton');
    const select = document.getElementById('weatherProviderSelect');
    if (input) input.disabled = weatherProviderBusy;
    if (select) select.disabled = weatherProviderBusy;
    if (saveButton) {
        saveButton.disabled = weatherProviderBusy;
        saveButton.textContent = weatherProviderBusy
            ? `💾 ${window.I18N.t('weather.saving')}`
            : `💾 ${window.I18N.t('common.save')}`;
    }
}

async function saveWeatherProviderApiKey() {
    const input = document.getElementById('weatherProviderApiKeyInput');
    const apiKey = String(input?.value || '').trim();
    if (!apiKey) {
        setWeatherProviderStatus(window.I18N.t('settings.weather_enter_api_key'), 'error');
        input?.focus();
        return;
    }

    setWeatherProviderBusy(true);
    try {
        const response = await fetch('/api/weather/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: apiKey }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) {
            throw new Error(data.error || window.I18N.t('settings.unable_to_save_weather_api_key', { reason: '' }));
        }

        weatherSetupRequired = false;
        if (input) input.value = '';
        setWeatherProviderStatus(`✓ ${window.I18N.t('settings.weather_configured')}`, 'configured');
        showToast(`✅ ${window.I18N.t('weather.api_key_saved')}`, 'success');
        await loadWeather(true);
    } catch (error) {
        showToast(
            `❌ ${window.I18N.t('settings.unable_to_save_weather_api_key', { reason: error.message })}`,
            'error'
        );
    } finally {
        setWeatherProviderBusy(false);
    }
}

function toggleWeatherProviderKeyVisibility(button) {
    const input = document.getElementById('weatherProviderApiKeyInput');
    if (!input) return;
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    if (button) button.textContent = show ? window.I18N.t('weather.hide') : window.I18N.t('common.show');
}

document.addEventListener('DOMContentLoaded', loadWeatherProviderStatus);
window.loadWeatherProviderStatus = loadWeatherProviderStatus;
