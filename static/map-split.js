// ============================================================
// MAP-SPLIT: a draggable, remembered height split between the map and the
// active tab content when Map layout mode is "Split" (static/chat.js's
// MapLayout). Replaces the plain `flex: 0 0 50%` CSS basis on #mapView with
// an explicit pixel height, computed from the real measured space - on a
// tablet in landscape (drawer-mode sidebars, taller pointer:coarse touch
// targets) the pure-CSS percentage basis was observed collapsing to a
// ~140px strip in practice (TAB1 landscape bug report, portrait unaffected).
// Measuring the real remaining space and setting it explicitly is robust to
// whatever else changes the surrounding chrome's height, not just this one
// case.
//
// The ratio (the map's share of the available split height) is remembered
// per device band + orientation (static/tablet-layout.js's band(), the same
// "device kind" MCTabletLayout and this module share) in localStorage, and
// is adjustable with a draggable, touch-friendly divider between the map
// and the tab content (static/ui-kit.css gives it a 44px tap target under
// pointer: coarse, matching the rest of this PR's touch-target rules).
//
// Phones (<= 768px) are untouched: MOB2 already owns that band's layout and
// was not reported broken ("portrait is fine" - the bug is landscape-tablet
// only).
// ============================================================
(function () {
    'use strict';

    var RATIO_MIN = 0.25;
    var RATIO_MAX = 0.75;
    var RATIO_DEFAULT = 0.5;
    var MIN_PANE = 120;
    var MEM_PREFIX = 'meshcenter.mapSplit.ratio.';

    var divider = null;
    var dragging = false;
    var dragPointerId = null;
    var dragStartY = 0;
    var dragStartRatio = RATIO_DEFAULT;
    var dragAvailable = 0;
    var currentRatio = RATIO_DEFAULT;
    var currentKey = null;

    function isPhoneWidth() { return window.innerWidth <= 768; }

    // static/chat.js declares `const MapLayout = {...}` at top level of a
    // classic script - shared via the page's global lexical scope, never as
    // `window.MapLayout` (see static/tablet-layout.js's identical note about
    // `Workspace`). Guard with typeof, not a `window.` property check.
    function hasMapLayout() { return typeof MapLayout !== 'undefined'; }

    function deviceKind() {
        if (window.MCTabletLayout && typeof window.MCTabletLayout.band === 'function') {
            return window.MCTabletLayout.band();
        }
        return isPhoneWidth() ? 'phone' : 'desktop';
    }

    function orientationKind() {
        return window.innerHeight >= window.innerWidth ? 'portrait' : 'landscape';
    }

    function memKey() { return MEM_PREFIX + deviceKind() + '-' + orientationKind(); }

    function clampRatio(value) {
        if (!Number.isFinite(value)) return RATIO_DEFAULT;
        return Math.min(RATIO_MAX, Math.max(RATIO_MIN, value));
    }

    function loadRatio(key) {
        try {
            var value = parseFloat(localStorage.getItem(key));
            if (Number.isFinite(value)) return clampRatio(value);
        } catch (error) { /* private mode, etc. */ }
        return RATIO_DEFAULT;
    }

    function saveRatio(key, value) {
        try { localStorage.setItem(key, String(value)); } catch (error) { /* ignore */ }
    }

    function ensureCurrentRatio() {
        var key = memKey();
        if (key !== currentKey) {
            currentKey = key;
            currentRatio = loadRatio(key);
        }
        return currentRatio;
    }

    function activePartner() {
        var candidates = [
            document.querySelector('.chat-area > .chat-panels'),
            document.getElementById('videoView'),
            document.getElementById('mediaView'),
            document.getElementById('devicesView')
        ];
        for (var i = 0; i < candidates.length; i += 1) {
            var el = candidates[i];
            if (el && el.style.display && el.style.display !== 'none') return el;
        }
        return null;
    }

    function ensureDivider() {
        if (divider) return divider;
        var area = document.querySelector('.chat-area');
        if (!area) return null;
        divider = document.createElement('div');
        divider.className = 'map-split-divider';
        divider.setAttribute('role', 'separator');
        divider.setAttribute('aria-orientation', 'horizontal');
        divider.setAttribute('aria-label', 'Resize map');
        divider.setAttribute('tabindex', '0');
        var handle = document.createElement('span');
        handle.className = 'map-split-divider-handle';
        handle.setAttribute('aria-hidden', 'true');
        divider.appendChild(handle);
        area.appendChild(divider);
        divider.addEventListener('pointerdown', onDividerPointerDown);
        divider.addEventListener('keydown', onDividerKeydown);
        return divider;
    }

    function removeDivider() {
        if (divider && divider.parentNode) divider.parentNode.removeChild(divider);
        divider = null;
    }

    function resetInlineOverrides() {
        var mapView = document.getElementById('mapView');
        if (mapView) {
            mapView.style.flex = '';
            mapView.style.maxHeight = '';
            mapView.style.height = '';
        }
        var partner = activePartner();
        if (partner) partner.style.flex = '';
    }

    // Both the generic (order:1/2) and the later "restoration fix" (order:2/3)
    // rules in static/style-part4.css give chatHeader the same order value as
    // the divider in "top" position; ties break by DOM source order, so
    // inserting the divider just before chatHeader is enough to land it
    // between the map and the header/content block without touching the
    // header's or partner's own order.
    function placeDivider(d, area, position) {
        if (position === 'top') {
            d.style.order = '2';
            var chatHeader = document.getElementById('chatHeader');
            if (chatHeader && chatHeader.parentNode === area && d.nextElementSibling !== chatHeader) {
                area.insertBefore(d, chatHeader);
            }
        } else {
            d.style.order = '10';
            if (d.parentNode !== area) area.appendChild(d);
        }
    }

    function measureReserved(divEl) {
        var chatHeader = document.getElementById('chatHeader');
        var tabs = document.querySelector('.main-content-tabs');
        var reserved = divEl ? divEl.getBoundingClientRect().height : 16;
        if (chatHeader && chatHeader.style.display !== 'none') reserved += chatHeader.getBoundingClientRect().height;
        if (tabs) reserved += tabs.getBoundingClientRect().height;
        return reserved;
    }

    function layout() {
        if (isPhoneWidth() || !hasMapLayout()) { removeDivider(); resetInlineOverrides(); return; }
        var area = document.querySelector('.chat-area');
        if (!area || !area.classList.contains('map-layout-split')) { removeDivider(); resetInlineOverrides(); return; }

        var mapView = document.getElementById('mapView');
        var partner = activePartner();
        if (!mapView || !partner) { removeDivider(); return; }

        var d = ensureDivider();
        if (!d) return;
        placeDivider(d, area, MapLayout.state.position);

        var ratio = ensureCurrentRatio();
        var areaRect = area.getBoundingClientRect();
        var reserved = measureReserved(d);
        var available = Math.max(MIN_PANE * 2, areaRect.height - reserved);
        var mapHeight = Math.round(available * ratio);
        mapHeight = Math.min(Math.max(mapHeight, MIN_PANE), available - MIN_PANE);

        mapView.style.flex = '0 0 ' + mapHeight + 'px';
        mapView.style.maxHeight = 'none';
        mapView.style.height = mapHeight + 'px';
        partner.style.flex = '1 1 auto';
    }

    function onDividerPointerDown(event) {
        var area = document.querySelector('.chat-area');
        if (!area || !divider) return;
        dragging = true;
        dragPointerId = event.pointerId;
        dragStartY = event.clientY;
        dragStartRatio = ensureCurrentRatio();
        var areaRect = area.getBoundingClientRect();
        dragAvailable = Math.max(MIN_PANE * 2, areaRect.height - measureReserved(divider));
        try { divider.setPointerCapture(dragPointerId); } catch (error) { /* ignore */ }
        document.addEventListener('pointermove', onDividerPointerMove);
        document.addEventListener('pointerup', onDividerPointerUp);
        event.preventDefault();
    }

    function onDividerPointerMove(event) {
        if (!dragging || !dragAvailable) return;
        var deltaY = event.clientY - dragStartY;
        var position = hasMapLayout() ? MapLayout.state.position : 'bottom';
        // Map on top: dragging the divider down grows the map. Map on
        // bottom: dragging the divider down grows the pane above it (the
        // tab content), i.e. shrinks the map.
        var sign = position === 'top' ? 1 : -1;
        currentRatio = clampRatio(dragStartRatio + (sign * deltaY) / dragAvailable);
        currentKey = memKey();
        layout();
    }

    function onDividerPointerUp() {
        if (!dragging) return;
        dragging = false;
        document.removeEventListener('pointermove', onDividerPointerMove);
        document.removeEventListener('pointerup', onDividerPointerUp);
        if (divider && dragPointerId !== null) {
            try { divider.releasePointerCapture(dragPointerId); } catch (error) { /* ignore */ }
        }
        dragPointerId = null;
        saveRatio(memKey(), currentRatio);
    }

    function onDividerKeydown(event) {
        var step = 0.05;
        var ratio = ensureCurrentRatio();
        if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') ratio = clampRatio(ratio - step);
        else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') ratio = clampRatio(ratio + step);
        else return;
        event.preventDefault();
        currentRatio = ratio;
        saveRatio(memKey(), ratio);
        layout();
    }

    var relayoutTimer = null;
    function scheduleLayout() {
        clearTimeout(relayoutTimer);
        relayoutTimer = setTimeout(layout, 60);
    }

    window.addEventListener('resize', scheduleLayout);
    window.addEventListener('orientationchange', function () { setTimeout(layout, 150); });

    var observer = new MutationObserver(scheduleLayout);
    function startObserving() {
        var area = document.querySelector('.chat-area');
        if (!area) { setTimeout(startObserving, 200); return; }
        observer.observe(area, { attributes: true, attributeFilter: ['class', 'style'], subtree: true });
        layout();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startObserving);
    else startObserving();

    window.MCMapSplit = { layout: layout, _memKey: memKey };
})();
