// ============================================================
// MEDIA WORKSPACE
// Independent frontend module for locally stored camera media (G1): working sort, three views, small server-made
// thumbnails (lazy), a lightbox, selection with bulk download / delete. The pure logic is static/media-views.js.
// ============================================================

let mediaGalleryLoaded = false;
let mediaGalleryLoading = false;
let mediaItems = [];          // as the server listed them
let mediaShown = [];          // sorted
let mediaStorage = {};
let mediaSelection = new Set();
let mediaSort = 'date_desc';
let mediaView = 'large';
let mediaLightboxIndex = null;
let mediaLightboxOpener = null;

function mediaEscapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

function mediaT(key, params) {
    return window.I18N.t(key, params);
}

function mediaViews() {
    return window.MCMediaViews;
}

// the remembered sort and view (settings.media, round-tripped through /api/settings like every form preference)
function mediaLoadPrefs() {
    const saved = (typeof appSettings !== 'undefined' && appSettings && appSettings.media) || {};
    mediaSort = mediaViews().normalizeSort(saved.sort);
    mediaView = mediaViews().pickView(saved, mediaIsPhone());
}

// the sort is shared by all devices, the view is remembered for a phone and for a desktop separately
function mediaIsPhone() {
    return Boolean(window.MCMobileLayout && typeof window.MCMobileLayout.isMobile === 'function' && window.MCMobileLayout.isMobile());
}

async function mediaSavePrefs() {
    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ media: { sort: mediaSort, [mediaViews().viewField(mediaIsPhone())]: mediaView } })
        });
        const data = await response.json();
        if (data && data.ok && data.settings && typeof appSettings !== 'undefined') appSettings = data.settings;
    } catch (error) {
        // the choice just is not remembered
    }
}

async function loadMediaGallery(force = false) {
    const content = document.getElementById('mediaGalleryContent');
    if (!content || mediaGalleryLoading) return;
    if (mediaGalleryLoaded && !force) return;

    mediaGalleryLoading = true;
    content.innerHTML = `<div class="media-loading">🖼️ ${mediaEscapeHtml(mediaT('media.loading'))}</div>`;

    try {
        const response = await fetch('/api/camera/screenshots');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        const screenshots = Array.isArray(data.screenshots) ? data.screenshots : [];
        mediaLoadPrefs();
        renderMediaGallery(screenshots, data.storage || {});
        mediaGalleryLoaded = true;
    } catch (error) {
        console.error('Error loading media gallery:', error);
        content.innerHTML = `
            <div class="media-empty-state media-error-state">
                <div class="media-empty-icon">⚠️</div>
                <div class="media-empty-title">${mediaEscapeHtml(mediaT('media.could_not_load'))}</div>
                <div class="media-empty-text">${mediaEscapeHtml(mediaT('media.check_camera_service'))}</div>
                <button type="button" class="media-primary-btn" onclick="loadMediaGallery(true)">${mediaEscapeHtml(mediaT('common.retry'))}</button>
            </div>`;
    } finally {
        mediaGalleryLoading = false;
    }
}

