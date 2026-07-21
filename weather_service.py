"""OpenWeather backend service for MeshCenter.

The browser never receives the API key. Current conditions and the compact
three-day forecast are cached together so all connected clients reuse the same
provider data.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class WeatherConfig:
    api_key: str
    latitude: float
    longitude: float
    location_name: str = ""
    language: str = "en"
    cache_seconds: int = 600
    timeout_seconds: int = 8


class OpenWeatherService:
    CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
    FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"

    def __init__(self, config: WeatherConfig):
        self.config = config
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0
        self._cache_key: tuple[float, float] | None = None

    def get_current(
        self,
        force: bool = False,
        latitude: float | None = None,
        longitude: float | None = None,
        location_name: str = "",
        location_source: str = "configured",
    ) -> dict[str, Any]:
        """Return current weather plus the next three complete forecast days."""
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
                current = self._request(self.CURRENT_URL, latitude, longitude)
                forecast = self._request(self.FORECAST_URL, latitude, longitude)
                payload = self._build_payload(
                    current, forecast, now, latitude, longitude,
                    location_name=location_name,
                    location_source=location_source,
                )
                self._cache = dict(payload)
                self._cache_time = now
                self._cache_key = cache_key
                return payload
            except Exception as exc:
                # Keep the module useful during a temporary provider/network
                # outage by returning the last successful response.
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

    def _request(self, endpoint: str, latitude: float, longitude: float) -> dict[str, Any]:
        params = urllib.parse.urlencode({
            "lat": latitude,
            "lon": longitude,
            "appid": self.config.api_key,
            "units": "metric",
            "lang": self.config.language,
        })
        request = urllib.request.Request(
            f"{endpoint}?{params}",
            headers={"User-Agent": "MeshCenter/1.1"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                details = json.loads(exc.read().decode("utf-8"))
                message = details.get("message") or str(exc)
            except Exception:
                message = str(exc)
            raise RuntimeError(f"OpenWeather {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenWeather connection failed: {exc.reason}") from exc

    def _build_payload(
        self,
        current: dict[str, Any],
        forecast: dict[str, Any],
        fetched_at: float,
        latitude: float,
        longitude: float,
        location_name: str = "",
        location_source: str = "configured",
    ) -> dict[str, Any]:
        main = current.get("main") or {}
        wind = current.get("wind") or {}
        weather = (current.get("weather") or [{}])[0]
        sys_data = current.get("sys") or {}

        timezone_offset = int(
            current.get("timezone")
            or (forecast.get("city") or {}).get("timezone")
            or 0
        )
        local_tz = timezone(timedelta(seconds=timezone_offset))
        provider_time = datetime.fromtimestamp(
            int(current.get("dt") or fetched_at), tz=local_tz
        )

        return {
            "ok": True,
            "configured": True,
            "cached": False,
            "stale": False,
            "location": current.get("name") or location_name or self.config.location_name,
            "country": sys_data.get("country") or (forecast.get("city") or {}).get("country"),
            "latitude": latitude,
            "longitude": longitude,
            "location_source": location_source,
            "temperature": main.get("temp"),
            "feels_like": main.get("feels_like"),
            "humidity": main.get("humidity"),
            "pressure": main.get("pressure"),
            "wind_speed": wind.get("speed"),
            "wind_gust": wind.get("gust"),
            "wind_direction": wind.get("deg"),
            "condition": weather.get("main"),
            "description": weather.get("description"),
            "icon_code": weather.get("icon"),
            "sunrise": sys_data.get("sunrise"),
            "sunset": sys_data.get("sunset"),
            "timezone_offset": timezone_offset,
            "updated_local": provider_time.strftime("%H:%M"),
            "fetched_at": int(fetched_at),
            "forecast": self._build_three_day_forecast(forecast, local_tz),
        }

    @staticmethod
    def _build_three_day_forecast(
        forecast: dict[str, Any], local_tz: timezone
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        today = datetime.now(local_tz).date()

        for item in forecast.get("list") or []:
            timestamp = item.get("dt")
            if not timestamp:
                continue
            local_dt = datetime.fromtimestamp(int(timestamp), tz=local_tz)
            day_offset = (local_dt.date() - today).days
            if 1 <= day_offset <= 3:
                enriched = dict(item)
                enriched["_local_dt"] = local_dt
                grouped[local_dt.date().isoformat()].append(enriched)

        result: list[dict[str, Any]] = []
        for date_key in sorted(grouped.keys())[:3]:
            items = grouped[date_key]
            temperatures_min = [
                float((entry.get("main") or {}).get("temp_min"))
                for entry in items
                if (entry.get("main") or {}).get("temp_min") is not None
            ]
            temperatures_max = [
                float((entry.get("main") or {}).get("temp_max"))
                for entry in items
                if (entry.get("main") or {}).get("temp_max") is not None
            ]

            # The sample nearest local noon gives a stable daytime icon.
            representative = min(
                items,
                key=lambda entry: abs(entry["_local_dt"].hour - 12),
            )
            representative_weather = (representative.get("weather") or [{}])[0]

            descriptions = [
                ((entry.get("weather") or [{}])[0].get("description") or "")
                for entry in items
            ]
            common_description = Counter(filter(None, descriptions)).most_common(1)

            precipitation_probability = max(
                [float(entry.get("pop") or 0) for entry in items] or [0]
            )

            result.append({
                "date": date_key,
                "day_offset": (datetime.fromisoformat(date_key).date() - today).days,
                "temp_min": min(temperatures_min) if temperatures_min else None,
                "temp_max": max(temperatures_max) if temperatures_max else None,
                "condition": representative_weather.get("main"),
                "description": (
                    common_description[0][0]
                    if common_description
                    else representative_weather.get("description")
                ),
                "icon_code": representative_weather.get("icon"),
                "precipitation_probability": round(precipitation_probability * 100),
            })

        return result

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        message = str(exc)
        if "401" in message:
            return "OpenWeather API key is invalid or not active yet"
        if "429" in message:
            return "OpenWeather request limit reached"
        return message or "Weather data unavailable"
