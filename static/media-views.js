// ============================================================
// MEDIA GALLERY - pure view logic (G1). No DOM, no fetch: sorting, selection, the zip size rule and the lightbox
// neighbours, unit-tested in tests/frontend/test_media_views.mjs. static/media.js is the UI around it.
// ============================================================
(function (root) {
    'use strict';

    const SORTS = ['date_desc', 'date_asc', 'name', 'size', 'camera'];
    const VIEWS = ['large', 'small', 'list'];
    const DEFAULT_SORT = 'date_desc';
    const DEFAULT_VIEW = 'large';
    // must match GALLERY_ZIP_MAX_BYTES in api/api_camera.py (the server enforces it; this lets the page say so BEFORE the tap)
    const ZIP_MAX_BYTES = 250 * 1024 * 1024;
    const BULK_MAX = 500;

    function normalizeSort(value) {
        return SORTS.indexOf(value) !== -1 ? value : DEFAULT_SORT;
    }

    function normalizeView(value) {
        return VIEWS.indexOf(value) !== -1 ? value : DEFAULT_VIEW;
    }

    // The view is remembered per kind of device: settings.media.view_mobile on a phone, view_desktop elsewhere
    // (a single `view` saved by the first G1 build seeds both until the device chooses).
    function viewField(isMobile) {
        return isMobile ? 'view_mobile' : 'view_desktop';
    }

    function pickView(media, isMobile) {
        const saved = media && typeof media === 'object' ? media : {};
        const own = saved[viewField(isMobile)];
        return normalizeView(VIEWS.indexOf(own) !== -1 ? own : saved.view);
    }

    function byText(a, b) {
        return String(a || '').localeCompare(String(b || ''), undefined, { sensitivity: 'base', numeric: true });
    }

    function timeOf(item) {
        const ts = Number(item && item.modified_ts);
        return Number.isFinite(ts) ? ts : 0;
    }

    function nameOf(item) {
        return String((item && (item.display_name || item.filename)) || '');
    }

    // A new array, never the caller's. Ties always fall back to the newest first, so the order is stable and predictable.
    function sortItems(items, sort) {
        const list = Array.isArray(items) ? items.slice() : [];
        const newestFirst = (a, b) => timeOf(b) - timeOf(a) || byText(nameOf(a), nameOf(b));
        switch (normalizeSort(sort)) {
            case 'date_asc':
                return list.sort((a, b) => timeOf(a) - timeOf(b) || byText(nameOf(a), nameOf(b)));
            case 'name':
                return list.sort((a, b) => byText(nameOf(a), nameOf(b)) || newestFirst(a, b));
            case 'size':
                return list.sort((a, b) => (Number(b.size) || 0) - (Number(a.size) || 0) || newestFirst(a, b));
            case 'camera':
                // named cameras first (A to Z), pictures without a camera name (the local camera) last
                return list.sort((a, b) => {
                    const ca = String((a && a.camera) || '');
                    const cb = String((b && b.camera) || '');
                    if (!ca !== !cb) return ca ? -1 : 1;
                    return byText(ca, cb) || newestFirst(a, b);
                });
            default:
                return list.sort(newestFirst);
        }
    }

    function formatBytes(bytes) {
        const n = Number(bytes);
        if (!Number.isFinite(n) || n < 0) return '-';
        if (n < 1024) return n + ' B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10 * 1024 ? 1 : 0) + ' KB';
        if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0) + ' MB';
        return (n / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    }

    function formatResolution(item) {
        const w = Number(item && item.width);
        const h = Number(item && item.height);
        return Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0 ? w + 'x' + h : '';
    }

    // ---- selection: a Set of filenames ------------------------------------------------------------------------

    function toggle(selection, filename) {
        const next = new Set(selection);
        if (next.has(filename)) next.delete(filename); else next.add(filename);
        return next;
    }

    function allSelected(selection, items) {
        return Array.isArray(items) && items.length > 0 && items.every((item) => selection.has(item.filename));
    }

    // "Select all": everything shown, or nothing when everything already is
    function toggleAll(selection, items) {
        if (allSelected(selection, items)) return new Set();
        return new Set((items || []).map((item) => item.filename));
    }

    // a deleted or vanished picture cannot stay selected
    function prune(selection, items) {
        const present = new Set((items || []).map((item) => item.filename));
        return new Set(Array.from(selection).filter((name) => present.has(name)));
    }

    function totals(selection, items) {
        let count = 0;
        let bytes = 0;
        (items || []).forEach((item) => {
            if (selection.has(item.filename)) {
                count += 1;
                bytes += Number(item.size) || 0;
            }
        });
        return { count, bytes };
    }

    // What the bulk bar may do with this selection. `bytes` over the zip cap disables the download and carries the numbers
    // for the message shown above the bar.
    function bulkState(selection, items) {
        const sum = totals(selection, items);
        return {
            count: sum.count,
            bytes: sum.bytes,
            any: sum.count > 0,
            tooMany: sum.count > BULK_MAX,
            zipTooLarge: sum.bytes > ZIP_MAX_BYTES,
            canDownload: sum.count > 0 && sum.count <= BULK_MAX && sum.bytes <= ZIP_MAX_BYTES,
            canDelete: sum.count > 0 && sum.count <= BULK_MAX,
            zipMaxBytes: ZIP_MAX_BYTES,
        };
    }

    // ---- lightbox: previous / next without wrapping --------------------------------------------------------------

    function neighbour(index, delta, length) {
        const target = index + delta;
        return target >= 0 && target < length ? target : null;
    }

    // after the picture at `index` was deleted from a list of `length` pictures: the one to show now, or null (none left)
    function indexAfterDelete(index, length) {
        const remaining = length - 1;
        if (remaining <= 0) return null;
        return Math.min(index, remaining - 1);
    }

    // ---- selection mode and long press (touch) ----------------------------------------------------------------------

    const LONG_PRESS_MS = 500;
    const LONG_PRESS_SLOP_PX = 10;

    // selection mode = something is selected; the toolbar switches state and a touch tap toggles instead of opening
    function selectionMode(selection) {
        return Boolean(selection && selection.size > 0);
    }

    // What a click on a picture does: 'toggle' while selecting (touch only: a mouse has the checkbox), else 'open'
    function tapAction(selection, touch) {
        return touch && selectionMode(selection) ? 'toggle' : 'open';
    }

    // A long press that is cancelled by moving more than `slop` px (a scroll) or by lifting early. All timing is
    // injected, so the unit test needs no real clock. end() says whether the press had already fired.
    function createLongPress(options) {
        const delay = (options && options.delay) || LONG_PRESS_MS;
        const slop = (options && options.slop) || LONG_PRESS_SLOP_PX;
        const setTimer = (options && options.setTimer) || ((fn, ms) => setTimeout(fn, ms));
        const clearTimer = (options && options.clearTimer) || ((id) => clearTimeout(id));
        const onFire = (options && options.onFire) || function () {};
        let timer = null;
        let origin = null;
        let fired = false;
        function cancel() {
            if (timer !== null) clearTimer(timer);
            timer = null;
            origin = null;
        }
        return {
            start(x, y, context) {
                cancel();
                fired = false;
                origin = { x, y };
                timer = setTimer(() => {
                    timer = null;
                    fired = true;
                    onFire(context);
                }, delay);
            },
            move(x, y) {
                if (!origin) return;
                if (Math.hypot(x - origin.x, y - origin.y) > slop) cancel();
            },
            cancel,
            end() {
                cancel();
                const did = fired;
                fired = false;
                return did;
            },
            get pending() { return timer !== null; },
        };
    }

    root.MCMediaViews = {
        LONG_PRESS_MS, LONG_PRESS_SLOP_PX, selectionMode, tapAction, createLongPress,
        SORTS, VIEWS, DEFAULT_SORT, DEFAULT_VIEW, ZIP_MAX_BYTES, BULK_MAX,
        normalizeSort, normalizeView, viewField, pickView, sortItems, formatBytes, formatResolution,
        toggle, allSelected, toggleAll, prune, totals, bulkState, neighbour, indexAfterDelete,
    };
})(typeof window !== 'undefined' ? window : globalThis);
