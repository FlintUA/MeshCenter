"""Global response security headers (H3).

Registered once from server.py. Uses setdefault so a route that sets its own
value (e.g. api_attachments.py's `Content-Security-Policy: sandbox`) is never
overwritten. Only touches headers - safe for the streaming /video_feed MJPEG
response, whose body is never read here.

No Content-Security-Policy yet: inline handlers still exist (see
tests/test_check_inline_handlers.py's ratchet).

Referrer-Policy is `strict-origin-when-cross-origin` rather than the stricter
`same-origin`: the map loads tiles from tile.openstreetmap.org, whose tile
usage policy requires a Referer, and `same-origin` strips it from every
cross-origin subresource request. This still never leaks a path or query
string off-site, and same-origin requests keep the full Referer.
"""

# Security headers set across all HTTP responses:
# - X-Content-Type-Options: nosniff prevents MIME type sniffing exploitation.
# - X-Frame-Options: SAMEORIGIN guards against clickjacking.
# - Referrer-Policy: strict-origin-when-cross-origin protects user path privacy while supporting OSM tiles.
# - X-Permitted-Cross-Domain-Policies: none blocks Flash/PDF cross-domain policy execution.
# - Cross-Origin-Opener-Policy: same-origin isolates browsing context from cross-origin popups/openers.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def register_security_headers(app):
    @app.after_request
    def _add_security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
