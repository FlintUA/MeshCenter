// ============================================================
// MEDIA WORKSPACE
// Independent frontend module for locally stored camera media.
// ============================================================

let mediaGalleryLoaded = false;
let mediaGalleryLoading = false;

function mediaEscapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

function mediaFilenameForHandler(filename) {
    return JSON.stringify(String(filename ?? ''));
}

async function loadMediaGallery(force = false) {
    const content = document.getElementById('mediaGalleryContent');
    if (!content || mediaGalleryLoading) return;
    if (mediaGalleryLoaded && !force) return;

    mediaGalleryLoading = true;
    content.innerHTML = `<div class="media-loading">🖼️ ${mediaEscapeHtml(window.I18N.t('media.loading'))}</div>`;

    try {
        const response = await fetch('/api/camera/screenshots');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        const screenshots = Array.isArray(data.screenshots) ? data.screenshots : [];
        const storage = data.storage || {};

        renderMediaGallery(screenshots, storage);
        mediaGalleryLoaded = true;
    } catch (error) {
        console.error('Error loading media gallery:', error);
        content.innerHTML = `
            <div class="media-empty-state media-error-state">
                <div class="media-empty-icon">⚠️</div>
                <div class="media-empty-title">${mediaEscapeHtml(window.I18N.t('media.could_not_load'))}</div>
                <div class="media-empty-text">${mediaEscapeHtml(window.I18N.t('media.check_camera_service'))}</div>
                <button type="button" class="media-primary-btn" onclick="loadMediaGallery(true)">${mediaEscapeHtml(window.I18N.t('common.retry'))}</button>
            </div>`;
    } finally {
        mediaGalleryLoading = false;
    }
}

function renderMediaGallery(screenshots, storage) {
    const content = document.getElementById('mediaGalleryContent');
    if (!content) return;

    const imageCount = Number(storage.images ?? screenshots.length) || screenshots.length;
    const usedMb = Number(storage.used_mb ?? 0);
    const freeGb = Number(storage.free_gb ?? 0);

    if (!screenshots.length) {
        content.innerHTML = `
            <div class="media-toolbar">
                <div class="media-summary">
                    <strong>${mediaEscapeHtml(window.I18N.plural('media.image_count', 0, { count: 0 }))}</strong>
                    <span>${mediaEscapeHtml(window.I18N.t('media.storage_used_free', { used: usedMb.toFixed(1), free: freeGb.toFixed(1) }))}</span>
                </div>
            </div>
            <div class="media-empty-state">
                <div class="media-empty-icon">📭</div>
                <div class="media-empty-title">${mediaEscapeHtml(window.I18N.t('media.no_images_yet'))}</div>
                <div class="media-empty-text">${mediaEscapeHtml(window.I18N.t('media.no_images_text'))}</div>
            </div>`;
        return;
    }

    content.innerHTML = `
        <div class="media-toolbar">
            <div class="media-summary">
                <strong>${mediaEscapeHtml(window.I18N.plural('media.image_count', imageCount, { count: imageCount }))}</strong>
                <span>${mediaEscapeHtml(window.I18N.t('media.storage_used_free', { used: usedMb.toFixed(1), free: freeGb.toFixed(1) }))} · ${mediaEscapeHtml(window.I18N.t('media.newest_first'))}</span>
            </div>
            <div class="media-toolbar-actions">
                <span class="media-sort-label">${mediaEscapeHtml(window.I18N.t('media.date_desc'))}</span>
                <button type="button" class="media-delete-all-btn" onclick="deleteAllMedia()">🗑 ${mediaEscapeHtml(window.I18N.t('media.delete_all'))}</button>
            </div>
        </div>
        <div class="media-grid">
            ${screenshots.map(renderMediaItem).join('')}
        </div>`;
}

function renderMediaItem(item) {
    const filename = String(item.filename || '');
    const displayName = String(item.display_name || filename.split('/').pop() || window.I18N.t('media.image_fallback_name'));
    const url = String(item.url || '#');
    const modified = String(item.modified || '');
    const sizeKb = (Number(item.size || 0) / 1024).toFixed(1);
    const handlerFilename = mediaFilenameForHandler(filename);

    return `
        <article class="media-item" data-media-filename="${mediaEscapeHtml(filename)}">
            <a class="media-preview" href="${mediaEscapeHtml(url)}" target="_blank" rel="noopener">
                <img src="${mediaEscapeHtml(url)}"
                     alt="${mediaEscapeHtml(displayName)}"
                     loading="lazy"
                     onerror="this.closest('.media-preview').classList.add('media-preview-error'); this.style.display='none';">
                <span class="media-preview-error-text">${mediaEscapeHtml(window.I18N.t('media.image_unavailable'))}</span>
            </a>
            <div class="media-item-info">
                <div class="media-item-copy">
                    <strong title="${mediaEscapeHtml(displayName)}">${mediaEscapeHtml(displayName)}</strong>
                    <span>${mediaEscapeHtml(modified)} · ${sizeKb} KB</span>
                </div>
                <div class="media-item-actions">
                    <a class="media-icon-btn" href="${mediaEscapeHtml(url)}" download="${mediaEscapeHtml(displayName)}" title="${mediaEscapeHtml(window.I18N.t('media.download'))}" aria-label="${mediaEscapeHtml(window.I18N.t('media.download_image_aria'))}">⬇</a>
                    <button type="button" class="media-icon-btn media-delete-btn" onclick='deleteMediaItem(${handlerFilename}, event)' title="${mediaEscapeHtml(window.I18N.t('common.delete'))}" aria-label="${mediaEscapeHtml(window.I18N.t('media.delete_image_aria'))}">🗑</button>
                </div>
            </div>
        </article>`;
}

async function deleteMediaItem(filename, event) {
    event?.preventDefault();
    event?.stopPropagation();

    if (!confirm(window.I18N.t('media.delete_image_confirm', { filename }))) return;

    try {
        const response = await fetch(`/api/camera/screenshot/${encodeURIComponent(filename)}`, {
            method: 'DELETE'
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || window.I18N.t('media.delete_failed'));
        }

        if (typeof showToast === 'function') showToast(`✅ ${window.I18N.t('media.image_deleted')}`, 'success');
        mediaGalleryLoaded = false;
        await loadMediaGallery(true);
    } catch (error) {
        console.error('Error deleting media item:', error);
        if (typeof showToast === 'function') showToast(`❌ ${error.message}`, 'error');
    }
}

async function deleteAllMedia() {
    if (!confirm(window.I18N.t('media.delete_all_confirm'))) return;
    if (!confirm(window.I18N.t('media.delete_all_confirm2'))) return;

    try {
        const response = await fetch('/api/camera/screenshots', { method: 'DELETE' });
        const data = await response.json().catch(() => ({}));

        if (!response.ok) throw new Error(data.error || window.I18N.t('media.delete_failed'));

        if (typeof showToast === 'function') showToast(`✅ ${window.I18N.t('media.all_images_deleted')}`, 'success');
        mediaGalleryLoaded = false;
        await loadMediaGallery(true);
    } catch (error) {
        console.error('Error deleting all media:', error);
        if (typeof showToast === 'function') showToast(`❌ ${error.message}`, 'error');
    }
}

// Compatibility alias for older camera hooks or bookmarked UI actions.
function showScreenshots() {
    if (typeof switchMainTab === 'function') switchMainTab('media');
}

window.loadMediaGallery = loadMediaGallery;
window.deleteMediaItem = deleteMediaItem;
window.deleteAllMedia = deleteAllMedia;
window.showScreenshots = showScreenshots;
