// ============================================================
// TAB1: tablet layout (769-1100 px) and touch input at any width.
//
// The desktop shell is three flex columns (Base | workspace | Nodes), each
// sidebar a fixed width. Below roughly 1300 px that leaves too little for the
// center column - at 800 px it dropped to exactly 0 px (see
// TAB1_phase0_audit.md). This module:
//   - never lets the center column drop below ~520 px at ANY width: if the two
//     push-mode sidebars would squeeze it further, the left one collapses
//     first, then the right (this also fixes an ordinary narrow desktop
//     window, not just a tablet);
//   - in the 769-1100 px band, collapses both sidebars by default and lets the
//     existing dock buttons open either one as an overlay drawer instead of
//     pushing it back into the row;
//   - on a coarse pointer in the 1101-1400 px landscape band, shows the
//     center + Nodes panel by default and keeps Base as a drawer;
//   - remembers a manual open/close choice per band (per device, in
//     localStorage - "Interface preferences for this device" is already how
//     Workspace describes itself) until the band itself changes.
//
// Phones (<= 768 px, MOB1/MOB2 own that band) and a normal desktop width with
// a mouse are unaffected: band() returns 'phone' or 'desktop' for them and the
// drawer/band-default code paths never run; only the generic 520px floor can
// still apply to a desktop window narrow enough to need it.
// ============================================================
(function () {
    'use strict';

    var MIN_CENTER = 520;
    var MEMORY_KEY = 'meshcenter.workspace.bandPanels';
    var BAND_DEFAULTS = {
        tabletPortrait: { left: false, right: false },
        landscapeTouch: { left: false, right: true }
    };

    var mqPhone = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 768px)') : null;
    var mqPortrait = typeof window.matchMedia === 'function' ? window.matchMedia('(min-width: 769px) and (max-width: 1100px)') : null;
    var mqTouchLandscape = typeof window.matchMedia === 'function'
        ? window.matchMedia('(min-width: 1101px) and (max-width: 1400px) and (pointer: coarse)') : null;

    var lastBand = null;
    var backdrop = null;
    var lastFocused = null;
    var memory = loadMemory();

    function loadMemory() {
        try { return JSON.parse(localStorage.getItem(MEMORY_KEY) || '{}'); } catch (error) { return {}; }
    }

    function saveMemory() {
        try { localStorage.setItem(MEMORY_KEY, JSON.stringify(memory)); } catch (error) { /* private mode, etc. */ }
    }

    function band() {
        if (mqPhone && mqPhone.matches) return 'phone';
        if (mqPortrait && mqPortrait.matches) return 'tabletPortrait';
        if (mqTouchLandscape && mqTouchLandscape.matches) return 'landscapeTouch';
        return 'desktop';
    }

    // Which side renders as a drawer in the given band - the other renders pushed into the row as before.
    function modeOf(side, currentBand) {
        if (currentBand === 'tabletPortrait') return 'drawer';
        if (currentBand === 'landscapeTouch') return side === 'left' ? 'drawer' : 'push';
        return 'push';
    }

    // Workspace (static/chat.js) is a top-level `const`, visible to this script's own top-level
    // code via the shared global scope classic <script> tags get - but never as `window.Workspace`
    // (only `var`/function declarations attach to `window`; `const` does not, even in the global
    // scope of a non-module script). Guard with typeof, not a `window.` property check.
    function hasWorkspace() { return typeof Workspace !== 'undefined'; }

    function isOpen(side) {
        if (!hasWorkspace()) return false;
        return side === 'left' ? Boolean(Workspace.state.leftPanel) : Boolean(Workspace.state.rightPanel);
    }

    function setOpen(side, open) {
        if (side === 'left') { if (typeof window.setBasePanelVisible === 'function') window.setBasePanelVisible(open); }
        else if (typeof window.setNodesPanelVisible === 'function') window.setNodesPanelVisible(open);
    }

    function panelEl(side) { return document.getElementById(side === 'left' ? 'baseSidebar' : 'sidebar'); }
    function buttonEl(side) { return document.getElementById(side === 'left' ? 'toggleBaseSidebarBtn' : 'toggleSidebarBtn'); }

    function ensureBackdrop() {
        if (backdrop) return backdrop;
        if (!document.body || typeof document.createElement !== 'function') return null;
        backdrop = document.createElement('div');
        backdrop.id = 'tabletDrawerBackdrop';
        backdrop.className = 'tablet-drawer-backdrop';
        backdrop.setAttribute('aria-hidden', 'true');
        backdrop.addEventListener('click', function () { closeAllDrawers(true); });
        document.body.appendChild(backdrop);
        return backdrop;
    }

    function renderDom() {
        var currentBand = band();
        if (!document.body) return;
        if (currentBand === 'phone') {
            document.body.removeAttribute('data-sidebar-mode-left');
            document.body.removeAttribute('data-sidebar-mode-right');
            if (backdrop) backdrop.classList.remove('tablet-drawer-backdrop-open');
            return;
        }
        document.body.setAttribute('data-sidebar-mode-left', modeOf('left', currentBand));
        document.body.setAttribute('data-sidebar-mode-right', modeOf('right', currentBand));
        var left = panelEl('left');
        var right = panelEl('right');
        var leftDrawerOpen = modeOf('left', currentBand) === 'drawer' && isOpen('left');
        var rightDrawerOpen = modeOf('right', currentBand) === 'drawer' && isOpen('right');
        if (left) left.classList.toggle('sidebar-drawer-open', leftDrawerOpen);
        if (right) right.classList.toggle('sidebar-drawer-open', rightDrawerOpen);
        var backdropEl = ensureBackdrop();
        if (backdropEl) backdropEl.classList.toggle('tablet-drawer-backdrop-open', leftDrawerOpen || rightDrawerOpen);
    }

    // Only a manual (user-initiated) choice is remembered, and only for the bands that have a
    // band default to begin with - 'desktop' already persists through Workspace's own state/localStorage.
    function remember(side, open) {
        var currentBand = band();
        if (currentBand !== 'tabletPortrait' && currentBand !== 'landscapeTouch') return;
        memory[currentBand] = memory[currentBand] || {};
        memory[currentBand][side] = open;
        saveMemory();
    }

    function measureCenterWidth() {
        var currentBand = band();
        var total = window.innerWidth;
        ['left', 'right'].forEach(function (side) {
            if (modeOf(side, currentBand) !== 'push' || !isOpen(side)) return;
            var el = panelEl(side);
            if (el) total -= el.getBoundingClientRect().width;
        });
        return total;
    }

    // The center column never drops below MIN_CENTER: collapse push-mode panels, left first,
    // to make room. Automatic - never recorded as a manual choice. Drawer-mode panels are never
    // touched here (closed or open, they never take space from the center).
    function runSafetyNet() {
        var currentBand = band();
        if (currentBand === 'phone') return;
        var order = ['left', 'right'];
        for (var i = 0; i < order.length; i += 1) {
            if (measureCenterWidth() >= MIN_CENTER) return;
            var side = order[i];
            if (modeOf(side, currentBand) !== 'push' || !isOpen(side)) continue;
            setOpen(side, false);
            renderDom();
        }
    }

    function focusDrawer(side) {
        lastFocused = document.activeElement;
        var panel = panelEl(side);
        if (!panel) return;
        if (!panel.hasAttribute('tabindex')) panel.setAttribute('tabindex', '-1');
        try { panel.focus({ preventScroll: true }); } catch (error) { panel.focus(); }
    }

    function returnFocus(side) {
        var button = buttonEl(side);
        if (button && typeof button.focus === 'function') { button.focus(); return; }
        if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
    }

    function toggleDrawer(side, fromUser) {
        var currentBand = band();
        var nextOpen = !isOpen(side);
        if (nextOpen) {
            // one drawer open at a time - opening the other closes this band's first one
            var other = side === 'left' ? 'right' : 'left';
            if (modeOf(other, currentBand) === 'drawer' && isOpen(other)) {
                setOpen(other, false);
                if (fromUser) remember(other, false);
            }
        }
        setOpen(side, nextOpen);
        if (fromUser) remember(side, nextOpen);
        renderDom();
        if (nextOpen) focusDrawer(side); else returnFocus(side);
    }

    function closeAllDrawers(fromUser) {
        var currentBand = band();
        ['left', 'right'].forEach(function (side) {
            if (modeOf(side, currentBand) === 'drawer' && isOpen(side)) {
                setOpen(side, false);
                if (fromUser) remember(side, false);
            }
        });
        renderDom();
    }

    function applyBandEntry(force) {
        var currentBand = band();
        if (currentBand === lastBand && !force) { runSafetyNet(); return; }
        lastBand = currentBand;
        if (currentBand === 'phone') { renderDom(); return; }
        var defaults = BAND_DEFAULTS[currentBand] || { left: isOpen('left'), right: isOpen('right') };
        var remembered = memory[currentBand];
        var left = remembered && typeof remembered.left === 'boolean' ? remembered.left : defaults.left;
        var right = remembered && typeof remembered.right === 'boolean' ? remembered.right : defaults.right;
        setOpen('left', left);
        setOpen('right', right);
        renderDom();
        runSafetyNet();
    }

    // Intercept the two existing dock toggle buttons at the capture phase - same technique
    // static/mobile-layout.js already uses for the phone band - so their normal (push-mode)
    // click handler, wired in chat.js's initializeWorkspace(), never runs for a drawer-mode side.
    document.addEventListener('click', function (event) {
        var target = event.target && event.target.closest ? event.target.closest('button') : null;
        if (!target) return;
        var side = target.id === 'toggleBaseSidebarBtn' ? 'left' : target.id === 'toggleSidebarBtn' ? 'right' : null;
        if (!side) return;
        var currentBand = band();
        if (currentBand === 'phone') return; // MOB1 owns this button entirely on a phone
        if (modeOf(side, currentBand) === 'drawer') {
            event.preventDefault();
            event.stopImmediatePropagation();
            toggleDrawer(side, true);
            return;
        }
        // push mode: let the default (desktop) handler run this tick, then remember the choice
        // for this band and re-check the 520px floor against the new state.
        setTimeout(function () { remember(side, isOpen(side)); runSafetyNet(); }, 0);
    }, true);

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') closeAllDrawers(true);
    });

    var resizeTimer = null;
    function onResize() {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () { applyBandEntry(false); }, 120);
    }

    [mqPhone, mqPortrait, mqTouchLandscape].forEach(function (mq) {
        if (!mq) return;
        var onChange = function () { applyBandEntry(false); };
        if (typeof mq.addEventListener === 'function') mq.addEventListener('change', onChange);
        else if (typeof mq.addListener === 'function') mq.addListener(onChange);
    });
    window.addEventListener('resize', onResize);
    window.addEventListener('orientationchange', function () { setTimeout(function () { applyBandEntry(true); }, 50); });

    function init() {
        applyBandEntry(true);
        window.addEventListener('load', function () { applyBandEntry(true); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();

    window.MCTabletLayout = {
        band: band,
        modeOf: modeOf,
        toggleDrawer: toggleDrawer,
        closeAllDrawers: closeAllDrawers,
        runSafetyNet: runSafetyNet,
        measureCenterWidth: measureCenterWidth,
        _applyBandEntry: applyBandEntry
    };
})();