function renderMediaGallery(screenshots, storage) {
    const content = document.getElementById('mediaGalleryContent');
    if (!content) return;

    mediaItems = screenshots;
    mediaStorage = storage || {};
    mediaSelection = mediaViews().prune(mediaSelection, mediaItems);
    const imageCount = Number(mediaStorage.images ?? screenshots.length) || screenshots.length;
    const usedMb = Number(mediaStorage.used_mb ?? 0);
    const freeGb = Number(mediaStorage.free_gb ?? 0);
    const summary = `
        <div class="media-summary">
            <strong>${mediaEscapeHtml(window.I18N.plural('media.image_count', imageCount, { count: imageCount }))}</strong>
            <span>${mediaEscapeHtml(mediaT('media.storage_used_free', { used: usedMb.toFixed(1), free: freeGb.toFixed(1) }))}</span>
        </div>`;

    if (!screenshots.length) {
        content.innerHTML = `
            <div class="media-toolbar">${summary}</div>
            <div class="media-empty-state">
                <div class="media-empty-icon">📭</div>
                <div class="media-empty-title">${mediaEscapeHtml(mediaT('media.no_images_yet'))}</div>
                <div class="media-empty-text">${mediaEscapeHtml(mediaT('media.no_images_text'))}</div>
            </div>`;
        return;
    }

    const sortOptions = mediaViews().SORTS.map((key) =>
        `<option value="${key}"${key === mediaSort ? ' selected' : ''}>${mediaEscapeHtml(mediaT('media.sort_' + key))}</option>`).join('');
    const viewButtons = mediaViews().VIEWS.map((key) =>
        `<button type="button" class="media-view-btn" data-media-view="${key}" aria-pressed="${key === mediaView}" title="${mediaEscapeHtml(mediaT('media.view_' + key))}" aria-label="${mediaEscapeHtml(mediaT('media.view_' + key))}">${key === 'large' ? '▦' : key === 'small' ? '▩' : '☰'}</button>`).join('');

    content.innerHTML = `
        <div class="media-toolbar">
            ${summary}
            <div class="media-toolbar-actions">
                <label class="media-sort">
                    <span class="media-sort-caption">${mediaEscapeHtml(mediaT('media.sort_label'))}</span>
                    <select id="mediaSortSelect" class="media-sort-select" aria-label="${mediaEscapeHtml(mediaT('media.sort_label'))}">${sortOptions}</select>
                </label>
                <div class="media-view-group" role="group" aria-label="${mediaEscapeHtml(mediaT('media.view_label'))}">${viewButtons}</div>
                <button type="button" class="media-delete-all-btn" onclick="deleteAllMedia()">🗑 ${mediaEscapeHtml(mediaT('media.delete_all'))}</button>
            </div>
        </div>
        <div class="media-selectbar">
            <label class="media-select-all"><input type="checkbox" id="mediaSelectAll"> <span>${mediaEscapeHtml(mediaT('media.select_all'))}</span></label>
        </div>
        <div class="media-zip-note" id="mediaZipNote" role="status" hidden></div>
        <div class="media-bulkbar" id="mediaBulkBar" hidden>
            <span class="media-bulk-summary" id="mediaBulkSummary"></span>
            <button type="button" class="media-bulk-btn" data-media-bulk="download" id="mediaBulkDownload">⬇ ${mediaEscapeHtml(mediaT('media.bulk_download'))}</button>
            <button type="button" class="media-bulk-btn media-bulk-danger" data-media-bulk="delete">🗑 ${mediaEscapeHtml(mediaT('media.bulk_delete'))}</button>
            <button type="button" class="media-bulk-btn" data-media-bulk="clear">${mediaEscapeHtml(mediaT('media.bulk_clear'))}</button>
        </div>
        <div class="media-pictures" id="mediaPictures"></div>`;

    mediaWireGallery(content);
    mediaRenderPictures();
}

function mediaWireGallery(content) {
    content.onclick = mediaHandleClick;
    content.onchange = (event) => {
        const target = event.target;
        if (target.id === 'mediaSortSelect') {
            mediaSort = mediaViews().normalizeSort(target.value);
            mediaSavePrefs();
            mediaRenderPictures();
        } else if (target.id === 'mediaSelectAll') {
            mediaSelection = mediaViews().toggleAll(mediaSelection, mediaShown);
            mediaRenderPictures();
        } else if (target.classList && target.classList.contains('media-check')) {
            mediaSelection = mediaViews().toggle(mediaSelection, target.getAttribute('data-media-name'));
            mediaUpdateSelectionUi();
        }
    };
}

