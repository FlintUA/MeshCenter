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
    const SNAPSHOT_URL = '/api/camera/snapshot';
    // Snapshot mode (browsers without MediaSource, camera with an API snapshot): the server fetches at most
    // one picture per second per camera and shares it, so asking faster would only get the cached picture.
    const SNAPSHOT_INTERVAL_MS = 1000;
    // Playback buffer target. The camera delivers frames unevenly (measured 13-14.5 fps for a
    // nominal 15), so a 1.5 s lead stalled constantly while 3 s played smoothly.
    const TARGET_LEAD_S = 3;
    const MAX_BUFFERED_S = 20;
    const RETRY_MS = [1000, 2000, 4000, 8000, 15000];
    const SOURCEOPEN_TIMEOUT_MS = 6000;
    const FIRST_FRAME_TIMEOUT_MS = 10000;
    const MAX_QUEUED_CHUNKS = 120;
    const WATCHDOG_MS = 2000;
    const STABLE_PLAY_MS = 15000;
    const FIRST_FRAME_RETRIES = 2; // a stream that shows nothing is rebuilt this often before the message appears

    const POLL_MS = 2000;
    const TERMINAL = ['auth_failed', 'stream_not_found', 'unsupported_codec', 'address_blocked', 'ffmpeg_missing'];

    let status = null;
    let generation = 0;
    let abortController = null;
    let pollTimer = null;
    let retryTimer = null;
    let objectUrl = null;
    let mode = 'none'; // 'mse' | 'mjpeg' | 'snapshot' | 'none'
    let snapshotTimer = null;
    let snapshotReason = ''; // 'ffmpeg' (the host has none) | 'browser' (it cannot play the stream)
    let snapshotSize = null; // {width, height} of the latest picture
    let snapshotUrl = null;
    let currentMediaSource = null; // the MediaSource of the running player (a profile switch or an error builds a new one)
    let mseFailures = 0; // runtime MSE errors since the last stable playback: spaces the reconnects
    let watchdogTimer = null;

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
        if (mode === 'snapshot') {
            // pictures, not a stream: the picture's own size and how often it is renewed (no codec, fps or bitrate)
            if (snapshotSize) parts.push(snapshotSize.width + 'x' + snapshotSize.height);
            parts.push(t('ipcamera.snapshot_interval'));
            return parts.join(' · ');
        }
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
        // The active camera is no longer a network camera (switched to CSI/USB): end this player. Left running, its
        // stream / snapshot loop and retries kept hiding and replacing the local camera's picture ("connecting" forever).
        if (!handles() && (mode !== 'none' || pollTimer || abortController || snapshotTimer || retryTimer)) stop();
        applyModeClass();
        return status;
    }

    function handles() {
        return Boolean(status && status.source_type === 'rtsp');
    }

    // On a phone the stream picker is narrow: the short labels ("Sub (recommended)") keep it readable.
    function narrowViewport() {
        return typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 768px)').matches;
    }

    function labelProfiles() {
        const select = el('ipCameraProfile');
        if (!select || !select.options) return;
        const short = narrowViewport();
        for (const option of Array.from(select.options)) {
            const key = option.value === 'main' ? 'ipcamera.profile_main' : 'ipcamera.profile_sub';
            const text = t(short ? key + '_short' : key);
            if (text.indexOf('[[') !== 0) option.textContent = text; // catalog not loaded yet: keep what is there
        }
    }

    // The frame box follows the picture's own aspect ratio (CSS: --camera-aspect), not a fixed height.
    function setAspect(width, height) {
        const view = el('videoView');
        if (!view || !view.style) return;
        if (width > 0 && height > 0) {
            view.style.setProperty('--camera-aspect', width + ' / ' + height);
            view.style.setProperty('--camera-ratio', String(width / height));
        } else {
            view.style.removeProperty('--camera-aspect');
            view.style.removeProperty('--camera-ratio');
        }
    }

    function applyModeClass() {
        const view = el('videoView');
        if (view) view.classList.toggle('camera-ip-mode', handles());
        if (window.MCIpControls) window.MCIpControls.update(handles() ? status : null); // Mode / Lens under the picture
        const select = el('ipCameraProfile');
        if (select) {
            labelProfiles();
            if (handles() && document.activeElement !== select) select.value = status.profile || 'sub';
        }
    }

    function renderStatus() {
        const st = status;
        const info = el('videoLiveInfo');
        const label = el('videoStatus');
        const reconnectBtn = el('ipCameraReconnectBtn');
        if (!st) return;
        if (info) {
            // nothing known yet: no placeholder dash, the empty part and its divider are hidden
            const text = infoLine(st);
            info.textContent = text;
            setInfoVisible(Boolean(text));
        }
        if (label) {
            // the status bar is one short line; the full explanation of a failure is the overlay text
            let text = isTerminal(st) ? t('ipcamera.state_error') : t(stateMessageKey(st));
            if (mode === 'snapshot') text = t('ipcamera.snapshot_status'); // the RTSP upstream is idle on purpose
            if (st.state === 'reconnecting' && st.reconnect_in_s != null) {
                text = t('ipcamera.state_reconnecting_in', { seconds: Math.ceil(st.reconnect_in_s) });
            }
            label.textContent = text;
            label.style.color = (st.state === 'live' || mode === 'snapshot') ? '#4caf50' : (isTerminal(st) ? '#f44336' : '#ff9800');
        }
        if (reconnectBtn) reconnectBtn.style.display = isTerminal(st) || st.state === 'reconnecting' ? '' : 'none';
    }

    function setInfoVisible(visible) {
        const info = el('videoLiveInfo');
        if (!info) return;
        info.hidden = !visible;
        const divider = info.nextElementSibling;
        if (divider && divider.classList && divider.classList.contains('camera-status-divider')) divider.hidden = !visible;
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
        // the static second line says "Waiting for the first video frame": wrong next to a final message
        const sub = document.querySelector('#cameraLoadingPlaceholder .camera-loading-text');
        if (sub) sub.style.display = sticky ? 'none' : '';
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
        if (watchdogTimer) clearInterval(watchdogTimer);
        watchdogTimer = null;
        currentMediaSource = null;
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
        snapshotSize = null;
        snapshotReason = '';
        setInfoVisible(true); // a local camera writes its own text there
        stopSnapshots();
        setAspect(0, 0);
        overlayActive = false;
        stickyMessage = false;
        const subLine = document.querySelector('#cameraLoadingPlaceholder .camera-loading-text');
        if (subLine) subLine.style.display = '';
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
        img.onload = () => {
            if (gen !== generation) return;
            setAspect(img.naturalWidth, img.naturalHeight);
            if (typeof showCameraFeed === 'function') showCameraFeed();
        };
        img.onerror = () => {
            if (gen !== generation) return;
            showMessage(t('ipcamera.error_mjpeg_failed'), false);
        };
        img.src = '/video_feed?t=' + Date.now();
    }

    // The browser cannot play the fragmented MP4 stream. MJPEG is the only way left, and only where the host allows it.
    function startUnsupported(gen) {
        abortStream();
        // the camera makes the pictures itself (its API): no ffmpeg, no transcode, works on a Pi
        if (status && status.snapshot_fallback) return startSnapshotMode(gen);
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

    // ---------- snapshot mode ----------

    function stopSnapshots() {
        if (snapshotTimer) clearTimeout(snapshotTimer);
        snapshotTimer = null;
        if (snapshotUrl) {
            try { URL.revokeObjectURL(snapshotUrl); } catch (error) { /* ignore */ }
            snapshotUrl = null;
        }
    }

    function startSnapshotMode(gen, reason) {
        const img = el('videoFeed');
        if (!img) return false;
        mode = 'snapshot';
        snapshotReason = reason === 'ffmpeg' ? 'ffmpeg' : 'browser';
        snapshotSize = null;
        setMjpegLayout(true);
        if (window.MCIpControls && status) window.MCIpControls.update(status); // Mode switches to the snapshot facts
        let failures = 0;
        const next = (delay) => { if (gen === generation) snapshotTimer = setTimeout(tick, delay); };
        const tick = async () => {
            if (gen !== generation) return;
            if (typeof document.hidden === 'boolean' && document.hidden) return next(SNAPSHOT_INTERVAL_MS); // a hidden tab asks for nothing
            const began = Date.now();
            let delay = SNAPSHOT_INTERVAL_MS;
            try {
                const response = await fetch(SNAPSHOT_URL, { cache: 'no-store' });
                if (gen !== generation) return;
                if (response.ok) {
                    const blob = await response.blob();
                    if (gen !== generation) return;
                    const url = URL.createObjectURL(blob);
                    const previous = snapshotUrl;
                    snapshotUrl = url;
                    img.onload = () => {
                        if (gen !== generation) return;
                        setAspect(img.naturalWidth, img.naturalHeight);
                        if (!snapshotSize || snapshotSize.width !== img.naturalWidth || snapshotSize.height !== img.naturalHeight) {
                            snapshotSize = { width: img.naturalWidth, height: img.naturalHeight };
                            if (window.MCIpControls && status) window.MCIpControls.update(status);
                        }
                        overlayActive = false;
                        if (typeof showCameraFeed === 'function') showCameraFeed();
                    };
                    img.src = url;
                    if (previous) URL.revokeObjectURL(previous);
                    failures = 0;
                } else {
                    let body = null;
                    try { body = await response.json(); } catch (error) { body = null; }
                    if (response.status === 429) return void showMessage(t('ipcamera.error_viewer_limit'), false, true);
                    if (body && body.error_code === 'snapshot_not_supported') return void showMessage(t('ipcamera.error_not_supported'), false, true);
                    failures += 1;
                    delay = Math.min(8000, 1500 * failures);
                    if (failures >= 3) showMessage(t('ipcamera.snapshot_unavailable'), false);
                }
            } catch (error) {
                failures += 1;
                delay = Math.min(8000, 1500 * failures);
            }
            next(Math.max(0, delay - (Date.now() - began)));
        };
        tick();
        return true;
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
                showMessage(t('ipcamera.error_viewer_limit'), false, true); // sticky: the status poll must not turn it back into "connecting"
                return false;
            }
            return isTerminal(status) ? showTerminal() : scheduleRetry(gen, attempt + 1);
        }
        const codec = response.headers.get('X-Codec') || '';
        if (!codec) {
            // the server has no init segment for the stream yet (a profile switch or a reconnect is in progress):
            // that says nothing about this browser, so wait and ask again, never fall back to pictures
            try { abortController.abort(); } catch (error) { /* ignore */ }
            return scheduleRetry(gen, attempt + 1);
        }
        if (!mseSupports(codec)) {
            try { abortController.abort(); } catch (error) { /* ignore */ }
            if (status && status.snapshot_fallback) return startSnapshotMode(gen);
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
        currentMediaSource = mediaSource;
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
        // An error while the player runs (a decode error, a SourceBuffer that cannot take the next segment, a
        // MediaSource that was closed) is the player's problem, not a missing capability: rebuild it with a NEW
        // MediaSource and SourceBuffer and ask the server again. Never switch to pictures from here.
        let recovering = false;
        const recover = () => {
            if (gen !== generation || recovering) return;
            recovering = true;
            mseFailures += 1;
            abortStream();
            queue.length = 0;
            scheduleRetry(gen, Math.min(mseFailures, RETRY_MS.length - 1));
        };
        buffer.addEventListener('error', recover);
        mediaSource.addEventListener('sourceclose', recover);
        video.addEventListener('error', recover);
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
                    recover();
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
        // (a slow first keyframe, e.g. a 4K main stream after a profile switch, is rebuilt first: the message is the last resort)
        const firstFrameTimer = setTimeout(() => {
            if (gen !== generation || playing) return;
            if (mseFailures < FIRST_FRAME_RETRIES) return recover();
            abortStream();
            teardownVideo();
            showMessage(t('ipcamera.error_not_supported'), false, true);
        }, FIRST_FRAME_TIMEOUT_MS);
        const reportSize = () => { if (gen === generation) setAspect(video.videoWidth, video.videoHeight); };
        video.addEventListener('loadedmetadata', reportSize);
        video.addEventListener('resize', reportSize);
        video.addEventListener('playing', () => {
            if (gen !== generation) return;
            reportSize();
            clearTimeout(firstFrameTimer);
            playing = true;
            overlayActive = false;
            video.classList.remove('camera-stream-hidden');
            if (typeof showCameraFeed === 'function') showCameraFeed();
            video.classList.remove('camera-stream-hidden');
            setTimeout(() => { if (gen === generation) mseFailures = 0; }, STABLE_PLAY_MS);
            startWatchdog(gen, video, () => playing, recover);
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

    // Every 2 s while a stream plays: an element that errored or lost its MediaSource is rebuilt; a paused element in
    // view (the browser pauses media in a hidden tab or pane) plays again; one that stands still although data is
    // buffered jumps to the live edge, and one that stands still for 16 s is rebuilt.
    function startWatchdog(gen, video, isPlaying, recover) {
        if (watchdogTimer) clearInterval(watchdogTimer);
        let lastTime = -1;
        let still = 0;
        watchdogTimer = setInterval(() => {
            if (gen !== generation) { clearInterval(watchdogTimer); watchdogTimer = null; return; }
            if (!isPlaying() || (typeof document.hidden === 'boolean' && document.hidden)) { still = 0; return; }
            const inView = video.offsetParent === undefined || video.offsetParent !== null;
            if (!inView) { still = 0; return; }
            if (video.error || (currentMediaSource && currentMediaSource.readyState === 'closed')) return recover();
            if (video.paused) {
                try { const promise = video.play(); if (promise && promise.catch) promise.catch(() => { /* muted autoplay */ }); } catch (error) { /* ignore */ }
                return;
            }
            const now = video.currentTime;
            still = Math.abs(now - lastTime) < 0.01 ? still + 1 : 0;
            lastTime = now;
            if (still === 3 && video.buffered && video.buffered.length) {
                const end = video.buffered.end(video.buffered.length - 1);
                if (end - now > 1) video.currentTime = Math.max(video.buffered.start(0), end - TARGET_LEAD_S);
            }
            if (still >= 8) recover();
        }, WATCHDOG_MS);
    }

    function resumeAfterReturn() {
        if (!handles() || document.hidden) return;
        const video = el('videoFeedIp');
        if (mode === 'mse') {
            const dead = !video || video.error || video.ended || !currentMediaSource || currentMediaSource.readyState !== 'open';
            if (dead) start();
            else if (video.paused) { try { const promise = video.play(); if (promise && promise.catch) promise.catch(() => { /* ignore */ }); } catch (error) { /* ignore */ } }
        } else if (mode === 'snapshot' && snapshotReason === 'browser' && mseConstructor()) {
            start(); // the browser can play streams: look at the codec again instead of staying on pictures
        }
    }

    function showTerminal() {
        showMessage(t(stateMessageKey(status)), false);
        return false;
    }

    function scheduleRetry(gen, attempt) {
        if (gen !== generation) return false;
        if (retryTimer) return false; // one retry at a time: an error event after the teardown must not start a second
        teardownVideo();
        showMessage(progressText(status), true);
        const delay = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
        retryTimer = setTimeout(async () => {
            retryTimer = null;
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
        // No ffmpeg (Docker image) but the camera makes pictures itself: show those instead of an error
        if (status.ffmpeg_available === false && status.snapshot_fallback) return startSnapshotMode(gen, 'ffmpeg');
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
        // the picker and Reconnect sit in the Mode panel, which camera-ip-controls.js (re)builds: delegate
        document.addEventListener('change', (event) => {
            const target = event.target;
            if (target && target.id === 'ipCameraProfile') changeProfile(target.value);
        });
        document.addEventListener('click', (event) => {
            const target = event.target;
            const button = target && typeof target.closest === 'function' ? target.closest('#ipCameraReconnectBtn') : null;
            if (button) reconnect();
        });
        if (typeof window.matchMedia === 'function') {
            const mq = window.matchMedia('(max-width: 768px)');
            if (typeof mq.addEventListener === 'function') mq.addEventListener('change', labelProfiles);
        }
        labelProfiles();
    }
    document.addEventListener('visibilitychange', resumeAfterReturn);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wireControls);
    else wireControls();

    window.MCIpCamera = {
        handles, start, stop, refresh, reconnect, changeProfile,
        // what the player is showing when it shows pictures (null for a stream): the Mode tab reads it
        snapshotInfo: () => (mode === 'snapshot'
            ? { reason: snapshotReason, width: snapshotSize ? snapshotSize.width : null, height: snapshotSize ? snapshotSize.height : null, intervalMs: SNAPSHOT_INTERVAL_MS }
            : null),
        // exported for tests
        lastError: () => lastError,
        mseFailures: () => mseFailures,
        _helpers: { reopenStream, startUnsupported, startSnapshotMode, labelProfiles, setAspect, SNAPSHOT_INTERVAL_MS, progressText, mseSupports, liveEdgeTarget, stateMessageKey, isTerminal, infoLine, mseConstructor, TARGET_LEAD_S },
    };
})();
