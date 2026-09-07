// ============================================================
// CSRF — project-wide X-CSRF-Token helper
// (docs/attachments/internal-rest-api.md §2.3). No build step, no
// dependencies; loaded before the other API scripts in index.html.
//
// One small wrapper around window.fetch that transparently adds the
// session's CSRF token (read from a <meta name="csrf-token"> tag) to
// same-origin /api/ requests whose method mutates state (POST/PUT/
// PATCH/DELETE). It never touches GET/HEAD, cross-origin URLs, or
// non-/api/ paths, so the token can't leak to the Leaflet CDN, the
// weather/map providers, or any other third party. Caller-supplied
// options (body, method, credentials, signal, Headers, FormData,
// Request, ...) are preserved untouched — the helper builds a shallow
// copy rather than mutating the caller's objects, and it never forces a
// Content-Type (so FormData keeps its automatic multipart boundary).
//
// Failure behaviour: a 403 whose JSON body names error_code ===
// "csrf_invalid" is never retried or replayed. It shows one persistent
// notification telling the user to reload the page (which fetches a fresh
// token). Every other response — including ordinary 403s — passes through
// unchanged, body unconsumed.
// ============================================================
(function (window) {
    'use strict';

    if (window.MeshCenterCSRF) {
        return; // already installed (idempotent against double-load)
    }

    var TOKEN_META_SELECTOR = 'meta[name="csrf-token"]';
    var CSRF_HEADER = 'X-CSRF-Token';
    var UNSAFE_METHODS = { POST: true, PUT: true, PATCH: true, DELETE: true };

    var originalFetch = window.fetch;

    function getToken() {
        if (typeof document === 'undefined') return '';
        var meta = document.querySelector(TOKEN_META_SELECTOR);
        if (!meta) return '';
        return meta.getAttribute('content') || '';
    }

    // Resolve the request URL against the current page, the same way fetch
    // itself would, so same-origin/relative/absolute URLs all compare
    // consistently. Returns null when the URL can't be parsed.
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
        return UNSAFE_METHODS[String(method || '').toUpperCase()] === true;
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

    // Returns a new init with the header added, leaving the caller's init
    // and Headers untouched. Handles the three header shapes fetch accepts
    // (Headers instance, array of [name, value] pairs, plain object).
    function addTokenHeader(init, token) {
        var next = Object.assign({}, init || {});
        var headers = next.headers;

        if (typeof Headers !== 'undefined' && headers instanceof Headers) {
            var copied = new Headers(headers);
            copied.set(CSRF_HEADER, token);
            next.headers = copied;
        } else if (Array.isArray(headers)) {
            next.headers = headers.slice();
            next.headers.push([CSRF_HEADER, token]);
        } else if (headers && typeof headers === 'object') {
            next.headers = Object.assign({}, headers);
            next.headers[CSRF_HEADER] = token;
        } else {
            next.headers = {};
            next.headers[CSRF_HEADER] = token;
        }
        return next;
    }

    function showReloadPrompt() {
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
        var token = getToken();
        if (token && needsToken(input, init)) {
            init = addTokenHeader(init, token);
        }

        var promise = originalFetch.call(window, input, init);

        if (token) {
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
        version: '1',
        getToken: getToken,
        needsToken: needsToken,
        addTokenHeader: addTokenHeader,
        showReloadPrompt: showReloadPrompt,
    };
})(window);