function mediaHandleClick(event) {
    const view = event.target.closest('[data-media-view]');
    if (view) {
        mediaView = mediaViews().normalizeView(view.getAttribute('data-media-view'));
        mediaSavePrefs();
        document.querySelectorAll('.media-view-btn').forEach((button) => {
            button.setAttribute('aria-pressed', String(button.getAttribute('data-media-view') === mediaView));
        });
        mediaRenderPictures();
        return;
    }
    const bulk = event.target.closest('[data-media-bulk]');
    if (bulk) {
        const action = bulk.getAttribute('data-media-bulk');
        if (action === 'clear') { mediaSelection = new Set(); mediaRenderPictures(); }
        else if (action === 'download') mediaDownloadSelected();
        else if (action === 'delete') mediaDeleteSelected();
        return;
    }
    const open = event.target.closest('[data-media-open]');
    if (open) {
        mediaOpenLightbox(Number(open.getAttribute('data-media-open')), open);
        return;
    }
    const del = event.target.closest('[data-media-delete]');
    if (del) {
        event.preventDefault();
        deleteMediaItem(del.getAttribute('data-media-delete'), event);
    }
}

function mediaItemMeta(item) {
    const parts = [];
    const camera = item.camera || mediaT('media.camera_local');
    parts.push(camera);
    parts.push(String(item.modified || ''));
    return parts.join(' · ');
}

function mediaItemFacts(item) {
    const resolution = mediaViews().formatResolution(item);
    return [resolution, mediaViews().formatBytes(item.size)].filter(Boolean).join(' · ');
}

function mediaRenderPictures() {
    const host = document.getElementById('mediaPictures');
    if (!host) return;
    mediaShown = mediaViews().sortItems(mediaItems, mediaSort);
    host.className = 'media-pictures media-view-' + mediaView;
    host.innerHTML = mediaShown.map((item, index) => renderMediaItem(item, index)).join('');
    mediaUpdateSelectionUi();
}

function renderMediaItem(item, index) {
    const filename = String(item.filename || '');
    const displayName = String(item.display_name || filename.split('/').pop() || mediaT('media.image_fallback_name'));
    const url = String(item.url || '#');
    const thumb = String(item.thumb_url || item.url || '');
    const checked = mediaSelection.has(filename) ? ' checked' : '';
    const nameAttr = mediaEscapeHtml(filename);
    const check = `<input type="checkbox" class="media-check" data-media-name="${nameAttr}"${checked} aria-label="${mediaEscapeHtml(mediaT('media.select_item'))}">`;
    const preview = `
        <button type="button" class="media-preview media-open" data-media-open="${index}" aria-label="${mediaEscapeHtml(mediaT('media.open_image'))}: ${mediaEscapeHtml(displayName)}">
            <img src="${mediaEscapeHtml(thumb)}" alt="${mediaEscapeHtml(displayName)}" loading="lazy" decoding="async"
                 onerror="this.closest('.media-preview').classList.add('media-preview-error'); this.style.display='none';">
            <span class="media-preview-error-text">${mediaEscapeHtml(mediaT('media.image_unavailable'))}</span>
        </button>`;
    const actions = `
        <div class="media-item-actions">
            <a class="media-icon-btn" href="${mediaEscapeHtml(url)}" download="${mediaEscapeHtml(displayName)}" title="${mediaEscapeHtml(mediaT('media.download'))}" aria-label="${mediaEscapeHtml(mediaT('media.download_image_aria'))}">⬇</a>
            <button type="button" class="media-icon-btn media-delete-btn" data-media-delete="${nameAttr}" title="${mediaEscapeHtml(mediaT('common.delete'))}" aria-label="${mediaEscapeHtml(mediaT('media.delete_image_aria'))}">🗑</button>
        </div>`;

    if (mediaView === 'list') {
        return `
        <article class="media-row" data-media-filename="${nameAttr}">
            ${check}
            ${preview}
            <div class="media-row-name"><strong title="${mediaEscapeHtml(displayName)}">${mediaEscapeHtml(displayName)}</strong><span>${mediaEscapeHtml(item.camera || mediaT('media.camera_local'))}</span></div>
            <div class="media-row-date">${mediaEscapeHtml(String(item.modified || ''))}</div>
            <div class="media-row-facts">${mediaEscapeHtml(mediaItemFacts(item))}</div>
            ${actions}
        </article>`;
    }
    return `
        <article class="media-item media-card" data-media-filename="${nameAttr}">
            <div class="media-card-top">${check}</div>
            ${preview}
            <div class="media-item-info">
                <div class="media-item-copy">
                    <strong title="${mediaEscapeHtml(displayName)}">${mediaEscapeHtml(displayName)}</strong>
                    <span>${mediaEscapeHtml(mediaItemMeta(item))}</span>
                    <span>${mediaEscapeHtml(mediaItemFacts(item))}</span>
                </div>
                ${actions}
            </div>
        </article>`;
}

