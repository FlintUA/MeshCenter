/* Runtime capabilities (Docker support). The server writes the capability
 * flags from meshsrv/runtime_env.py into <meta name="runtime-capabilities">.
 * Natively every "can_*" flag is true, so this file changes nothing there.
 * In container mode it hides host-only controls and shows an explanation. */
(function () {
    'use strict';
    var caps = {};
    try {
        var meta = document.querySelector('meta[name="runtime-capabilities"]');
        caps = meta ? JSON.parse(meta.getAttribute('content') || '{}') : {};
    } catch (e) {
        caps = {};
    }
    window.MC_RUNTIME = caps;
    if (caps.mode !== 'container') return;

    document.documentElement.classList.add('mc-container');
    var rules = [];
    if (!caps.can_reboot_host) rules.push('#rebootSystemBtn, #shutdownSystemBtn');
    if (!caps.can_manage_wifi) rules.push('[data-runtime-hide="wifi"]');
    if (!caps.can_self_update) rules.push('#updatesApplyBtn');
    if (!caps.has_host_metrics) rules.push('.top-processes-section');
    if (rules.length) {
        var style = document.createElement('style');
        style.textContent = rules.join(',\n') + ' { display: none !important; }';
        document.head.appendChild(style);
    }

    function showNotice() {
        var notice = document.getElementById('containerModeNotice');
        if (!notice) return;
        notice.style.display = '';
        if (window.I18N && typeof window.I18N.t === 'function') {
            notice.textContent = window.I18N.t('system.container_mode_notice');
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', showNotice);
    } else {
        showNotice();
    }
})();
