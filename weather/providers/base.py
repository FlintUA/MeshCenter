"""Common contract every weather provider backend must implement.

get_current() must return a dict shaped like the payload OpenWeatherProvider
already produced before this abstraction existed - static/weather.js renders
that shape regardless of which provider is active. See openweather.py for the
canonical field list.

condition_key is the provider-neutral piece of that payload: each provider
maps its own icon/condition vocabulary onto CONDITION_KEYS so weatherEmoji()
in weather.js has exactly one vocabulary to render, no matter which backend
supplied the data.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# Neutral condition vocabulary shared by every provider's get_current()/
# get_forecast() output. Day/night variants only exist for the two states
# that actually look different at night (clear, partly cloudy) - clouds,
# rain, snow etc. read the same regardless of time of day.
CONDITION_KEYS = (
    "clear-day", "clear-night",
    "partly-cloudy-day", "partly-cloudy-night",
    "cloudy",
    "fog",
    "drizzle", "rain",
    "thunderstorm",
    "snow",
)


class WeatherProvider(ABC):
    # Stable id: used as the settings.weather.provider value and as the
    # variable name suffix in weather_secrets.py (see weather_manager.py).
    id: str
    display_name: str

    # ui.language -> this provider's own language code, e.g. OpenWeather
    # maps "uk" to "ua" while WeatherAPI may not need any remapping at all.
    LANGUAGE_MAP: dict[str, str] = {}

    def resolve_language(self, ui_language: str) -> str:
        if not self.LANGUAGE_MAP:
            return ui_language
        default = next(iter(self.LANGUAGE_MAP.values()))
        return self.LANGUAGE_MAP.get(ui_language, default)

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def set_api_key(self, api_key: str) -> None: ...

    @abstractmethod
    def set_language(self, language: str) -> None: ...

    @abstractmethod
    def test_api_key(
        self,
        api_key: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_current(
        self,
        force: bool = False,
        latitude: float | None = None,
        longitude: float | None = None,
        location_name: str = "",
        location_source: str = "configured",
    ) -> dict[str, Any]: ...
