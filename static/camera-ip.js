// ============================================================
// NETWORK (IP) CAMERA LIVE VIEW - CAM2 Phase 1b
//
// The CSI/USB cameras stream MJPEG into the <img id="videoFeed">. A network
// camera streams H.264 that MeshCenter does NOT transcode: the server remuxes
// it into fragmented MP4 (/api/camera/stream/fmp4) and this file plays it with
// MediaSource (or ManagedMediaSource on iOS Safari) into <video id="videoFeedIp">.
// Browsers that cannot do that fall back to the MJPEG feed (/video_feed), which
// the server only offers on hosts strong enough for the transcode.
//
// chat-camera.js calls into window.MCIpCamera (handles / start / stop / refresh);
// nothing else in the camera tab knows about network cameras.
// ============================================================
(function () {
    'use strict';

    const STATUS_URL = '/api/camera/status';
    const STREAM_URL = '/api/camera/stream/fmp4';
    const PROFILE_URL = '/api/camera/stream/profile';
    const RECONNECT_URL = '/api/camera/stream/reconnect';
    // Playback buffer target. The camera delivers frames unevenly (measured 13-14.5 fps for a
    // nominal 15), so a 1.5 s lead stalled constantly while 3 s played smoothly.
    const TARGET_LEAD_S = 3;
    const MAX_BUFFERED_S = 20;
    const RETRY_MS = [1000, 2000, 4000, 8000, 15000];
    const SOURCEOPEN_TIMEOUT_MS = 6000;
    const FIRST_FRAME_TIMEOUT_MS = 10000;
    const MAX_QUEUED_CHUNKS = 120;

    const POLL_MS = 2000;
    const TERMINAL = ['auth_failed', 'stream_not_found', 'unsupported_codec', 'address_blocked', 'ffmpeg_missing'];

    let status = null;
    let generation = 0;
    let abortController = null;
    let pollTimer = null;
    let retryTimer = null;
    let objectUrl = null;
    let mode = 'none'; // 'mse' | 'mjpeg' | 'none'

    function t(key, params) {
        return window.I18N.t(key, params);
    }

    function el(id) {
        return document.getElementById(id);
    }

    // ---------- pure helpers (unit tested, see tests/frontend/test_camera_ip.mjs) ----------

    function mseConstructor(win) {
        win = win || window;
        return win.ManagedMediaSource || win.MediaSource || null;
    }

    function mseSupports(codec, win) {
        const MS = mseConstructor(win);
        return Boolean(MS && codec && typeof MS.isTypeSupported === 'function'
            && MS.isTypeSupported('video/mp4; codecs="' + codec + '"'));
    }

    // Where playback should jump to: null = leave it alone.
    function liveEdgeTarget(currentTime, bufferedStart, bufferedEnd, targetLead) {
        if (!(bufferedEnd > bufferedStart)) return null;
        if (currentTime < bufferedStart) return Math.max(bufferedStart, bufferedEnd - targetLead);
        if (bufferedEnd - currentTime > targetLead + 2.5) return bufferedEnd - targetLead;
        return null;
    }

    function stateMessageKey(st) {
        if (!st) return 'ipcamera.state_connecting';
        if (st.state === 'ffmpeg_missing' && st.container) return 'ipcamera.error_ffmpeg_missing_container';
        if (st.state === 'unsupported_codec') {
            return st.codec === 'h265' ? 'ipcamera.error_h265' : 'ipcamera.error_codec';
        }
        const byCode = {
            auth_failed: 'ipcamera.error_auth_failed',
            stream_not_found: 'ipcamera.error_stream_not_found',
            address_blocked: 'ipcamera.error_address_blocked',
            ffmpeg_missing: 'ipcamera.error_ffmpeg_missing',
        };
        if (byCode[st.state]) return byCode[st.state];
        if (st.state === 'reconnecting') return 'ipcamera.state_reconnecting';
        if (st.state === 'live') return 'ipcamera.state_live';
        if (st.state === 'idle' || st.state === 'stopped') return 'ipcamera.state_idle';
        return 'ipcamera.state_connecting';
    }

    function isTerminal(st) {
        return Boolean(st && TERMINAL.indexOf(st.state) !== -1);
    }

    function infoLine(st) {
        if (!st) return '';
        const parts = [];
        if (st.resolution) parts.push(st.resolution);
        if (st.fps) parts.push(st.fps + ' FPS');
        if (st.codec) parts.push(String(st.codec).toUpperCase());
        if (st.bitrate_kbps) parts.push(st.bitrate_kbps + ' kbps');
        return parts.join(' · ');
    }

    // ---------- status ----------

    async function refresh() {
        try {
            const response = await fetch(STATUS_URL, { cache: 'no-store' });
            const data = await response.json();
            status = data && data.source_type === 'rtsp' ? data : null;
        } catch (error) {
            status = null;
        }
        applyModeClass();
        return status;
    }

    function handles() {
        return Boolean(status && status.source_type === 'rtsp');
    }

    function applyModeClass() {
        const view = el('videoView');
        if (view) view.classList.toggle('camera-ip-mode', handles());
        const select = el('ipCameraProfile');
        if (select) {
            select.style.display = handles() ? '' : 'none';
            if (handles()) select.value = status.profile || 'sub';
        }
    }

    function renderStatus() {
        const st = status;
        const info = el('videoLiveInfo');
        const label = el('videoStatus');
        const reconnectBtn = el('ipCameraReconnectBtn');
        if (!st) return;
        if (info) info.textContent = infoLine(st) || '-';
        if (label) {
            // the status bar is one short line; the full explanation of a failure is the overlay text
            let text = isTerminal(st) ? t('ipcamera.state_error') : t(stateMessageKey(st));
            if (st.state === 'reconnecting' && st.reconnect_in_s != null) {
                text = t('ipcamera.state_reconnecting_in', { seconds: Math.ceil(st.reconnect_in_s) });
            }
            label.textContent = text;
            label.style.color = st.state === 'live' ? '#4caf50' : (isTerminal(st) ? '#f44336' : '#ff9800');
        }
        if (reconnectBtn) reconnectBtn.style.display = isTerminal(st) || st.state === 'reconnecting' ? '' : 'none';
    }

    function setSpinner(visible) {
        const placeholder = el('cameraLoadingPlaceholder');
        const spin = placeholder && placeholder.querySelector('.camera-loading-spinner');
        if (spin) spin.style.display = visible ? '' : 'none';
    }

    let overlayActive = false;
    let lastError = '';
    let stickyMessage = false;

    function showMessage(text, spinner, sticky) {
        overlayActive = true;
        stickyMessage = Boolean(sticky);
        if (typeof setCameraFeedLoading === 'function') setCameraFeedLoading(true, text);
        setSpinner(spinner);
    }

    // The text for the "not playing yet" overlay while the camera connects or reconnects.
    function progressText(st) {
        if (st && st.state === 'reconnecting') {
            return st.reconnect_in_s != null
                ? t('ipcamera.state_reconnecting_in', { seconds: Math.ceil(st.reconnect_in_s) })
                : t('ipcamera.state_reconnecting');
        }
        return t('ipcamera.state_connecting');
    }

    function startPolling() {
        stopPolling();
        pollTimer = setInterval(async () => {
            await refresh();
            renderStatus();
            if (overlayActive && !stickyMessage && status && !isTerminal(status)) {
                if (typeof setCameraFeedLoading === 'function') setCameraFeedLoading(true, progressText(status));
            }
        }, POLL_MS);
    }

    function stopPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
    }

    // ---------- playback ----------

    function teardownVideo() {
        if (abortController) {
            try { abortController.abort(); } catch (error) { /* already aborted */ }
            abortController = null;
        }
        if (retryTimer) clearTimeout(retryTimer);
        retryTimer = null;
        const video = el('videoFeedIp');
        if (video) {
            try { video.pause(); } catch (error) { /* not playing */ }
            video.removeAttribute('src');
            while (video.firstChild) video.removeChild(video.firstChild);
            try { video.load(); } catch (error) { /* ignore */ }
            video.classList.add('camera-stream-hidden');
        }
        if (objectUrl) {
            try { URL.revokeObjectURL(objectUrl); } catch (error) { /* ignore */ }
            objectUrl = null;
        }
    }

    function stop() {
        generation += 1;
        mode = 'none';
        overlayActive = false;
        stickyMessage = false;
        stopPolling();
        teardownVideo();
        setSpinner(true);
        setMjpegLayout(false);
        const img = el('videoFeed');
        if (img && handles()) {
            img.onload = null;
            img.onerror = null;
        }
    }

    function setMjpegLayout(on) {
        const view = el('videoView');
        if (view) view.classList.toggle('camera-ip-mjpeg', Boolean(on));
    }

    function startMjpegFallback(gen) {
        const img = el('videoFeed');
        if (!img) return;
        mode = 'mjpeg';
        setMjpegLayout(true);
        img.onload = () => { if (gen === generation && typeof showCameraFeed === 'function') showCameraFeed(); };
        img.onerror = () => {
            if (gen !== generation) return;
            showMessage(t('ipcamera.error_mjpeg_failed'), false);
        };
        img.src = '/video_feed?t=' + Date.now();
    }

    // The browser cannot play the fragmented MP4 stream. MJPEG is the only way left, and only where the host allows it.
    function startUnsupported(gen) {
        abortStream();
        if (status && status.mjpeg_fallback === 'disabled_low_power') {
            showMessage(t('ipcamera.error_not_supported'), false, true);
            return false;
        }
        startMjpegFallback(gen);
        return true;
    }

    function abortStream() {
        if (abortController) {
            try { abortController.abort(); } catch (error) { /* already aborted */ }
        }
    }

    function waitFor(target, name, ms) {
        return new Promise((resolve) => {
            const timer = setTimeout(() => resolve(false), ms);
            target.addEventListener(name, () => { clearTimeout(timer); resolve(true); }, { once: true });
        });
    }

    async function startMse(gen, attempt) {
        const video = el('videoFeedIp');
        if (!video) return false;
        abortController = new AbortController();
        const signal = abortController.signal;
        let response;
        try {
            response = await fetch(STREAM_URL, { cache: 'no-store', signal });
        } catch (error) {
            return scheduleRetry(gen, attempt + 1);
        }
        if (gen !== generation) return false;
        if (!response.ok) {
            await refresh();
            renderStatus();
            if (response.status === 429) {
                showMessage(t('ipcamera.error_viewer_limit'), false);
                return false;
            }
            return isTerminal(status) ? showTerminal() : scheduleRetry(gen, attempt + 1);
        }
        const codec = response.headers.get('X-Codec') || '';
        if (!mseSupports(codec)) {
            try { abortController.abort(); } catch (error) { /* ignore */ }
            const reason = status && status.mjpeg_fallback;
            if (reason === 'disabled_low_power') {
                showMessage(t('ipcamera.error_no_mse_low_power'), false);
                return false;
            }
            startMjpegFallback(gen);
            return true;
        }

        const MS = mseConstructor();
        const mediaSource = new MS();
        objectUrl = URL.createObjectURL(mediaSource);
        if (window.ManagedMediaSource && MS === window.ManagedMediaSource) {
            video.disableRemotePlayback = true;
            const source = document.createElement('source');
            source.type = 'video/mp4';
            source.src = objectUrl;
            video.appendChild(source);
            // a <source> child added to an idle element does not start loading on every Safari: load() does
            try { video.load(); } catch (error) { /* ignore */ }
        } else {
            video.src = objectUrl;
        }
        const opened = await waitFor(mediaSource, 'sourceopen', SOURCEOPEN_TIMEOUT_MS);
        if (gen !== generation) return false;
        if (!opened) {
            abortStream();
            teardownVideo();
            showMessage(t('ipcamera.error_not_supported'), false, true);
            return false;
        }

        const buffer = mediaSource.addSourceBuffer('video/mp4; codecs="' + codec + '"');
        // "sequence": the browser lays the segments end to end and ignores the stream's own timestamps.
        // The server ends every stream after 10 min (so a logged-out client cannot keep one) and every
        // new stream starts its timeline at 0; in the default "segments" mode that would jump the
        // timeline back and stall. In sequence mode the next stream simply continues the buffer.
        try { buffer.mode = 'sequence'; } catch (error) { /* an older engine keeps "segments" */ }
        const queue = [];
        let appending = false;
        let playing = false;
        let streamingAllowed = true;
        const pump = () => {
            if (appending || !queue.length || buffer.updating || gen !== generation || !streamingAllowed) return;
            appending = true;
            try {
                buffer.appendBuffer(queue.shift());
            } catch (error) {
                appending = false;
                if (error && error.name === 'QuotaExceededError') {
                    trimBuffer(buffer, video, 4);
                } else {
                    queue.length = 0;
                    scheduleRetry(gen, attempt + 1);
                }
            }
        };
        buffer.addEventListener('updateend', () => {
            appending = false;
            if (buffer.buffered.length) {
                const start = buffer.buffered.start(0);
                const end = buffer.buffered.end(buffer.buffered.length - 1);
                const jump = liveEdgeTarget(video.currentTime, start, end, TARGET_LEAD_S);
                if (jump !== null) video.currentTime = jump;
                if (end - start > MAX_BUFFERED_S) trimBuffer(buffer, video, 10);
            }
            pump();
        });
        // ManagedMediaSource asks the page to stop and resume feeding it (battery, network policy)
        mediaSource.addEventListener('startstreaming', () => { streamingAllowed = true; pump(); });
        mediaSource.addEventListener('endstreaming', () => { streamingAllowed = false; });
        // nothing playing 10 s after the stream opened: stop waiting and say why instead of spinning
        const firstFrameTimer = setTimeout(() => {
            if (gen !== generation || playing) return;
            abortStream();
            teardownVideo();
            showMessage(t('ipcamera.error_not_supported'), false, true);
        }, FIRST_FRAME_TIMEOUT_MS);
        video.addEventListener('playing', () => {
            if (gen !== generation) return;
            clearTimeout(firstFrameTimer);
            playing = true;
            overlayActive = false;
            video.classList.remove('camera-stream-hidden');
            if (typeof showCameraFeed === 'function') showCameraFeed();
            video.classList.remove('camera-stream-hidden');
        }, { once: true });
        video.play().catch(() => { /* autoplay needs muted; the element is muted */ });

        mode = 'mse';
        // The read loop runs for the whole viewing session: do not await it (start() must return).
        // When the server ends a stream (its 10 minute lifetime) while we are playing, the next stream is
        // opened on the SAME MediaSource and SourceBuffer, so nothing is torn down: no overlay, no black
        // frame. Only a failure of that quick reopen falls back to the full reconnect.
        (async () => {
            let current = response;
            for (;;) {
                const reader = current.body.getReader();
                try {
                    for (;;) {
                        const { value, done } = await reader.read();
                        if (done || gen !== generation) break;
                        queue.push(value);
                        if (queue.length > MAX_QUEUED_CHUNKS) queue.splice(0, queue.length - MAX_QUEUED_CHUNKS);
                        pump();
                    }
                } catch (error) {
                    if (gen !== generation) return;
                }
                if (gen !== generation) return;
                if (!playing) return void scheduleRetry(gen, attempt + 1);
                const next = await reopenStream(gen, codec);
                if (!next) return void scheduleRetry(gen, 0);
                current = next;
            }
        })();
        return true;
    }

    // Open the next stream for the same player. null = could not (the caller does a full reconnect).
    async function reopenStream(gen, codec) {
        for (let tries = 0; tries < 3 && gen === generation; tries += 1) {
            try {
                abortController = new AbortController();
                const response = await fetch(STREAM_URL, { cache: 'no-store', signal: abortController.signal });
                if (gen !== generation) return null;
                if (response.ok && (response.headers.get('X-Codec') || '') === codec) return response;
                try { abortController.abort(); } catch (error) { /* ignore */ }
                if (response.status === 401 || response.status === 403 || response.status === 429) return null;
            } catch (error) {
                if (gen !== generation) return null;
            }
            await new Promise((resolve) => setTimeout(resolve, 300 * (tries + 1)));
        }
        return null;
    }

    function trimBuffer(buffer, video, keepSeconds) {
        try {
            if (buffer.updating || !buffer.buffered.length) return;
            const end = buffer.buffered.end(buffer.buffered.length - 1);
            const cut = Math.min(video.currentTime, end - keepSeconds);
            if (cut > buffer.buffered.start(0)) buffer.remove(buffer.buffered.start(0), cut);
        } catch (error) { /* the next updateend retries */ }
    }

    function showTerminal() {
        showMessage(t(stateMessageKey(status)), false);
        return false;
    }

    function scheduleRetry(gen, attempt) {
        if (gen !== generation) return false;
        teardownVideo();
        showMessage(progressText(status), true);
        const delay = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
        retryTimer = setTimeout(async () => {
            if (gen !== generation) return;
            await refresh();
            renderStatus();
            if (isTerminal(status)) return showTerminal();
            startMse(gen, attempt);
        }, delay);
        return false;
    }

    async function start() {
        stop();
        const gen = generation;
        await refresh();
        if (gen !== generation) return false;
        renderStatus();
        if (!handles()) return false;
        showMessage(t('ipcamera.state_connecting'), true);
        startPolling();
        if (isTerminal(status)) return showTerminal();
        // No MediaSource and no ManagedMediaSource (iPhone before iOS 17.1): say so at once, before a
        // stream is opened (it would only occupy a viewer slot on the server for nothing).
        try {
            if (!mseConstructor()) return startUnsupported(gen);
            return await startMse(gen, 0);
        } catch (error) {
            // an old engine that throws somewhere in the player: say so instead of "waiting for stream" forever
            lastError = String(error && error.message || error);
            abortStream();
            showMessage(t('ipcamera.error_not_supported'), false, true);
            return false;
        }
    }

    // ---------- user actions ----------

    async function changeProfile(profile) {
        try {
            const response = await fetch(PROFILE_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profile }),
            });
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || 'failed');
            if (typeof showToast === 'function') {
                showToast(t('ipcamera.profile_switched'), 'success');
                if (profile === 'main') showToast(t('ipcamera.profile_main_hint'), 'info', { duration: 10000 });
            }
        } catch (error) {
            if (typeof showToast === 'function') showToast(t('ipcamera.profile_switch_failed'), 'error', { duration: 10000 });
        }
        await start();
    }

    async function reconnect() {
        try {
            await fetch(RECONNECT_URL, { method: 'POST' });
        } catch (error) { /* the poll shows the state */ }
        await start();
    }

    function wireControls() {
        const select = el('ipCameraProfile');
        if (select) select.addEventListener('change', () => changeProfile(select.value));
        const button = el('ipCameraReconnectBtn');
        if (button) button.addEventListener('click', () => reconnect());
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wireControls);
    else wireControls();

    window.MCIpCamera = {
        handles, start, stop, refresh, reconnect, changeProfile,
        // exported for tests
        lastError: () => lastError,
        _helpers: { reopenStream, startUnsupported, progressText, mseSupports, liveEdgeTarget, stateMessageKey, isTerminal, infoLine, mseConstructor, TARGET_LEAD_S },
    };
})();
