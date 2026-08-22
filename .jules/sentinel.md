## 2026-08-17 - Sanitize next_url Parameters to Prevent Open Redirects
**Vulnerability:** Open redirect vulnerability in `/login` route handler where `next` parameters such as `/\example.com` or `/\\example.com` bypassed `next_url.startswith("/")` and `next_url.startswith("//")` checks in browsers that normalize backslashes to forward slashes.
**Learning:** Checking only `startswith("/")` and `startswith("//")` is insufficient for `next` URL redirection because browsers handle backslashes (`\`) differently during URL resolution.
**Prevention:** Always parse `next` URLs using `urllib.parse.urlsplit` to verify that `scheme` and `netloc` are empty, reject URLs containing backslashes (`\`), and ensure the path strictly starts with `/` without leading double slashes or backslashes.
