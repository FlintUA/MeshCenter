// ===== PHOTO =====
let photoPreviewResolution = '640x480';
let photoSaveResolution = '3280x2464';
let currentPhotoQuality = 85;
let currentPhotoData = null;

// ============================================================
// PHOTO FUNCTIONS
// ============================================================
async function loadPhotoSettings() {
    try {
        const response = await fetch('/api/photo/settings');
        const data = await response.json();
        
        if (data.ok) {
            photoPreviewResolution = data.config.resolution || '640x480';
            photoSaveResolution = data.save_resolution || '3280x2464';
            currentPhotoQuality = data.config.quality || 85;
            
            const resSelect = document.getElementById('photoResolution');
            const qualitySlider = document.getElementById('photoQuality');
            const qualityLabel = document.getElementById('photoQualityLabel');
            
            if (resSelect) resSelect.value = photoPreviewResolution;
            if (qualitySlider) {
                qualitySlider.value = currentPhotoQuality;
                if (qualityLabel) qualityLabel.textContent = currentPhotoQuality + '%';
            }
            
            const photoInfo = document.getElementById('photoInfo');
            if (photoInfo) {
                const res = photoPreviewResolution.replace('x', '×');
                photoInfo.textContent = window.I18N.t('camera.photo_info', {
                    resolution: res,
                    quality: currentPhotoQuality,
                    saveResolution: photoSaveResolution.replace('x', '×')
                });
            }
            
            console.log('[PHOTO] Settings loaded:', { preview: photoPreviewResolution, quality: currentPhotoQuality });
        }
    } catch (error) {
        console.error('Error loading photo settings:', error);
    }
}

async function updatePhotoSettings(showMessage = false) {
    const resolution = document.getElementById('photoResolution')?.value;
    const quality = parseInt(document.getElementById('photoQuality')?.value || '95');

    const label = document.getElementById('photoQualityLabel');
    if (label) label.textContent = `${quality}%`;

    try {
        const response = await fetch('/api/photo/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                resolution: resolution,
                quality: quality
            })
        });

        const data = await response.json();

        if (!data.ok) {
            showToast(`❌ ${window.I18N.t('camera.update_photo_settings_failed')}`, 'error');
            return;
        }

        if (showMessage) {
            showToast(`✅ ${window.I18N.t('camera.photo_quality_set', { quality })}`, 'success');
        }

    } catch (error) {
        console.error('Error updating photo settings:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
    }
}

async function capturePhotoPreview() {
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    // For a USB-backed camera, capture_photo() briefly stops and restarts
    // the live stream to shoot at the camera's real max resolution (see
    // usb_driver.py) - /video_feed's connection ends, not just pauses, so
    // without this the live feed would just look frozen/broken for ~1-2s
    // with no explanation. Same dim-and-reconnect pattern
    // captureCameraPhoto() already uses for the old CSI highres-save path,
    // which has the same kind of stream disruption during capture.
    const videoFeed = document.getElementById('videoFeed');

    if (videoFeed) videoFeed.classList.add('camera-capturing');

    try {
        if (status) {
            status.textContent = `⏳ ${window.I18N.t('camera.capturing_preview')}`;
            status.style.color = '#ff9800';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = '⏳...';
        }
        if (display) display.style.display = 'none';
        if (placeholder) placeholder.style.display = 'flex';

        console.log('[PHOTO] Capturing preview with quality:', currentPhotoQuality);
        const response = await fetch('/api/photo/capture', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();
        console.log('[PHOTO] Response:', data);

        if (data.ok && data.image_data) {
            if (display) {
                display.src = 'data:image/jpeg;base64,' + data.image_data;
                display.style.display = 'block';
            }
            if (placeholder) placeholder.style.display = 'none';
            if (status) {
                const res = data.preview_resolution || photoPreviewResolution;
                const quality = data.quality || currentPhotoQuality;
                status.textContent = `📷 ${window.I18N.t('camera.preview_ready', { resolution: res.replace('x', '×'), quality })}`;
                status.style.color = '#2e7d32';
            }
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
            }
            currentPhotoData = data.image_data;
        } else {
            console.error('[PHOTO] Failed:', data.error);
            if (status) {
                status.textContent = `❌ ${window.I18N.t('camera.failed_prefix', { reason: data.error || window.I18N.t('errors.unknown_error') })}`;
                status.style.color = '#c62828';
            }
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
            }
            if (placeholder) placeholder.style.display = 'flex';
        }
    } catch (error) {
        console.error('[PHOTO] Error:', error);
        if (status) {
            status.textContent = `❌ ${window.I18N.t('errors.network_error')}`;
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
        }
        if (placeholder) placeholder.style.display = 'flex';
    } finally {
        if (videoFeed) videoFeed.classList.remove('camera-capturing');
        // capture_photo() has already fully restored the stream (it's
        // synchronous - the HTTP response only comes back after the resume
        // start() call returns) by the time this fetch resolves, but the
        // resumed reader thread still needs a moment to deliver its first
        // frame - same 1200ms buffer captureCameraPhoto() already uses
        // before reconnecting, for the same reason.
        setTimeout(() => { refreshVideoFeed(); }, 1200);
    }
}

