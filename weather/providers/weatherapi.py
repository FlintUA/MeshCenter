"""WeatherAPI (weatherapi.com) backend for MeshCenter.

Mirrors OpenWeatherProvider's contract and caching shape (see openweather.py)
but needs only a single request per refresh: WeatherAPI's forecast.json
endpoint returns current conditions and daily forecast together, so there's
no OpenWeather-style bucketing of 3-hourly samples into daily cards.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

from .base import WeatherProvider


@dataclass(frozen=True)
class WeatherApiConfig:
    api_key: str
    latitude: float
    longitude: float
    location_name: str = ""
    language: str = "en"
    cache_seconds: int = 600
    timeout_seconds: int = 8


# https://www.weatherapi.com/docs/weather_conditions.json - grouped into the
# neutral CONDITION_KEYS vocabulary shared with OpenWeatherProvider. Anything
# named "rain"/"drizzle"/"snow"/"thunder" stays in its own bucket - only
# haze/mist/dust-family codes fall back to "fog", and unknown codes fall back
# to "cloudy" rather than guessing at a more specific state.
_CLEAR = {1000}
_PARTLY_CLOUDY = {1003}
_CLOUDY = {1006, 1009}
_FOG = {
    1012, 1015, 1018, 1021, 1024, 1027,
    1030, 1033, 1036, 1039, 1042, 1045, 1048,
    1135, 1147,
}
_DRIZZLE = {1072, 1150, 1153, 1168, 1171}
_RAIN = {1063, 1180, 1183, 1186, 1189, 1192, 1195, 1198, 1201, 1240, 1243, 1246}
_SNOW = {
    1066, 1069, 1114, 1117,
    1204, 1207, 1210, 1213, 1216, 1219, 1222, 1225, 1237,
    1249, 1252, 1255, 1258, 1261, 1264,
}
_THUNDERSTORM = {1087, 1273, 1276, 1279, 1282}


def _condition_key(code: int, is_day: bool) -> str:
    if code in _CLEAR:
        return "clear-day" if is_day else "clear-night"
    if code in _PARTLY_CLOUDY:
        return "partly-cloudy-day" if is_day else "partly-cloudy-night"
    if code in _CLOUDY:
        return "cloudy"
    if code in _FOG:
        return "fog"
    if code in _DRIZZLE:
        return "drizzle"
    if code in _RAIN:
        return "rain"
    if code in _SNOW:
        return "snow"
    if code in _THUNDERSTORM:
        return "thunderstorm"
    return "cloudy"


class WeatherApiProvider(WeatherProvider):
    id = "weatherapi"
    display_name = "WeatherAPI"

    # WeatherAPI's language codes (https://www.weatherapi.com/docs/#intro-languages)
    # already match our ui.language codes 1:1, including Ukrainian ("uk") -
    # unlike OpenWeather, no remapping is needed here.
    LANGUAGE_MAP = {
        "en": "en",
        "de": "de",
        "ru": "ru",
        "uk": "uk",
    }

    FORECAST_URL = "https://api.weatherapi.com/v1/forecast.json"

    def __init__(self, config: WeatherApiConfig):
        self.config = config
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0
        self._cache_key: tuple[float, float] | None = None

    def is_configured(self) -> bool:
        return bool(str(self.config.api_key or "").strip())

    def set_api_key(self, api_key: str) -> None:
        with self._lock:
            self.config = replace(self.config, api_key=str(api_key or "").strip())
            self._cache = None
            self._cache_time = 0.0
            self._cache_key = None

    def set_language(self, language: str) -> None:
        with self._lock:
            self.config = replace(self.config, language=str(language or "en").strip())
            self._cache = None
            self._cache_time = 0.0
            self._cache_key = None

    def test_api_key(
        self,
        api_key: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any]:
        try:
            latitude = float(self.config.latitude if latitude is None else latitude)
            longitude = float(self.config.longitude if longitude is None else longitude)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Reference location has invalid coordinates."}

        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return {"ok": False, "error": "Reference location coordinates are out of range."}

        try:
            payload = self._request(latitude, longitude, api_key=api_key, days=1)
            return {
                "ok": True,
                "location": (payload.get("location") or {}).get("name") or self.config.location_name,
                "message": "WeatherAPI key is valid.",
            }
        except Exception as exc:
            return {"ok": False, "error": self._friendly_error(exc)}

    def get_current(
        self,
        force: bool = False,
        latitude: float | None = None,
        longitude: float | None = None,
        location_name: str = "",
        location_source: str = "configured",
    ) -> dict[str, Any]:
        if not self.config.api_key:
            return {
                "ok": False,
                "configured": False,
                "error": "Weather not configured",
            }

        try:
            latitude = float(self.config.latitude if latitude is None else latitude)
            longitude = float(self.config.longitude if longitude is None else longitude)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "configured": True,
                "error": "Reference location has invalid coordinates",
            }

        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return {
                "ok": False,
                "configured": True,
                "error": "Reference location coordinates are out of range",
            }

        cache_key = (round(latitude, 6), round(longitude, 6))
        now = time.time()
        with self._lock:
            if (
                not force
                and self._cache is not None
                and self._cache_key == cache_key
                and now - self._cache_time < max(30, self.config.cache_seconds)
            ):
                result = dict(self._cache)
                result["cached"] = True
                result["stale"] = False
                return result

            try:
                # days=4: today + the next 3 days - forecastday[0] (today) is
                # dropped below so the 3-day forecast matches OpenWeatherProvider's
                # "tomorrow, day after, day after that" semantics.
                data = self._request(latitude, longitude, days=4)
                payload = self._build_payload(
                    data, now, latitude, longitude,
                    location_name=location_name,
                    location_source=location_source,
                )
                self._cache = dict(payload)
                self._cache_time = now
                self._cache_key = cache_key
                return payload
            except Exception as exc:
                if self._cache is not None and self._cache_key == cache_key:
                    result = dict(self._cache)
                    result.update({
                        "cached": True,
                        "stale": True,
                        "warning": str(exc),
                    })
                    return result

                return {
                    "ok": False,
                    "configured": True,
                    "error": self._friendly_error(exc),
                }

    def _request(
        self,
        latitude: float,
        longitude: float,
        api_key: str | None = None,
        days: int = 4,
    ) -> dict[str, Any]:
        params = urllib.parse.urlencode({
            "key": self.config.api_key if api_key is None else api_key,
            "q": f"{latitude},{longitude}",
            "days": days,
            "aqi": "no",
            "alerts": "no",
            "lang": self.config.language,
        })
        request = urllib.request.Request(
            f"{self.FORECAST_URL}?{params}",
            headers={"User-Agent": "MeshCenter/1.1"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                details = json.loads(exc.read().decode("utf-8"))
                message = (details.get("error") or {}).get("message") or str(exc)
            except Exception:
                message = str(exc)
            raise RuntimeError(f"WeatherAPI {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"WeatherAPI connection failed: {exc.reason}") from exc

    def _build_payload(
        self,
        data: dict[str, Any],
        fetched_at: float,
        latitude: float,
        longitude: float,
        location_name: str = "",
        location_source: str = "configured",
    ) -> dict[str, Any]:
        location = data.get("location") or {}
        current = data.get("current") or {}
        condition = current.get("condition") or {}
        forecast_days = (data.get("forecast") or {}).get("forecastday") or []

        try:
            local_tz = ZoneInfo(str(location.get("tz_id") or "UTC"))
        except Exception:
            local_tz = dt_timezone.utc
        timezone_offset = int(datetime.now(local_tz).utcoffset().total_seconds())

        try:
            condition_code = int(condition.get("code") or 0)
        except (TypeError, ValueError):
            condition_code = 0
        is_day = bool(current.get("is_day", 1))

        updated_epoch = current.get("last_updated_epoch") or fetched_at
        updated_local = datetime.fromtimestamp(int(updated_epoch), tz=local_tz)

        return {
            "ok": True,
            "configured": True,
            "cached": False,
            "stale": False,
            "location": location.get("name") or location_name or self.config.location_name,
            "country": location.get("country"),
            "latitude": latitude,
            "longitude": longitude,
            "location_source": location_source,
            "temperature": current.get("temp_c"),
            "feels_like": current.get("feelslike_c"),
            "humidity": current.get("humidity"),
            "pressure": current.get("pressure_mb"),
            "wind_speed": self._kph_to_ms(current.get("wind_kph")),
            "wind_gust": self._kph_to_ms(current.get("gust_kph")),
            "wind_direction": current.get("wind_degree"),
            "condition": condition.get("text"),
            "description": condition.get("text"),
            "icon_code": condition.get("icon"),
            "condition_key": _condition_key(condition_code, is_day),
            "sunrise": None,
            "sunset": None,
            "timezone_offset": timezone_offset,
            "updated_local": updated_local.strftime("%H:%M"),
            "fetched_at": int(fetched_at),
            "forecast": self._build_forecast(forecast_days[1:4]),
        }

    @staticmethod
    def _kph_to_ms(value: Any) -> float | None:
        try:
            return float(value) / 3.6
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_forecast(forecast_days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for index, entry in enumerate(forecast_days):
            day = entry.get("day") or {}
            condition = day.get("condition") or {}
            try:
                condition_code = int(condition.get("code") or 0)
            except (TypeError, ValueError):
                condition_code = 0

            try:
                precipitation_probability = int(day.get("daily_chance_of_rain") or 0)
            except (TypeError, ValueError):
                precipitation_probability = 0

            result.append({
                "date": entry.get("date"),
                "day_offset": index + 1,
                "temp_min": day.get("mintemp_c"),
                "temp_max": day.get("maxtemp_c"),
                "condition": condition.get("text"),
                "description": condition.get("text"),
                "icon_code": condition.get("icon"),
                # Forecast days summarize the whole day, not a single
                # daytime sample - always resolve the day-flavored icon,
                # same convention as OpenWeatherProvider's forecast cards.
                "condition_key": _condition_key(condition_code, True),
                "precipitation_probability": precipitation_probability,
            })
        return result

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        message = str(exc)
        if "1002" in message or "2008" in message or "401" in message:
            return "WeatherAPI key is invalid or not active yet"
        if "2007" in message or "429" in message:
            return "WeatherAPI request limit reached"
        return message or "Weather data unavailable"
