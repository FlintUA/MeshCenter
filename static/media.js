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
let mediaFilter = '';         // G4: the text in the filter field, remembered per kind of device
let mediaSearchOpen = false;  // G4: on a phone the filter field is a bar that opens in place of the toolbar
let mediaFilterSaveTimer = null;
let mediaLightboxIndex = null;
let mediaLightboxOpener = null;
const MEDIA_COMPACT_BELOW_PX = 700;   // a gallery column narrower than this gets the phone toolbar
let mediaSwallowClick = false;   // the click that ends a long press must not open the picture

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
    mediaFilter = mediaViews().pickFilter(saved, mediaIsPhone());
    mediaSearchOpen = Boolean(mediaFilter);
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
            body: JSON.stringify({ media: {
                sort: mediaSort,
                [mediaViews().viewField(mediaIsPhone())]: mediaView,
                [mediaViews().filterField(mediaIsPhone())]: mediaViews().normalizeFilter(mediaFilter)
            } })
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

// Inline SVG, one stroke style, colour from the text (an emoji or a unicode glyph draws differently on every OS; the
// "small thumbnails" glyph was a white square on iOS 15).
const MEDIA_ICON_PATHS = {
    sort: '<path d="M7 4v16"/><path d="M4 17l3 3 3-3"/><path d="M17 20V4"/><path d="M14 7l3-3 3 3"/>',
    large: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/>',
    small: '<rect x="3" y="3" width="5" height="5" rx="1"/><rect x="9.5" y="3" width="5" height="5" rx="1"/><rect x="16" y="3" width="5" height="5" rx="1"/><rect x="3" y="9.5" width="5" height="5" rx="1"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><rect x="16" y="9.5" width="5" height="5" rx="1"/><rect x="3" y="16" width="5" height="5" rx="1"/><rect x="9.5" y="16" width="5" height="5" rx="1"/><rect x="16" y="16" width="5" height="5" rx="1"/>',
    list: '<path d="M9 6h12"/><path d="M9 12h12"/><path d="M9 18h12"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    more: '<circle cx="5" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="19" cy="12" r="1.6" fill="currentColor"/>',
    download: '<path d="M12 4v11"/><path d="M7 11l5 5 5-5"/><path d="M5 20h14"/>',
    trash: '<path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>',
    close: '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    back: '<path d="M15 5l-7 7 7 7"/>',
    send: '<path d="M21 3L10 14"/><path d="M21 3l-7 18-4-8-8-4 19-6z"/>',
};

function mediaIcon(name) {
    return `<svg class="media-ico" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${MEDIA_ICON_PATHS[name] || ''}</svg>`;
}

// a toolbar/menu button: icon plus a label that stays for screen readers on a phone (CSS hides it visually there)
function mediaBtn(cls, attrs, icon, label) {
    return `<button type="button" class="${cls}" ${attrs} aria-label="${mediaEscapeHtml(label)}" title="${mediaEscapeHtml(label)}">${mediaIcon(icon)}<span class="media-btn-label">${mediaEscapeHtml(label)}</span></button>`;
}

// G4: the filter field (one on the toolbar of a desktop, one in the phone search bar; the two stay in step)
function mediaFilterField(id) {
    const label = mediaEscapeHtml(mediaT('media.filter_label'));
    return `<span class="media-filter-ico" aria-hidden="true">${mediaIcon('search')}</span>`
        + `<input type="text" id="${id}" class="media-filter-input" value="${mediaEscapeHtml(mediaFilter)}" maxlength="${mediaViews().FILTER_MAX}" `
        + `placeholder="${mediaEscapeHtml(mediaT('media.filter_placeholder'))}" aria-label="${label}" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="search">`
        + `<button type="button" class="media-filter-clear" data-media-action="filter-clear" aria-label="${mediaEscapeHtml(mediaT('media.filter_clear'))}" title="${mediaEscapeHtml(mediaT('media.filter_clear'))}"${mediaFilter ? '' : ' hidden'}>${mediaIcon('close')}</button>`;
}

