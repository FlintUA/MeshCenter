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
        """Build compact daily cards from OpenWeather 3-hour forecast data.

        OpenWeather's /forecast endpoint does not provide ready-made daily
        summaries. Each card therefore has to be derived from several 3-hour
        samples. The representative state is selected from local daytime
        samples (09:00-18:00), grouping related OpenWeather condition IDs into
        broad categories so broken/overcast/scattered clouds count as the same
        general weather state.
        """
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

        def weather_category(entry: dict[str, Any]) -> str:
            weather_data = (entry.get("weather") or [{}])[0]
            try:
                weather_id = int(weather_data.get("id") or 0)
            except (TypeError, ValueError):
                weather_id = 0

            if 200 <= weather_id < 300:
                return "Thunderstorm"
            if 300 <= weather_id < 400:
                return "Drizzle"
            if 500 <= weather_id < 600:
                return "Rain"
            if 600 <= weather_id < 700:
                return "Snow"
            if 700 <= weather_id < 800:
                return "Atmosphere"
            if weather_id == 800:
                return "Clear"
            if 801 <= weather_id < 900:
                return "Clouds"

            return str(weather_data.get("main") or "Unknown")

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

            # Prefer the part of the day users normally understand as the
            # daily forecast. Night-time rain must not label the whole day as
            # rainy when the daylight hours are predominantly cloudy or clear.
            daytime_items = [
                entry for entry in items
                if 9 <= entry["_local_dt"].hour <= 18
            ] or items

            category_counts = Counter(
                weather_category(entry) for entry in daytime_items
            )
            highest_count = max(category_counts.values())
            tied_categories = {
                category
                for category, count in category_counts.items()
                if count == highest_count
            }

            # Resolve equal counts by the category represented closest to
            # local noon. This produces a stable, human-friendly summary while
            # still keeping frequency as the primary criterion.
            representative_pool = [
                entry for entry in daytime_items
                if weather_category(entry) in tied_categories
            ]
            nearest_to_noon = min(
                representative_pool,
                key=lambda entry: (
                    abs(entry["_local_dt"].hour - 12),
                    entry["_local_dt"],
                ),
            )
            dominant_category = weather_category(nearest_to_noon)

            matching_items = [
                entry for entry in daytime_items
                if weather_category(entry) == dominant_category
            ]
            representative = min(
                matching_items,
                key=lambda entry: (
                    abs(entry["_local_dt"].hour - 12),
                    entry["_local_dt"],
                ),
            )
            representative_weather = (representative.get("weather") or [{}])[0]

            daytime_pop_values = [
                float(entry.get("pop") or 0)
                for entry in daytime_items
            ]
            precipitation_probability = max(daytime_pop_values or [0])

            result.append({
                "date": date_key,
                "day_offset": (datetime.fromisoformat(date_key).date() - today).days,
                "temp_min": min(temperatures_min) if temperatures_min else None,
                "temp_max": max(temperatures_max) if temperatures_max else None,
                "condition": dominant_category,
                "description": representative_weather.get("description"),
                "icon_code": representative_weather.get("icon"),
                "precipitation_probability": round(precipitation_probability * 100),
                "representative_local_time": representative["_local_dt"].strftime("%H:%M"),
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
