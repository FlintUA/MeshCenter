// ============================================================
// WEATHER MODULE
// OpenWeather data is fetched only through the MeshCenter backend.
// The API key never reaches the browser.
// ============================================================

let weatherRefreshTimer = null;
let weatherLoading = false;
let weatherLastData = null;
let weatherReferenceSignature = null;

function weatherText(id, value, fallback = '--') {
    const element = document.getElementById(id);
    if (element) element.textContent = value ?? fallback;
}

function weatherEmoji(code, condition) {
    const icon = String(code || '').toLowerCase();
    const name = String(condition || '').toLowerCase();

    // Prefer the OpenWeather icon code. It is more precise and stable than
    // matching translated/free-form description text.
    const iconMap = {
        '01d': '☀️', '01n': '🌙',
        '02d': '🌤️', '02n': '☁️',
        '03d': '☁️', '03n': '☁️',
        '04d': '☁️', '04n': '☁️',
        '09d': '🌧️', '09n': '🌧️',
        '10d': '🌦️', '10n': '🌧️',
        '11d': '⛈️', '11n': '⛈️',
        '13d': '🌨️', '13n': '🌨️',
        '50d': '🌫️', '50n': '🌫️',
    };
    if (iconMap[icon]) return iconMap[icon];

    const isNight = icon.endsWith('n');
    if (name.includes('thunder')) return '⛈️';
    if (name.includes('snow')) return '🌨️';
    if (name.includes('rain') || name.includes('drizzle')) return '🌧️';
    if (name.includes('mist') || name.includes('fog') || name.includes('haze')) return '🌫️';
    if (name.includes('clear')) return isNight ? '🌙' : '☀️';
    if (name.includes('cloud')) return '☁️';
    return '🌤️';
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
    return index === 0 ? 'Tomorrow' : formatWeatherDate(item?.date);
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
        grid.innerHTML = '<div class="weather-forecast-placeholder">Forecast unavailable</div>';
        return;
    }

    grid.innerHTML = forecast.map((item, index) => {
        const label = weatherDayLabel(item, index);
        const dateLine = index === 0 ? formatWeatherDate(item?.date) : '';
        const icon = weatherEmoji(item.icon_code, item.condition);
        const high = weatherShortTemperature(item.temp_max);
        const low = weatherShortTemperature(item.temp_min);
        const description = item.description || item.condition || '';
        const rainChance = Number(item.precipitation_probability);
        const titleParts = [description];
        if (Number.isFinite(rainChance) && rainChance > 0) {
            titleParts.push(`Precipitation chance: ${Math.round(rainChance)}%`);
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
    if (!Number.isFinite(fetchedAt)) return `Updated ${data?.updated_local || '--:--'}`;

    const ageMinutes = Math.max(0, Math.floor((Date.now() / 1000 - fetchedAt) / 60));
    if (ageMinutes < 1) return 'Updated just now';
    if (ageMinutes === 1) return 'Updated 1 min ago';
    if (ageMinutes < 60) return `Updated ${ageMinutes} min ago`;
    return `Updated ${data?.updated_local || '--:--'}`;
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

    weatherText('weatherLocation', [data.location, data.country].filter(Boolean).join(', '));
    weatherText('weatherIcon', weatherEmoji(data.icon_code, data.condition));
    weatherText('weatherTemperature', formatWeatherTemperature(data.temperature));
    weatherText('weatherCondition', data.description || data.condition || 'Current weather');
    weatherText('weatherHumidity', `${formatWeatherNumber(data.humidity)}%`);
    weatherText('weatherPressure', formatWeatherPressure(data.pressure));
    weatherText('weatherWind', formatWeatherWind(data.wind_speed));
    weatherText('weatherFeelsLike', `Feels like ${formatWeatherTemperature(data.feels_like)}`);
    weatherText('weatherUpdated', weatherUpdatedText(data));
    renderWeatherForecast(data.forecast);

    if (data.stale) {
        setWeatherState('stale', 'Cached');
    } else if (data.cached) {
        setWeatherState('online', 'Cached');
    } else {
        setWeatherState('online', 'Live');
    }
}

function renderWeatherError(data) {
    weatherLastData = null;
    setWeatherState('error', data?.configured === false ? 'Setup' : 'Offline');
    weatherText('weatherTemperature', `--${weatherTemperatureUnit()}`);
    weatherText('weatherCondition', data?.error || 'Weather data unavailable');
    weatherText('weatherHumidity', '--%');
    weatherText('weatherPressure', `-- ${weatherPressureUnit()}`);
    weatherText('weatherWind', '-- m/s');
    weatherText('weatherFeelsLike', 'OpenWeather');
    weatherText('weatherUpdated', 'Retry later');
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
    setWeatherState('loading', 'Loading');

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
