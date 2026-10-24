// ============================================================
// NETWORK CAMERA DIAGNOSTICS - open the app with ?camdiag=1 (CAM2-M)
//
// A browser that cannot show a network camera (Safari on iPhone ended in a
// long spinner) is hard to debug from outside. This panel runs the same steps
// the player does, one at a time, with timestamps, and prints plain text the
// user can copy: what the browser supports, which codec strings it accepts,
// when each MediaSource event fired and which step stalled.
//
// The text contains NO address, URL, host name or credential: only whitelisted
// facts (user agent, feature flags, codec strings, timings, error names). It is
// passed through sanitize() anyway. The panel only exists with ?camdiag=1 and
// the stream calls use the logged-in session like the player, so login is
// required exactly as for the camera.
// ============================================================
(function () {
    'use strict';

    const STREAM_URL = '/api/camera/stream/fmp4';
    const STATUS_URL = '/api/camera/status';
    const SOURCEOPEN_TIMEOUT_MS = 6000;
    const STEP_TIMEOUT_MS = 6000;
    const PLAYING_TIMEOUT_MS = 8000;
    const EXTRA_CODECS = ['avc1.640033', 'avc1.64001f', 'avc1.640028', 'avc1.4d401f', 'avc1.42c01e'];
    const MEDIA_EVENTS = ['loadstart', 'loadedmetadata', 'loadeddata', 'canplay', 'playing', 'waiting', 'stalled', 'suspend', 'emptied', 'abort', 'error'];
    let timeoutScale = 1; // tests shrink the waits
    const ms = (n) => n * timeoutScale;
    const SOURCE_EVENTS = ['sourceopen', 'sourceended', 'sourceclose', 'startstreaming', 'endstreaming'];

    function t(key) {
        return window.I18N.t(key);
    }

    // ---------- pure helpers (unit tested in tests/frontend/test_camera_diag.mjs) ----------

    // Defense in depth: whatever ends up in the report, nothing that looks like an address leaves it.
    function sanitize(text) {
        return String(text)
            .replace(/[a-z][a-z0-9+.-]*:\/\/\S+/gi, '<url removed>')
            .replace(/\b\d{1,3}(\.\d{1,3}){3}\b/g, '<ip removed>')
            .replace(/\b[0-9a-f]{1,4}(:[0-9a-f]{0,4}){3,7}\b/gi, '<ip removed>')
            .replace(/\S+@\S+/g, '<redacted>');
    }

    function fmtMs(ms) {
        return ms == null ? '-' : (Math.round(ms) + ' ms');
    }

    function mimeFor(codec) {
        return 'video/mp4; codecs="' + codec + '"';
    }

    function supportRow(label, ctor, codec) {
        let value = 'n/a';
        try {
            if (ctor && typeof ctor.isTypeSupported === 'function') value = String(ctor.isTypeSupported(mimeFor(codec)));
        } catch (error) {
            value = 'throws ' + (error && error.name);
        }
        return '  ' + label + ' ' + codec + ': ' + value;
    }

    function describeError(error) {
        return error ? ((error.name || 'Error') + (error.message ? ': ' + String(error.message).slice(0, 120) : '')) : 'none';
    }

    function boxTypes(bytes) {
        const types = [];
        let pos = 0;
        while (pos + 8 <= bytes.length && types.length < 4) {
            const size = (bytes[pos] << 24 | bytes[pos + 1] << 16 | bytes[pos + 2] << 8 | bytes[pos + 3]) >>> 0;
            types.push(String.fromCharCode(bytes[pos + 4], bytes[pos + 5], bytes[pos + 6], bytes[pos + 7]) + '(' + size + ')');
            if (size < 8) break;
            pos += size;
        }
        return types.join(' ');
    }

    function capabilityLines(win, codecFromServer) {
        const lines = [];
        const nav = win.navigator || {};
        lines.push('user agent: ' + (nav.userAgent || 'unknown'));
        lines.push('platform: ' + (nav.platform || 'unknown') + ', touch points: ' + (nav.maxTouchPoints == null ? '?' : nav.maxTouchPoints) +
            ', screen: ' + (win.screen ? win.screen.width + 'x' + win.screen.height : '?') + ', dpr: ' + (win.devicePixelRatio || '?'));
        lines.push('MediaSource: ' + (win.MediaSource ? 'yes' : 'NO') + ', ManagedMediaSource: ' + (win.ManagedMediaSource ? 'yes' : 'NO') +
            ', ManagedSourceBuffer: ' + (win.ManagedSourceBuffer ? 'yes' : 'no'));
        lines.push('fetch streaming (ReadableStream): ' + (typeof win.ReadableStream === 'function' ? 'yes' : 'NO') +
            ', AbortController: ' + (typeof win.AbortController === 'function' ? 'yes' : 'NO'));
        lines.push('which constructor the player picks: ' + (win.ManagedMediaSource ? 'ManagedMediaSource' : (win.MediaSource ? 'MediaSource' : 'none (capability check fails)')));
        const codecs = codecFromServer && EXTRA_CODECS.indexOf(codecFromServer) === -1 ? [codecFromServer].concat(EXTRA_CODECS) : EXTRA_CODECS;
        lines.push('isTypeSupported:');
        codecs.forEach((codec) => {
            if (win.MediaSource) lines.push(supportRow('MediaSource       ', win.MediaSource, codec));
            if (win.ManagedMediaSource) lines.push(supportRow('ManagedMediaSource', win.ManagedMediaSource, codec));
        });
        return lines;
    }

    // ---------- one staged playback attempt ----------

    function waitFor(promiseOrEvent, ms) {
        return new Promise((resolve) => {
            const timer = setTimeout(() => resolve('timeout'), ms);
            promiseOrEvent.then((value) => { clearTimeout(timer); resolve(value || 'ok'); }, () => { clearTimeout(timer); resolve('error'); });
        });
    }

    async function attempt(win, label, options, codec) {
        const doc = win.document;
        const out = [];
        const t0 = performance.now();
        const stamp = () => fmtMs(performance.now() - t0);
        const log = (text) => out.push('  [' + stamp() + '] ' + text);
        out.push('attempt "' + label + '" (constructor ' + options.ctorName + ', attach via ' + (options.sourceElement ? '<source>' : 'video.src') + (options.load ? ' + video.load()' : '') + ')');

        const Ctor = options.ctor;
        const video = doc.createElement('video');
        video.muted = true;
        video.setAttribute('muted', '');
        video.setAttribute('playsinline', '');
        video.playsInline = true;
        video.style.cssText = 'position:fixed;left:-9999px;top:0;width:2px;height:2px;opacity:0;pointer-events:none';
        if (options.ctorName === 'ManagedMediaSource') video.disableRemotePlayback = true;
        doc.body.appendChild(video);
        const abort = new win.AbortController();
        let objectUrl = null;
        try {
            MEDIA_EVENTS.forEach((name) => video.addEventListener(name, () => {
                log('video ' + name + ' (readyState ' + video.readyState + ', networkState ' + video.networkState + (name === 'error' && video.error ? ', code ' + video.error.code : '') + ')');
            }));

            const mediaSource = new Ctor();
            const opened = new Promise((resolve) => mediaSource.addEventListener('sourceopen', () => resolve('sourceopen'), { once: true }));
            SOURCE_EVENTS.forEach((name) => mediaSource.addEventListener(name, () => log('MediaSource ' + name + (name === 'sourceopen' ? '' : ' (streaming=' + mediaSource.streaming + ')'))));

            objectUrl = win.URL.createObjectURL(mediaSource);
            if (options.sourceElement) {
                const source = doc.createElement('source');
                source.type = 'video/mp4';
                source.src = objectUrl;
                video.appendChild(source);
            } else {
                video.src = objectUrl;
            }
            log('attached, readyState ' + mediaSource.readyState);
            if (options.load) {
                video.load();
                log('video.load() called');
            }

            const result = await waitFor(opened, ms(SOURCEOPEN_TIMEOUT_MS));
            log('sourceopen wait: ' + result + ' (readyState ' + mediaSource.readyState + ')');
            if (result !== 'sourceopen') {
                out.push('  RESULT: FAIL, sourceopen never fired within ' + (ms(SOURCEOPEN_TIMEOUT_MS) / 1000) + ' s');
                return out;
            }

            let buffer;
            try {
                buffer = mediaSource.addSourceBuffer(mimeFor(codec));
                log('addSourceBuffer: ok');
            } catch (error) {
                log('addSourceBuffer: FAIL ' + describeError(error));
                out.push('  RESULT: FAIL at addSourceBuffer');
                return out;
            }
            try { buffer.mode = 'sequence'; log('mode = sequence: ok'); } catch (error) { log('mode = sequence: ' + describeError(error)); }
            buffer.addEventListener('error', () => log('SourceBuffer error event'));

            const response = await win.fetch(STREAM_URL, { cache: 'no-store', signal: abort.signal });
            log('stream response: HTTP ' + response.status);
            if (!response.ok) {
                out.push('  RESULT: FAIL, the stream endpoint refused (see HTTP status above)');
                return out;
            }
            const reader = response.body.getReader();
            let appended = 0;
            let bytes = 0;
            let chunks = 0;
            const deadline = performance.now() + ms(STEP_TIMEOUT_MS) * 2;
            while (appended < 3 && performance.now() < deadline) {
                const { value, done } = await reader.read();
                if (done) { log('stream ended early'); break; }
                chunks += 1;
                bytes += value.length;
                if (chunks === 1) log('first chunk: ' + value.length + ' bytes, boxes ' + boxTypes(value));
                try {
                    buffer.appendBuffer(value);
                } catch (error) {
                    log('appendBuffer #' + chunks + ': FAIL ' + describeError(error));
                    out.push('  RESULT: FAIL at appendBuffer');
                    return out;
                }
                const done2 = await waitFor(new Promise((resolve) => buffer.addEventListener('updateend', () => resolve('updateend'), { once: true })), ms(STEP_TIMEOUT_MS));
                if (done2 !== 'updateend') { log('updateend after append #' + chunks + ': ' + done2); break; }
                appended += 1;
                log('append #' + chunks + ' done (' + value.length + ' bytes, total ' + bytes + ')');
            }
            const ranges = [];
            for (let i = 0; i < buffer.buffered.length; i += 1) ranges.push(buffer.buffered.start(i).toFixed(2) + '-' + buffer.buffered.end(i).toFixed(2));
            log('buffered ranges: ' + (ranges.join(', ') || 'none'));

            const played = await waitFor(video.play().then(() => 'play() resolved'), ms(PLAYING_TIMEOUT_MS));
            log('video.play(): ' + played);
            const ok = await waitFor(new Promise((resolve) => {
                if (!video.paused && video.readyState >= 3) resolve('already playing');
                video.addEventListener('playing', () => resolve('playing'), { once: true });
            }), ms(PLAYING_TIMEOUT_MS));
            log('wait for playing: ' + ok + ' (currentTime ' + video.currentTime.toFixed(2) + ', readyState ' + video.readyState + ', videoWidth ' + video.videoWidth + ')');
            out.push('  RESULT: ' + (ok === 'playing' || ok === 'already playing' ? 'PASS' : 'FAIL, no picture started'));
            return out;
        } catch (error) {
            log('EXCEPTION ' + describeError(error));
            out.push('  RESULT: FAIL (exception)');
            return out;
        } finally {
            try { abort.abort(); } catch (error) { /* already aborted */ }
            try { if (objectUrl) win.URL.revokeObjectURL(objectUrl); } catch (error) { /* ignore */ }
            try { video.remove(); } catch (error) { /* ignore */ }
        }
    }

    // Errors the page threw (window.onerror / unhandled rejections) once ?camdiag=1 is on, plus the player's own
    // caught error. File names are cut to the base name, line and column; the sanitizer removes anything address-like.
    const pageErrors = [];
    function recordError(kind, message, file, line, col) {
        const base = String(file || '').split('?')[0].split('/').pop();
        pageErrors.push(kind + ': ' + String(message) + (base ? ' @' + base + ':' + (line || 0) + ':' + (col || 0) : ''));
        if (pageErrors.length > 20) pageErrors.shift();
    }
    function errorLines(win) {
        const lines = pageErrors.map((e) => 'error ' + e);
        try {
            const player = win.MCIpCamera && win.MCIpCamera.lastError && win.MCIpCamera.lastError();
            if (player) lines.push('player caught: ' + player);
        } catch (error) { /* ignore */ }
        if (!lines.length) lines.push('errors: none recorded since the page opened');
        return lines;
    }

    async function run(win, options) {
        win = win || window;
        timeoutScale = (options && options.timeoutScale) || 1;
        const lines = ['camera diagnostics v1 (no addresses, URLs or credentials are included)'];
        let status = null;
        try {
            const response = await win.fetch(STATUS_URL, { cache: 'no-store' });
            status = await response.json();
        } catch (error) {
            lines.push('status: not readable (' + describeError(error) + ')');
        }
        if (status) {
            lines.push('server: source_type=' + status.source_type + ', state=' + status.state + ', profile=' + status.profile + ', codec=' + status.codec +
                ', mse_codec=' + status.mse_codec + ', resolution=' + status.resolution + ', fps=' + status.fps + ', mjpeg_fallback=' + status.mjpeg_fallback +
                ', container=' + status.container);
        }
        const codec = status && status.mse_codec;
        capabilityLines(win, codec).forEach((line) => lines.push(line));
        errorLines(win).forEach((line) => lines.push(line));

        if (!status || status.source_type !== 'rtsp') {
            lines.push('no network camera is active: the playback attempts are skipped');
            return sanitize(lines.join('\n'));
        }
        if (!codec) {
            lines.push('the server has no codec yet (state ' + status.state + '): open the Camera tab, wait for "Live", then run again');
            return sanitize(lines.join('\n'));
        }
        if (!win.MediaSource && !win.ManagedMediaSource) {
            lines.push('RESULT: this browser has neither MediaSource nor ManagedMediaSource: the player cannot work here (capability check fails)');
            return sanitize(lines.join('\n'));
        }

        const attempts = [];
        if (win.ManagedMediaSource) {
            attempts.push(['managed, as the player does today', { ctor: win.ManagedMediaSource, ctorName: 'ManagedMediaSource', sourceElement: true, load: false }]);
            attempts.push(['managed, with video.load()', { ctor: win.ManagedMediaSource, ctorName: 'ManagedMediaSource', sourceElement: true, load: true }]);
        }
        if (win.MediaSource) {
            attempts.push(['plain MediaSource, video.src', { ctor: win.MediaSource, ctorName: 'MediaSource', sourceElement: false, load: false }]);
        }
        for (const [label, options] of attempts) {
            const result = await attempt(win, label, options, codec);
            result.forEach((line) => lines.push(line));
        }
        return sanitize(lines.join('\n'));
    }

    // ---------- the panel (only with ?camdiag=1) ----------

    function mountPanel() {
        const view = document.getElementById('videoView');
        if (!view || document.getElementById('camDiagPanel')) return;
        const panel = document.createElement('section');
        panel.id = 'camDiagPanel';
        panel.className = 'mc-camdiag';
        panel.innerHTML =
            '<h3></h3><p class="mc-camdiag-hint"></p>' +
            '<div class="mc-camdiag-actions"><button type="button" id="camDiagRun" class="mc-refresh-btn"></button>' +
            '<button type="button" id="camDiagCopy" class="mc-refresh-btn" disabled></button></div>' +
            '<textarea id="camDiagOut" readonly rows="14" spellcheck="false"></textarea>';
        panel.querySelector('h3').textContent = t('camdiag.title');
        panel.querySelector('p').textContent = t('camdiag.hint');
        const runButton = panel.querySelector('#camDiagRun');
        const copyButton = panel.querySelector('#camDiagCopy');
        const output = panel.querySelector('#camDiagOut');
        runButton.textContent = t('camdiag.run');
        copyButton.textContent = t('camdiag.copy');
        runButton.addEventListener('click', async () => {
            runButton.disabled = true;
            copyButton.disabled = true;
            output.value = t('camdiag.running');
            try {
                output.value = await run(window);
            } catch (error) {
                output.value = sanitize('diagnostics failed: ' + describeError(error));
            }
            runButton.disabled = false;
            copyButton.disabled = false;
        });
        copyButton.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(output.value);
            } catch (error) {
                output.focus();
                output.select();
                try { document.execCommand('copy'); } catch (e) { /* the text is selected: copy by hand */ }
            }
            copyButton.textContent = t('camdiag.copied');
            setTimeout(() => { copyButton.textContent = t('camdiag.copy'); }, 2000);
        });
        view.insertBefore(panel, view.firstChild);
    }

    function init() {
        let enabled = false;
        try { enabled = new URLSearchParams(window.location.search).get('camdiag') === '1'; } catch (error) { enabled = false; }
        if (!enabled) return;
        window.addEventListener('error', (e) => recordError('onerror', e && e.message, e && e.filename, e && e.lineno, e && e.colno));
        window.addEventListener('unhandledrejection', (e) => {
            const r = e && e.reason;
            recordError('unhandledrejection', (r && r.message) || r);
        });
        const start = () => {
            if (window.I18N && typeof window.I18N.t === 'function') mountPanel();
            else setTimeout(start, 200);
        };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
        else start();
    }

    window.MCCameraDiag = { run, sanitize, capabilityLines, boxTypes, EXTRA_CODECS, _recordError: recordError };
    init();
})();