// the checkbox state, the select-all box, the bulk bar and the size note above it
function mediaUpdateSelectionUi() {
    const state = mediaViews().bulkState(mediaSelection, mediaShown);
    document.querySelectorAll('#mediaPictures .media-check').forEach((box) => {
        box.checked = mediaSelection.has(box.getAttribute('data-media-name'));
    });
    const all = document.getElementById('mediaSelectAll');
    if (all) {
        all.checked = mediaViews().allSelected(mediaSelection, mediaShown);
        all.indeterminate = state.any && !all.checked;
    }
    const bar = document.getElementById('mediaBulkBar');
    if (bar) bar.hidden = !state.any;
    const summary = document.getElementById('mediaBulkSummary');
    if (summary) summary.textContent = window.I18N.plural('media.selected_summary', state.count, { count: state.count, size: mediaViews().formatBytes(state.bytes) });
    const download = document.getElementById('mediaBulkDownload');
    if (download) download.disabled = !state.canDownload;
    const note = document.getElementById('mediaZipNote');
    if (note) {
        // said ABOVE the bar, before the tap: why the download is off and what to do
        note.hidden = !(state.any && (state.zipTooLarge || state.tooMany));
        note.textContent = state.tooMany
            ? mediaT('media.bulk_too_many', { max: mediaViews().BULK_MAX })
            : mediaT('media.zip_limit_message', { max: mediaViews().formatBytes(state.zipMaxBytes), size: mediaViews().formatBytes(state.bytes) });
    }
}

async function mediaDownloadSelected() {
    const names = Array.from(mediaSelection);
    if (!names.length) return;
    try {
        const response = await fetch('/api/camera/screenshots/zip', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filenames: names })
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 413) {
            if (typeof showToast === 'function') showToast(`❌ ${mediaT('media.zip_limit_message', { max: mediaViews().formatBytes(data.max_bytes), size: mediaViews().formatBytes(data.total_bytes) })}`, 'error', { duration: 10000 });
            return;
        }
        if (!response.ok || !data.ok || !data.url) throw new Error(data.error || mediaT('media.download_failed'));
        // a plain link: the browser streams the zip to disk itself
        const link = document.createElement('a');
        link.href = data.url;
        link.rel = 'noopener';
        document.body.appendChild(link);
        link.click();
        link.remove();
        if (typeof showToast === 'function') showToast(`✅ ${window.I18N.plural('media.zip_started', data.count || names.length, { count: data.count || names.length })}`, 'success');
    } catch (error) {
        console.error('Error preparing the download:', error);
        if (typeof showToast === 'function') showToast(`❌ ${error.message}`, 'error', { duration: 10000 });
    }
}

