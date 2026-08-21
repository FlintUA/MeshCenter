// ============================================================
// CAMERA CONTROL
// ============================================================

function isCameraTabVisible() {
    return currentMainTab === 'video';
}

function setCameraFeedLoading(loading, message = window.I18N.t('camera.connecting')) {
    const img = document.getElementById('videoFeed');
    const placeholder = document.getElementById('cameraLoadingPlaceholder');
    const title = placeholder?.querySelector('.camera-loading-title');

    if (title && message) {
        title.textContent = message;
    }

    if (placeholder) {
        placeholder.classList.toggle('camera-loading-visible', Boolean(loading));
        placeholder.setAttribute('aria-hidden', loading ? 'false' : 'true');
    }

    if (img) {
        img.classList.toggle('camera-stream-hidden', Boolean(loading));
    }
}

function showCameraFeed() {
    const img = document.getElementById('videoFeed');
    if (img) {
        img.classList.remove('camera-stream-hidden');
    }
    setCameraFeedLoading(false);
}

function hideCameraFeed(message = window.I18N.t('camera.connecting')) {
    setCameraFeedLoading(true, message);
}

function setCameraControlsDisabled(disabled) {
    const controls = document.getElementById('videoControls');

    if (!controls) {
        return;
    }

    controls.classList.toggle(
        'camera-controls-disabled',
        disabled
    );

    controls.querySelectorAll(
        'input, select, button'
    ).forEach(element => {
        if (element.id === 'cameraGalleryBtn') {
            return;
        }

        element.disabled = disabled;
    });
}

function renderCameraPowerState() {
    const button =
        document.getElementById('cameraPowerBtn');

    const buttonText =
        document.getElementById('cameraPowerBtnText');

    const placeholder =
        document.getElementById('cameraOffPlaceholder');

    const feed =
        document.getElementById('videoFeed');

    const status =
        document.getElementById('videoStatus');

    const liveInfo =
        document.getElementById('videoLiveInfo');

    const transitioning =
        cameraPowerStatus === 'starting'
        || cameraPowerStatus === 'stopping';

    if (button) {
        button.disabled =
            cameraPowerRequestInProgress || transitioning;

        button.classList.toggle(
            'camera-power-off',
            !cameraPowerEnabled
        );
    }

    if (cameraPowerStatus === 'starting') {
        if (buttonText) {
            buttonText.textContent = window.I18N.t('camera.starting');
        }

        if (status) {
            status.textContent = `🟡 ${window.I18N.t('camera.starting_camera')}`;
            status.style.color = '#d97706';
        }

        return;
    }

    if (cameraPowerStatus === 'stopping') {
        if (buttonText) {
            buttonText.textContent = window.I18N.t('camera.stopping');
        }

        if (status) {
            status.textContent = `🟠 ${window.I18N.t('camera.stopping_camera')}`;
            status.style.color = '#ea580c';
        }

        return;
    }

    if (cameraPowerStatus === 'error') {
        if (buttonText) {
            buttonText.textContent =
                cameraPowerEnabled ? window.I18N.t('camera.turn_off') : window.I18N.t('camera.try_again');
        }

        if (status) {
            status.textContent = `🔴 ${window.I18N.t('camera.camera_error')}`;
            status.style.color = '#c62828';
        }

        return;
    }

    if (!cameraPowerEnabled) {
        cameraActive = false;

        if (feed) {
            feed.removeAttribute('src');
            feed.classList.add('camera-stream-hidden');
        }
        setCameraFeedLoading(false);

        if (placeholder) {
            placeholder.style.display = 'flex';
        }

        if (buttonText) {
            buttonText.textContent = window.I18N.t('camera.turn_on');
        }

        if (status) {
            status.textContent = `⚫ ${window.I18N.t('notifications.camera_off')}`;
            status.style.color = '#64748b';
        }

        if (liveInfo) {
            liveInfo.textContent = window.I18N.t('camera.power_saving_mode');
        }

        setCameraControlsDisabled(true);
        updateStatusDock('video');
        return;
    }

    if (placeholder) {
        placeholder.style.display = 'none';
    }

    if (feed && cameraActive && feed.naturalWidth > 0) {
        showCameraFeed();
    }

    if (buttonText) {
        buttonText.textContent = window.I18N.t('camera.turn_off');
    }

    setCameraControlsDisabled(false);

    if (status) {
        status.textContent =
            cameraActive && isCameraTabVisible()
                ? `🟢 ${window.I18N.t('nodes.status_online')}`
                : `⏸️ ${window.I18N.t('camera.paused')}`;

        status.style.color =
            cameraActive && isCameraTabVisible()
                ? '#4caf50'
                : '#888';
    }

    updateStatusDock('video');
}

