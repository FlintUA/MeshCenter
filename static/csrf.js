// ============================================================
// CSRF — project-wide X-CSRF-Token helper
// (docs/attachments/internal-rest-api.md §2.3). No build step, no
// dependencies; loaded before the other API scripts in index.html.
//
// One small wrapper around window.fetch that transparently adds the
// session's CSRF token (read from a <meta name="csrf-token"> tag) to
// same-origin /api/ requests whose method is anything other than GET/HEAD
// (POST/PUT/PATCH/DELETE/OPTIONS and any future method — the same rule the
// backend enforces, so the two can't drift). It never touches GET/HEAD,
// cross-origin URLs, or non-/api/ paths, so the token can't leak to the
// Leaflet CDN, the weather/map providers, or any other third party.
// Caller-supplied options (body, method, credentials, signal, cache, mode,
// redirect, Headers, FormData, Request, ...) are preserved untouched — the
// helper builds a shallow copy rather than mutating the caller's objects,
// inherits a Request input's own headers when init.headers isn't supplied,
// and it never forces a Content-Type (so FormData keeps its automatic
// multipart boundary).
//
// Failure behaviour: a 403 whose JSON body names error_code ===
// "csrf_invalid" is never retried or replayed. It shows at most one
// persistent notification telling the user to reload the page (which
// fetches a fresh token) — checked whenever the original request was a
// same-origin mutating /api/ call, even if the page's token was missing or
// stale. Every other response — including ordinary 403s — passes through
// unchanged, body unconsumed.
// ============================================================
(function (window) {
    'use strict';

    if (window.MeshCenterCSRF) {
        return; // already installed (idempotent against double-load)
    }

    var TOKEN_META_SELECTOR = 'meta[name="csrf-token"]';
    var CSRF_HEADER = 'X-CSRF-Token';
    // Same rule as the backend's _CSRF_SAFE_METHODS: only GET and HEAD are
    // safe. Every other explicit method — including OPTIONS and any future
    // verb — mutates and needs the token. No four-method allowlist that can
    // drift out of sync with api/api_auth.py.
    var SAFE_METHODS = { GET: true, HEAD: true };

    var originalFetch = window.fetch;
    var reloadPromptShown = false;

    function getToken() {
        if (typeof document === 'undefined') return '';
        var meta = document.querySelector(TOKEN_META_SELECTOR);
        if (!meta) return '';
        return meta.getAttribute('content') || '';
    }

    // Resolve the request URL against the current page, the same way fetch
    // itself would, so same-origin/relative/absolute URLs all compare
    // consistently. A Request's own (already absolute) .url is picked up by
    // the object branch. Returns null when the URL can't be parsed.
    function resolveURL(input) {
        try {
            if (typeof input === 'string') {
                return new URL(input, window.location.href);
            }
            if (input && typeof input.url === 'string') {
                return new URL(input.url, window.location.href);
            }
        } catch (e) {
            return null;
        }
        return null;
    }

    function isUnsafeMethod(method) {
        var m = String(method || '').toUpperCase();
        // An empty/missing method defaults to GET and is safe.
        return m !== '' && SAFE_METHODS[m] !== true;
    }

    function extractMethod(input, init) {
        if (init && init.method) return init.method;
        if (input && input.method) return input.method;
        return 'GET';
    }

    function needsToken(input, init) {
        if (!isUnsafeMethod(extractMethod(input, init))) return false;
        var url = resolveURL(input);
        if (!url) return false;
        // Same-origin /api/ paths only — never cross-origin (Leaflet CDN,
        // weather/map providers), never the rest of the site.
        if (url.origin !== window.location.origin) return false;
        return url.pathname.indexOf('/api/') === 0;
    }

    // Fallback for environments with no global Headers (never in a browser,
    // never in Node 18+): flatten the header input to a plain object.
    function toHeaderObject(headers) {
        var obj = {};
        if (!headers) return obj;
        if (Array.isArray(headers)) {
            headers.forEach(function (pair) { obj[pair[0]] = pair[1]; });
        } else if (typeof headers === 'object') {
            Object.keys(headers).forEach(function (name) { obj[name] = headers[name]; });
        }
        return obj;
    }

    // Delete every header whose name matches case-insensitively, so a
    // pre-existing x-csrf-token is replaced rather than duplicated in the
    // (browser/Node-never) no-Headers fallback path.
    function removeHeaderCaseInsensitive(obj, targetName) {
        var lower = targetName.toLowerCase();
        Object.keys(obj).forEach(function (name) {
            if (name.toLowerCase() === lower) delete obj[name];
        });
    }

    // Returns a new init with the CSRF header added, leaving the caller's
    // init and Headers (and a Request input's Headers) untouched.
    //
    // Header-source precedence follows fetch itself: if init.headers is
    // explicitly supplied that set wins (the CSRF header is added/replaced
    // into a copy of it); otherwise a Request input's own headers are copied;
    // otherwise a fresh empty set. The copied set is normalized through the
    // standard Headers API, so X-CSRF-Token is added or replaced
    // case-insensitively (a caller's lowercase x-csrf-token is overwritten,
    // never duplicated) regardless of the original header shape.
    function addTokenHeader(input, init, token) {
        var next = Object.assign({}, init || {});
        var base = next.headers;
        if (base === undefined || base === null) {
            base = (input && input.headers) ? input.headers : {};
        }

        var out;
        if (typeof Headers !== 'undefined') {
            out = new Headers(base);
            out.set(CSRF_HEADER, token);
        } else {
            out = toHeaderObject(base);
            removeHeaderCaseInsensitive(out, CSRF_HEADER);
            out[CSRF_HEADER] = token;
        }
        next.headers = out;
        return next;
    }

    function showReloadPrompt() {
        // At most one persistent prompt per page lifetime — several requests
        // failing at once (e.g. a burst of parallel fetches after the session
        // token went stale) must not stack a wall of identical banners.
        if (reloadPromptShown) return;
        reloadPromptShown = true;

        var message;
        var actionLabel;
        if (window.I18N && typeof window.I18N.tOrFallback === 'function') {
            message = window.I18N.tOrFallback(
                'csrf.expired_reload', {},
                'Security check expired. Please reload the page to continue.'
            );
            actionLabel = window.I18N.tOrFallback('csrf.reload_action', {}, 'Reload');
        } else {
            message = 'Security check expired. Please reload the page to continue.';
            actionLabel = 'Reload';
        }

        // Prefer the app's own persistent notification center (chat.js's
        // addNotification) with a reload action. It's defined after csrf.js
        // loads but always exists by the time any request can fail.
        if (typeof window.addNotification === 'function') {
            window.addNotification(message, 'error', {
                persistent: true,
                actionLabel: actionLabel,
                action: function () { window.location.reload(); },
            });
            return;
        }

        // Rare fallback: chat.js hasn't loaded yet. Still persistent, still
        // safe, still tells the user to reload rather than auto-reloading.
        var banner = document.createElement('div');
        banner.textContent = message;
        banner.setAttribute('role', 'alert');
        banner.style.cssText =
            'position:fixed;top:0;left:0;right:0;background:#b91c1c;color:#fff;' +
            'padding:12px 16px;z-index:2147483647;font-family:sans-serif;text-align:center;';
        if (document.body) document.body.appendChild(banner);
    }

    // Returns null for anything that isn't our CSRF rejection; otherwise a
    // promise resolving to the parsed body when it names csrf_invalid (or
    // null). Uses clone() so the caller's own response body stays readable.
    function csrfRejectReason(response) {
        if (response.status !== 403) return null;
        try {
            return response.clone().json().then(function (body) {
                return body && body.error_code === 'csrf_invalid' ? body : null;
            }).catch(function () {
                return null;
            });
        } catch (e) {
            return null;
        }
    }

    function wrappedFetch(input, init) {
        // needsToken() (same-origin mutating /api/) decides BOTH whether the
        // token is added and whether a 403 is inspected for csrf_invalid. The
        // second must not depend on a token actually being present: a missing
        // or stale page token is precisely when the reload instruction is
        // needed, and it must never fire for cross-origin or safe calls.
        var needs = needsToken(input, init);
        if (needs) {
            var token = getToken();
            if (token) {
                init = addTokenHeader(input, init, token);
            }
        }

        var promise = originalFetch.call(window, input, init);

        if (needs) {
            promise = promise.then(function (response) {
                var reason = csrfRejectReason(response);
                if (reason) {
                    // Fire-and-forget: show the prompt without disturbing the
                    // caller's own .then() chain, and without consuming the
                    // response body (clone() already made a private copy).
                    reason.then(function (body) {
                        if (body) showReloadPrompt();
                    });
                }
                return response;
            });
        }

        return promise;
    }

    window.fetch = wrappedFetch;
    window.MeshCenterCSRF = {
        version: '2',
        getToken: getToken,
        needsToken: needsToken,
        addTokenHeader: addTokenHeader,
        showReloadPrompt: showReloadPrompt,
    };
})(window);
