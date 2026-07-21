"""Weather API routes for MeshCenter."""

from flask import jsonify, request


def register_weather_routes(app, weather_service, resolve_location=None):
    @app.get("/api/weather/current")
    def api_weather_current():
        force = request.args.get("refresh", "").lower() in {"1", "true", "yes"}

        location = resolve_location() if callable(resolve_location) else None
        location = location if isinstance(location, dict) else {}

        payload = weather_service.get_current(
            force=force,
            latitude=location.get("latitude"),
            longitude=location.get("longitude"),
            location_name=location.get("name", ""),
            location_source=location.get("source", "configured"),
        )
        return jsonify(payload), 200 if payload.get("ok") else 503