async function loadCameraPowerState() {
    try {
        const response =
            await fetch('/api/camera/power');

        const data = await response.json();

        if (!response.ok || !data.ok) {
            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            requestError.params = data.error_params || undefined;
            throw requestError;
        }

        cameraPowerEnabled = Boolean(data.enabled);
        cameraPowerStatus = data.status || (
            cameraPowerEnabled ? 'ready' : 'off'
        );

        renderCameraPowerState();
        return data;

    } catch (error) {
        cameraPowerStatus = 'error';
        renderCameraPowerState();

        console.error(
            '[CAMERA POWER] State load failed:',
            error
        );

        return null;
    }
}

async function setCameraPower(enabled) {
    if (cameraPowerRequestInProgress) {
        return;
    }

    cameraPowerRequestInProgress = true;
    cameraPowerStatus = enabled
        ? 'starting'
        : 'stopping';

    if (!enabled) {
        stopVideoFeed();
        cameraActive = false;
    }

    renderCameraPowerState();

    try {
        const response =
            await fetch('/api/camera/power', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    enabled: Boolean(enabled)
                })
            });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            if (data.technical_error) {
                console.error('[NODE TOOLS] Technical details:', data.technical_error);
            }

            const requestError = new Error(
                data.error || `HTTP ${response.status}`
            );
            requestError.code = data.error_code || '';
            requestError.params = data.error_params || undefined;
            throw requestError;
        }

        cameraPowerEnabled = Boolean(data.enabled);
        cameraPowerStatus = data.status || (
            cameraPowerEnabled ? 'ready' : 'off'
        );

        if (
            cameraPowerEnabled
            && isCameraTabVisible()
        ) {
            await switchCameraMode('video');
            await loadVideoSettings();
            await loadPhotoSettings();
            await reconnectCameraFeed();
            cameraActive = true;
        }

        renderCameraPowerState();

        showToast(
            cameraPowerEnabled
                ? `✅ ${window.I18N.t('camera.camera_turned_on')}`
                : `✅ ${window.I18N.t('camera.camera_turned_off')}`,
            'success'
        );

    } catch (error) {
        cameraPowerStatus = 'error';

        showToast(
            `❌ ${window.I18N.t('camera.camera_power_error', { message: translateRequestError(error) })}`,
            'error'
        );

        console.error(
            '[CAMERA POWER] Change failed:',
            error
        );

        await loadCameraPowerState();

    } finally {
        cameraPowerRequestInProgress = false;
        renderCameraPowerState();
    }
}

function toggleCameraPower() {
    setCameraPower(!cameraPowerEnabled);
}

async function startCameraStream() {
    if (!cameraPowerEnabled || cameraActive) return;
    cameraActive = true;

    console.log('[CAMERA] Starting stream...');
    hideCameraFeed(window.I18N.t('camera.connecting'));

    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = `🔄 ${window.I18N.t('camera.starting')}`;
        status.style.color = '#ff9800';
    }

    await reconnectCameraFeed();
}

function stopCameraStream() {
    if (!cameraActive) return;
    cameraActive = false;

    console.log('[CAMERA] Stopping stream...');
    const img = document.getElementById('videoFeed');
    if (img) {
        img.removeAttribute('src');
        img.classList.add('camera-stream-hidden');
    }
    setCameraFeedLoading(false);

    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = `⏸️ ${window.I18N.t('camera.paused')}`;
        status.style.color = '#888';
    }
}

// ============================================================
// VIDEO FUNCTIONS
// ============================================================
let currentVideoSettings = {};
let currentCameraControls = {};

let cameraControlRequestInProgress = false;
let cameraControlPending = false;
let cameraControlShowMessage = false;
let cameraFeedRefreshTimer = null;
let cameraFeedRefreshSequence = 0;
let videoSettingsRequestInProgress = false;
let videoSettingsPending = false;

