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
    content.innerHTML = '<div class="media-loading">🖼️ Loading media…</div>';

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
                <div class="media-empty-title">Could not load media</div>
                <div class="media-empty-text">Check the camera service and network connection.</div>
                <button type="button" class="media-primary-btn" onclick="loadMediaGallery(true)">Retry</button>
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
                    <strong>0 images</strong>
                    <span>${usedMb.toFixed(1)} MB used · ${freeGb.toFixed(1)} GB free</span>
                </div>
            </div>
            <div class="media-empty-state">
                <div class="media-empty-icon">📭</div>
                <div class="media-empty-title">No captured images yet</div>
                <div class="media-empty-text">Photos captured in Camera will appear here automatically.</div>
            </div>`;
        return;
    }

    content.innerHTML = `
        <div class="media-toolbar">
            <div class="media-summary">
                <strong>${imageCount} ${imageCount === 1 ? 'image' : 'images'}</strong>
                <span>${usedMb.toFixed(1)} MB used · ${freeGb.toFixed(1)} GB free · Newest first</span>
            </div>
            <div class="media-toolbar-actions">
                <span class="media-sort-label">Date ↓</span>
                <button type="button" class="media-delete-all-btn" onclick="deleteAllMedia()">🗑 Delete All</button>
            </div>
        </div>
        <div class="media-grid">
            ${screenshots.map(renderMediaItem).join('')}
        </div>`;
}

function renderMediaItem(item) {
    const filename = String(item.filename || '');
    const displayName = String(item.display_name || filename.split('/').pop() || 'Image');
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
                <span class="media-preview-error-text">Image unavailable</span>
            </a>
            <div class="media-item-info">
                <div class="media-item-copy">
                    <strong title="${mediaEscapeHtml(displayName)}">${mediaEscapeHtml(displayName)}</strong>
                    <span>${mediaEscapeHtml(modified)} · ${sizeKb} KB</span>
                </div>
                <div class="media-item-actions">
                    <a class="media-icon-btn" href="${mediaEscapeHtml(url)}" download="${mediaEscapeHtml(displayName)}" title="Download" aria-label="Download image">⬇</a>
                    <button type="button" class="media-icon-btn media-delete-btn" onclick='deleteMediaItem(${handlerFilename}, event)' title="Delete" aria-label="Delete image">🗑</button>
                </div>
            </div>
        </article>`;
}

async function deleteMediaItem(filename, event) {
    event?.preventDefault();
    event?.stopPropagation();

    if (!confirm(`Delete image "${filename}"?`)) return;

    try {
        const response = await fetch(`/api/camera/screenshot/${encodeURIComponent(filename)}`, {
            method: 'DELETE'
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || 'Delete failed');
        }

        if (typeof showToast === 'function') showToast('✅ Image deleted', 'success');
        mediaGalleryLoaded = false;
        await loadMediaGallery(true);
    } catch (error) {
        console.error('Error deleting media item:', error);
        if (typeof showToast === 'function') showToast(`❌ ${error.message}`, 'error');
    }
}

async function deleteAllMedia() {
    if (!confirm('Delete ALL images? This action cannot be undone.')) return;
    if (!confirm('Are you sure you want to permanently delete the complete gallery?')) return;

    try {
        const response = await fetch('/api/camera/screenshots', { method: 'DELETE' });
        const data = await response.json().catch(() => ({}));

        if (!response.ok) throw new Error(data.error || 'Delete failed');

        if (typeof showToast === 'function') showToast('✅ All images deleted', 'success');
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