async function captureCameraPhoto() {
    if (!cameraPowerEnabled) {
        showToast(
            `⚫ ${window.I18N.t('camera.turn_camera_on_first')}`,
            'error'
        );
        return;
    }

    const btn = document.querySelector('.camera-actions-block .screenshot-btn');
    const videoFeed = document.getElementById('videoFeed');

    try {
        if (btn) {
            btn.disabled = true;
            btn.textContent = `⏳ ${window.I18N.t('camera.saving')}`;
        }

        if (videoFeed) {
            videoFeed.classList.add('camera-capturing');
        }


        await updatePhotoSettings(false);

        const response = await fetch('/api/photo/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.ok) {
            showToast(`✅ ${window.I18N.t('camera.screenshot_saved_named', { name: data.display_name || data.filename })}`, 'success');
        } else {
            showToast(`❌ ${window.I18N.t('camera.screenshot_failed', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
        }

    } catch (error) {
        console.error('Screenshot error:', error);
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');

    } finally {

    if (videoFeed) {
        videoFeed.classList.remove('camera-capturing');
    }

    setTimeout(() => {
        refreshVideoFeed();
    }, 1200);

    if (btn) {
        btn.disabled = false;
        btn.textContent = `📸 ${window.I18N.t('camera.screenshot')}`;
        }
    }
}

async function savePhoto() {
    const display = document.getElementById('photoDisplay');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    
    if (!display || display.style.display === 'none' || !currentPhotoData) {
        showToast(`❌ ${window.I18N.t('camera.no_photo_to_save')}`, 'error');
        return;
    }

    try {
        if (status) {
            status.textContent = `⏳ ${window.I18N.t('camera.capturing_high_res')}`;
            status.style.color = '#ff9800';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = '⏳...';
        }

        const response = await fetch('/api/photo/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.ok) {
            if (data.preview_data && display) {
                display.src = 'data:image/jpeg;base64,' + data.preview_data;
            }

            if (status) {
                status.textContent = `✅ ${window.I18N.t('camera.saved_exclaim')}`;
                status.style.color = '#2e7d32';
            }
            showToast(`✅ ${window.I18N.t('camera.photo_saved', { filename: data.filename, size: (data.size / 1024).toFixed(1) })}`, 'success');

            setTimeout(() => {
                if (status) {
                    const res = photoPreviewResolution.replace('x', '×');
                    status.textContent = `📷 ${window.I18N.t('camera.preview_ready', { resolution: res, quality: currentPhotoQuality })}`;
                    status.style.color = '#2e7d32';
                }
                if (saveBtn) {
                    saveBtn.disabled = false;
                    saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
                }
            }, 2000);
        } else {
            if (status) {
                status.textContent = `❌ ${window.I18N.t('camera.save_failed')}`;
                status.style.color = '#c62828';
            }
            showToast(`❌ ${window.I18N.t('camera.failed_to_save', { reason: data.error || window.I18N.t('errors.unknown_error') })}`, 'error');
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
            }
        }
    } catch (error) {
        console.error('Error saving photo:', error);
        if (status) {
            status.textContent = `❌ ${window.I18N.t('errors.network_error')}`;
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = `💾 ${window.I18N.t('common.save')}`;
        }
        showToast(`❌ ${window.I18N.t('errors.network_error')}`, 'error');
    }
}

function refreshPhoto() {
    const status = document.getElementById('photoStatus');
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    
    if (status) {
        status.textContent = `⏳ ${window.I18N.t('camera.capturing')}`;
        status.style.color = '#ff9800';
    }
    if (display) display.style.display = 'none';
    if (placeholder) placeholder.style.display = 'flex';

    switchCameraMode('photo').then(() => {
        setTimeout(() => capturePhotoPreview(), 300);
    });
}