async function loadVideoSettings() {
    try {
        const response = await fetch('/api/camera/settings');
        const data = await response.json();
        
        if (data.ok) {
            currentVideoSettings = data.config;
            currentCameraControls = data.controls || {};
            
            const resSelect = document.getElementById('videoResolution');
            const fpsSelect = document.getElementById('videoFps');
            const qualitySlider = document.getElementById('videoQuality');
            const qualityLabel = document.getElementById('videoQualityLabel');
            
            if (resSelect) resSelect.value = data.config.resolution || '640x480';
            if (fpsSelect) fpsSelect.value = data.config.fps || 12;
            const cameraControlValues = {
                cameraBrightness: currentCameraControls.brightness ?? 0.0,
                cameraContrast: currentCameraControls.contrast ?? 1.0,
                cameraSaturation: currentCameraControls.saturation ?? 1.0,
                cameraSharpness: currentCameraControls.sharpness ?? 1.0,
                cameraExposure: currentCameraControls.exposure_compensation ?? 0.0
            };

            Object.entries(cameraControlValues).forEach(([id, value]) => {
                const element = document.getElementById(id);
                if (element) {
                    element.value = value;
                }
            });

            updateCameraControlLabels();

            const presetSelect = document.getElementById('cameraImagePreset');
            if (presetSelect) {
                presetSelect.value = 'custom';
            }

            const whiteBalanceSelect = document.getElementById('cameraWhiteBalance');
            if (whiteBalanceSelect) {
                const savedAwbMode = String(currentCameraControls.awb_mode || 'auto').toLowerCase();
                const availableMode = Array.from(whiteBalanceSelect.options)
                    .some(option => option.value === savedAwbMode);
                whiteBalanceSelect.value = availableMode ? savedAwbMode : 'auto';
            }

            const liveInfo = document.getElementById('videoLiveInfo');
            if (liveInfo) {
                liveInfo.textContent = window.I18N.t('camera.live_info', { resolution: data.config.resolution || '640×480', fps: data.config.fps || 12 });
            }

            const statusEl = document.getElementById('videoStatus');
            if (statusEl) {
                statusEl.textContent = cameraActive ? `🟢 ${window.I18N.t('nodes.status_online')}` : `⏸️ ${window.I18N.t('camera.paused')}`;
                statusEl.style.color = cameraActive ? '#4caf50' : '#888';
            }
            
            const controls = document.getElementById('videoControls');
            if (controls) {
                controls.style.display = 'block';
                controls.style.visibility = 'visible';
                controls.style.opacity = '1';
            }
        }
    } catch (error) {
        console.error('Error loading video settings:', error);
    }
}

function updateVideoQualityLabel(value) {
    const qualityLabel = document.getElementById('videoQualityLabel');
    if (qualityLabel) {
        qualityLabel.textContent = `${value}%`;
    }
}

function readVideoSettingsFromUi() {
    const resolution = document.getElementById('videoResolution')?.value || '640x480';
    const fps = parseInt(document.getElementById('videoFps')?.value || '12', 10);
    const quality = parseInt(document.getElementById('videoQuality')?.value || '75', 10);

    return { resolution, fps, quality };
}

function sameVideoSettings(left, right) {
    if (!left || !right) {
        return false;
    }

    return String(left.resolution) === String(right.resolution)
        && Number(left.fps) === Number(right.fps)
        && Number(left.quality) === Number(right.quality);
}

