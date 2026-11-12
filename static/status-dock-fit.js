// ============================================================
// STATUS-DOCK-FIT: the bottom dock is a 3-column CSS grid (static/style-
// part2.css's .status-dock, 1fr / minmax(92px,max-content) / 1fr). Below a
// certain width the two 1fr tracks (dock-left, dock-right) are allowed to
// shrink below their content's natural size (grid tracks, unlike flex
// items, are never forced to overflow their container - the CONTENT inside
// them overflows the track instead, with nothing to stop it since dock-left/
// dock-right use the default overflow: visible), which is what the TAB1
// screenshots showed as the brand name / uptime / status / CPU text drawing
// over each other at tablet widths.
//
// This measures the real (natural) width each dock section's children need
// against the width the grid track actually gave it, and - only when that
// section does not fit - hides its lowest-priority pieces first, in a fixed
// order, until it does. Nothing is ever truncated or overlapped; a hidden
// item is simply gone until there is room again.
//
// Right side, lowest priority first: uptime, then CPU/RAM/TEMP, then the
// version number (brand name and the online/offline status always stay).
// Left side, lowest priority first: the "Map"/"Workspace" button text
// labels (the icons and the current tab name stay - that's the one piece of
// context a drawer-mode dock still needs to show).
// ============================================================
(function () {
    'use strict';

    var FIT_HIDE = 'dock-fit-hide';

    function isHiddenByDisplay(el) {
        return getComputedStyle(el).display === 'none';
    }

    function neededWidth(container) {
        var gapValue = parseFloat(getComputedStyle(container).columnGap) || 0;
        var total = 0;
        var count = 0;
        Array.prototype.forEach.call(container.children, function (child) {
            if (isHiddenByDisplay(child)) return;
            total += child.getBoundingClientRect().width;
            count += 1;
        });
        if (count > 1) total += gapValue * (count - 1);
        return total;
    }

    function fits(container) {
        if (!container) return true;
        return neededWidth(container) <= container.getBoundingClientRect().width + 1;
    }

    function setTierHidden(selectors, hide) {
        selectors.forEach(function (selector) {
            var el = document.querySelector(selector);
            if (el) el.classList.toggle(FIT_HIDE, hide);
        });
    }

    var rightTiers = [
        ['#dockUptimeLeadSeparator', '#baseUptimeBadge', '#baseUptimeSeparator'],
        ['#dockContextText'],
        ['.dock-version-text']
    ];
    var leftTiers = [
        ['#mapMenuBtn .dock-btn-label'],
        ['#workspaceMenuBtn .dock-btn-label']
    ];

    function applyTiers(container, tiers) {
        if (!container) return;
        tiers.forEach(function (tier) { setTierHidden(tier, false); });
        if (fits(container)) return;
        for (var i = 0; i < tiers.length; i += 1) {
            setTierHidden(tiers[i], true);
            if (fits(container)) return;
        }
    }

    function run() {
        applyTiers(document.getElementById('dockRight'), rightTiers);
        applyTiers(document.getElementById('dockLeft'), leftTiers);
    }

    var timer = null;
    function schedule() {
        clearTimeout(timer);
        timer = setTimeout(run, 60);
    }

    window.addEventListener('resize', schedule);
    window.addEventListener('orientationchange', function () { setTimeout(run, 150); });

    // Re-check whenever the dock's own content changes (tab switch rewrites
    // dockWorkspaceLabel/dockContextText, CPU polling rewrites the metrics
    // text) - childList/characterData only, so toggling our own fit-hide
    // class (an attribute mutation) never re-triggers this observer.
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
