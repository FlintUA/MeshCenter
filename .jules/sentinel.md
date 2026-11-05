# Sentinel Journal

## 2026-10-24 - Permissions-Policy Security Header for Browser Feature Restriction
**Vulnerability:** Core web application responses lacked the `Permissions-Policy` header, leaving powerful browser APIs (such as client camera, microphone, geolocation, payment, and WebUSB) unrestricted by default in the browser context.
**Learning:** MeshCenter controls attached camera and radio peripherals exclusively through server-side daemons on the host (streaming MJPEG/MSE video downstream), so client browser hardware APIs are not required. A restricted `Permissions-Policy` prevents malicious embedded contexts or XSS escalation from querying host location or device interfaces.
**Prevention:** Centralize global HTTP security headers in `api/security_headers.py` and ensure sensitive browser capabilities are restricted by default unless explicitly needed.
