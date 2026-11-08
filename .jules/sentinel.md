# Sentinel Security Journal

## 2026-11-02 - Enforce Permissions-Policy on HTTP responses
**Vulnerability:** Browsers grant embedded/delegated contexts access to sensitive hardware and APIs (e.g., client-side camera, microphone, geolocation, payment, usb) unless explicitly restricted by response headers.
**Learning:** MeshCenter streams backend hardware camera feeds over MJPEG and renders node locations on an interactive Leaflet map, but the web UI itself does not access the browser's own camera/microphone, payment, WebUSB, or geolocation APIs. MeshCenter's standalone relay-server already enforced a restrictive `Permissions-Policy`, but the primary Flask application in Core was missing it from its global `SECURITY_HEADERS`.
**Prevention:** Include `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()` in `api/security_headers.py` so all responses adhere to least privilege and forbid client-side device/sensor access by default.
