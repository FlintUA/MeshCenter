// ============================================================
// STATUS-DOCK-FIT: the bottom dock is a 3-column CSS grid (static/style-
// part2.css's .status-dock, 1fr / minmax(92px,max-content) / 1fr). Below a
// certain width the two 1fr tracks (dock-left, dock-right) are allowed to
// shrink below their content's natural size, and several of the controls
// inside them have their own min-width floor (the pointer:coarse 44px touch
// target rules this same PR added) that is SMALLER than their own content's
// natural width - so the controls themselves shrink, and their content
// overflows past their own box into whatever sits next (confirmed live in
// both directions: #dockContextText's CPU/RAM/TEMP cluster overflows
// LEFTWARD past its own box into the brand name on the right-aligned dock-
// right; #workspaceMenuBtn's own 44px-wide clamp overflows its "Workspace"
// label RIGHTWARD into whatever sits next on the left-aligned dock-left -
// which, with a long enough translated tab name and notification text, can
// itself be the center column's content bleeding back into dock-left).
//
// None of this shows up as scrollWidth > clientWidth on any element
// involved (confirmed live - a known quirk for overflow: visible flex
// children), so this does not rely on scrollWidth. It treats the whole dock
// as one left-to-right chain (left section, then center, then right) and,
// for each piece in the order it is meant to appear, measures the real
// painted extent of that piece AND everything inside it, checking that the
// extent does not start before the previous piece's extent ends.
//
// Only when the chain does not fit are the lowest-priority pieces hidden,
// in a fixed order, until it does. Nothing is ever truncated or overlapped;
// a hidden item is simply gone until there is room again. Lowest priority
// first: uptime, then the left dock's "Map"/"Workspace" button text labels
// (the icons and the current tab name stay), then CPU/RAM/TEMP, then the
// version number, then - only if even that is not enough - the center
// notification/status text. The brand name, the online/offline status and
// the current tab name are never hidden.
// ============================================================
(function () {
    'use strict';

    var FIT_HIDE = 'dock-fit-hide';
    var TOLERANCE = 1;

    var FULL_ORDER = [
        '.dock-layout-controls',
        '.dock-map-wrap',
        '#workspaceMenuBtn',
        '#dockWorkspaceLabel',
        '#dockVaultChip',
        '#notificationCenterBtn',
        '.dock-brand',
        '#dockUptimeLeadSeparator',
        '#baseUptimeBadge',
        '#baseUptimeSeparator',
        '#headerStatusText',
        '#dockContextText'
    ];

    var TIERS = [
        ['#dockUptimeLeadSeparator', '#baseUptimeBadge', '#baseUptimeSeparator'],
        ['#mapMenuBtn .dock-btn-label', '#workspaceMenuBtn .dock-btn-label'],
        ['#dockContextText'],
        ['.dock-version-text'],
        ['#dockStatusText']
    ];

    // The real painted left/right edges of an element AND everything inside
    // it - not just the element's own (possibly shrunk-by-its-parent) box.
    function extent(el) {
        var rect = el.getBoundingClientRect();
        var left = rect.left;
        var right = rect.right;
        var all = el.querySelectorAll('*');
        for (var i = 0; i < all.length; i += 1) {
            var r = all[i].getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue; // not rendered
            if (r.left < left) left = r.left;
            if (r.right > right) right = r.right;
        }
        return { left: left, right: right };
    }

    function fits() {
        var dock = document.getElementById('statusDock');
        if (!dock) return true;
        var prevRight = null;
        for (var i = 0; i < FULL_ORDER.length; i += 1) {
            var el = document.querySelector(FULL_ORDER[i]);
            if (!el || getComputedStyle(el).display === 'none') continue;
            var rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;
            var ext = extent(el);
            if (prevRight !== null && ext.left < prevRight - TOLERANCE) return false;
            prevRight = ext.right;
        }
        return true;
    }

    function setTierHidden(selectors, hide) {
        selectors.forEach(function (selector) {
            var el = document.querySelector(selector);
            if (el) el.classList.toggle(FIT_HIDE, hide);
        });
    }

    function run() {
        TIERS.forEach(function (tier) { setTierHidden(tier, false); });
        if (fits()) return;
        for (var i = 0; i < TIERS.length; i += 1) {
            setTierHidden(TIERS[i], true);
            if (fits()) return;
        }
    }

    var timer = null;
    function schedule() {
        clearTimeout(timer);
        timer = setTimeout(run, 60);
    }

    window.addEventListener('resize', schedule);
    window.addEventListener('orientationchange', function () { setTimeout(run, 150); });

    // Re-check whenever the dock's own content changes (tab switch rewrites
    // dockWorkspaceLabel/dockContextText/dockStatusText, CPU polling
    // rewrites the metrics text) - childList/characterData only, so
    // toggling our own fit-hide class (an attribute mutation) never
    // re-triggers this observer.
    var observer = new MutationObserver(schedule);
    function start() {
        var dock = document.getElementById('statusDock');
        if (!dock) { setTimeout(start, 200); return; }
        observer.observe(dock, { childList: true, subtree: true, characterData: true });
        run();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();

    window.MCDockFit = { run: run };
})();