function renderMediaGallery(screenshots, storage) {
    const content = document.getElementById('mediaGalleryContent');
    if (!content) return;

    mediaItems = screenshots;
    mediaStorage = storage || {};
    mediaSelection = mediaViews().prune(mediaSelection, mediaItems);
    mediaCloseMenus();

    if (!screenshots.length) {
        content.innerHTML = `
            <div class="media-toolbar media-toolbar-empty" id="mediaToolbar">
                <div class="media-tb">
                    <span class="media-tb-summary" id="mediaSummary"></span>
                    <span class="media-tb-spacer"></span>
                    ${mediaBtn('media-tb-btn', 'data-media-action="refresh"', 'refresh', mediaT('common.refresh'))}
                </div>
            </div>
            <div class="media-empty-state">
                <div class="media-empty-icon">📭</div>
                <div class="media-empty-title">${mediaEscapeHtml(mediaT('media.no_images_yet'))}</div>
                <div class="media-empty-text">${mediaEscapeHtml(mediaT('media.no_images_text'))}</div>
            </div>`;
        mediaRenderSummary();
        return;
    }

    const sortOptions = mediaViews().SORTS.map((key) =>
        `<option value="${key}"${key === mediaSort ? ' selected' : ''}>${mediaEscapeHtml(mediaT('media.sort_' + key))}</option>`).join('');
    const viewButtons = mediaViews().VIEWS.map((key) =>
        mediaBtn('media-view-btn', `data-media-view="${key}" aria-pressed="${key === mediaView}"`, key, mediaT('media.view_' + key))).join('');
    const viewItems = mediaViews().VIEWS.map((key) =>
        `<button type="button" class="media-menu-item" role="menuitemradio" data-media-view="${key}" aria-checked="${key === mediaView}">${mediaIcon(key)}<span>${mediaEscapeHtml(mediaT('media.view_' + key))}</span></button>`).join('');
    const selectAllLabel = mediaEscapeHtml(mediaT('media.select_all'));

    content.innerHTML = `
        <div class="media-toolbar" id="mediaToolbar" data-mode="normal">
            <div class="media-tb media-tb-normal" id="mediaTbNormal">
                <label class="media-tb-check" title="${selectAllLabel}"><input type="checkbox" data-media-selectall aria-label="${selectAllLabel}"></label>
                <span class="media-tb-summary" id="mediaSummary"></span>
                <span class="media-tb-spacer"></span>
                <div class="media-filter media-only-desktop">${mediaFilterField('mediaFilterInput')}</div>
                <button type="button" class="media-tb-btn media-only-phone media-search-btn${mediaFilter ? ' is-active' : ''}" id="mediaSearchBtn" data-media-action="search-open" aria-label="${mediaEscapeHtml(mediaT('media.filter_open'))}" title="${mediaEscapeHtml(mediaT('media.filter_open'))}">${mediaIcon('search')}</button>
                <label class="media-sort">
                    <span class="media-sort-caption">${mediaEscapeHtml(mediaT('media.sort_label'))}</span>
                    <span class="media-sort-ico">${mediaIcon('sort')}</span>
                    <select id="mediaSortSelect" class="media-sort-select" aria-label="${mediaEscapeHtml(mediaT('media.sort_label'))}">${sortOptions}</select>
                </label>
                <div class="media-view-group media-only-desktop" role="group" aria-label="${mediaEscapeHtml(mediaT('media.view_label'))}">${viewButtons}</div>
                <div class="media-menu-host media-only-phone">
                    <button type="button" class="media-tb-btn" id="mediaViewMenuBtn" data-media-menu="view" aria-haspopup="menu" aria-expanded="false" aria-label="${mediaEscapeHtml(mediaT('media.view_label'))}" title="${mediaEscapeHtml(mediaT('media.view_label'))}"><span id="mediaViewMenuIcon">${mediaIcon(mediaView)}</span></button>
                    <div class="media-menu" id="mediaViewMenu" role="menu" hidden>${viewItems}</div>
                </div>
                ${mediaBtn('media-tb-btn media-only-desktop', 'data-media-action="refresh"', 'refresh', mediaT('common.refresh'))}
                <div class="media-menu-host">
                    <button type="button" class="media-tb-btn" id="mediaMoreBtn" data-media-menu="more" aria-haspopup="menu" aria-expanded="false" aria-label="${mediaEscapeHtml(mediaT('media.more'))}" title="${mediaEscapeHtml(mediaT('media.more'))}">${mediaIcon('more')}</button>
                    <div class="media-menu media-menu-end" id="mediaMoreMenu" role="menu" hidden>
                        <div class="media-menu-info media-only-phone" id="mediaFreeSpace"></div>
                        <button type="button" class="media-menu-item media-only-phone" role="menuitem" data-media-action="refresh">${mediaIcon('refresh')}<span>${mediaEscapeHtml(mediaT('common.refresh'))}</span></button>
                        <button type="button" class="media-menu-item media-menu-danger" role="menuitem" data-media-action="delete-all">${mediaIcon('trash')}<span>${mediaEscapeHtml(mediaT('media.delete_all'))}</span></button>
                    </div>
                </div>
            </div>
            <div class="media-tb media-tb-search" id="mediaTbSearch" hidden>
                <button type="button" class="media-tb-btn" data-media-action="search-close" aria-label="${mediaEscapeHtml(mediaT('media.filter_close'))}" title="${mediaEscapeHtml(mediaT('media.filter_close'))}">${mediaIcon('back')}</button>
                <div class="media-filter media-filter-wide">${mediaFilterField('mediaFilterInputPhone')}</div>
            </div>
            <div class="media-tb media-tb-selection" id="mediaTbSelection" hidden>
                <label class="media-tb-check media-only-desktop" title="${selectAllLabel}"><input type="checkbox" data-media-selectall aria-label="${selectAllLabel}"></label>
                ${mediaBtn('media-tb-btn media-only-phone', 'data-media-bulk="clear"', 'close', mediaT('media.bulk_clear'))}
                <span class="media-tb-summary" id="mediaBulkSummary"></span>
                <span class="media-tb-spacer"></span>
                ${mediaBtn('media-tb-btn media-tb-labeled', 'data-media-bulk="send" id="mediaBulkSend"', 'send', mediaT('media.send'))}
                ${mediaBtn('media-tb-btn media-tb-labeled', 'data-media-bulk="download" id="mediaBulkDownload"', 'download', mediaT('media.bulk_download'))}
                ${mediaBtn('media-tb-btn media-tb-labeled media-tb-danger', 'data-media-bulk="delete"', 'trash', mediaT('media.bulk_delete'))}
                <button type="button" class="media-tb-text media-only-desktop" data-media-bulk="clear">${mediaEscapeHtml(mediaT('common.cancel'))}</button>
            </div>
        </div>
        <div class="media-zip-note" id="mediaZipNote" role="status" hidden></div>
        <div class="media-pictures" id="mediaPictures"></div>`;

    mediaWireGallery(content);
    mediaRenderPictures();
}

