// ============================================================
// I18N — minimal client-side translation runtime.
//
// No build step, no bundler (see CLAUDE.md) — this is the whole
// implementation, not a stripped-down build of a larger library.
// Catalogs live at /static/i18n/<locale>.json, one flat/dotted-key
// JSON file per locale. English is always loaded as a fallback so a
// missing key/locale never shows up blank in the UI.
//
// Before translating catalog content, read static/i18n/README.md — it
// lists terms that must stay untranslated in every locale (Meshtastic,
// LongFast, PSK, etc.) and documents the one genuinely ambiguous case
// ("Channel") that was already investigated against the code.
// ============================================================
(function (window) {
    'use strict';

    var SUPPORTED_LOCALES = ['en', 'de', 'ru', 'uk'];
    var FALLBACK_LOCALE = 'en';
    // Bumped manually alongside catalog content changes, same convention as
    // the ?v= query strings on <script>/<link> tags in index.html.
    var CATALOG_VERSION = '20260904-theme-stage6-1';

    var catalogs = {};      // locale -> flattened {dottedKey: value}
    var loadPromises = {};  // locale -> in-flight/completed fetch promise
    var currentLocale = FALLBACK_LOCALE;

    function flatten(obj, prefix, out) {
        out = out || {};
        Object.keys(obj || {}).forEach(function (key) {
            var value = obj[key];
            var dotted = prefix ? (prefix + '.' + key) : key;
            if (
                value !== null &&
                typeof value === 'object' &&
                !Array.isArray(value) &&
                !isPluralForm(value)
            ) {
                flatten(value, dotted, out);
            } else {
                out[dotted] = value;
            }
        });
        return out;
    }

    function isPluralForm(value) {
        if (!value || typeof value !== 'object') return false;
        return ['one', 'few', 'many', 'other', 'zero', 'two'].some(function (form) {
            return Object.prototype.hasOwnProperty.call(value, form);
        });
    }

    function resolveLocale(preferred) {
        var candidate = String(preferred || '').trim().toLowerCase();

        if (candidate && candidate !== 'auto' && SUPPORTED_LOCALES.indexOf(candidate) !== -1) {
            return candidate;
        }

        // "auto" (or unset/unsupported): fall back to the browser's language.
        var browserLangs = (navigator.languages && navigator.languages.length)
            ? navigator.languages
            : [navigator.language || FALLBACK_LOCALE];

        for (var i = 0; i < browserLangs.length; i++) {
            var short = String(browserLangs[i] || '').trim().toLowerCase().slice(0, 2);
            if (SUPPORTED_LOCALES.indexOf(short) !== -1) {
                return short;
            }
        }

        return FALLBACK_LOCALE;
    }

    function fetchCatalog(locale) {
        if (loadPromises[locale]) {
            return loadPromises[locale];
        }

        loadPromises[locale] = fetch('/static/i18n/' + locale + '.json?v=' + CATALOG_VERSION)
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            })
            .then(function (raw) {
                catalogs[locale] = flatten(raw, '', {});
            })
            .catch(function (error) {
                console.warn('[I18N] Failed to load catalog "' + locale + '":', error);
                catalogs[locale] = catalogs[locale] || {};
            });

        return loadPromises[locale];
    }

    function lookupRaw(key) {
        var fromCurrent = catalogs[currentLocale] && catalogs[currentLocale][key];
        if (fromCurrent !== undefined) return fromCurrent;

        var fromFallback = catalogs[FALLBACK_LOCALE] && catalogs[FALLBACK_LOCALE][key];
        if (fromFallback !== undefined) return fromFallback;

        return undefined;
    }

    function interpolate(template, params) {
        if (typeof template !== 'string' || !params) return template;
        return template.replace(/\{(\w+)\}/g, function (match, name) {
            return Object.prototype.hasOwnProperty.call(params, name)
                ? String(params[name])
                : match;
        });
    }

    /**
     * Look up `key` (dotted path into the active catalog) and interpolate
     * `{placeholder}` tokens from `params`. Falls back to the English
     * catalog, then to a visible "[[key]]" marker so gaps are obvious
     * instead of silently blank.
     */
    function t(key, params) {
        var value = lookupRaw(key);

        if (value === undefined) {
            return '[[' + key + ']]';
        }

        if (isPluralForm(value)) {
            // A plural-shaped catalog entry used via t() (no count) — best
            // effort: prefer "other", the category every locale defines.
            return interpolate(value.other || value.one || '', params);
        }

        return interpolate(value, params);
    }

    /**
     * Like t(), but returns `fallback` instead of a "[[key]]" marker when
     * the key isn't in any loaded catalog — for translating server-sent
     * error/message codes, where a missing catalog entry should silently
     * fall back to the server's own English string rather than show a
     * broken-looking marker to the user. (t() itself intentionally keeps
     * the visible marker — that's the right behavior for static markup,
     * where a missing key is a bug to notice, not a runtime condition to
     * degrade gracefully from.)
     */
    function tOrFallback(key, params, fallback) {
        var value = lookupRaw(key);

        if (value === undefined) {
            return fallback;
        }

        if (isPluralForm(value)) {
            return interpolate(value.other || value.one || '', params);
        }

        return interpolate(value, params);
    }

    /**
     * Like t(), but selects the correct CLDR plural category for `count`
     * via the native Intl.PluralRules (covers ru/uk's one/few/many/other,
     * not just English's one/other). `params` is interpolated in addition
     * to an implicit {count}.
     */
    function plural(key, count, params) {
        var value = lookupRaw(key);
        var allParams = Object.assign({ count: count }, params || {});

        if (value === undefined) {
            return '[[' + key + ']]';
        }

        if (!isPluralForm(value)) {
            // Not a plural-shaped entry — treat as a plain string.
            return interpolate(value, allParams);
        }

        var category;
        try {
            category = new Intl.PluralRules(currentLocale).select(count);
        } catch (error) {
            category = count === 1 ? 'one' : 'other';
        }

        var chosen = value[category] !== undefined ? value[category] : value.other;
        return interpolate(chosen, allParams);
    }

    /**
     * Applies translations to the static DOM shell under `root` via
     * data-i18n / data-i18n-placeholder / data-i18n-title /
     * data-i18n-aria-label attributes. Only covers markup present at call
     * time — content rendered later by chat.js/media.js/weather.js must
     * call t()/plural() directly in its own template literals.
     */
    function applyStaticDom(root) {
        root = root || document;

        root.querySelectorAll('[data-i18n]').forEach(function (el) {
            el.textContent = t(el.getAttribute('data-i18n'));
        });
        root.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            el.setAttribute('placeholder', t(el.getAttribute('data-i18n-placeholder')));
        });
        root.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            el.setAttribute('title', t(el.getAttribute('data-i18n-title')));
        });
        root.querySelectorAll('[data-i18n-aria-label]').forEach(function (el) {
            el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria-label')));
        });
    }

    function formatDate(epochSeconds, opts) {
        try {
            return new Intl.DateTimeFormat(currentLocale, opts).format(new Date(epochSeconds * 1000));
        } catch (error) {
            return new Date(epochSeconds * 1000).toISOString();
        }
    }

    function formatNumber(value, opts) {
        try {
            return new Intl.NumberFormat(currentLocale, opts).format(value);
        } catch (error) {
            return String(value);
        }
    }

    /**
     * Resolves the active locale (explicit setting or "auto" -> browser
     * language) and loads its catalog plus the English fallback. Safe to
     * call multiple times (e.g. after a settings change).
     */
    function init(preferredLanguage) {
        currentLocale = resolveLocale(preferredLanguage);

        var toLoad = [fetchCatalog(currentLocale)];
        if (currentLocale !== FALLBACK_LOCALE) {
            toLoad.push(fetchCatalog(FALLBACK_LOCALE));
        }

        return Promise.all(toLoad).then(function () {
            return currentLocale;
        });
    }

    window.I18N = {
        SUPPORTED_LOCALES: SUPPORTED_LOCALES,
        init: init,
        t: t,
        tOrFallback: tOrFallback,
        plural: plural,
        applyStaticDom: applyStaticDom,
        formatDate: formatDate,
        formatNumber: formatNumber,
        get locale() { return currentLocale; },
    };
})(window);
