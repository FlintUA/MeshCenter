"""Tests for modules/display/i18n.py's task-37 additions: status/uptime/
battery/node/port/hardware/listener_running/listener_stopped/message_to/dm
must exist (non-empty) in all four supported locales, matching this
catalog's existing convention (every key present in every locale, English
fallback only for genuinely missing keys - see t()'s own fallback logic).
"""

from __future__ import annotations

from modules.display.i18n import SUPPORTED_LOCALES, _CATALOG, t

NEW_KEYS = (
    "status", "uptime", "battery", "node", "port", "hardware",
    "listener_running", "listener_stopped", "message_to", "dm",
)


def test_all_new_keys_present_in_every_locale():
    for locale in SUPPORTED_LOCALES:
        for key in NEW_KEYS:
            assert key in _CATALOG[locale], f"{key!r} missing from {locale!r} catalog"
            assert _CATALOG[locale][key].strip(), f"{key!r} is empty in {locale!r} catalog"


def test_message_to_interpolates_name():
    for locale in SUPPORTED_LOCALES:
        result = t("message_to", locale, name="Flint Base")
        assert "Flint Base" in result


def test_status_title_translated_per_locale_not_a_raw_literal():
    # status.py used to draw the literal "MeshCenter" - task 37 replaced it
    # with a real translated "STATUS" title, so this key must actually
    # differ from the untranslated English word in at least one locale
    # (the point of adding it at all).
    assert t("status", "ru") != t("status", "en")