// "9 pictures · 0.9 MB · 51 GB free" on a desktop, "9 · 0.9 MB" on a phone (CSS shows one of the two)
function mediaRenderSummary() {
    const target = document.getElementById('mediaSummary');
    if (!target) return;
    const count = Number(mediaStorage.images ?? mediaItems.length) || mediaItems.length;
    const used = (Number(mediaStorage.used_mb ?? 0) || 0).toFixed(1);
    const free = (Number(mediaStorage.free_gb ?? 0) || 0).toFixed(1);
    if (mediaViews().normalizeFilter(mediaFilter)) {
        // G4: "3 of 12 pictures" while a filter narrows the list
        const shown = mediaShown.length;
        const total = mediaItems.length;
        target.innerHTML = `<span class="media-sum-full">${mediaEscapeHtml(mediaT('media.summary_filtered_full', { pictures: window.I18N.plural('media.filtered_count', total, { shown, total }), used, free }))}</span>`
            + `<span class="media-sum-short">${mediaEscapeHtml(mediaT('media.summary_filtered_short', { shown, total, used }))}</span>`
            + `<span class="media-sum-tiny">${mediaEscapeHtml(mediaT('media.summary_filtered_tiny', { shown, total }))}</span>`;
        const spaceNow = document.getElementById('mediaFreeSpace');
        if (spaceNow) spaceNow.textContent = mediaT('media.free_space', { free });
        return;
    }
    target.innerHTML = `<span class="media-sum-full">${mediaEscapeHtml(mediaT('media.summary_full', { pictures: window.I18N.plural('media.image_count', count, { count }), used, free }))}</span>`
        + `<span class="media-sum-short">${mediaEscapeHtml(mediaT('media.summary_short', { count, used }))}</span>`
        + `<span class="media-sum-tiny">${mediaEscapeHtml(String(count))}</span>`;
    const space = document.getElementById('mediaFreeSpace');
    if (space) space.textContent = mediaT('media.free_space', { free });
}