async function updateVideoSettings() {
    updateVideoQualityLabel(
        document.getElementById('videoQuality')?.value || 75
    );

    /*
     * Camera reconfiguration is expensive on Raspberry Pi Zero 2 W.
     * Serialize requests and keep only the latest pending UI state so a
     * quick resolution/FPS change cannot start overlapping pipelines.
     */
    if (videoSettingsRequestInProgress) {
        videoSettingsPending = true;
        return;
    }

    const requestedSettings = readVideoSettingsFromUi();

    if (sameVideoSettings(requestedSettings, currentVideoSettings)) {
        return;
    }

    videoSettingsRequestInProgress = true;
    videoSettingsPending = false;

    const liveInfo = document.getElementById('videoLiveInfo');
    const status = document.getElementById('videoStatus');

    if (status) {
        status.textContent = `🔄 ${window.I18N.t('camera.applying_settings')}`;
        status.style.color = '#ff9800';
    }

    try {
        const response = await fetch('/api/camera/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestedSettings)
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        currentVideoSettings = data.config || requestedSettings;

        if (liveInfo) {
            const appliedResolution = currentVideoSettings.resolution || requestedSettings.resolution;
            const appliedFps = currentVideoSettings.fps || requestedSettings.fps;
            liveInfo.textContent = window.I18N.t('camera.live_info', { resolution: appliedResolution.replace('x', '×'), fps: appliedFps });
        }

        if (cameraActive) {
            await reconnectCameraFeed();
        } else if (status) {
            status.textContent = `⏸️ ${window.I18N.t('camera.paused')}`;
            status.style.color = '#888';
        }

        showToast(`✅ ${window.I18N.t('camera.video_settings_updated')}`, 'success');

    } catch (error) {
        console.error('Error updating video settings:', error);
        showToast(`❌ ${window.I18N.t('camera.settings_failed', { message: error.message })}`, 'error');
        await loadVideoSettings();

    } finally {
        videoSettingsRequestInProgress = false;

        if (videoSettingsPending) {
            videoSettingsPending = false;
            queueMicrotask(updateVideoSettings);
        }
    }
}


const CAMERA_IMAGE_PRESETS = Object.freeze({
    neutral: {
        brightness: 0.0,
        contrast: 1.0,
        saturation: 1.0,
        sharpness: 1.0,
        exposure_compensation: 0.0
    },
    indoor: {
        brightness: 0.2,
        contrast: 1.1,
        saturation: 1.0,
        sharpness: 1.1,
        exposure_compensation: 0.5
    },
    night: {
        brightness: 0.5,
        contrast: 1.15,
        saturation: 0.0,
        sharpness: 1.0,
        exposure_compensation: 1.0
    },
    outdoor: {
        brightness: -0.1,
        contrast: 1.1,
        saturation: 1.15,
        sharpness: 1.2,
        exposure_compensation: -0.5
    },
    monochrome: {
        brightness: 0.1,
        contrast: 1.2,
        saturation: 0.0,
        sharpness: 1.2,
        exposure_compensation: 0.0
    },
    highContrast: {
        brightness: 0.0,
        contrast: 1.5,
        saturation: 0.9,
        sharpness: 1.4,
        exposure_compensation: 0.0
    }
});

function switchCameraControlTab(tabName) {
    const showImage = tabName === 'image';

    const cameraTab = document.getElementById('cameraControlTabCamera');
    const imageTab = document.getElementById('cameraControlTabImage');
    const cameraPanel = document.getElementById('cameraControlsPanel');
    const imagePanel = document.getElementById('imageControlsPanel');

    cameraTab?.classList.toggle('active', !showImage);
    imageTab?.classList.toggle('active', showImage);

    cameraTab?.setAttribute('aria-selected', String(!showImage));
    imageTab?.setAttribute('aria-selected', String(showImage));

    if (cameraPanel) {
        cameraPanel.hidden = showImage;
        cameraPanel.classList.toggle('active', !showImage);
    }

    if (imagePanel) {
        imagePanel.hidden = !showImage;
        imagePanel.classList.toggle('active', showImage);
    }
}

function markCameraImagePresetCustom() {
    const presetSelect = document.getElementById('cameraImagePreset');

    if (presetSelect) {
        presetSelect.value = 'custom';
    }
}

function writeCameraImageControls(values) {
    const mapping = {
        cameraBrightness: values.brightness,
        cameraContrast: values.contrast,
        cameraSaturation: values.saturation,
        cameraSharpness: values.sharpness,
        cameraExposure: values.exposure_compensation
    };

    Object.entries(mapping).forEach(([id, value]) => {
        const element = document.getElementById(id);

        if (element && Number.isFinite(Number(value))) {
            element.value = String(value);
        }
    });

    updateCameraControlLabels();
}

async function applyCameraImagePreset(presetName) {
    if (!presetName || presetName === 'custom') {
        return;
    }

    const preset = CAMERA_IMAGE_PRESETS[presetName];

    if (!preset) {
        return;
    }

    writeCameraImageControls(preset);
    await updateCameraImageControls(false);

    showToast(
        `✅ ${window.I18N.t('camera.image_preset_applied', { name: document.getElementById('cameraImagePreset')?.selectedOptions[0]?.textContent || presetName })}`,
        'success'
    );
}

function formatCameraControlValue(value) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
        return '0.0';
    }

    return number.toFixed(1);
}


