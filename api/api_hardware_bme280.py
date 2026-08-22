"""REST route for the BME280 hardware card - task 26, second I2C device
after RTC (task 23). Read-only, single route: BME280 needs no config.txt
overlay and nothing to configure once the I2C bus itself is enabled (see
hardware/bme280_service.py's module docstring), so unlike
api/api_hardware_i2c.py there's no POST route here at all.
"""

from __future__ import annotations

from flask import jsonify

from hardware import bme280_service


def register_hardware_bme280_routes(app, handle_errors):
    @app.route("/api/hardware/bme280")
    @handle_errors
    def api_hardware_bme280_status():
        return jsonify(bme280_service.get_status())
