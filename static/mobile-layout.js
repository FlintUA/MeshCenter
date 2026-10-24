// ============================================================
// MOB1: one screen at a time on a narrow viewport (<= 768 px).
//
// The desktop shell is three columns (Base | workspace | Nodes). Below 768 px
// there is no room for that, so only ONE of them is shown: the workspace by
// default, and the two dock buttons that hide/show the Base and Nodes panels on
// desktop open them as full-screen pages instead (a second press goes back).
// The pane lives in <body data-mobile-pane="..."> and is set only while the
// viewport is narrow; the CSS for it is inside the same media query (ui-kit.css,
// "MOB1"), so a wide viewport never sees any of this. The persisted desktop
// workspace state (Workspace.leftPanel / rightPanel) is never touched here.
// ============================================================
(function () {
    'use strict';

    var QUERY = '(max-width: 768px)';
    var BUTTON_PANES = { toggleBaseSidebarBtn: 'base', toggleSidebarBtn: 'nodes' };
    var pane = 'workspace';
    var mq = typeof window.matchMedia === 'function' ? window.matchMedia(QUERY) : null;

    function isMobile() {
        return Boolean(mq && mq.matches);
    }

    // Pressing the button of the pane that is already open goes back to the workspace.
    function nextPane(current, target) {
        if (target !== 'base' && target !== 'nodes') return 'workspace';
        return current === target ? 'workspace' : target;
    }

    function label(key, fallback) {
        var i18n = window.I18N;
        return i18n && typeof i18n.tOrFallback === 'function' ? i18n.tOrFallback(key, {}, fallback) : fallback;
    }

    // On a phone the button opens a page (or goes back); on desktop it hides/shows a column.
    function buttonLabel(id, active) {
        var which = BUTTON_PANES[id];
        if (isMobile()) {
            return active
                ? label('workspace.mobile_back', 'Back to the workspace')
                : label('workspace.mobile_open_' + which, which === 'base' ? 'Open Base page' : 'Open Nodes page');
        }
        var button = document.getElementById(id);
        var hidden = Boolean(button && button.classList.contains('panel-is-hidden'));
        var name = which === 'base' ? 'Base' : 'Nodes';
        return label('workspace.panel_' + (hidden ? 'show' : 'hide') + '_' + which, (hidden ? 'Show ' : 'Hide ') + name + ' panel');
    }

    function syncButtons() {
        Object.keys(BUTTON_PANES).forEach(function (id) {
            var button = document.getElementById(id);
            if (!button) return;
            var active = isMobile() && pane === BUTTON_PANES[id];
            button.classList.toggle('mobile-pane-active', active);
            var text = buttonLabel(id, active);
            // only when different: syncButtons also runs from the observer below
            if (button.title !== text) button.title = text;
            if (button.getAttribute('aria-label') !== text) button.setAttribute('aria-label', text);
            if (isMobile() && button.getAttribute('aria-pressed') !== String(active)) button.setAttribute('aria-pressed', String(active));
        });
    }

    function apply() {
        var body = document.body;
        if (!body) return;
        if (isMobile()) body.setAttribute('data-mobile-pane', pane);
        else body.removeAttribute('data-mobile-pane');
        syncButtons();
    }

    function setPane(next) {
        pane = next;
        apply();
        var scroller = document.querySelector('.main-content-tabs');
        if (scroller && pane === 'workspace') revealActiveTab();
    }

    // The tab row scrolls sideways on a phone: keep the open tab in view.
    function revealActiveTab() {
        if (!isMobile()) return; // never scroll anything on a wide viewport
        var active = document.querySelector('.main-content-tab.active');
        if (active && typeof active.scrollIntoView === 'function') {
            try { active.scrollIntoView({ inline: 'center', block: 'nearest' }); } catch (error) { /* older engine */ }
        }
    }

    document.addEventListener('click', function (event) {
        var target = event.target && event.target.closest ? event.target.closest('button') : null;
        if (!target) return;
        if (BUTTON_PANES[target.id] && isMobile()) {
            // capture phase: the desktop handler (hide/show a column) must not run on a phone
            event.preventDefault();
            event.stopImmediatePropagation();
            setPane(nextPane(pane, BUTTON_PANES[target.id]));
            return;
        }
        if (target.classList.contains('main-content-tab')) {
            // the tab handler switches the tab; the page it opens is the workspace
            if (isMobile() && pane !== 'workspace') setPane('workspace');
            setTimeout(revealActiveTab, 0);
        }
    }, true);

    // Choosing a channel or a node (both end in openChat) on the Base/Nodes page returns to the workspace,
    // where the conversation is. Wrapped once, here, so chat.js stays untouched.
    function wrapOpenChat() {
        if (typeof window.openChat !== 'function' || window.openChat.mobileWrapped) return;
        var original = window.openChat;
        var wrapped = function () {
            var result = original.apply(this, arguments);
            if (isMobile() && pane !== 'workspace') setPane('workspace');
            return result;
        };
        wrapped.mobileWrapped = true;
        window.openChat = wrapped;
    }

    if (mq) {
        var onChange = function () { apply(); };
        if (typeof mq.addEventListener === 'function') mq.addEventListener('change', onChange);
        else if (typeof mq.addListener === 'function') mq.addListener(onChange);
    }

    function init() {
        apply();
        // switchMainTab() also runs from code (deep links, "close" buttons), not only from a tab click
        var tabs = document.querySelector('.main-content-tabs');
        if (tabs && typeof MutationObserver === 'function') {
            new MutationObserver(function () { if (isMobile()) revealActiveTab(); })
                .observe(tabs, { attributes: true, attributeFilter: ['class'], subtree: true });
        }
        // Workspace.apply() / applyPanelState() write the desktop title and aria-pressed onto the buttons,
        // at start and whenever the language changes: put the phone ones back
        if (typeof MutationObserver === 'function') {
            var watcher = new MutationObserver(function () { if (isMobile()) syncButtons(); });
            Object.keys(BUTTON_PANES).forEach(function (id) {
                var button = document.getElementById(id);
                if (button) watcher.observe(button, { attributes: true, attributeFilter: ['title', 'aria-label', 'aria-pressed'] });
            });
        }
        // Workspace.apply() (desktop button state) runs after load: set ours again once it has
        window.addEventListener('load', function () { apply(); revealActiveTab(); });
        wrapOpenChat();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();

    window.MCMobileLayout = { nextPane: nextPane, isMobile: isMobile, setPane: setPane, getPane: function () { return pane; }, QUERY: QUERY, _wrapOpenChat: wrapOpenChat };
})();