function updateCameraControlLabels() {
    const controls = [
        ['cameraBrightness', 'cameraBrightnessValue'],
        ['cameraContrast', 'cameraContrastValue'],
        ['cameraSaturation', 'cameraSaturationValue'],
        ['cameraSharpness', 'cameraSharpnessValue'],
        ['cameraExposure', 'cameraExposureValue']
    ];

    controls.forEach(([inputId, labelId]) => {
        const input = document.getElementById(inputId);
        const label = document.getElementById(labelId);

        if (input && label) {
            label.textContent = formatCameraControlValue(input.value);
        }
    });
}


function readCameraImageControls() {
    return {
        brightness: parseFloat(
            document.getElementById('cameraBrightness')?.value ?? 0
        ),

        contrast: parseFloat(
            document.getElementById('cameraContrast')?.value ?? 1
        ),

        saturation: parseFloat(
            document.getElementById('cameraSaturation')?.value ?? 1
        ),

        sharpness: parseFloat(
            document.getElementById('cameraSharpness')?.value ?? 1
        ),

        exposure_compensation: parseFloat(
            document.getElementById('cameraExposure')?.value ?? 0
        ),

        awb_mode: document.getElementById('cameraWhiteBalance')?.value || 'auto'
    };
}


async function updateCameraImageControls(showMessage = false) {
    cameraControlPending = true;

    if (showMessage) {
        cameraControlShowMessage = true;
    }

    if (cameraControlRequestInProgress) {
        return;
    }

    cameraControlRequestInProgress = true;

    try {
        while (cameraControlPending) {
            cameraControlPending = false;

            const controls = readCameraImageControls();

            const response = await fetch('/api/camera/settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    controls: controls
                })
            });

            const data = await response.json();

            if (!response.ok || !data.ok) {
                throw new Error(
                    data.error || `HTTP ${response.status}`
                );
            }

            currentCameraControls = data.controls || controls;
            updateCameraControlLabels();

            /*
             * Image controls restart the Picamera2 pipeline.
             * The browser must always reconnect to the new MJPEG stream.
             */
            if (data.restarted) {
                await reconnectCameraFeed();
            }
        }

        if (cameraControlShowMessage) {
            showToast(`✅ ${window.I18N.t('camera.image_settings_updated')}`, 'success');
        }

    } catch (error) {
        console.error(
            'Error updating camera image controls:',
            error
        );

        showToast(
            `❌ ${window.I18N.t('camera.image_settings_failed', { message: error.message })}`,
            'error'
        );

    } finally {
        cameraControlRequestInProgress = false;
        cameraControlShowMessage = false;

        if (cameraControlPending) {
            updateCameraImageControls(false);
        }
    }
}

async function restoreCameraImageDefaults() {
    writeCameraImageControls(CAMERA_IMAGE_PRESETS.neutral);

    const presetSelect = document.getElementById('cameraImagePreset');
    if (presetSelect) {
        presetSelect.value = 'neutral';
    }

    const whiteBalanceSelect = document.getElementById('cameraWhiteBalance');
    if (whiteBalanceSelect) {
        whiteBalanceSelect.value = 'auto';
    }

    await updateCameraImageControls(false);

    showToast(
        `✅ ${window.I18N.t('camera.neutral_settings_restored')}`,
        'success'
    );
}