async function mediaDeleteSelected() {
    const names = Array.from(mediaSelection);
    if (!names.length) return;
    const confirmed = await appConfirm({
        title: mediaT('dialog.title_delete_images'),
        message: window.I18N.plural('media.bulk_delete_confirm', names.length, { count: names.length }),
        confirmLabel: mediaT('dialog.delete'),
        danger: true,
    });
    if (!confirmed) return;
    try {
        const response = await fetch('/api/camera/screenshots/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filenames: names })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.error || mediaT('media.delete_failed'));
        const failed = Array.isArray(data.failed) ? data.failed.length : 0;
        if (typeof showToast === 'function') {
            if (failed) showToast(`⚠️ ${mediaT('media.bulk_delete_partial', { done: data.deleted_count, failed })}`, 'error', { duration: 10000 });
            else showToast(`✅ ${window.I18N.plural('media.bulk_deleted', data.deleted_count, { count: data.deleted_count })}`, 'success');
        }
        mediaSelection = new Set();
        mediaGalleryLoaded = false;
        await loadMediaGallery(true);
    } catch (error) {
        console.error('Error deleting selected media:', error);
        if (typeof showToast === 'function') showToast(`❌ ${error.message}`, 'error', { duration: 10000 });
    }
}

// ---------------------------------------------------------------- lightbox

function mediaEnsureLightbox() {
    let box = document.getElementById('mediaLightbox');
    if (box) return box;
    box = document.createElement('div');
    box.id = 'mediaLightbox';
    box.className = 'media-lightbox';
    box.hidden = true;
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', mediaT('media.lightbox_label'));
    box.innerHTML = `
        <button type="button" class="media-lb-btn media-lb-close" data-lb="close" aria-label="${mediaEscapeHtml(mediaT('media.lightbox_close'))}">✕</button>
        <button type="button" class="media-lb-btn media-lb-prev" data-lb="prev" aria-label="${mediaEscapeHtml(mediaT('media.lightbox_prev'))}">‹</button>
        <button type="button" class="media-lb-btn media-lb-next" data-lb="next" aria-label="${mediaEscapeHtml(mediaT('media.lightbox_next'))}">›</button>
        <div class="media-lb-stage"><img class="media-lb-img" alt=""></div>
        <div class="media-lb-caption">
            <div class="media-lb-text"><strong id="mediaLbName"></strong><span id="mediaLbMeta"></span><span id="mediaLbFacts"></span></div>
            <div class="media-lb-actions">
                <a class="media-lb-link" id="mediaLbDownload" download>⬇ ${mediaEscapeHtml(mediaT('media.download'))}</a>
                <button type="button" class="media-lb-link media-lb-danger" data-lb="delete">🗑 ${mediaEscapeHtml(mediaT('common.delete'))}</button>
            </div>
        </div>`;
    box.addEventListener('click', (event) => {
        const action = event.target.closest('[data-lb]');
        if (action) {
            const what = action.getAttribute('data-lb');
            if (what === 'close') mediaCloseLightbox();
            else if (what === 'prev') mediaStepLightbox(-1);
            else if (what === 'next') mediaStepLightbox(1);
            else if (what === 'delete') mediaDeleteFromLightbox();
        } else if (event.target === box || event.target.classList.contains('media-lb-stage')) {
            mediaCloseLightbox();
        }
    });
    let startX = null;
    box.addEventListener('touchstart', (event) => { startX = event.touches.length === 1 ? event.touches[0].clientX : null; }, { passive: true });
    box.addEventListener('touchend', (event) => {
        if (startX === null) return;
        const dx = event.changedTouches[0].clientX - startX;
        startX = null;
        if (Math.abs(dx) > 50) mediaStepLightbox(dx < 0 ? 1 : -1);
    }, { passive: true });
    document.addEventListener('keydown', (event) => {
        if (mediaLightboxIndex === null) return;
        if (event.key === 'Escape') { event.preventDefault(); mediaCloseLightbox(); }
        else if (event.key === 'ArrowLeft') mediaStepLightbox(-1);
        else if (event.key === 'ArrowRight') mediaStepLightbox(1);
    });
    document.body.appendChild(box);
    return box;
}

function mediaOpenLightbox(index, opener) {
    if (!Number.isInteger(index) || !mediaShown[index]) return;
    mediaLightboxOpener = opener || null;
    const box = mediaEnsureLightbox();
    box.hidden = false;
    document.body.classList.add('media-lightbox-open');
    mediaShowLightboxItem(index);
    const close = box.querySelector('.media-lb-close');
    if (close) close.focus();
}

function mediaShowLightboxItem(index) {
    const item = mediaShown[index];
    const box = document.getElementById('mediaLightbox');
    if (!item || !box) return;
    mediaLightboxIndex = index;
    const img = box.querySelector('.media-lb-img');
    img.alt = String(item.display_name || '');
    img.src = String(item.url || '');
    document.getElementById('mediaLbName').textContent = String(item.display_name || '');
    document.getElementById('mediaLbMeta').textContent = mediaItemMeta(item);
    document.getElementById('mediaLbFacts').textContent = [mediaItemFacts(item), `${index + 1} / ${mediaShown.length}`].filter(Boolean).join(' · ');
    const download = document.getElementById('mediaLbDownload');
    download.href = String(item.url || '#');
    download.setAttribute('download', String(item.display_name || ''));
    box.querySelector('.media-lb-prev').disabled = mediaViews().neighbour(index, -1, mediaShown.length) === null;
    box.querySelector('.media-lb-next').disabled = mediaViews().neighbour(index, 1, mediaShown.length) === null;
}

function mediaStepLightbox(delta) {
    if (mediaLightboxIndex === null) return;
    const next = mediaViews().neighbour(mediaLightboxIndex, delta, mediaShown.length);
    if (next !== null) mediaShowLightboxItem(next);
}

function mediaCloseLightbox() {
    const box = document.getElementById('mediaLightbox');
    if (box) {
        box.hidden = true;
        const img = box.querySelector('.media-lb-img');
        if (img) img.removeAttribute('src');
    }
    mediaLightboxIndex = null;
    document.body.classList.remove('media-lightbox-open');
    if (mediaLightboxOpener && typeof mediaLightboxOpener.focus === 'function' && document.contains(mediaLightboxOpener)) mediaLightboxOpener.focus();
    mediaLightboxOpener = null;
}

async function mediaDeleteFromLightbox() {
    const index = mediaLightboxIndex;
    const item = mediaShown[index];
    if (!item) return;
    const deleted = await deleteMediaItem(item.filename, null, { reload: false });
    if (!deleted) return;
    mediaItems = mediaItems.filter((entry) => entry.filename !== item.filename);
    mediaSelection.delete(item.filename);
    const after = mediaViews().indexAfterDelete(index, mediaShown.length);
    mediaGalleryLoaded = false;
    renderMediaGallery(mediaItems, mediaStorage);
    if (after === null) mediaCloseLightbox();
    else mediaShowLightboxItem(after);
}

async function deleteMediaItem(filename, event, options = {}) {
    event?.preventDefault();
    event?.stopPropagation();

    if (!(await appConfirm({ title: mediaT('dialog.title_delete_image'), message: mediaT('media.delete_image_confirm', { filename }), confirmLabel: mediaT('dialog.delete'), danger: true }))) return false;

    try {
        const response = await fetch(`/api/camera/screenshot/${encodeURIComponent(filename)}`, {
            method: 'DELETE'
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || mediaT('media.delete_failed'));
        }

        if (typeof showToast === 'function') showToast(`✅ ${mediaT('media.image_deleted')}`, 'success');
        mediaSelection.delete(filename);
        if (options.reload !== false) {
            mediaGalleryLoaded = false;
            await loadMediaGallery(true);
        }
        return true;
    } catch (error) {
        console.error('Error deleting media item:', error);
        if (typeof showToast === 'function') showToast(`❌ ${error.message}`, 'error');
        return false;
    }
}

async function deleteAllMedia() {
    // One danger dialog (the old second "are you sure" confirm is gone); says how many
    // images are about to be deleted when the gallery is on screen.
    const shown = document.querySelectorAll('#mediaPictures [data-media-filename]').length;
    const message = shown > 0
        ? window.I18N.plural('media.delete_all_confirm_count', shown, { count: shown })
        : window.I18N.t('media.delete_all_confirm');
    if (!(await appConfirm({
        title: window.I18N.t('dialog.title_delete_all_images'),
        message,
        confirmLabel: window.I18N.t('dialog.delete'),
        danger: true,
    }))) return;

    try {
        const response = await fetch('/api/camera/screenshots', { method: 'DELETE' });
        const data = await response.json().catch(() => ({}));

        if (!response.ok) throw new Error(data.error || window.I18N.t('media.delete_failed'));

        if (typeof showToast === 'function') showToast(`✅ ${window.I18N.t('media.all_images_deleted')}`, 'success');
        if (typeof mediaSelection !== 'undefined') mediaSelection = new Set();
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
