"""Tests for security headers added to Flask responses.
"""

def test_security_headers_present(server_module):
    client = server_module.app.test_client()
    response = client.get("/api/sensors")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