async function takeScreenshot(source = 'video') {
    const btn = document.querySelector('.screenshot-btn');
    const originalText = btn.textContent;
    
    try {
        btn.disabled = true;
        btn.textContent = `⏳ ${window.I18N.t('camera.capturing')}`;

        const response = await fetch('/api/camera/screenshot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: source })
        });

        const data = await response.json();

        if (data.ok) {
            showToast(`✅ ${window.I18N.t('camera.screenshot_saved')}`, 'success');
        } else {
            showToast(`❌ ${window.I18N.t('camera.failed_prefix', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
        }
    } catch (error) {
        console.error('Error taking screenshot:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

function stopVideoFeed() {
    const img = document.getElementById('videoFeed');
    if (img) {
        img.onload = null;
        img.onerror = null;
        img.removeAttribute('src');
        img.classList.add('camera-stream-hidden');
    }

    setCameraFeedLoading(false);
}

function reconnectCameraFeed() {
    if (!cameraPowerEnabled) {
        renderCameraPowerState();
        return Promise.resolve(false);
    }

    return new Promise((resolve) => {
        const img = document.getElementById('videoFeed');
        const status = document.getElementById('videoStatus');
        const frameWrap = img?.closest('.video-frame-wrap');

        if (!img) {
            resolve();
            return;
        }

        const sequence = ++cameraFeedRefreshSequence;

        if (cameraFeedRefreshTimer) {
            clearTimeout(cameraFeedRefreshTimer);
            cameraFeedRefreshTimer = null;
        }

        hideCameraFeed(window.I18N.t('camera.connecting'));

        if (status) {
            status.textContent = `🔄 ${window.I18N.t('camera.connecting_short')}`;
            status.style.color = '#ff9800';
        }

        let freezeFrame = null;

        /* A stale overlay from an interrupted reconnect must never survive. */
        frameWrap?.querySelectorAll('.camera-freeze-frame').forEach(element => element.remove());
        frameWrap?.classList.add('camera-feed-reconfiguring');

        /*
         * Preserve the latest visible camera frame while the
         * Picamera2 pipeline and MJPEG connection are restarting.
         */
        if (
            frameWrap &&
            img.complete &&
            img.naturalWidth > 0 &&
            img.naturalHeight > 0
        ) {
            try {
                const canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth;
                canvas.height = img.naturalHeight;

                const context = canvas.getContext('2d');

                if (context) {
                    context.drawImage(
                        img,
                        0,
                        0,
                        canvas.width,
                        canvas.height
                    );

                freezeFrame = document.createElement('img');
                freezeFrame.className = 'camera-freeze-frame';
                freezeFrame.src = canvas.toDataURL(
                    'image/jpeg',
                    0.85
                );

                frameWrap.appendChild(freezeFrame);
                }

            } catch (error) {
                console.warn(
                    '[CAMERA] Could not preserve current frame:',
                    error
                );
            }
        }

        img.onload = null;
        img.onerror = null;
        img.removeAttribute('src');

        cameraFeedRefreshTimer = setTimeout(() => {
            cameraFeedRefreshTimer = null;

            if (sequence !== cameraFeedRefreshSequence) {
                freezeFrame?.remove();
                frameWrap?.classList.remove('camera-feed-reconfiguring');
                resolve();
                return;
            }

            const finishReconnect = (online) => {
                if (sequence !== cameraFeedRefreshSequence) {
                    return;
                }

                frameWrap?.classList.remove('camera-feed-reconfiguring');

                if (online) {
                    showCameraFeed();
                } else {
                    hideCameraFeed(window.I18N.t('camera.stream_unavailable'));
                }

                if (status) {
                    status.textContent = online
                        ? `🟢 ${window.I18N.t('nodes.status_online')}`
                        : `🔴 ${window.I18N.t('camera.camera_unavailable')}`;

                    status.style.color = online
                        ? '#4caf50'
                        : '#c62828';
                }

                if (freezeFrame) {
                    freezeFrame.classList.add(
                        'camera-freeze-frame-hide'
                    );

                    setTimeout(() => {
                        freezeFrame.remove();
                    }, 250);
                }

                resolve();
            };

            img.onload = function() {
                finishReconnect(true);
            };

            img.onerror = function() {
                finishReconnect(false);
            };

            img.src = '/video_feed?t=' + Date.now();

            /*
             * MJPEG load events can behave differently between
             * browsers. Use a fallback after the new connection
             * has had enough time to produce its first frame.
             */
            setTimeout(() => {
                if (
                    sequence === cameraFeedRefreshSequence &&
                    img.naturalWidth > 0
                ) {
                    finishReconnect(true);
                }
            }, 1600);

        }, 500);
    });
}

function refreshVideoFeed() {
    if (!cameraPowerEnabled) {
        renderCameraPowerState();
        return;
    }

    reconnectCameraFeed();
}

// ============================================================
// SWITCH CAMERA MODE
// ============================================================
async function switchCameraMode(mode) {
    if (!cameraPowerEnabled) {
        renderCameraPowerState();
        return false;
    }

    try {
        console.log(`[CAMERA] Switching to ${mode} mode...`);
        const response = await fetch('/api/camera/switch_mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode })
        });
        
        const data = await response.json();
        if (data.ok) {
            console.log(`[CAMERA] Switched to ${mode} mode: ${data.resolution}`);
            return true;
        } else {
            console.error(`[CAMERA] Failed to switch to ${mode}:`, data.error);
            return false;
        }
    } catch (error) {
        console.error(`[CAMERA] Error switching to ${mode}:`, error);
        return false;
    }
}

