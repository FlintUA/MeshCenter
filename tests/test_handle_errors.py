import pytest
from flask import Flask


def test_handle_errors_no_traceback_when_debug_false(server_module):
    handle_errors = server_module.handle_errors
    app = Flask(__name__)
    app.debug = False

    @app.route("/test-error")
    @handle_errors
    def failing_route():
        raise RuntimeError("Simulated failure")

    client = app.test_client()
    response = client.get("/test-error")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert data["error"] == "Simulated failure"
    assert "traceback" not in data


def test_handle_errors_no_traceback_when_debug_true(server_module):
    handle_errors = server_module.handle_errors
    app = Flask(__name__)
    app.debug = True

    @app.route("/test-error")
    @handle_errors
    def failing_route():
        raise RuntimeError("Simulated debug failure")

    client = app.test_client()
    response = client.get("/test-error")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert data["error"] == "Simulated debug failure"
    assert "traceback" not in data
