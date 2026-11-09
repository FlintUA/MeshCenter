# Sentinel Journal - MeshCenter

## 2026-10-30 - Permissions-Policy Header Hardening
**Vulnerability:** MeshCenter web application lacked a `Permissions-Policy` header, allowing the browser to permit features like geolocation, microphone, camera, payment, and USB by default or if requested by embedded content/contexts.
**Learning:** Modern web security headers provide defense-in-depth against unauthorized browser feature access. MeshCenter is an IoT web interface controlling radios; restricting browser capabilities (like camera, microphone, geolocation, payment, usb) prevents malicious third-party scripts or iframes from abusing browser APIs.
**Prevention:** Always maintain comprehensive baseline security headers in `api/security_headers.py`.