function mediaWireGallery(content) {
    content.onclick = mediaHandleClick;
    content.onchange = (event) => {
        const target = event.target;
        if (target.id === 'mediaSortSelect') {
            mediaSort = mediaViews().normalizeSort(target.value);
            mediaSavePrefs();
            mediaRenderPictures();
        } else if (target.hasAttribute && target.hasAttribute('data-media-selectall')) {
            mediaSelection = mediaViews().toggleAll(mediaSelection, mediaShown);
            mediaRenderPictures();
        } else if (target.classList && target.classList.contains('media-check')) {
            mediaSelection = mediaViews().toggle(mediaSelection, target.getAttribute('data-media-name'));
            mediaUpdateSelectionUi();
        }
    };
    content.oninput = (event) => {
        const target = event.target;
        if (target.classList && target.classList.contains('media-filter-input')) mediaSetFilter(target.value, target);
    };
    content.onkeydown = (event) => {
        const target = event.target;
        if (event.key !== 'Escape' || !target.classList || !target.classList.contains('media-filter-input')) return;
        event.stopPropagation();
        if (target.value) mediaSetFilter('', target);       // first Esc clears the text, the next one closes the phone bar
        else if (mediaToolbarIsCompact()) mediaCloseSearch();
    };
    mediaWireLongPress(content);
    // a light shadow under the toolbar once the pictures have scrolled beneath it
    // the toolbar goes compact (icons, short summary) on a phone and in any narrow column; it re-checks when resized
    const fit = () => {
        const bar = document.getElementById('mediaToolbar');
        if (bar) bar.classList.toggle('is-compact', mediaIsPhone() || content.clientWidth < MEDIA_COMPACT_BELOW_PX);
        mediaApplyToolbarMode();
    };
    fit();
    if (typeof ResizeObserver === 'function') {
        if (content.__mediaRo) content.__mediaRo.disconnect();
        content.__mediaRo = new ResizeObserver(fit);
        content.__mediaRo.observe(content);
    }
    content.onscroll = () => {
        const bar = document.getElementById('mediaToolbar');
        if (bar) bar.classList.toggle('is-stuck', content.scrollTop > 2);
    };
}

// ---- menus (view on a phone, more everywhere) ----------------------------------------------------------------------

function mediaCloseMenus() {
    document.querySelectorAll('.media-menu').forEach((menu) => { menu.hidden = true; });
    document.querySelectorAll('[data-media-menu]').forEach((button) => button.setAttribute('aria-expanded', 'false'));
}

