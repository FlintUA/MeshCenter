// ============================================================
// Shared size formatter (UX5). One place for "1.1 KB" / "1,1 КБ" so every part of the UI (Files list, Details, storage
// summary, send dialogs, gallery, system cards) writes sizes the same way. Units by UI language:
//   en, de: B, KB, MB, GB      ru, uk: Б, КБ, МБ, ГБ
// 1024-based; KB is the kilobyte (never "Kb"). Unit-tested in tests/frontend/test_format_size.mjs.
// ============================================================
(function (root) {
    'use strict';

    var UNITS = {
        en: ['B', 'KB', 'MB', 'GB'],
        de: ['B', 'KB', 'MB', 'GB'],
        ru: ['Б', 'КБ', 'МБ', 'ГБ'],
        uk: ['Б', 'КБ', 'МБ', 'ГБ']
    };

    function currentLocale() {
        var i18n = root && root.I18N;
        var loc = i18n && i18n.locale;
        if (!loc && root && root.document && root.document.documentElement) loc = root.document.documentElement.lang;
        loc = String(loc || 'en').toLowerCase().split(/[-_]/)[0];
        return UNITS[loc] ? loc : 'en';
    }

    // unit(0..3) in the UI language (or the one asked for)
    function unit(index, locale) {
        var list = UNITS[locale && UNITS[locale] ? locale : currentLocale()];
        return list[Math.max(0, Math.min(3, index))];
    }

    // format(1126) -> "1.1 KB"; null/NaN/negative -> "-". Under 1 KB no decimals; KB/MB one decimal below 10 units, none above
    // when opts.compact is set (the gallery's densest lines); otherwise one decimal, as Files always did.
    function format(bytes, opts) {
        opts = opts || {};
        if (bytes === null || bytes === undefined || bytes === '') return '-';
        var n = Number(bytes);
        if (!isFinite(n) || n < 0) return '-';
        var loc = opts.locale && UNITS[opts.locale] ? opts.locale : currentLocale();
        var u = UNITS[loc];
        var idx = 0;
        var value = n;
        while (idx < 3 && value >= 1024) { value /= 1024; idx++; }
        if (idx === 0) return String(Math.round(n)) + ' ' + u[0];
        var digits = opts.compact && value >= 10 && idx < 3 ? 0 : 1;
        return value.toFixed(digits) + ' ' + u[idx];
    }

    var api = { format: format, unit: unit, UNITS: UNITS };
    if (root) root.MCSize = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
