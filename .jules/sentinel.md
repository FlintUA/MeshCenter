## 2026-08-17 - Open Redirect Vulnerability in Auth Flow
**Vulnerability:** The login endpoint checked `next_url.startswith("/") and not next_url.startswith("//")` which allowed malicious parameters containing backslashes (e.g. `/\evil.com`) or schemes/netlocs to bypass validation and trigger open redirects after authentication.
**Learning:** Checking leading slashes alone is insufficient for URL redirect safety because browsers (and WHATWG URL parsing standards) normalize backslashes `\` to forward slashes `/`, transforming `/\host` or `/\\host` into protocol-relative URLs `//host`.
**Prevention:** Always validate target redirect URLs using `urllib.parse.urlsplit` to ensure no `netloc` or `scheme` exists, and explicitly reject any URL containing backslashes `\` or starting with `//`.