function mediaToggleMenu(button) {
    const menu = document.getElementById(button.getAttribute('data-media-menu') === 'view' ? 'mediaViewMenu' : 'mediaMoreMenu');
    if (!menu) return;
    const open = menu.hidden;
    mediaCloseMenus();
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
}

if (!window.__mediaMenuListeners) {
    window.__mediaMenuListeners = true;
    document.addEventListener('click', (event) => {
        if (!event.target.closest || !event.target.closest('.media-menu-host')) mediaCloseMenus();
    });
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || mediaLightboxIndex !== null) return;
        if (document.querySelector('.media-menu:not([hidden])')) { mediaCloseMenus(); return; }
        if (mediaViews().selectionMode(mediaSelection) && document.getElementById('mediaPictures')) { mediaSelection = new Set(); mediaUpdateSelectionUi(); }
    });
}

function mediaHandleClick(event) {
    if (mediaSwallowClick && event.target.closest('#mediaPictures')) {
        mediaSwallowClick = false;
        event.preventDefault();     // the click that ends a long press
        return;
    }
    const menu = event.target.closest('[data-media-menu]');
    if (menu) { mediaToggleMenu(menu); return; }
    const view = event.target.closest('[data-media-view]');
    if (view) {
        mediaView = mediaViews().normalizeView(view.getAttribute('data-media-view'));
        mediaSavePrefs();
        mediaCloseMenus();
        document.querySelectorAll('.media-view-btn').forEach((button) => {
            button.setAttribute('aria-pressed', String(button.getAttribute('data-media-view') === mediaView));
        });
        document.querySelectorAll('.media-menu-item[data-media-view]').forEach((item) => {
            item.setAttribute('aria-checked', String(item.getAttribute('data-media-view') === mediaView));
        });
        const icon = document.getElementById('mediaViewMenuIcon');
        if (icon) icon.innerHTML = mediaIcon(mediaView);
        mediaRenderPictures();
        return;
    }
    const action = event.target.closest('[data-media-action]');
    if (action) {
        mediaCloseMenus();
        const what = action.getAttribute('data-media-action');
        if (what === 'refresh') loadMediaGallery(true);
        else if (what === 'delete-all') deleteAllMedia();
        else if (what === 'search-open') mediaOpenSearch();
        else if (what === 'search-close') mediaCloseSearch();
        else if (what === 'filter-clear') mediaSetFilter('', null, true);
        return;
    }
    const bulk = event.target.closest('[data-media-bulk]');
    if (bulk) {
        const what = bulk.getAttribute('data-media-bulk');
        if (what === 'clear') { mediaSelection = new Set(); mediaRenderPictures(); }
        else if (what === 'download') mediaDownloadSelected();
        else if (what === 'send') mediaSendSelected();
        else if (what === 'delete') mediaDeleteSelected();
        return;
    }
    const open = event.target.closest('[data-media-open]');
    if (open) {
        const holder = open.closest('[data-media-filename]');
        if (holder && mediaViews().tapAction(mediaSelection, mediaIsTouchEvent(event)) === 'toggle') {
            mediaSelection = mediaViews().toggle(mediaSelection, holder.getAttribute('data-media-filename'));
            mediaUpdateSelectionUi();
            return;
        }
        mediaOpenLightbox(Number(open.getAttribute('data-media-open')), open);
        return;
    }
    const del = event.target.closest('[data-media-delete]');
    if (del) {
        event.preventDefault();
        deleteMediaItem(del.getAttribute('data-media-delete'), event);
    }
}

// a tap on a phone or from a touch screen; a mouse keeps using the checkboxes
function mediaIsTouchEvent(event) {
    return event.pointerType === 'touch' || (event.pointerType !== 'mouse' && mediaIsPhone());
}

// ---- long press selects (touch) --------------------------------------------------------------------------------------

