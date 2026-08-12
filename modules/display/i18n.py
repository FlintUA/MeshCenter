"""Server-side translation catalog for e-paper display text.

Separate from static/i18n.js by necessity, not by choice: display text is
rendered into a PIL image on the backend (modules/display/pages/*.py), so
the browser-side I18N runtime (and static/i18n/*.json) is unreachable from
here - see static/i18n/README.md's note that the backend otherwise has no
i18n awareness at all, by design. This mirrors that system's locale set
(en/de/ru/uk) and, where the same concept already has an established
translation there (voltage/current/power/temperature/mode/online/offline/
yes/no/error), reuses that exact wording rather than inventing new terms.

"MeshCenter" itself is never translated (see static/i18n/README.md's
do-not-translate glossary convention for proper nouns) and stays a literal
in the page modules, not a catalog key.
"""

from __future__ import annotations

SUPPORTED_LOCALES = ("en", "de", "ru", "uk")
DEFAULT_LOCALE = "en"

_CATALOG: dict[str, dict[str, str]] = {
    "en": {
        "radio": "Radio",
        "nodes": "Nodes",
        "last_rx": "Last RX",
        "last_update": "Last update {time}",
        "cpu": "CPU",
        "ram": "RAM",
        "temp": "Temp",
        "power": "Power",
        "voltage": "Voltage",
        "current": "Current",
        "mode": "Mode",
        "listener": "Listener",
        "error": "Error: {detail}",
        "message": "Message",
        "system": "System",
        "online": "Online",
        "warning": "Warning",
        "offline": "Offline",
        "yes": "Yes",
        "no": "No",
        "radio_offline_title": "RADIO OFFLINE",
        "connection_lost": "Connection lost",
        "low_battery_title": "LOW BATTERY ({percent:.0f}%)",
        "critically_low_power": "Critically low power",
        "device_prefix": "Device: {path}",
        "last_seen_prefix": "Last seen: {time}",
        "mode_connected": "connected",
        "mode_reconnecting": "reconnecting",
        "mode_releasing": "releasing",
        "mode_released": "released",
        "mode_error": "error",
        "no_messages": "(no messages)",
    },
    "de": {
        "radio": "Funkgerät",
        "nodes": "Knoten",
        "last_rx": "Letzter Empfang",
        "last_update": "Letzte Aktualisierung {time}",
        "cpu": "CPU",
        "ram": "RAM",
        "temp": "Temp.",
        "power": "Leistung",
        "voltage": "Spannung",
        "current": "Stromstärke",
        "mode": "Modus",
        "listener": "Listener",
        "error": "Fehler: {detail}",
        "message": "Nachricht",
        "system": "System",
        "online": "Online",
        "warning": "Warnung",
        "offline": "Offline",
        "yes": "Ja",
        "no": "Nein",
        "radio_offline_title": "FUNKGERÄT OFFLINE",
        "connection_lost": "Verbindung verloren",
        "low_battery_title": "AKKU SCHWACH ({percent:.0f}%)",
        "critically_low_power": "Kritisch niedrige Leistung",
        "device_prefix": "Gerät: {path}",
        "last_seen_prefix": "Zuletzt gesehen: {time}",
        "mode_connected": "verbunden",
        "mode_reconnecting": "verbindet erneut",
        "mode_releasing": "wird freigegeben",
        "mode_released": "freigegeben",
        "mode_error": "Fehler",
        "no_messages": "(keine Nachrichten)",
    },
    "ru": {
        "radio": "Радио",
        "nodes": "Узлы",
        "last_rx": "Посл. приём",
        "last_update": "Обновлено {time}",
        "cpu": "CPU",
        "ram": "RAM",
        "temp": "Темп.",
        "power": "Мощность",
        "voltage": "Напряжение",
        "current": "Ток",
        "mode": "Режим",
        "listener": "Слушатель",
        "error": "Ошибка: {detail}",
        "message": "Сообщение",
        "system": "Система",
        "online": "Онлайн",
        "warning": "Внимание",
        "offline": "Офлайн",
        "yes": "Да",
        "no": "Нет",
        "radio_offline_title": "РАДИО ОФЛАЙН",
        "connection_lost": "Соединение потеряно",
        "low_battery_title": "НИЗКИЙ ЗАРЯД ({percent:.0f}%)",
        "critically_low_power": "Критически низкий заряд",
        "device_prefix": "Устройство: {path}",
        "last_seen_prefix": "Последний раз: {time}",
        "mode_connected": "подключено",
        "mode_reconnecting": "переподкл.",
        "mode_releasing": "освобожд.",
        "mode_released": "освобождено",
        "mode_error": "ошибка",
        "no_messages": "(нет сообщений)",
    },
    "uk": {
        "radio": "Радіо",
        "nodes": "Вузли",
        "last_rx": "Ост. прийом",
        "last_update": "Оновлено {time}",
        "cpu": "CPU",
        "ram": "RAM",
        "temp": "Темп.",
        "power": "Потужність",
        "voltage": "Напруга",
        "current": "Струм",
        "mode": "Режим",
        "listener": "Слухач",
        "error": "Помилка: {detail}",
        "message": "Повідомлення",
        "system": "Система",
        "online": "Онлайн",
        "warning": "Попередження",
        "offline": "Офлайн",
        "yes": "Так",
        "no": "Ні",
        "radio_offline_title": "РАДІО ОФЛАЙН",
        "connection_lost": "З'єднання втрачено",
        "low_battery_title": "НИЗКИЙ ЗАРЯД ({percent:.0f}%)",
        "critically_low_power": "Критично низький заряд",
        "device_prefix": "Пристрій: {path}",
        "last_seen_prefix": "Востаннє: {time}",
        "mode_connected": "підключено",
        "mode_reconnecting": "перепідключення",
        "mode_releasing": "звільнення",
        "mode_released": "звільнено",
        "mode_error": "помилка",
        "no_messages": "(немає повідомлень)",
    },
}


def t(key: str, locale: str = DEFAULT_LOCALE, **kwargs) -> str:
    catalog = _CATALOG.get(locale, _CATALOG[DEFAULT_LOCALE])
    template = catalog.get(key) or _CATALOG[DEFAULT_LOCALE].get(key, key)
    return template.format(**kwargs) if kwargs else template


def resolve_display_language(configured: str | None) -> str:
    """"auto" (or anything unrecognized) resolves to English here, not to
    a sampled browser Accept-Language like server.py's resolve_ui_language()
    does for the web UI - epaper_worker runs in a background thread with no
    Flask request in scope to sample from."""
    if configured in SUPPORTED_LOCALES:
        return configured
    return DEFAULT_LOCALE
