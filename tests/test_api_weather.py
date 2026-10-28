"""Tests for weather API routes in api/api_weather.py."""

import pytest
from flask import Flask

from api.api_weather import register_weather_routes
from weather.weather_manager import WeatherManager
from weather.providers.openweather import OpenWeatherProvider, WeatherConfig


class ErrorHandlingTestProvider(OpenWeatherProvider):
    def get_current(self, *args, **kwargs):
        raise RuntimeError("Secret weather internal error details")

    def is_configured(self):
        raise RuntimeError("Secret config check internal error")


@pytest.fixture
def weather_client():
    app = Flask(__name__)

    from functools import wraps

    def test_handle_errors(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception:
                return {"ok": False, "error_id": "err_12345", "error": "An internal error occurred. Please quote ID err_12345"}, 500
        return wrapper

    provider = ErrorHandlingTestProvider(WeatherConfig(api_key="testkey1234567890123456", latitude=55.75, longitude=37.61))
    manager = WeatherManager({"openweather": provider}, active_id="openweather")

    register_weather_routes(
        app,
        weather_manager=manager,
        resolve_location=lambda: {"latitude": 55.75, "longitude": 37.61},
        secrets_path="/tmp/test_weather_secrets.py",
        handle_errors=test_handle_errors,
    )

    return app.test_client()


def test_weather_current_uncaught_exception_sanitized(weather_client):
    response = weather_client.get("/api/weather/current")
    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert "error_id" in data
    assert "Secret weather internal error details" not in response.get_data(as_text=True)


def test_weather_config_uncaught_exception_sanitized(weather_client):
    response = weather_client.get("/api/weather/config")
    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert "error_id" in data
    assert "Secret config check internal error" not in response.get_data(as_text=True)