function mediaWireLongPress(content) {
    const press = mediaViews().createLongPress({
        onFire: (holder) => {
            if (!holder) return;
            const name = holder.getAttribute('data-media-filename');
            if (!mediaSelection.has(name)) mediaSelection = mediaViews().toggle(mediaSelection, name);
            mediaSwallowClick = true;
            if (navigator.vibrate) { try { navigator.vibrate(12); } catch (error) { /* not allowed */ } }
            mediaUpdateSelectionUi();
        },
    });
    content.addEventListener('pointerdown', (event) => {
        if (event.pointerType !== 'touch') return;
        const holder = event.target.closest('#mediaPictures [data-media-filename]');
        if (!holder || event.target.closest('.media-check, .media-item-actions')) return;
        press.start(event.clientX, event.clientY, holder);
    });
    content.addEventListener('pointermove', (event) => press.move(event.clientX, event.clientY));
    // the click that follows the lifted finger is swallowed once; a browser that sends none must not swallow a later tap
    ['pointerup', 'pointercancel', 'pointerleave'].forEach((type) => content.addEventListener(type, () => {
        if (press.end()) setTimeout(() => { mediaSwallowClick = false; }, 350);
    }));
    content.addEventListener('scroll', () => press.cancel(), true);
    // no callout / image menu on a long press of a picture (Android shows one for the image under the finger)
    content.addEventListener('contextmenu', (event) => {
        if (event.target.closest && event.target.closest('#mediaPictures .media-preview')) event.preventDefault();
    });
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
    mediaShown = mediaViews().filterItems(mediaViews().sortItems(mediaItems, mediaSort), mediaFilter);
    // a picture the filter hides cannot stay selected: bulk actions only ever act on what is shown
    mediaSelection = mediaViews().prune(mediaSelection, mediaShown);
    host.className = 'media-pictures media-view-' + mediaView;
    host.innerHTML = mediaShown.length || !mediaItems.length
        ? mediaShown.map((item, index) => renderMediaItem(item, index)).join('')
        : `<div class="media-filter-empty" role="status">
               <div class="media-empty-title">${mediaEscapeHtml(mediaT('media.filter_empty_title'))}</div>
               <div class="media-empty-text">${mediaEscapeHtml(mediaT('media.filter_empty_text'))}</div>
               <button type="button" class="media-primary-btn" data-media-action="filter-clear">${mediaEscapeHtml(mediaT('media.filter_empty_clear'))}</button>
           </div>`;
    mediaUpdateSelectionUi();
}

// ---- filter (G4) ---------------------------------------------------------------------------------------------------

function mediaToolbarIsCompact() {
    const bar = document.getElementById('mediaToolbar');
    return Boolean(bar && bar.classList.contains('is-compact'));
}

// Which of the three bars shows: selecting wins; on a phone or narrow column the opened search bar replaces the normal
// bar (same height, nothing below moves); a wide toolbar has the field inline and never shows the search bar.
function mediaApplyToolbarMode() {
    const toolbar = document.getElementById('mediaToolbar');
    if (!toolbar) return;
    const selecting = mediaViews().selectionMode(mediaSelection);
    const searching = !selecting && mediaSearchOpen && mediaToolbarIsCompact();
    toolbar.setAttribute('data-mode', selecting ? 'selection' : searching ? 'search' : 'normal');
    const normal = document.getElementById('mediaTbNormal');
    const search = document.getElementById('mediaTbSearch');
    const selection = document.getElementById('mediaTbSelection');
    if (normal) normal.hidden = selecting || searching;
    if (search) search.hidden = !searching;
    if (selection) selection.hidden = !selecting;
}

function mediaSyncFilterFields(except) {
    document.querySelectorAll('.media-filter-input').forEach((input) => {
        if (input !== except && input.value !== mediaFilter) input.value = mediaFilter;
    });
    document.querySelectorAll('.media-filter-clear').forEach((button) => { button.hidden = !mediaFilter; });
    const searchBtn = document.getElementById('mediaSearchBtn');
    if (searchBtn) searchBtn.classList.toggle('is-active', Boolean(mediaViews().normalizeFilter(mediaFilter)));
}

