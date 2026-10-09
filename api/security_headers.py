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

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def register_security_headers(app):
    @app.after_request
    def _add_security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