// Live narrowing as you type. The field keeps the text exactly as typed; matching and saving use the normalized form.
function mediaSetFilter(value, source, focusAfter) {
    mediaFilter = String(value || '').slice(0, mediaViews().FILTER_MAX);
    mediaSyncFilterFields(source);
    mediaRenderPictures();
    if (mediaFilterSaveTimer) clearTimeout(mediaFilterSaveTimer);
    mediaFilterSaveTimer = setTimeout(() => { mediaFilterSaveTimer = null; mediaSavePrefs(); }, 600);
    if (focusAfter) {
        const input = document.querySelector(mediaToolbarIsCompact() && mediaSearchOpen ? '#mediaFilterInputPhone' : '#mediaFilterInput');
        if (input) input.focus();
    }
}

function mediaOpenSearch() {
    mediaSearchOpen = true;
    mediaApplyToolbarMode();
    const input = document.getElementById('mediaFilterInputPhone');
    if (input) input.focus();
}

function mediaCloseSearch() {
    mediaSearchOpen = false;
    mediaApplyToolbarMode();
    const button = document.getElementById('mediaSearchBtn');
    if (button) button.focus();
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
            <img src="${mediaEscapeHtml(thumb)}" alt="${mediaEscapeHtml(displayName)}" loading="lazy" decoding="async" draggable="false"
                 onerror="this.closest('.media-preview').classList.add('media-preview-error'); this.style.display='none';">
            <span class="media-preview-error-text">${mediaEscapeHtml(mediaT('media.image_unavailable'))}</span>
        </button>`;
    const actions = `
        <div class="media-item-actions">
            <a class="media-icon-btn" href="${mediaEscapeHtml(url)}" download="${mediaEscapeHtml(displayName)}" title="${mediaEscapeHtml(mediaT('media.download'))}" aria-label="${mediaEscapeHtml(mediaT('media.download_image_aria'))}">${mediaIcon('download')}</a>
            <button type="button" class="media-icon-btn media-delete-btn" data-media-delete="${nameAttr}" title="${mediaEscapeHtml(mediaT('common.delete'))}" aria-label="${mediaEscapeHtml(mediaT('media.delete_image_aria'))}">${mediaIcon('trash')}</button>
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

// The ONE toolbar switches state (same height, nothing below moves): normal or selection. The checkboxes, the
// select-all boxes and the size note follow the selection.
function mediaUpdateSelectionUi() {
    const state = mediaViews().bulkState(mediaSelection, mediaShown);
    const selecting = mediaViews().selectionMode(mediaSelection);
    document.querySelectorAll('#mediaPictures .media-check').forEach((box) => {
        box.checked = mediaSelection.has(box.getAttribute('data-media-name'));
    });
    document.querySelectorAll('#mediaPictures [data-media-filename]').forEach((card) => {
        card.classList.toggle('is-selected', mediaSelection.has(card.getAttribute('data-media-filename')));
    });
    const pictures = document.getElementById('mediaPictures');
    if (pictures) pictures.classList.toggle('is-selecting', selecting);
    document.querySelectorAll('[data-media-selectall]').forEach((all) => {
        all.checked = mediaViews().allSelected(mediaSelection, mediaShown);
        all.indeterminate = state.any && !all.checked;
    });
    mediaApplyToolbarMode();
    if (selecting) mediaCloseMenus();
    mediaRenderSummary();
    const summary = document.getElementById('mediaBulkSummary');
    if (summary) {
        const size = mediaViews().formatBytes(state.bytes);
        summary.innerHTML = `<span class="media-sum-full">${mediaEscapeHtml(window.I18N.plural('media.selected_summary', state.count, { count: state.count, size }))}</span>`
            + `<span class="media-sum-short">${mediaEscapeHtml(`${state.count} · ${size}`)}</span>`;
    }
    const download = document.getElementById('mediaBulkDownload');
    if (download) download.disabled = !state.canDownload;
    const send = document.getElementById('mediaBulkSend');
    if (send) send.disabled = !state.canSend;
    const note = document.getElementById('mediaZipNote');
    if (note) {
        // said right under the toolbar, before the tap: why the download is off and what to do
        note.hidden = !(state.any && (state.zipTooLarge || state.tooMany || state.sendTooMany));
        note.textContent = state.tooMany
            ? mediaT('media.bulk_too_many', { max: mediaViews().BULK_MAX })
            : state.zipTooLarge
                ? mediaT('media.zip_limit_message', { max: mediaViews().formatBytes(state.zipMaxBytes), size: mediaViews().formatBytes(state.bytes) })
                : mediaT('media.send_too_many', { max: state.sendMax });
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

// G2: the selected pictures go through the Files send dialog (contact, relay, expiry), one MCAttach transfer each
function mediaOpenSend(pictures) {
    if (!pictures.length) return;
    const files = window.MeshCenterFiles;
    if (!files || typeof files.openSendPictures !== 'function') {
        if (typeof showToast === 'function') showToast(`❌ ${mediaT('media.send_unavailable')}`, 'error', { duration: 10000 });
        return;
    }
    files.openSendPictures(pictures, () => {
        mediaSelection = new Set();
        mediaUpdateSelectionUi();
    });
}

function mediaSendSelected() {
    const state = mediaViews().bulkState(mediaSelection, mediaShown);
    if (!state.canSend) return;
    mediaOpenSend(mediaViews().sendItems(mediaSelection, mediaShown));
}

function mediaSendFromLightbox() {
    const item = mediaShown[mediaLightboxIndex];
    if (!item) return;
    mediaCloseLightbox();   // the send dialog opens above the page, not behind the viewer
    mediaOpenSend([{ filename: item.filename, display_name: item.display_name || '', size: Number(item.size) || 0 }]);
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
        <button type="button" class="media-lb-btn media-lb-close" data-lb="close" aria-label="${mediaEscapeHtml(mediaT('media.lightbox_close'))}">${mediaIcon('close')}</button>
        <button type="button" class="media-lb-btn media-lb-prev" data-lb="prev" aria-label="${mediaEscapeHtml(mediaT('media.lightbox_prev'))}">‹</button>
        <button type="button" class="media-lb-btn media-lb-next" data-lb="next" aria-label="${mediaEscapeHtml(mediaT('media.lightbox_next'))}">›</button>
        <div class="media-lb-stage"><img class="media-lb-img" alt=""></div>
        <div class="media-lb-caption">
            <div class="media-lb-text"><strong id="mediaLbName"></strong><span id="mediaLbMeta"></span><span id="mediaLbFacts"></span></div>
            <div class="media-lb-actions">
                <button type="button" class="media-lb-link" data-lb="send">${mediaIcon('send')}<span>${mediaEscapeHtml(mediaT('media.send'))}</span></button>
                <a class="media-lb-link" id="mediaLbDownload" download>${mediaIcon('download')}<span>${mediaEscapeHtml(mediaT('media.download'))}</span></a>
                <button type="button" class="media-lb-link media-lb-danger" data-lb="delete">${mediaIcon('trash')}<span>${mediaEscapeHtml(mediaT('common.delete'))}</span></button>
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
            else if (what === 'send') mediaSendFromLightbox();
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
    // the arrows sit above the caption: its height (it wraps) goes to the CSS
    const caption = box.querySelector('.media-lb-caption');
    const syncCaptionHeight = () => box.style.setProperty('--lb-caption-h', caption.offsetHeight + 'px');
    if (typeof ResizeObserver === 'function') new ResizeObserver(syncCaptionHeight).observe(caption);
    window.addEventListener('resize', syncCaptionHeight);
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
