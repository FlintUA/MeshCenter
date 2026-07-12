// ============================================================
// ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
// ============================================================
let currentChatId = null;
let currentChatName = null;
let currentChatType = null;
let lastMessagesSignature = '';
let nodeSearchTerm = '';
let directMessageTarget = null;
let chatListCache = [];              // <-- ЭТО ОБЯЗАТЕЛЬНО!
let messagePollInterval = null;
let showIgnored = false;
let showFavorites = false;
let nodeCache = [];
let deleteTargetChatId = null;
let clearTargetChatId = null;
let totalUnreadCount = 0;
let showDuplicatesOnly = false;
let currentMainTab = 'chats';
let cameraActive = false;
let isInitialized = false;
let contextChatMode = false;
let contextBaseTab = null;
let radioHealthTimer = null;

// ===== TELEMETRY =====
let telemetryData = {
    temperature: null,
    humidity: null,
    pressure: null,
    voltage: null,
    current: null,
    last_update: null
};
let telemetryHistory = [];
let telemetryChart = null;
let telemetryInterval = 900;
let telemetryUpdateInterval = null;
let telemetryTimeRange = 60;
let telemetryFullHistory = [];
let appSettings = {
    units: {
        temperature: "c",
        pressure: "hpa",
        wind: "ms"
    }
};

const SENSOR_COLORS = {
    temperature: '#ef4444',
    humidity: '#3b82f6',
    pressure: '#facc15',
    voltage: '#22c55e',
    current: '#38bdf8',
    power: '#f97316'
};

const SENSOR_BG_COLORS = {
    temperature: 'rgba(239, 68, 68, 0.10)',
    humidity: 'rgba(59, 130, 246, 0.10)',
    pressure: 'rgba(250, 204, 21, 0.14)',
    voltage: 'rgba(34, 197, 94, 0.10)',
    current: 'rgba(56, 189, 248, 0.10)',
    power: 'rgba(249, 115, 22, 0.10)'
};

let telemetryVisibleSeries = {
    environment: {
        temperature: true,
        humidity: true,
        pressure: true
    },
    power: {
        voltage: true,
        current: true,
        power: true
    }
};

function celsiusToFahrenheit(c) {
    return (c * 9 / 5) + 32;
}

function formatTemperature(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return "--";
    }

    const unit = appSettings?.units?.temperature || "c";
    const c = Number(value);

    if (unit === "f") {
        return celsiusToFahrenheit(c).toFixed(1) + "°F";
    }

    if (unit === "both") {
        return c.toFixed(1) + "°C / " + celsiusToFahrenheit(c).toFixed(1) + "°F";
    }

    return c.toFixed(1) + "°C";
}

function temperatureChartUnit() {
    const unit = appSettings?.units?.temperature || "c";
    return unit === "f" ? "°F" : "°C";
}

function temperatureChartValue(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return null;
    }

    const unit = appSettings?.units?.temperature || "c";
    const c = Number(value);

    if (unit === "f") {
        return celsiusToFahrenheit(c);
    }

    return c;
}

function hPaToMmHg(hpa) {
    return hpa * 0.750061683;
}

function formatPressure(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return "--";
    }

    const unit = appSettings?.units?.pressure || "hpa";
    const hpa = Number(value);
    const mmhg = hPaToMmHg(hpa);

    if (unit === "mmhg") {
        return mmhg.toFixed(1) + " mmHg";
    }

    if (unit === "both") {
        return hpa.toFixed(1) + " hPa / " + mmhg.toFixed(1) + " mmHg";
    }

    return hpa.toFixed(1) + " hPa";
}

function pressureChartUnit() {
    const unit = appSettings?.units?.pressure || "hpa";
    return unit === "mmhg" ? "mmHg" : "hPa";
}

function pressureChartValue(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return null;
    }

    const unit = appSettings?.units?.pressure || "hpa";
    const hpa = Number(value);

    if (unit === "mmhg") {
        return hPaToMmHg(hpa);
    }

    return hpa;
}

async function loadSettings() {
    try {
        const response = await fetch("/api/settings");
        const data = await response.json();

        if (data.ok && data.settings) {
            appSettings = data.settings;
            console.log("[SETTINGS] Loaded:", appSettings);
        }
    } catch (error) {
        console.warn("[SETTINGS] Failed to load:", error);
    }

    updateSettingsUi();
}

function updateSettingsUi() {
    const units = appSettings?.units || {};

    document.getElementById('unitTempC')?.classList.toggle('active', units.temperature === 'c');
    document.getElementById('unitTempF')?.classList.toggle('active', units.temperature === 'f');

    document.getElementById('unitPressureHpa')?.classList.toggle('active', units.pressure === 'hpa');
    document.getElementById('unitPressureMmhg')?.classList.toggle('active', units.pressure === 'mmhg');

    const recovery = appSettings?.listener_autorecovery || {};

    const enabled = !!recovery.enabled;
    const delay = recovery.delay || 60;

    const checkbox = document.getElementById("listenerRecoveryEnabled");
    const select = document.getElementById("listenerRecoveryDelay");

    if (checkbox)
        checkbox.checked = enabled;

    if (select) {
        select.value = delay;
        select.disabled = !enabled;
    }
}

async function setUnitSetting(name, value) {
    const units = {
        ...(appSettings?.units || {}),
        [name]: value
    };

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ units })
        });

        const data = await response.json();

        if (!data.ok) {
            alert('Unable to save settings: ' + (data.error || 'Unknown error'));
            return;
        }

        appSettings = data.settings;
        updateSettingsUi();

        if (typeof loadSensors === 'function') loadSensors();
        if (typeof loadTelemetry === 'function') loadTelemetry();

        const modal = document.getElementById('telemetryModal');
        if (modal && modal.style.display !== 'none') {
            const type = modal.dataset.type || 'environment';
            renderTelemetryWithRange(type, telemetryTimeRange);
        }

    } catch (error) {
        alert('Unable to save settings: ' + error.message);
    }
}

async function updateListenerRecoverySettings() {

    const enabled =
        document.getElementById("listenerRecoveryEnabled").checked;

    const delay =
        parseInt(
            document.getElementById("listenerRecoveryDelay").value
        );

    document.getElementById("listenerRecoveryDelay").disabled =
        !enabled;

    const listener_autorecovery = {
        enabled,
        delay
    };

    try {

        const response = await fetch("/api/settings", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

            body: JSON.stringify({
                listener_autorecovery
            })

        });

        const data = await response.json();

        if (!data.ok) {

            showToast("Unable to save settings", "error");
            return;

        }

        appSettings = data.settings;

        updateSettingsUi();

        showToast(
            "Listener Auto Recovery updated",
            "success"
        );

    }

    catch (e) {

        showToast(e.message, "error");

    }

}

// ===== PHOTO =====
let photoPreviewResolution = '640x480';
let photoSaveResolution = '3280x2464';
let currentPhotoQuality = 85;
let currentPhotoData = null;

// ===== MESSAGE CACHE =====
let messageCache = {};
let currentLoadRequest = null;
const CACHE_TTL = 30000;

// ===== RENDER SIGNATURES =====
let lastRenderedSignature = {};

// ============================================================
// TOAST FUNCTION
// ============================================================
function showToast(message, type = 'info') {
    const oldToast = document.getElementById('toast');
    if (oldToast) oldToast.remove();
    
    const toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    
    setTimeout(() => toast.classList.add('show'), 10);
    
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============================================================
// UTILITY FUNCTIONS
// ============================================================
document.addEventListener('DOMContentLoaded', async function() {
    await loadSettings();

    const title = document.getElementById('appTitle');
    if (title) {
        title.addEventListener('click', function(e) {
            const icon = this.querySelector('span');
            if (icon) {
                icon.style.display = 'inline-block';
                icon.style.transition = 'transform 0.4s ease';
                icon.style.transform = 'rotate(720deg) scale(1.3)';
                setTimeout(() => {
                    icon.style.transform = 'rotate(0deg) scale(1)';
                }, 400);
            }

            this.innerHTML = '🔄 Reloading...';
            this.style.opacity = '0.6';
            this.style.cursor = 'default';

            setTimeout(() => {
                window.location.reload(true);
            }, 500);
        });
    }

    const headerStatus = document.getElementById('headerStatusText');

    if (headerStatus) {
        headerStatus.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();

            switchMainTab('system');

            setTimeout(() => {
                document.querySelector('.radio-health-card')?.scrollIntoView({
                    behavior: 'smooth',
                    block: 'start'
                });
            }, 100);
        });
    }

});

function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(value);
    return div.innerHTML;
}

function formatTime(timeStr) {
    if (!timeStr) return '';
    return timeStr;
}

function truncateText(text, maxLen) {
    if (!text) return '';
    if (text.length <= maxLen) return text;
    return text.substring(0, maxLen) + '...';
}

function toggleShowIgnored() {
    const checkbox = document.getElementById('showIgnoredToggle');
    showIgnored = checkbox ? checkbox.checked : false;
    localStorage.setItem('mesh_show_ignored', showIgnored);
    loadMessages();
}

function toggleShowFavorites() {
    const checkbox = document.getElementById('showFavoritesToggle');
    showFavorites = checkbox ? checkbox.checked : false;
    localStorage.setItem('mesh_show_favorites', showFavorites);
    loadMessages();
}

// ============================================================
// RENDER CHAT ITEM
// ============================================================
function renderChatItem(chat) {
    const icon = chat.is_channel ? '📡' : '👤';
    const iconClass = chat.is_channel ? 'channel' : 'dm';
    const lastMsg = chat.last_message || 'No messages yet';
    const time = chat.last_time || '';
    const ignored = chat.ignored ? '🚫 ' : '';
    const favorite = chat.favorite ? '⭐ ' : '';
    const unreadBadge = (chat.unread || 0) > 0 ? `<span class="chat-unread-badge">${chat.unread}</span>` : '';
    const hasUnread = (chat.unread || 0) > 0 ? 'has-unread' : '';

    let lastMsgDisplay = '';
    
    if (chat.is_channel) {
        if (chat.last_sender && lastMsg) {
            lastMsgDisplay = `<span class="chat-last-sender">${escapeHtml(chat.last_sender)}</span> <span class="chat-last-text">${escapeHtml(truncateText(lastMsg, 50))}</span>`;
        } else {
            lastMsgDisplay = `<span class="chat-last-text">${escapeHtml(truncateText(lastMsg, 60))}</span>`;
        }
    } else {
        lastMsgDisplay = `<span class="chat-last-text">${escapeHtml(truncateText(lastMsg, 60))}</span>`;
    }

    return `
        <div class="chat-item ${hasUnread}" onclick="openChat('${escapeHtml(chat.id)}', '${escapeHtml(chat.name)}', '${escapeHtml(chat.type)}')">
            <div class="chat-icon ${iconClass}">${icon}</div>
            <div class="chat-info">
                <div class="chat-name">${ignored}${favorite}${escapeHtml(chat.name)}</div>
                <div class="chat-last-msg">${lastMsgDisplay}</div>
            </div>
            <div class="chat-meta">
                <div class="chat-time">${escapeHtml(time)}</div>
                ${unreadBadge}
            </div>
        </div>
    `;
}

// ============================================================
// LOAD CHAT LIST
// ============================================================
async function loadChatList() {
    console.log('[CHAT] loadChatList called');
    
    // Скрываем индикатор загрузки
    const initialLoading = document.getElementById('initialLoading');
    if (initialLoading) {
        initialLoading.style.display = 'none';
        console.log('[CHAT] Hidden loading indicator');
    }
    
    try {
        console.log('[CHAT] Fetching /api/chats...');
        
        const controller = new AbortController();
        const timeoutId = setTimeout(() => {
            console.log('[CHAT] Request timeout, aborting...');
            controller.abort();
        }, 10000);
        
        const response = await fetch('/api/chats', {
            signal: controller.signal,
            headers: { 'Cache-Control': 'no-cache' }
        });
        clearTimeout(timeoutId);
        
        console.log('[CHAT] Response status:', response.status);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const data = await response.json();
        console.log('[CHAT] Received data:', data.chats ? data.chats.length : 0, 'chats');
        
        chatListCache = data.chats || [];
        totalUnreadCount = data.total_unread || 0;

        const container = document.getElementById('chatList');
        if (!container) {
            console.error('[CHAT] Container #chatList not found');
            return;
        }

        if (!currentChatId) {
            const chatTitle = document.getElementById('chatTitle');
            if (chatTitle) {
                if (totalUnreadCount > 0) {
                    chatTitle.textContent = `💬 Chats (${totalUnreadCount})`;
                } else {
                    chatTitle.textContent = '💬 Chats';
                }
            }
            const subtitleEl = document.getElementById('chatSubtitle');
            if (subtitleEl) subtitleEl.textContent = '';
        }

        if (chatListCache.length === 0) {
            container.innerHTML = '<div class="loading">💬 No chats yet</div>';
            console.log('[CHAT] No chats found');
            return;
        }

        const channelChat = chatListCache.find(c => c.is_channel);
        const dmChats = chatListCache.filter(c => !c.is_channel);

        let html = '';

        if (channelChat) {
            html += renderChatItem(channelChat);
        }

        if (dmChats.length > 0) {
            html += `<div class="chat-section-title">💬 Direct Messages</div>`;
            html += dmChats.map(chat => renderChatItem(chat)).join('');
        } else if (!channelChat) {
            html = '<div class="loading">💬 No chats yet</div>';
        }

        container.innerHTML = html;
        console.log('[CHAT] Chat list rendered successfully');

    } catch (error) {
        console.error('[CHAT] Error:', error);
        const container = document.getElementById('chatList');
        
        if (container) {
            if (error.name === 'AbortError') {
                container.innerHTML = `
                    <div class="loading" style="color:#ff9800;">
                        ⏳ Request timeout - retrying...<br>
                        <button onclick="loadChatList()" style="margin-top:8px;padding:4px 12px;border:none;border-radius:4px;background:#1a73e8;color:white;cursor:pointer;">
                            🔄 Retry
                        </button>
                    </div>
                `;
                setTimeout(() => loadChatList(), 3000);
            } else {
                container.innerHTML = `
                    <div class="loading" style="color:#c62828;">
                        ⚠️ Error loading chats<br>
                        <small style="font-size:12px;color:#999;">${error.message}</small>
                        <br><br>
                        <button onclick="loadChatList()" style="padding:6px 16px;border:none;border-radius:6px;background:#1a73e8;color:white;cursor:pointer;">
                            🔄 Retry
                        </button>
                    </div>
                `;
            }
        }
    }
}

// ============================================================
// LOAD MESSAGES
// ============================================================
async function loadMessages() {
    try {
        console.log('[MESSAGES] Loading messages...');
        
        const controller = new AbortController();
        const timeoutId = setTimeout(() => {
            console.log('[MESSAGES] Request timeout, aborting...');
            controller.abort();
        }, 8000);
        
        const response = await fetch('/api/messages', {
            signal: controller.signal,
            headers: { 'Cache-Control': 'no-cache' }
        });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const data = await response.json();
        console.log('[MESSAGES] Received', data.nodes ? data.nodes.length : 0, 'nodes');

        nodeCache = data.nodes || [];
        
        if (currentChatId && currentChatType === 'dm') {
            updateChatHeaderStatus();
        }

        const statusEl = document.getElementById('statusText');
        const nodeCountEl = document.getElementById('nodeCount');

        if (statusEl && statusEl.innerHTML !== '🔴 Error loading - refresh page') {
            statusEl.innerHTML = '🟢 Mesh online';
        }
        
        const allNodes = data.nodes || [];
        const ignoredNodes = allNodes.filter(n => n.ignored);
        const favoriteNodes = allNodes.filter(n => n.favorite);
        
        const ignoredCountEl = document.getElementById('ignoredCount');
        if (ignoredCountEl) {
            ignoredCountEl.textContent = ignoredNodes.length + ' ignored';
        }
        
        const favoritesCountEl = document.getElementById('favoritesCount');
        if (favoritesCountEl) {
            favoritesCountEl.textContent = favoriteNodes.length + ' favorites';
        }
        
        let displayNodes = [];
        
        if (showFavorites && showIgnored) {
            displayNodes = allNodes.filter(n => n.favorite && n.ignored);
        } else if (showFavorites) {
            displayNodes = allNodes.filter(n => n.favorite && !n.ignored);
        } else if (showIgnored) {
            displayNodes = allNodes.filter(n => n.ignored);
        } else {
            displayNodes = allNodes.filter(n => !n.ignored);
        }
        
        if (nodeCountEl) {
            const totalDisplay = displayNodes.length;
            nodeCountEl.innerHTML = '🖥️ Nodes [' + totalDisplay + ']';
        }

        const nodesList = document.getElementById('nodesList');
        if (!nodesList) return;

        let filteredNodes = displayNodes;
        if (nodeSearchTerm) {
            filteredNodes = filteredNodes.filter(node =>
                node.clean_name.toLowerCase().includes(nodeSearchTerm.toLowerCase()) ||
                node.node_id.toLowerCase().includes(nodeSearchTerm.toLowerCase())
            );
        }

        if (filteredNodes.length === 0) {
            let message = '🔍 No nodes found';
            if (showFavorites && showIgnored) {
                message = '⭐ No favorite ignored nodes found';
            } else if (showFavorites) {
                message = '⭐ No favorite nodes found';
            } else if (showIgnored) {
                message = '🚫 No ignored nodes found';
            }
            nodesList.innerHTML = `<div class="loading" style="padding: 16px;">${message}</div>`;
        } else {
            nodesList.innerHTML = filteredNodes.map(node => {
                const badgeClass = signalBadgeClass(node.signal_quality);
                const badgeText = signalBadgeText(node.signal_quality);
                const isIgnored = node.ignored || false;
                const isFavorite = node.favorite || false;
                const cardClass = isIgnored ? 'node-card ignored' : (isFavorite ? 'node-card favorite' : 'node-card');
                const lastText = node.last_text ? 
                    `<div class="node-last-text">📝 ${escapeHtml(truncateText(node.last_text, 60))}</div>` : '';
                const ignoreStatus = isIgnored ? '🚫 ' : '';
                const favoriteStatus = isFavorite ? '⭐ ' : '';

                const unignoreBtn = isIgnored ? 
                    `<button class="unignore-btn-mini" onclick="event.stopPropagation(); toggleIgnore('${escapeHtml(node.node_id)}')">Unignore</button>` : '';

                return `
                    <div class="${cardClass}" onclick="selectNode('${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name)}')">
                        <div class="node-name" style="display:flex;align-items:center;gap:4px;">
                            ${ignoreStatus}${favoriteStatus}${escapeHtml(node.name)}
                            <span class="node-inline-id">[${escapeHtml(node.node_id)}]</span>
                            <span class="badge ${badgeClass}">${badgeText}</span>
                            ${unignoreBtn}
                        </div>
                        <div class="node-meta">${escapeHtml(node.meta)}</div>
                        ${lastText}
                    </div>
                `;
            }).join('');
        }

        const selectedNode = allNodes.find(n => n.node_id === currentChatId);
        if (selectedNode) {
            renderNodeDetails(selectedNode);
        } else {
            renderNodeDetails(null);
        }

    } catch (error) {
        console.error('[MESSAGES] Error:', error);
        const statusEl = document.getElementById('statusText');
        if (statusEl && error.name !== 'AbortError') {
            statusEl.innerHTML = '🔴 Connection error';
        }
        if (error.name !== 'AbortError') {
            setTimeout(() => loadMessages(), 5000);
        }
    }
}

function signalBadgeClass(signalQuality) {
    if (signalQuality === 'good') return 'badge-online';
    if (signalQuality === 'medium') return 'badge-medium';
    return 'badge-offline';
}

function signalBadgeText(signalQuality) {
    if (signalQuality === 'good') return '●';
    if (signalQuality === 'medium') return '○';
    return '○';
}

function checkNodeIgnored(nodeId) {
    try {
        return fetch(`/api/node_status?node_id=${encodeURIComponent(nodeId)}`)
            .then(response => response.json())
            .then(data => data.ignored || false)
            .catch(() => false);
    } catch (error) {
        console.error('Error checking ignore status:', error);
        return false;
    }
}

function showIgnoredBanner(nodeId, nodeName) {
    hideIgnoredBanner();
    
    const container = document.getElementById('messagesContainer');
    if (!container) return;
    
    const banner = document.createElement('div');
    banner.id = 'ignoreBanner';
    banner.className = 'ignore-banner';
    banner.innerHTML = `
        <div class="ignore-banner-content">
            <span>🚫 Node "${escapeHtml(nodeName)}" is ignored</span>
            <button class="unignore-btn" onclick="toggleIgnore('${escapeHtml(nodeId)}')">
                Unignore
            </button>
        </div>
    `;
    
    container.prepend(banner);
}

function hideIgnoredBanner() {
    const banner = document.getElementById('ignoreBanner');
    if (banner) banner.remove();
}

function updateChatHeaderStatus() {
    if (!currentChatId || currentChatType === 'channel') return;
    
    const node = nodeCache.find(n => n.node_id === currentChatId);
    const titleEl = document.getElementById('chatTitle');
    const subtitleEl = document.getElementById('chatSubtitle');
    
    if (!titleEl || !subtitleEl) return;
    
    let statusIcon = '🟢';
    let statusText = 'Online';
    
    if (node && node.age) {
        const age = node.age;
        if (age.includes('h') || age.includes('day') || (age.includes('min') && parseInt(age) > 10)) {
            statusIcon = '🟡';
            statusText = 'Away';
        }
        if (age.includes('day') || (age.includes('h') && parseInt(age) > 24)) {
            statusIcon = '🔴';
            statusText = 'Radio Offline';
        }
    }
    
    const shortId = currentChatId ? currentChatId.slice(-4) : '';
    titleEl.innerHTML = `${statusIcon} ${currentChatName} <span style="font-size:12px;font-weight:400;color:#888;margin-left:6px;">${shortId}</span>`;
    subtitleEl.textContent = `Direct Message • ${statusText}`;
    subtitleEl.style.color = statusIcon === '🟢' ? '#2e7d32' : (statusIcon === '🟡' ? '#f57c00' : '#c62828');
}

function openChat(chatId, chatName, chatType) {
    currentChatId = chatId;
    currentChatName = chatName || chatId;
    currentChatType = chatType || 'dm';

    if (currentMainTab && currentMainTab !== 'chats') {
        contextChatMode = true;
        contextBaseTab = currentMainTab;
        document.body.classList.add('context-chat-mode');
    } else {
        contextChatMode = false;
        contextBaseTab = null;
        document.body.classList.remove('context-chat-mode');
    }

    document.getElementById('chatListContainer').style.display = 'none';
    document.getElementById('messagesView').style.display = 'flex';


    document.getElementById('chatHeader').style.display = 'flex';
    document.getElementById('backToChatsBtn').style.display = 'block';
    document.getElementById('chatActionsBtn').style.display = 'block';
    
    document.getElementById('deleteAllDmHeaderBtn').style.display = 'none';

    const titleEl = document.getElementById('chatTitle');
    const subtitleEl = document.getElementById('chatSubtitle');
    
    if (chatType === 'channel') {
        titleEl.textContent = '📡 ' + chatName;
        subtitleEl.textContent = 'Channel • All messages are broadcast';
        subtitleEl.style.color = '#1a73e8';
    } else {
        updateChatHeaderStatus();
    }

    const input = document.getElementById('messageInput');
    if (input) {
        input.placeholder = chatType === 'channel' ? 'Type a message to channel...' : `Message ${chatName}...`;
        input.value = '';
        input.focus();
    }

    directMessageTarget = null;
    document.querySelectorAll('.node-title-btn').forEach(btn => {
        btn.style.background = 'linear-gradient(135deg, #4a5a7a 0%, #3a4a6a 100%)';
        btn.style.boxShadow = 'none';
    });

    if (chatType === 'dm' && chatId !== 'channel') {
        checkNodeIgnored(chatId).then(isIgnored => {
            if (isIgnored) {
                showIgnoredBanner(chatId, chatName);
            } else {
                hideIgnoredBanner();
            }
        });
    } else {
        hideIgnoredBanner();
    }

    const container = document.getElementById('messagesContainer');
    if (container) {
        container.innerHTML = '<div class="loading">⏳ Loading messages...</div>';
        container.scrollTop = 0;
    }

    lastRenderedSignature[chatId] = null;
    lastMessagesSignature = '';
    loadChatMessages(chatId);
    startMessagePolling(chatId);
    
    if (chatType === 'dm' && chatId !== 'channel') {
        updateNodeDetails(chatId);
    } else {
        renderNodeDetails(null);
    }
    
    loadChatList();
}

function showChatList() {
    currentChatId = null;
    currentChatName = null;
    currentChatType = null;

    const chatHeader = document.getElementById('chatHeader');
    const chatListContainer = document.getElementById('chatListContainer');
    const messagesView = document.getElementById('messagesView');
    const backBtn = document.getElementById('backToChatsBtn');
    const actionsBtn = document.getElementById('chatActionsBtn');
    const deleteDmBtn = document.getElementById('deleteAllDmHeaderBtn');

    if (contextChatMode && contextBaseTab && contextBaseTab !== 'chats') {
        contextChatMode = false;
        contextBaseTab = null;
        document.body.classList.remove('context-chat-mode');

        if (chatHeader) chatHeader.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (backBtn) backBtn.style.display = 'none';
        if (actionsBtn) actionsBtn.style.display = 'none';
        if (deleteDmBtn) deleteDmBtn.style.display = 'none';

        stopMessagePolling();
        return;
    }

    if (chatHeader) chatHeader.style.display = 'none';
    if (chatListContainer) chatListContainer.style.display = 'block';
    if (messagesView) messagesView.style.display = 'none';
    if (backBtn) backBtn.style.display = 'none';
    if (actionsBtn) actionsBtn.style.display = 'none';
    if (deleteDmBtn) deleteDmBtn.style.display = 'none';

    stopMessagePolling();
    loadChatList();
}

function renderMessages(container, messages, chatId) {
    if (!container) return;
    
    const signature = messages.map(m => 
        [m.kind, m.sender, m.text, m.time].join('|')
    ).join('||');
    
    if (lastRenderedSignature[chatId] === signature) {
        console.log(`[RENDER] No changes for chat: ${chatId}, skipping render`);
        return;
    }
    
    lastRenderedSignature[chatId] = signature;
    
    if (messages.length === 0) {
        const chatName = currentChatName || chatId;
        container.innerHTML = `<div class="loading">💬 No messages yet with ${escapeHtml(chatName)}. Send the first one!</div>`;
    } else {
        container.innerHTML = messages.map(msg => {
            const isMe = msg.kind === 'me';
            const isSystem = msg.kind === 'system' || msg.sender === 'SYSTEM ERROR';
            const sender = escapeHtml(msg.sender || 'Unknown');
            const text = escapeHtml(msg.text || '');
            const time = escapeHtml(msg.time || '');

            if (isSystem) {
                return `
                    <div class="message system">
                        <div class="bubble">
                            <div class="text">${text}</div>
                            <div class="time">${time}</div>
                        </div>
                    </div>
                `;
            }

            return `
                <div class="message ${isMe ? 'me' : 'rx'}">
                    <div class="bubble">
                        <div class="sender">${sender}</div>
                        <div class="text">${text}</div>
                        <div class="time">${time}</div>
                    </div>
                </div>
            `;
        }).join('');
    }
    
    setTimeout(() => {
        container.scrollTop = container.scrollHeight;
    }, 50);
}

function invalidateCache(chatId) {
    if (messageCache[chatId]) {
        delete messageCache[chatId];
        console.log(`[CACHE] Invalidated cache for chat: ${chatId}`);
    }
    if (lastRenderedSignature[chatId]) {
        lastRenderedSignature[chatId] = null;
    }
}

async function loadChatMessages(chatId) {
    if (!chatId) return;

    const container = document.getElementById('messagesContainer');
    if (!container) return;
    
    if (currentLoadRequest) {
        currentLoadRequest = null;
    }
    
    const requestId = Date.now() + '_' + chatId;
    currentLoadRequest = requestId;
    
    if (messageCache[chatId]) {
        const cached = messageCache[chatId];
        const isFresh = (Date.now() - cached.timestamp) < CACHE_TTL;
        
        if (isFresh && currentChatId === chatId) {
            console.log(`[CACHE] Using cached messages for: ${chatId} (${cached.messages.length} messages)`);
            renderMessages(container, cached.messages, chatId);
            currentLoadRequest = null;
            return;
        }
    }

    try {
        const response = await fetch(`/api/messages?chat_id=${encodeURIComponent(chatId)}`);
        const data = await response.json();

        if (currentChatId !== chatId || currentLoadRequest !== requestId) {
            console.log(`[DEBUG] Chat changed or request cancelled: ${chatId} → ${currentChatId}`);
            currentLoadRequest = null;
            return;
        }

        const messages = data.messages || [];
        
        messageCache[chatId] = {
            messages: messages,
            timestamp: Date.now()
        };
        
        const keys = Object.keys(messageCache);
        if (keys.length > 20) {
            const sortedKeys = keys.sort((a, b) => {
                return (messageCache[a].timestamp || 0) - (messageCache[b].timestamp || 0);
            });
            delete messageCache[sortedKeys[0]];
            console.log(`[CACHE] Removed oldest entry: ${sortedKeys[0]}`);
        }

        renderMessages(container, messages, chatId);
        currentLoadRequest = null;

    } catch (error) {
        console.error('Error loading messages:', error);
        if (currentChatId === chatId && currentLoadRequest === requestId) {
            container.innerHTML = '<div class="loading">⚠️ Error loading messages</div>';
        }
        currentLoadRequest = null;
    }
}

let messagePollingInterval = null;

function startMessagePolling(chatId) {
    stopMessagePolling();
    messagePollingInterval = setInterval(() => {
        if (currentChatId === chatId) {
            loadChatMessages(chatId);
        }
    }, 5000);
}

function stopMessagePolling() {
    if (messagePollingInterval) {
        clearInterval(messagePollingInterval);
        messagePollingInterval = null;
    }
}

// ============================================================
// SEND FORM
// ============================================================
const sendForm = document.getElementById('sendForm');
if (sendForm) {
    sendForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const input = document.getElementById('messageInput');
        const text = input ? input.value.trim() : '';
        if (!text || !currentChatId) return;

        if (currentChatType === 'dm' && currentChatId !== 'channel') {
            const isIgnored = await checkNodeIgnored(currentChatId);
            if (isIgnored) {
                if (!confirm(`⚠️ Node "${currentChatName}" is ignored. Send message anyway?`)) {
                    return;
                }
            }
        }

        const button = e.target.querySelector('button[type="submit"]');
        const originalHtml = button ? button.innerHTML : 'Send';

        if (button) {
            button.disabled = true;
            const currentWidth = button.offsetWidth;
            button.style.minWidth = '100px';
            button.style.width = currentWidth + 'px';
            button.classList.add('sending');
            button.innerHTML = `<span style="display:inline-block;min-width:80px;text-align:left;">Sending<span class="dots"></span></span>`;
            button.style.animation = 'pulse 1s ease-in-out infinite';
        }

        try {
            const payload = {
                text: text,
                chat_id: currentChatId
            };

            if (currentChatType === 'dm') {
                payload.target_node = currentChatId;
            }

            const response = await fetch('/api/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });

            if (response.ok) {
                if (input) input.value = '';
                
                invalidateCache(currentChatId);
                lastMessagesSignature = '';
                await loadChatMessages(currentChatId);
                loadChatList();
                
                if (button) {
                    button.innerHTML = '✓ Sent!';
                    button.style.background = '#4caf50';
                    button.style.borderColor = '#4caf50';
                    button.style.animation = '';
                    button.classList.remove('sending');
                    setTimeout(() => {
                        button.disabled = false;
                        button.style.width = '';
                        button.style.minWidth = '';
                        button.style.background = '';
                        button.style.borderColor = '';
                        button.innerHTML = originalHtml;
                        button.classList.remove('sending');
                    }, 1200);
                }
            } else {
                const error = await response.json();
                alert('Failed to send: ' + (error.error || 'Unknown error'));
                if (button) {
                    button.disabled = false;
                    button.style.width = '';
                    button.style.minWidth = '';
                    button.style.animation = '';
                    button.classList.remove('sending');
                    button.innerHTML = originalHtml;
                }
            }

        } catch (error) {
            console.error('Error sending message:', error);
            alert('Network error');
            if (button) {
                button.disabled = false;
                button.style.width = '';
                button.style.minWidth = '';
                button.style.animation = '';
                button.classList.remove('sending');
                button.innerHTML = originalHtml;
            }
        } finally {
            if (input) input.focus();
        }
    });
}

// ============================================================
// CHAT ACTIONS
// ============================================================
function showChatActions() {
    const modal = document.getElementById('chatActionsModal');
    if (modal) {
        modal.style.display = 'flex';
        const deleteBtn = document.getElementById('deleteChatBtn');
        const clearBtn = document.getElementById('clearChatBtn');
        if (deleteBtn) {
            deleteBtn.style.display = currentChatType === 'channel' ? 'none' : 'block';
        }
        if (clearBtn) {
            clearBtn.style.display = 'block';
        }
    }
}

function closeChatActions() {
    const modal = document.getElementById('chatActionsModal');
    if (modal) modal.style.display = 'none';
}

function showConfirmDelete(chatName, chatId) {
    deleteTargetChatId = chatId;
    const modal = document.getElementById('confirmDeleteModal');
    const text = document.getElementById('confirmDeleteText');
    if (modal && text) {
        text.textContent = `Delete chat with "${chatName}"? This action cannot be undone.`;
        modal.style.display = 'flex';
    }
}

function closeConfirmDelete() {
    const modal = document.getElementById('confirmDeleteModal');
    if (modal) modal.style.display = 'none';
    deleteTargetChatId = null;
}

async function executeDeleteChat() {
    if (!deleteTargetChatId) return;
    
    try {
        const response = await fetch('/api/delete_chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ chat_id: deleteTargetChatId })
        });

        closeConfirmDelete();

        if (response.ok) {
            invalidateCache(deleteTargetChatId);
            showChatList();
        } else {
            const error = await response.json();
            alert('Failed to delete chat: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error deleting chat:', error);
        alert('Network error');
    }
}

async function deleteCurrentChat() {
    if (!currentChatId || currentChatType === 'channel') return;
    closeChatActions();
    showConfirmDelete(currentChatName, currentChatId);
}

function showConfirmClear(chatName, chatId) {
    clearTargetChatId = chatId;
    const modal = document.getElementById('confirmClearModal');
    const text = document.getElementById('confirmClearText');
    if (modal && text) {
        text.textContent = `Clear all messages in "${chatName}"? This action cannot be undone.`;
        modal.style.display = 'flex';
    }
}

function closeConfirmClear() {
    const modal = document.getElementById('confirmClearModal');
    if (modal) modal.style.display = 'none';
    clearTargetChatId = null;
}

async function executeClearChat() {
    if (!clearTargetChatId) return;
    
    try {
        const response = await fetch('/api/clear_chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ chat_id: clearTargetChatId })
        });

        closeConfirmClear();

        if (response.ok) {
            invalidateCache(clearTargetChatId);
            lastMessagesSignature = '';
            await loadChatMessages(clearTargetChatId);
            loadChatList();
            loadMessages();
        } else {
            const error = await response.json();
            alert('Failed to clear chat: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error clearing chat:', error);
        alert('Network error');
    }
}

function clearCurrentChat() {
    if (!currentChatId) return;
    closeChatActions();
    showConfirmClear(currentChatName, currentChatId);
}

// ============================================================
// NODE OPERATIONS
// ============================================================
function setDirectMessage(nodeId, nodeName) {
    if (directMessageTarget === nodeId) {
        directMessageTarget = null;
        document.querySelectorAll('.node-title-btn').forEach(btn => {
            btn.style.background = 'linear-gradient(135deg, #4a5a7a 0%, #3a4a6a 100%)';
            btn.style.boxShadow = 'none';
        });
        document.getElementById('messageInput')?.focus();
        return;
    }

    directMessageTarget = nodeId;
    document.querySelectorAll('.node-title-btn').forEach(btn => {
        if (btn.dataset.nodeId === nodeId) {
            btn.style.background = '#ff9800';
            btn.style.boxShadow = '0 0 0 3px rgba(255, 152, 0, 0.4)';
        } else {
            btn.style.background = 'linear-gradient(135deg, #4a5a7a 0%, #3a4a6a 100%)';
            btn.style.boxShadow = 'none';
        }
    });

    openChat(nodeId, nodeName, 'dm');
}

async function toggleIgnore(nodeId) {
    try {
        const response = await fetch('/api/toggle_ignore', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ node_id: nodeId })
        });

        if (response.ok) {
            const data = await response.json();
            
            loadMessages();
            loadChatList();
            
            updateNodeDetails(nodeId);
            
            if (currentChatId === nodeId) {
                if (data.ignored) {
                    showIgnoredBanner(nodeId, currentChatName);
                } else {
                    hideIgnoredBanner();
                }
                invalidateCache(nodeId);
                lastMessagesSignature = '';
                await loadChatMessages(nodeId);
            }
        } else {
            const error = await response.json();
            alert('Failed to toggle ignore: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error toggling ignore:', error);
        alert('Network error');
    }
}

async function toggleFavorite(nodeId) {
    try {
        const response = await fetch('/api/toggle_favorite', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ node_id: nodeId })
        });

        if (response.ok) {
            const data = await response.json();
            loadMessages();
            loadChatList();
            updateNodeDetails(nodeId);
        } else {
            const error = await response.json();
            alert('Failed to toggle favorite: ' + (error.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error toggling favorite:', error);
        alert('Network error');
    }
}

function updateNodeDetails(nodeId) {
    const cachedNode = nodeCache.find(n => n.node_id === nodeId);
    if (cachedNode) {
        renderNodeDetails(cachedNode);
        return;
    }
    
    fetch('/api/messages')
        .then(response => response.json())
        .then(data => {
            const allNodes = data.nodes || [];
            nodeCache = allNodes;
            const selectedNode = allNodes.find(n => n.node_id === nodeId);
            if (selectedNode) {
                renderNodeDetails(selectedNode);
            } else {
                renderNodeDetails(null);
            }
        })
        .catch(error => {
            console.error('Error updating node details:', error);
        });
}

function renderNodeDetails(node) {
    const details = document.getElementById('nodeDetails');
    if (!details) return;

    if (!node) {
        details.className = 'node-details-placeholder';
        details.innerHTML = 'Select a node below';
        return;
    }

    const isIgnored = node.ignored || false;
    const isFavorite = node.favorite || false;
    const ignoreBtnClass = isIgnored ? 'ignore-btn active' : 'ignore-btn';
    const ignoreBtnText = isIgnored ? 'Ignored' : 'Ignore';
    const favoriteBtnClass = isFavorite ? 'favorite-btn active' : 'favorite-btn';
    const isActive = directMessageTarget === node.node_id;

    details.className = '';
    details.innerHTML = `
        <div class="node-details">
            <div class="node-details-header">
                <button class="node-title-btn" 
                        data-node-id="${escapeHtml(node.node_id)}"
                        onclick="setDirectMessage('${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name)}')"
                        style="${isActive ? 'background: #ff9800; box-shadow: 0 0 0 3px rgba(255, 152, 0, 0.4);' : ''}">
                    <span class="node-title-name">${isFavorite ? '⭐ ' : ''}${escapeHtml(node.clean_name)}</span>
                    <span class="node-title-action">→ Direct Message</span>
                </button>
            </div>
            <div class="node-details-grid">
                <div class="node-details-col">
                    <div class="node-details-item">
                        <span class="label">ID:</span>
                        <span class="value" style="font-size:10px;word-break:break-all;">${escapeHtml(node.node_id)}</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">HW:</span>
                        <span class="value">${escapeHtml(node.hw_model || '-')}</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">Seen:</span>
                        <span class="value">${escapeHtml(node.age || '-')}</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">RSSI:</span>
                        <span class="value">${escapeHtml(node.rssi || '-')} dBm</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">Hops:</span>
                        <span class="value">${escapeHtml(node.hop_start || '-')}</span>
                    </div>
                </div>
                <div class="node-details-col">
                    <div class="node-details-item">
                        <span class="label">Short:</span>
                        <span class="value">${escapeHtml(node.short_name || '-')}</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">Role:</span>
                        <span class="value">${escapeHtml(node.role || 'CLIENT')}</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">Signal:</span>
                        <span class="value">${escapeHtml(node.signal_quality || '-')}</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">SNR:</span>
                        <span class="value">${escapeHtml(node.snr || '-')} dB</span>
                    </div>
                    <div class="node-details-item">
                        <span class="label">Relay:</span>
                        <span class="value">${escapeHtml(node.relay_node || '-')}</span>
                    </div>
                </div>
                <div class="node-details-col node-details-col-actions">
                    <button class="${favoriteBtnClass}" 
                            data-node-id="${escapeHtml(node.node_id)}"
                            onclick="toggleFavorite('${escapeHtml(node.node_id)}')"
                            title="${isFavorite ? 'Remove from favorites' : 'Add to favorites'}">
                        ${isFavorite ? '⭐ Unstar' : '☆ Favorite'}
                    </button>
                    <button class="${ignoreBtnClass}" 
                            data-node-id="${escapeHtml(node.node_id)}"
                            onclick="toggleIgnore('${escapeHtml(node.node_id)}')"
                            title="${isIgnored ? 'Unignore this node' : 'Ignore this node'}">
                        ${ignoreBtnText}
                    </button>
                </div>
            </div>
        </div>`;
}

function clearNodeSearch() {
    nodeSearchTerm = '';
    const searchInput = document.getElementById('nodeSearchInput');
    if (searchInput) searchInput.value = '';
    loadMessages();
}

function selectNode(nodeId, nodeName) {
    if (currentChatId === nodeId) return;
    openChat(nodeId, nodeName, 'dm');
    updateNodeDetails(nodeId);
}

// ============================================================
// SENSORS & BASE STATUS
// ============================================================
async function loadSensors() {
    try {
        const response = await fetch('/api/sensors');
        const data = await response.json();

        const sensorsCard = document.getElementById('sensorsCard');
        if (sensorsCard && (data.temperature !== null || data.voltage !== null)) {
            sensorsCard.style.display = 'block';

            console.log("Sensors API:", data);
            console.log("Temperature:", data.temperature);

            document.getElementById('tempValue').textContent = formatTemperature(data.temperature);
            document.getElementById('humValue').textContent = data.humidity !== null ? data.humidity.toFixed(1) : '--';
            document.getElementById('presValue').textContent = formatPressure(data.pressure);
            document.getElementById('voltValue').textContent = data.voltage !== null ? data.voltage.toFixed(2) : '--';
            document.getElementById('currValue').textContent = data.current !== null ? Math.round(data.current) : '--';
            document.getElementById('powValue').textContent = data.power !== null ? Math.round(data.power) : '--';

            if (data.battery_percent !== null) {
                const batteryIndicator = document.getElementById('batteryIndicator');
                if (batteryIndicator) batteryIndicator.style.display = 'block';
                const percent = Math.min(100, Math.max(0, data.battery_percent));
                document.getElementById('batteryFill').style.width = percent + '%';
                document.getElementById('batteryPercent').textContent = percent + '%';
            }

            document.getElementById('sensorUpdate').textContent = `Last update: ${data.last_update || '--'}`;
        }
    } catch (error) {
        console.error('Error loading sensors:', error);
    }
}

async function loadBaseStatus() {
    try {
        const response = await fetch('/api/base_status');
        const data = await response.json();

        const card = document.getElementById('baseCard');
        if (!card) return;
        
        const nodeName = data.node_name || 'Flint Base';

        const battery = data.real_battery !== null ? '~' + data.real_battery + '%' :
                       data.battery_level !== null ? data.battery_level + '%' : '--%';
        const voltage = data.voltage !== null ? Number(data.voltage).toFixed(3) + ' V' : '-- V';
        const channel = data.channel_utilization !== null ? Number(data.channel_utilization).toFixed(2) + '%' : '--%';
        const airTx = data.air_util_tx !== null ? Number(data.air_util_tx).toFixed(2) + '%' : '--%';
        const uptime = data.uptime_seconds !== null ? formatUptime(data.uptime_seconds) : '--';

        card.innerHTML = `
            <div class="base-card-title">
                <span>📡 ${escapeHtml(nodeName)}</span>
                <span style="font-size:11px;opacity:0.8;">⏱ ${escapeHtml(uptime)}</span>
            </div>
            <div class="base-status-line">
                ⚡ ${escapeHtml(voltage)}
                🔋 ${escapeHtml(battery)}
                📶 ${escapeHtml(channel)}
                📡 ${escapeHtml(airTx)}
            </div>
        `;

    } catch (error) {
        console.error('Error loading base status:', error);
    }
}

function formatUptime(seconds) {
    seconds = Number(seconds);
    if (isNaN(seconds)) return '--';
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
}

// ============================================================
// EVENT LISTENERS
// ============================================================
document.getElementById('toggleSidebarBtn')?.addEventListener('click', () => {
    const sidebar = document.getElementById('sidebar');
    if (sidebar) {
        sidebar.classList.toggle('hidden');
    }
});

document.getElementById('nodeSearchInput')?.addEventListener('input', (e) => {
    nodeSearchTerm = e.target.value;
    loadMessages();
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeChatActions();
        closeConfirmDelete();
        closeConfirmClear();
        closeDeleteAllDmModal();
        if (isEmojiPickerOpen) {
            closeEmojiPicker();
        }
        if (currentChatId) {
            showChatList();
        }
    }
});

document.getElementById('chatActionsModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeChatActions();
    }
});

document.getElementById('confirmDeleteModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeConfirmDelete();
    }
});

document.getElementById('confirmClearModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeConfirmClear();
    }
});

document.getElementById('confirmDeleteAllDmModal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        closeDeleteAllDmModal();
    }
});

// ============================================================
// EMOJI
// ============================================================
const EMOJI_DATA = {
    smileys: [
        '😊', '😂', '❤️', '🔥', '👍', '💯', '🎉', '✨',
        '🤔', '😎', '💪', '🙏', '🥰', '😍', '🤗', '🫶',
        '😘', '😗', '😙', '🥲', '😅', '😆', '🤣', '🥹',
        '😌', '😏', '😒', '😔', '😕', '🙃', '🤑', '😲',
        '😳', '😱', '🤯', '🥳', '🤩', '😇', '🥺', '🤪',
        '😜', '😝', '🫠', '🤭', '🫣', '🤫', '🤥', '😶'
    ],
    gestures: [
        '👋', '🤚', '🖐️', '✋', '🖖', '👌', '🤌', '🤏',
        '✌️', '🤞', '🫰', '🤟', '🤘', '👈', '👉', '👆',
        '👇', '☝️', '👍', '👎', '👊', '✊', '🤛', '🤜',
        '👏', '🙌', '🫶', '🤲', '🤝', '🙏', '✍️', '💅'
    ],
    food: [
        '🍕', '🍔', '🌮', '🌯', '🥗', '🍣', '🍱', '🍜',
        '🍲', '🍛', '🍙', '🍚', '🍘', '🥟', '🍤', '🍗',
        '🥩', '🍖', '🥓', '🧀', '🥚', '🍳', '🥞', '🧇',
        '🥐', '🥖', '🍞', '🧈', '🧂', '🍿', '🧁', '🍰',
        '🎂', '🍪', '🍩', '🍫', '🍬', '🍭', '🍮', '☕',
        '🍵', '🧃', '🥤', '🧋', '🍺', '🍷', '🥂', '🍾'
    ],
    activities: [
        '🎉', '🎊', '🎁', '🎈', '🎀', '🎂', '🎆', '🎇',
        '✨', '🌟', '⭐', '🌈', '☀️', '🌙', '🌟', '💫',
        '🎵', '🎶', '🎤', '🎧', '🎼', '🎹', '🥁', '🎸',
        '🎺', '🎻', '🪕', '🎯', '🎳', '🎮', '🎲', '♟️',
        '🏆', '🏅', '🥇', '🥈', '🥉', '⚽', '🏀', '🏈',
        '⚾', '🥎', '🎾', '🏐', '🏉', '🥏', '🎱', '🪀'
    ],
    travel: [
        '🚗', '🚕', '🚙', '🚌', '🚎', '🏎️', '🚓', '🚑',
        '🚒', '🚐', '🛻', '🚚', '🚛', '🚜', '🏍️', '🛵',
        '🚲', '🛴', '🛹', '🛼', '🚁', '✈️', '🛩️', '🛫',
        '🛬', '🪂', '💺', '🚀', '🛸', '🚢', '🛳️', '⛵',
        '🚤', '🛥️', '🛶', '🚂', '🚆', '🚇', '🚉', '🚊',
        '🚝', '🚞', '🚋', '🚃', '🚄', '🚅', '🚈', '🚍'
    ],
    objects: [
        '💡', '🔦', '🕯️', '🧯', '🪣', '🧹', '🧺', '🪥',
        '🧽', '🪒', '💈', '🧴', '🧵', '🧶', '👓', '🕶️',
        '🥽', '🥼', '🦺', '👔', '👕', '👖', '🧣', '🧤',
        '🧥', '🧦', '👗', '👘', '🥻', '🩱', '🩲', '🩳',
        '👙', '👚', '👛', '👜', '👝', '🛍️', '🎒', '👞',
        '👟', '🥾', '🥿', '👠', '👡', '👢', '👑', '🎩'
    ],
    symbols: [
        '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍',
        '🤎', '💔', '❤️‍🔥', '❤️‍🩹', '💕', '💞', '💓', '💗',
        '💖', '💘', '💝', '💟', '☮️', '✝️', '☪️', '🕉️',
        '☸️', '✡️', '🔯', '🕎', '☯️', '☦️', '🛐', '⛎',
        '♈', '♉', '♊', '♋', '♌', '♍', '♎', '♏',
        '♐', '♑', '♒', '♓', '🆔', '⚛️', '🉑', '☢️'
    ]
};

let currentEmojiCategory = 'smileys';
let isEmojiPickerOpen = false;

function openEmojiPicker() {
    const picker = document.getElementById('emojiPicker');
    if (!picker) return;
    
    isEmojiPickerOpen = true;
    picker.style.display = 'flex';
    renderEmojiCategory(currentEmojiCategory);
    
    document.querySelectorAll('.emoji-cat-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.cat === currentEmojiCategory);
    });
}

function closeEmojiPicker() {
    const picker = document.getElementById('emojiPicker');
    if (picker) {
        picker.style.display = 'none';
    }
    isEmojiPickerOpen = false;
}

function toggleEmojiPicker() {
    if (isEmojiPickerOpen) {
        closeEmojiPicker();
    } else {
        openEmojiPicker();
    }
}

function renderEmojiCategory(category) {
    const grid = document.getElementById('emojiGrid');
    if (!grid) return;
    
    const emojis = EMOJI_DATA[category] || EMOJI_DATA.smileys;
    grid.innerHTML = emojis.map(emoji => 
        `<button class="emoji-item" data-emoji="${emoji}">${emoji}</button>`
    ).join('');
}

function insertEmoji(emoji) {
    const input = document.getElementById('messageInput');
    if (!input) return;
    
    const start = input.selectionStart;
    const end = input.selectionEnd;
    const text = input.value;
    
    input.value = text.substring(0, start) + emoji + text.substring(end);
    const newPos = start + emoji.length;
    input.selectionStart = input.selectionEnd = newPos;
    
    input.focus();
    closeEmojiPicker();
}

document.addEventListener('DOMContentLoaded', function() {
    const emojiBtn = document.getElementById('emojiBtn');
    if (emojiBtn) {
        emojiBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            toggleEmojiPicker();
        });
    }
    
    const closeBtn = document.getElementById('emojiCloseBtn');
    if (closeBtn) {
        closeBtn.addEventListener('click', function() {
            closeEmojiPicker();
        });
    }
    
    document.querySelectorAll('.emoji-cat-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const cat = this.dataset.cat;
            currentEmojiCategory = cat;
            document.querySelectorAll('.emoji-cat-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            renderEmojiCategory(cat);
        });
    });
    
    document.getElementById('emojiGrid')?.addEventListener('click', function(e) {
        const item = e.target.closest('.emoji-item');
        if (item) {
            const emoji = item.dataset.emoji;
            if (emoji) {
                insertEmoji(emoji);
            }
        }
    });
    
    document.addEventListener('click', function(e) {
        const picker = document.getElementById('emojiPicker');
        const btn = document.getElementById('emojiBtn');
        if (isEmojiPickerOpen && picker && btn) {
            if (!picker.contains(e.target) && !btn.contains(e.target)) {
                closeEmojiPicker();
            }
        }
    });
    
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && isEmojiPickerOpen) {
            closeEmojiPicker();
        }
    });
    
    document.querySelector('.messages-container')?.addEventListener('scroll', function() {
        if (isEmojiPickerOpen) {
            closeEmojiPicker();
        }
    });
});

// ============================================================
// SWITCH SIDEBAR TAB
// ============================================================
function switchSidebarTab(tab) {
    document.querySelectorAll('.sidebar-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    
    document.querySelectorAll('.sidebar-tab-content').forEach(content => {
        content.style.display = content.id === 'tab-' + tab ? 'flex' : 'none';
        content.classList.toggle('active', content.id === 'tab-' + tab);
    });
    
    if (tab === 'tools') {
        loadNodesManagement();
    } else if (tab === 'nodes') {
        loadMessages();
        if (currentChatId && currentChatType === 'dm') {
            updateNodeDetails(currentChatId);
        }
    }
}

// ============================================================
// NODE MANAGEMENT
// ============================================================
async function loadNodesManagement() {
    const container = document.getElementById('nodesManagementList');
    if (!container) return;
    
    try {
        const response = await fetch('/api/nodes_management');
        const data = await response.json();
        
        document.getElementById('totalNodesCount').textContent = data.total || 0;
        
        if (data.nodes.length === 0) {
            container.innerHTML = '<div class="loading">No nodes found</div>';
            return;
        }
        
        const nameMap = {};
        const duplicates = new Set();
        data.nodes.forEach(n => {
            if (nameMap[n.name]) {
                duplicates.add(n.name);
            } else {
                nameMap[n.name] = true;
            }
        });
        
        let filteredNodes = data.nodes;
        if (showDuplicatesOnly) {
            filteredNodes = data.nodes.filter(n => duplicates.has(n.name));
        }
        
        container.innerHTML = filteredNodes.map(node => {
            const isDuplicate = duplicates.has(node.name);
            const statusClass = node.ignored ? 'ignored' : (isDuplicate ? 'duplicate' : 'normal');
            const statusText = node.ignored ? '🚫 Ignored' : (isDuplicate ? '⚠️ Duplicate' : '✅ Normal');
            
            return `
                <div class="nodes-management-item">
                    <div class="name-wrapper">
                        <span class="name">${escapeHtml(node.name)}</span>
                        <span class="id">${escapeHtml(node.node_id)}</span>
                    </div>
                    <span class="status ${statusClass}">${statusText}</span>
                </div>
            `;
        }).join('');
    } catch (error) {
        console.error('Error loading nodes management:', error);
        container.innerHTML = '<div class="loading">⚠️ Error loading nodes</div>';
    }
}

// ============================================================
// EXPORT/IMPORT
// ============================================================
async function exportNodesCSV() {
    try {
        const response = await fetch('/api/nodes_export');
        const data = await response.json();
        
        if (!data.nodes || data.nodes.length === 0) {
            showToast('❌ No nodes to export', 'error');
            return;
        }
        
        const headers = ['"Node Name","Node ID","Last Seen","RSSI","SNR","Role","Short Name","HW Model"'];
        const rows = data.nodes.map(n => 
            `"${escapeCsv(n.name)}","${n.node_id}","${n.last_time || ''}","${n.rssi || ''}","${n.snr || ''}","${n.role || 'CLIENT'}","${n.short_name || ''}","${n.hw_model || ''}"`
        );
        
        const csv = headers.concat(rows).join('\n');
        const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `meshtastic_nodes_${new Date().toISOString().slice(0,10)}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        
        showToast(`✅ Exported ${data.nodes.length} nodes to CSV`, 'success');
    } catch (error) {
        console.error('Export CSV error:', error);
        showToast('❌ Export failed', 'error');
    }
}

async function exportNodesJSON() {
    try {
        const response = await fetch('/api/nodes_export');
        const data = await response.json();
        
        if (!data.nodes || data.nodes.length === 0) {
            showToast('❌ No nodes to export', 'error');
            return;
        }
        
        const blob = new Blob([JSON.stringify(data.nodes, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `meshtastic_nodes_${new Date().toISOString().slice(0,10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
        
        showToast(`✅ Exported ${data.nodes.length} nodes to JSON`, 'success');
    } catch (error) {
        console.error('Export JSON error:', error);
        showToast('❌ Export failed', 'error');
    }
}

async function importNodesCSV(event) {
    const file = event.target.files[0];
    if (!file) return;
    
    const reader = new FileReader();
    reader.onload = async function(e) {
        try {
            const text = e.target.result;
            const lines = text.split('\n').filter(line => line.trim());
            if (lines.length < 2) {
                showToast('❌ Invalid CSV file', 'error');
                return;
            }
            
            const headerLine = lines[0].replace(/^"|"$/g, '').split('","');
            const headers = headerLine.map(h => h.replace(/"/g, '').trim());
            
            const nodes = [];
            for (let i = 1; i < lines.length; i++) {
                const line = lines[i].replace(/^"|"$/g, '').split('","');
                const node = {};
                headers.forEach((h, idx) => {
                    const val = (line[idx] || '').replace(/"/g, '').trim();
                    if (h === 'Node Name') node.name = val;
                    else if (h === 'Node ID') node.node_id = val;
                    else if (h === 'Short Name') node.short_name = val;
                    else if (h === 'HW Model') node.hw_model = val;
                    else if (h === 'Role') node.role = val;
                    else if (h === 'Last Seen') node.last_time = val;
                    else if (h === 'RSSI') node.rssi = val;
                    else if (h === 'SNR') node.snr = val;
                });
                if (node.node_id) nodes.push(node);
            }
            
            if (nodes.length === 0) {
                showToast('❌ No valid nodes found in CSV', 'error');
                return;
            }
            
            const response = await fetch('/api/nodes_import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes })
            });
            
            const result = await response.json();
            if (result.ok) {
                showToast(`✅ Imported ${result.imported_count} nodes from CSV`, 'success');
                loadMessages();
                loadNodesManagement();
            } else {
                showToast('❌ Import failed: ' + result.error, 'error');
            }
        } catch (error) {
            console.error('Import CSV error:', error);
            showToast('❌ Import failed', 'error');
        }
    };
    reader.readAsText(file);
    event.target.value = '';
}

async function importNodesJSON(event) {
    const file = event.target.files[0];
    if (!file) return;
    
    const reader = new FileReader();
    reader.onload = async function(e) {
        try {
            const nodes = JSON.parse(e.target.result);
            if (!Array.isArray(nodes) || nodes.length === 0) {
                showToast('❌ Invalid JSON file', 'error');
                return;
            }
            
            const response = await fetch('/api/nodes_import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodes })
            });
            
            const result = await response.json();
            if (result.ok) {
                showToast(`✅ Imported ${result.imported_count} nodes from JSON`, 'success');
                loadMessages();
                loadNodesManagement();
            } else {
                showToast('❌ Import failed: ' + result.error, 'error');
            }
        } catch (error) {
            console.error('Import JSON error:', error);
            showToast('❌ Import failed', 'error');
        }
    };
    reader.readAsText(file);
    event.target.value = '';
}

async function mergeDuplicates() {
    if (!confirm('⚠️ Merge duplicate nodes?\n\nThis will merge nodes with the same name, keeping the most recent one.\n\nThis action cannot be undone!')) {
        return;
    }
    
    try {
        const response = await fetch('/api/nodes_merge_duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast(`✅ Merged ${data.merged_count} duplicates`, 'success');
            loadMessages();
            loadNodesManagement();
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Merge duplicates error:', error);
        showToast('❌ Network error', 'error');
    }
}

function toggleShowDuplicates() {
    showDuplicatesOnly = !showDuplicatesOnly;
    const btn = document.querySelector('.nodes-tool-btn.show');
    if (btn) {
        btn.style.background = showDuplicatesOnly ? '#d0c0e0' : '';
        btn.textContent = showDuplicatesOnly ? '📋 Hide Duplicates' : '📋 Show Duplicates';
    }
    loadNodesManagement();
}

async function cleanupAllNodes() {
    if (!confirm('⚠️ Delete ALL nodes?\n\nThis will delete all nodes and their chats.\nThe LongFast channel will remain.\n\nThis action cannot be undone!')) {
        return;
    }
    
    if (!confirm('Are you sure? All nodes and DM chats will be permanently deleted!')) {
        return;
    }
    
    try {
        const response = await fetch('/api/cleanup_all_nodes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast(`✅ Deleted ${data.deleted_count} nodes`, 'success');
            loadMessages();
            loadChatList();
            loadNodesManagement();
            if (currentChatType === 'dm') {
                showChatList();
            }
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Cleanup all nodes error:', error);
        showToast('❌ Network error', 'error');
    }
}

function escapeCsv(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/"/g, '""');
}

async function restartListener() {
    if (!confirm("Restart MeshCenter listener?\n\nCurrent reception will be interrupted for a few seconds.")) {
        return;
    }

    const button = document.getElementById('restartListenerBtn');
    const originalText = button?.textContent || '🔄 Restart Listener';

    if (button) {
        button.disabled = true;
        button.textContent = 'Restarting Listener...';
    }

    try {
        const response = await fetch('/api/restart_listener', { method: 'POST' });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        showToast('✅ Meshtastic listener restart requested', 'success');
        setTimeout(loadRadioHealth, 1000);
    } catch (error) {
        showToast('❌ Restart failed: ' + error.message, 'error');
    } finally {
        if (button) {
            setTimeout(() => {
                button.disabled = false;
                button.textContent = originalText;
            }, 1500);
        }
    }
}

async function rescanNodes() {
    const btn = document.getElementById('rescanNodesBtn');
    const originalText = btn.textContent;
    
    try {
        btn.disabled = true;
        btn.textContent = '⏳ Scanning...';
        
        const response = await fetch('/api/rescan_nodes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.ok) {
            btn.textContent = '⏳ Waiting for nodes...';
            await new Promise(resolve => setTimeout(resolve, 5000));
            
            await loadMessages();
            await loadChatList();
            
            btn.textContent = '✅ Done!';
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 2000);
            
            showToast('✅ Network rescanned', 'success');
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
            btn.textContent = originalText;
            btn.disabled = false;
        }
    } catch (error) {
        console.error('Rescan error:', error);
        showToast('❌ Network error', 'error');
        btn.textContent = originalText;
        btn.disabled = false;
    }
}

// ============================================================
// DELETE ALL DM CHATS
// ============================================================
let deleteAllDmState = 'first';

function deleteAllDmChats() {
    deleteAllDmState = 'first';
    const modal = document.getElementById('confirmDeleteAllDmModal');
    const text = document.getElementById('deleteAllDmText');
    const btn = document.getElementById('confirmDeleteAllDmBtn');
    
    if (modal && text) {
        text.textContent = '⚠️ Delete ALL Direct Message chats?\n\nThis will delete all DM chats and their messages.\nThe LongFast channel will remain.\n\nThis action cannot be undone!';
        btn.textContent = 'Delete All';
        btn.style.background = '';
        modal.style.display = 'flex';
    }
}

function closeDeleteAllDmModal() {
    const modal = document.getElementById('confirmDeleteAllDmModal');
    if (modal) modal.style.display = 'none';
    deleteAllDmState = 'first';
}

function executeDeleteAllDm() {
    const btn = document.getElementById('confirmDeleteAllDmBtn');
    const text = document.getElementById('deleteAllDmText');
    
    if (deleteAllDmState === 'first') {
        deleteAllDmState = 'second';
        text.textContent = '⚠️ Are you sure?\n\nAll DM chats and messages will be permanently deleted!\n\nThis action cannot be undone!';
        btn.textContent = 'Yes, Delete Everything!';
        btn.style.background = '#c62828';
        return;
    }
    
    btn.disabled = true;
    btn.textContent = '⏳ Deleting...';
    
    fetch('/api/delete_all_dm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.ok) {
            closeDeleteAllDmModal();
            loadChatList();
            loadMessages();
            if (currentChatType === 'dm') {
                showChatList();
            }
            showToast(`✅ Deleted ${data.deleted_count} DM chats`, 'success');
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
            btn.disabled = false;
            btn.textContent = 'Delete All';
            btn.style.background = '';
        }
    })
    .catch(error => {
        console.error('Delete all DM error:', error);
        showToast('❌ Network error', 'error');
        btn.disabled = false;
        btn.textContent = 'Delete All';
        btn.style.background = '';
    });
}

// ============================================================
// EXPORT/IMPORT MENUS
// ============================================================
function showExportOptions() {
    closeFormatMenus();
    const menu = document.getElementById('exportOptionsMenu');
    if (menu) menu.style.display = 'block';
}

function showImportOptions() {
    closeFormatMenus();
    const menu = document.getElementById('importOptionsMenu');
    if (menu) menu.style.display = 'block';
}

function closeFormatMenus() {
    const exportMenu = document.getElementById('exportOptionsMenu');
    const importMenu = document.getElementById('importOptionsMenu');
    if (exportMenu) exportMenu.style.display = 'none';
    if (importMenu) importMenu.style.display = 'none';
}

document.addEventListener('click', function(e) {
    const exportMenu = document.getElementById('exportOptionsMenu');
    const importMenu = document.getElementById('importOptionsMenu');
    const exportBtn = document.querySelector('.nodes-tool-btn.export');
    const importBtn = document.querySelector('.nodes-tool-btn.import');
    
    if (exportMenu && exportMenu.style.display === 'block' && !exportMenu.contains(e.target) && !exportBtn?.contains(e.target)) {
        exportMenu.style.display = 'none';
    }
    if (importMenu && importMenu.style.display === 'block' && !importMenu.contains(e.target) && !importBtn?.contains(e.target)) {
        importMenu.style.display = 'none';
    }
});

// ============================================================
// TELEMETRY FUNCTIONS
// ============================================================
async function loadTelemetry() {
    console.log('[TELEMETRY] loadTelemetry called');
    try {
        const response = await fetch('/api/telemetry');
        const data = await response.json();
        telemetryData = data;
        updateTelemetryUI();
    } catch (error) {
        console.error('[TELEMETRY] Error loading telemetry:', error);
    }
}

async function loadTelemetryHistory() {
    try {
        const historyResponse = await fetch('/api/telemetry/history?limit=5000');
        const historyData = await historyResponse.json();

        telemetryFullHistory = historyData.history || [];
        telemetryHistory = telemetryFullHistory;

        if (historyData.config) {
            telemetryInterval = historyData.config.interval || 900;
            const select = document.getElementById('telemetryInterval');
            if (select) {
                select.value = telemetryInterval;
            }
        }

        console.log('[TELEMETRY] History records:', telemetryHistory.length);
    } catch (error) {
        console.error('[TELEMETRY] Error loading telemetry history:', error);
    }
}

function updateTelemetryUI() {
    const data = telemetryData;
    
    const envValue = document.getElementById('telemetryEnvValue');
    const envUpdate = document.getElementById('telemetryEnvUpdate');
    if (envValue) {
        let parts = [];
        if (data.temperature !== null && data.temperature !== undefined) {
            parts.push(formatTemperature(data.temperature));
        }
        if (data.humidity !== null && data.humidity !== undefined) {
            parts.push(`${data.humidity.toFixed(1)}%`);
        }
        if (data.pressure !== null && data.pressure !== undefined) {
            parts.push(formatPressure(data.pressure));
        }
        envValue.textContent = parts.length > 0 ? parts.join('  ') : '—';
    }
    if (envUpdate) {
        envUpdate.textContent = data.last_update ? `⏱${data.last_update}` : '';
    }
    
    const powerValue = document.getElementById('telemetryPowerValue');
    const powerUpdate = document.getElementById('telemetryPowerUpdate');
    if (powerValue) {
        let parts = [];
        if (data.voltage !== null && data.voltage !== undefined) {
            parts.push(`${data.voltage.toFixed(3)}V`);
        }
        if (data.current !== null && data.current !== undefined && data.current > 0) {
            parts.push(`${data.current.toFixed(0)}mA`);
        }
        powerValue.textContent = parts.length > 0 ? parts.join('  ') : '—';
    }
    if (powerUpdate) {
        powerUpdate.textContent = data.last_update ? `⏱${data.last_update}` : '';
    }
    
    const statusEl = document.getElementById('telemetryStatus');
    if (statusEl) {
        if (data.last_update) {
            statusEl.textContent = `🟢 ${data.last_update}`;
        } else {
            statusEl.textContent = '⚪ No data';
        }
    }
}

async function updateTelemetryConfig() {
    const select = document.getElementById('telemetryInterval');
    const interval = parseInt(select.value);
    
    try {
        const response = await fetch('/api/telemetry/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ interval: interval })
        });
        
        const data = await response.json();
        if (data.ok) {
            telemetryInterval = interval;
            showToast(`✅ Interval set to ${interval/60} minutes`, 'success');
        } else {
            showToast('❌ Failed to update interval', 'error');
        }
    } catch (error) {
        console.error('Error updating telemetry config:', error);
        showToast('❌ Network error', 'error');
    }
}

async function openTelemetryModal(type) {
    const modal = document.getElementById('telemetryModal');
    const title = document.getElementById('telemetryModalTitle');
    const container = document.getElementById('telemetryChartContainer');

    if (!modal || !title || !container) {
        console.error('Modal elements not found');
        return;
    }

    modal.dataset.type = type;
    modal.style.display = 'flex';
    container.innerHTML = '<div class="loading">⏳ Loading telemetry data...</div>';

    const labels = {
        'environment': '🌡️ Environment Sensors',
        'power': '⚡ Power Sensors'
    };
    title.textContent = labels[type] || '📊 Telemetry';

    const footer = document.getElementById('telemetryFooter');
    if (footer) {
        footer.innerHTML = `
        <div class="telemetry-time-controls">
            <button class="time-btn active" data-range="60" onclick="setTelemetryRange(60)">1h</button>
            <button class="time-btn" data-range="360" onclick="setTelemetryRange(360)">6h</button>
            <button class="time-btn" data-range="720" onclick="setTelemetryRange(720)">12h</button>
            <button class="time-btn" data-range="1440" onclick="setTelemetryRange(1440)">24h</button>
            <button class="time-btn" data-range="10080" onclick="setTelemetryRange(10080)">7d</button>
            <button class="time-btn" data-range="43200" onclick="setTelemetryRange(43200)">30d</button>
        </div>

        <div class="telemetry-footer-actions">
            <button class="telemetry-export-btn" onclick="exportTelemetryData()">⬇ Export</button>
            <span class="telemetry-records-count" id="telemetryRecordsCount">📊 0 records</span>
        </div>
        `;
    }

    try {
        await loadTelemetry();
        await loadTelemetryHistory();
        renderTelemetryWithRange(type, telemetryTimeRange);    
        } catch (error) {
        console.error('Error loading telemetry modal:', error);
        container.innerHTML = '<div class="loading">⚠️ Error loading telemetry data</div>';
    }
}

function setTelemetryRange(minutes) {
    telemetryTimeRange = minutes;
    
    document.querySelectorAll('.time-btn').forEach(btn => {
        btn.classList.toggle('active', parseInt(btn.dataset.range) === minutes);
    });
    
    const modal = document.getElementById('telemetryModal');
    const type = modal ? modal.dataset.type : 'environment';
    renderTelemetryWithRange(type, minutes);
}

function toggleTelemetrySeries(seriesName) {
    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';

    if (!telemetryVisibleSeries[type] || !(seriesName in telemetryVisibleSeries[type])) {
        return;
    }

    telemetryVisibleSeries[type][seriesName] = !telemetryVisibleSeries[type][seriesName];

    const activeCount = Object.values(telemetryVisibleSeries[type]).filter(Boolean).length;

    if (activeCount === 0) {
        telemetryVisibleSeries[type][seriesName] = true;
    }

    renderTelemetryWithRange(type, telemetryTimeRange);
}

function exportTelemetryData() {
    openCustomTelemetryExport();
}

function closeTelemetryExportMenu() {
    const menu = document.getElementById('telemetryExportMenu');
    if (menu) menu.remove();
}

function openCustomTelemetryExport() {
    closeTelemetryExportMenu();

    const oldModal = document.getElementById('customTelemetryExportModal');
    if (oldModal) oldModal.remove();

    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';

    const now = new Date();
    const rangeMinutes = telemetryTimeRange || 1440;
    const from = new Date(now.getTime() - rangeMinutes * 60 * 1000);

    const seriesText = getTelemetryVisibleSeriesText(type);
    const rangeLabel = getTelemetryRangeLabel(rangeMinutes);

    const overlay = document.createElement('div');
    overlay.id = 'customTelemetryExportModal';
    overlay.className = 'custom-export-overlay';

    overlay.innerHTML = `
        <div class="custom-export-dialog">
            <div class="custom-export-header">
                <div>
                    <div class="custom-export-title">📤 Export telemetry</div>
                    <div class="custom-export-subtitle">Export selected telemetry data</div>
                </div>
                <button class="custom-export-close" onclick="closeCustomTelemetryExport()">×</button>
            </div>

            <div class="custom-export-body">

                <div class="export-section">
                    <div class="export-section-title">Export source</div>

                    <label class="export-radio-row">
                        <input type="radio" name="exportRangeMode" value="visible" checked onchange="updateCustomExportMode()">
                        <span>Current visible range (${rangeLabel})</span>
                    </label>

                    <label class="export-radio-row">
                        <input type="radio" name="exportRangeMode" value="custom" onchange="updateCustomExportMode()">
                        <span>Custom range</span>
                    </label>
                </div>

                <div class="export-section custom-export-range" id="customExportRangeFields" style="display:none;">
                    <div class="export-date-grid">
                        <label>
                            <span>From</span>
                            <input type="datetime-local" id="exportStartDate" value="${datetimeLocalValue(from)}">
                        </label>

                        <label>
                            <span>To</span>
                            <input type="datetime-local" id="exportEndDate" value="${datetimeLocalValue(now)}">
                        </label>
                    </div>
                </div>

                <div class="export-section">
                    <div class="export-section-title">Series</div>
                    <div class="export-series-summary">${seriesText}</div>
                </div>

                <div class="export-section">
                    <div class="export-section-title">Format</div>

                    <div class="export-format-row">
                        <label class="export-format-option">
                            <input type="radio" name="exportFormat" value="csv" checked>
                            <span>📄 CSV</span>
                        </label>

                        <label class="export-format-option">
                            <input type="radio" name="exportFormat" value="json">
                            <span>📄 JSON</span>
                        </label>
                    </div>
                </div>

            </div>

            <div class="custom-export-footer">
                <button class="custom-export-cancel" onclick="closeCustomTelemetryExport()">Cancel</button>
                <button class="custom-export-primary" onclick="runCustomTelemetryExport()">⬇ Export</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
}

function closeCustomTelemetryExport() {
    const modal = document.getElementById('customTelemetryExportModal');
    if (modal) modal.remove();
}

function updateCustomExportMode() {
    const mode = document.querySelector('input[name="exportRangeMode"]:checked')?.value || 'visible';
    const fields = document.getElementById('customExportRangeFields');

    if (fields) {
        fields.style.display = mode === 'custom' ? 'block' : 'none';
    }
}

function datetimeLocalValue(date) {
    const pad = n => String(n).padStart(2, '0');

    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function getTelemetryRangeLabel(minutes) {
    if (minutes < 1440) return `${minutes / 60}h`;
    return `${minutes / 1440}d`;
}

function getTelemetryVisibleSeriesText(type) {
    const visible = telemetryVisibleSeries[type] || {};
    const labels = {
        temperature: 'Temperature',
        humidity: 'Humidity',
        pressure: 'Pressure',
        voltage: 'Voltage',
        current: 'Current',
        power: 'Power'
    };

    const active = Object.keys(visible)
        .filter(key => visible[key])
        .map(key => labels[key] || key);

    return active.length > 0 ? active.join(' • ') : 'No series selected';
}

function runCustomTelemetryExport() {
    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';

    const format = document.querySelector('input[name="exportFormat"]:checked')?.value || 'csv';
    const mode = document.querySelector('input[name="exportRangeMode"]:checked')?.value || 'visible';

    const series = Object.keys(telemetryVisibleSeries[type] || {})
        .filter(key => telemetryVisibleSeries[type][key])
        .join(',');

    let url = `/api/export/telemetry?type=${encodeURIComponent(type)}&format=${encodeURIComponent(format)}&series=${encodeURIComponent(series)}`;

    if (mode === 'custom') {
        const startValue = document.getElementById('exportStartDate')?.value;
        const endValue = document.getElementById('exportEndDate')?.value;

        if (!startValue || !endValue) {
            alert('Please select start and end date/time.');
            return;
        }

        const startTs = Math.floor(new Date(startValue).getTime() / 1000);
        const endTs = Math.floor(new Date(endValue).getTime() / 1000);

        if (!startTs || !endTs || startTs >= endTs) {
            alert('Invalid date range.');
            return;
        }

        url += `&start=${encodeURIComponent(startTs)}&end=${encodeURIComponent(endTs)}`;
    } else {
        url += `&range=${encodeURIComponent(telemetryTimeRange || 1440)}`;
    }

    closeCustomTelemetryExport();
    window.location.href = url;
}

function downloadTelemetryExport(format) {
    closeTelemetryExportMenu();

    const modal = document.getElementById('telemetryModal');
    const type = modal ? (modal.dataset.type || 'environment') : 'environment';
    const range = telemetryTimeRange || 1440;

    const url = `/api/export/telemetry?type=${encodeURIComponent(type)}&range=${encodeURIComponent(range)}&format=${encodeURIComponent(format)}`;
    window.location.href = url;
}

function renderTelemetryWithRange(type, minutes) {
    const container = document.getElementById('telemetryChartContainer');
    const recordsCount = document.getElementById('telemetryRecordsCount');
    
    if (!container) return;
    
    const now = Date.now() / 1000;
    const cutoff = now - (minutes * 60);
    
    const filteredRecords = telemetryFullHistory.filter(r => r.timestamp >= cutoff);
    
const rangeLabel = minutes < 1440
    ? `${minutes / 60}h`
    : `${minutes / 1440}d`;

if (filteredRecords.length === 0) {
    container.innerHTML = `<div class="loading">📊 No data for this period (${rangeLabel}). Try a longer range.</div>`;
    if (recordsCount) recordsCount.textContent = '📊 0 records';
    return;
}

if (recordsCount) {
    recordsCount.textContent = `📊 ${filteredRecords.length} records (${rangeLabel})`;
}

    renderTelemetryChart(container, filteredRecords, type);
    updateTelemetryCards(filteredRecords, type);
}

function renderTelemetryChart(container, records, type) {
    container.innerHTML = '<canvas id="telemetryChartCanvas"></canvas>';

    const canvas = document.getElementById('telemetryChartCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');

    const labels = records.map(r => {
        const t = new Date(r.timestamp * 1000);
        return t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    });

    let datasets = [];
    let hasPressure = false;
    let hasCurrent = false;
    let hasPower = false;

    if (type === 'environment') {
        const tempData = records.map(r => r.temperature).filter(v => v !== null && v !== undefined);
        if (tempData.length > 0 && telemetryVisibleSeries.environment.temperature) {
            datasets.push({
                label: 'Temperature ' + temperatureChartUnit(),
                data: records.map(r => temperatureChartValue(r.temperature)),
                borderColor: SENSOR_COLORS.temperature,
                backgroundColor: SENSOR_BG_COLORS.temperature,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y'
            });
        }

        const humData = records.map(r => r.humidity).filter(v => v !== null && v !== undefined);
        if (humData.length > 0 && telemetryVisibleSeries.environment.humidity) {
            datasets.push({
                label: 'Humidity %',
                data: records.map(r => r.humidity),
                borderColor: SENSOR_COLORS.humidity,
                backgroundColor: SENSOR_BG_COLORS.humidity,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y'
            });
        }

        const pressData = records.map(r => r.pressure).filter(v => v !== null && v !== undefined && !isNaN(v));
        if (pressData.length > 0 && telemetryVisibleSeries.environment.pressure) {
            hasPressure = true;
            datasets.push({
                label: 'Pressure ' + pressureChartUnit(),
                data: records.map(r => pressureChartValue(r.pressure)),
                borderColor: SENSOR_COLORS.pressure,
                backgroundColor: SENSOR_BG_COLORS.pressure,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y1'
            });
        }

    } else if (type === 'power') {
        const voltData = records.map(r => r.voltage).filter(v => v !== null && v !== undefined);
        if (voltData.length > 0 && telemetryVisibleSeries.power.voltage) {
            datasets.push({
                label: 'Voltage V',
                data: records.map(r => r.voltage),
                borderColor: SENSOR_COLORS.voltage,
                backgroundColor: SENSOR_BG_COLORS.voltage,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y'
            });
        }

        const currData = records.map(r => r.current).filter(v => v !== null && v !== undefined);
        if (currData.length > 0 && telemetryVisibleSeries.power.current) {
            hasCurrent = true;
            datasets.push({
                label: 'Current mA',
                data: records.map(r => r.current),
                borderColor: SENSOR_COLORS.current,
                backgroundColor: SENSOR_BG_COLORS.current,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y1'
            });
        }

        const powerSeries = records.map(r => {
            if (r.power !== null && r.power !== undefined) return r.power / 1000;
            if (r.voltage !== null && r.voltage !== undefined && r.current !== null && r.current !== undefined) {
                return (r.voltage * r.current) / 1000;
            }
            return null;
        });

        const powerData = powerSeries.filter(v => v !== null && v !== undefined);
        if (powerData.length > 0 && telemetryVisibleSeries.power.power) {
            hasPower = true;
            datasets.push({
                label: 'Power W',
                data: powerSeries,
                borderColor: SENSOR_COLORS.power,
                backgroundColor: SENSOR_BG_COLORS.power,
                fill: true,
                tension: 0.3,
                spanGaps: true,
                yAxisID: 'y2'
            });
        }
    }

    if (telemetryChart) {
        telemetryChart.destroy();
        telemetryChart = null;
    }

    if (datasets.length === 0) {
        container.innerHTML = '<div class="loading">📊 No data available for this sensor type</div>';
        return;
    }

    let yConfig = {
        position: 'left',
        grid: { color: 'rgba(0,0,0,0.08)', drawBorder: true },
        ticks: { font: { size: 9 }, color: '#666' }
    };

    let y1Config = {
        position: 'right',
        grid: { drawOnChartArea: false, drawBorder: true },
        ticks: { font: { size: 9 }, color: '#666' }
    };

    let y2Config = {
        position: 'right',
        offset: true,
        grid: { drawOnChartArea: false, drawBorder: true },
        ticks: {
            font: { size: 9 },
            color: '#666',
            callback: value => value.toFixed(1)
        }
    };

    if (type === 'environment') {
        const tempUnit = appSettings?.units?.temperature || "c";
        yConfig.min = tempUnit === "f" ? 20 : -5;
        yConfig.max = tempUnit === "f" ? 120 : 100;

        if (hasPressure) {
            const pressureUnit = appSettings?.units?.pressure || "hpa";
            y1Config.min = pressureUnit === "mmhg" ? 675 : 900;
            y1Config.max = pressureUnit === "mmhg" ? 900 : 1200;
        } else {
            y1Config.min = -10;
            y1Config.max = 100;
        }
    }

    if (type === 'power') {
        yConfig.min = 3.40;
        yConfig.max = 4.30;

        y1Config.min = 250;
        y1Config.max = 1000;

        if (hasPower) {
            const powerValues = datasets
                .filter(d => d.label === 'Power W')
                .flatMap(d => d.data)
                .filter(v => v !== null && v !== undefined && !isNaN(v));

            const maxPower = powerValues.length > 0 ? Math.max(...powerValues) : 2;
            y2Config.min = 0;
            y2Config.max = Math.max(2, Math.ceil(maxPower * 1.2));
        }
    }

    try {
        telemetryChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        position: 'top',
                        labels: { usePointStyle: true, padding: 15, font: { size: 11 }, color: '#333' }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(255,255,255,0.95)',
                        titleColor: '#333',
                        bodyColor: '#666',
                        borderColor: 'rgba(0,0,0,0.1)',
                        borderWidth: 1,
                        callbacks: {
                            title: function(context) {
                                if (!context || !context.length) return '';
                                const record = records[context[0].dataIndex];
                                if (!record || !record.timestamp) return '';
                                return new Date(record.timestamp * 1000).toLocaleString([], {
                                    year: 'numeric',
                                    month: '2-digit',
                                    day: '2-digit',
                                    hour: '2-digit',
                                    minute: '2-digit'
                                });
                            },
                            label: function(context) {
                                const label = context.dataset.label || '';
                                const value = context.parsed.y;

                                if (value === null || value === undefined || isNaN(value)) return label + ': —';
                                if (label.startsWith('Temperature')) return label + ': ' + value.toFixed(1) + temperatureChartUnit();
                                if (label.startsWith('Humidity')) return label + ': ' + value.toFixed(1) + '%';
                                if (label.startsWith('Pressure')) return label + ': ' + value.toFixed(1) + ' ' + pressureChartUnit();
                                if (label.startsWith('Voltage')) return label + ': ' + value.toFixed(3) + ' V';
                                if (label.startsWith('Current')) return label + ': ' + value.toFixed(1) + ' mA';
                                if (label.startsWith('Power')) return label + ': ' + value.toFixed(3) + ' W';

                                return label + ': ' + value.toFixed(2);
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: 'rgba(0,0,0,0.06)', drawBorder: true },
                        ticks: { maxTicksLimit: 20, font: { size: 9 }, color: '#666' }
                    },
                    y: yConfig,
                    y1: y1Config,
                    ...(hasPower ? { y2: y2Config } : {})
                }
            }
        });

    } catch (error) {
        console.error('Chart creation error:', error);
        container.innerHTML = '<div class="loading">⚠️ Error creating chart: ' + error.message + '</div>';
    }
}

function updateTelemetryCards(records, type) {
    if (type === 'environment') {
        const historyLast = records[records.length - 1] || {};
        const last = {
            temperature: telemetryData.temperature ?? historyLast.temperature,
            humidity: telemetryData.humidity ?? historyLast.humidity,
            pressure: telemetryData.pressure ?? historyLast.pressure
        };

        const tempValues = records.map(r => r.temperature).filter(v => v !== null && v !== undefined);
        const humValues = records.map(r => r.humidity).filter(v => v !== null && v !== undefined);
        const pressValues = records.map(r => r.pressure).filter(v => v !== null && v !== undefined);

        const card1 = document.getElementById('cardTemp').parentElement;
        card1.onclick = () => toggleTelemetrySeries('temperature');
        card1.classList.toggle('inactive', !telemetryVisibleSeries.environment.temperature);
        card1.querySelector('.card-label').textContent = '🌡️ Temperature';
        document.getElementById('cardTemp').textContent = formatTemperature(last.temperature);
        document.getElementById('cardTemp').style.color = SENSOR_COLORS.temperature;

        if (tempValues.length > 0) {
            document.getElementById('cardTempMin').textContent = formatTemperature(Math.min(...tempValues));
            document.getElementById('cardTempMax').textContent = formatTemperature(Math.max(...tempValues));
        }
        card1.querySelector('.card-range').style.display = 'flex';

        const card2 = document.getElementById('cardHum').parentElement;
        card2.onclick = () => toggleTelemetrySeries('humidity');
        card2.classList.toggle('inactive', !telemetryVisibleSeries.environment.humidity);
        card2.querySelector('.card-label').textContent = '💧 Humidity';
        document.getElementById('cardHum').textContent =
            last.humidity !== null && last.humidity !== undefined ? last.humidity.toFixed(1) + '%' : '--';
        document.getElementById('cardHum').style.color = SENSOR_COLORS.humidity;

        if (humValues.length > 0) {
            document.getElementById('cardHumMin').textContent = Math.min(...humValues).toFixed(1) + '%';
            document.getElementById('cardHumMax').textContent = Math.max(...humValues).toFixed(1) + '%';
        }
        card2.querySelector('.card-range').style.display = 'flex';

        const card3 = document.getElementById('cardPress').parentElement;
        card3.onclick = () => toggleTelemetrySeries('pressure');
        card3.classList.toggle('inactive', !telemetryVisibleSeries.environment.pressure);
        card3.querySelector('.card-label').textContent = '📊 Pressure';
        document.getElementById('cardPress').textContent = formatPressure(last.pressure);
        document.getElementById('cardPress').style.color = SENSOR_COLORS.pressure;

        if (pressValues.length > 0) {
            document.getElementById('cardPressMin').textContent = formatPressure(Math.min(...pressValues));
            document.getElementById('cardPressMax').textContent = formatPressure(Math.max(...pressValues));
        }
        card3.querySelector('.card-range').style.display = 'flex';

    } else if (type === 'power') {
        const historyLast = records[records.length - 1] || {};
        const last = {
            voltage: telemetryData.voltage ?? historyLast.voltage,
            current: telemetryData.current ?? historyLast.current,
            power: telemetryData.power ?? historyLast.power
        };

        const voltValues = records.map(r => r.voltage).filter(v => v !== null && v !== undefined);
        const currValues = records.map(r => r.current).filter(v => v !== null && v !== undefined);
        const powerValues = records.map(r => {
            if (r.power !== null && r.power !== undefined) return r.power;
            if (r.voltage !== null && r.voltage !== undefined && r.current !== null && r.current !== undefined) {
                return r.voltage * r.current;
            }
            return null;
        }).filter(v => v !== null && v !== undefined);

        const card1 = document.getElementById('cardTemp').parentElement;
        card1.onclick = () => toggleTelemetrySeries('voltage');
        card1.classList.toggle('inactive', !telemetryVisibleSeries.power.voltage);
        card1.querySelector('.card-label').textContent = '⚡ Voltage';
        document.getElementById('cardTemp').textContent =
            last.voltage !== null && last.voltage !== undefined ? last.voltage.toFixed(3) + ' V' : '--';
        document.getElementById('cardTemp').style.color = SENSOR_COLORS.voltage;

        if (voltValues.length > 0) {
            document.getElementById('cardTempMin').textContent = Math.min(...voltValues).toFixed(3) + ' V';
            document.getElementById('cardTempMax').textContent = Math.max(...voltValues).toFixed(3) + ' V';
        }
        card1.querySelector('.card-range').style.display = 'flex';

        const card2 = document.getElementById('cardHum').parentElement;
        card2.onclick = () => toggleTelemetrySeries('current');
        card2.classList.toggle('inactive', !telemetryVisibleSeries.power.current);
        card2.querySelector('.card-label').textContent = '🔌 Current';
        document.getElementById('cardHum').textContent =
            last.current !== null && last.current !== undefined ? last.current.toFixed(1) + ' mA' : '--';
        document.getElementById('cardHum').style.color = SENSOR_COLORS.current;

        if (currValues.length > 0) {
            document.getElementById('cardHumMin').textContent = Math.min(...currValues).toFixed(1) + ' mA';
            document.getElementById('cardHumMax').textContent = Math.max(...currValues).toFixed(1) + ' mA';
        }
        card2.querySelector('.card-range').style.display = 'flex';

        const card3 = document.getElementById('cardPress').parentElement;
        card3.onclick = () => toggleTelemetrySeries('power');
        card3.classList.toggle('inactive', !telemetryVisibleSeries.power.power);
        card3.querySelector('.card-label').textContent = '⚡ Power';

        const powerValue =
            last.power !== null && last.power !== undefined
                ? last.power
                : (last.voltage !== null && last.voltage !== undefined && last.current !== null && last.current !== undefined
                    ? last.voltage * last.current
                    : null);

        document.getElementById('cardPress').textContent =
            powerValue !== null && powerValue !== undefined ? (powerValue / 1000).toFixed(3) + ' W' : '--';
        document.getElementById('cardPress').style.color = SENSOR_COLORS.power;

        if (powerValues.length > 0) {
            document.getElementById('cardPressMin').textContent = (Math.min(...powerValues) / 1000).toFixed(3) + ' W';
            document.getElementById('cardPressMax').textContent = (Math.max(...powerValues) / 1000).toFixed(3) + ' W';
            card3.querySelector('.card-range').style.display = 'flex';
        } else {
            document.getElementById('cardPressMin').textContent = '--';
            document.getElementById('cardPressMax').textContent = '--';
            card3.querySelector('.card-range').style.display = 'none';
        }
    }
}

function closeTelemetryModal() {
    const modal = document.getElementById('telemetryModal');
    if (modal) {
        modal.style.display = 'none';
        modal.dataset.type = '';
    }
    if (telemetryChart) {
        telemetryChart.destroy();
        telemetryChart = null;
    }
    telemetryTimeRange = 60;
}

// ============================================================
// CAMERA CONTROL
// ============================================================
async function startCameraStream() {
    if (cameraActive) return;
    cameraActive = true;
    
    console.log('[CAMERA] Starting stream...');
    const img = document.getElementById('videoFeed');
    if (img) {
        img.src = '/video_feed?t=' + Date.now();
        img.style.display = 'block';
    }
    
    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = '🔄 Starting...';
        status.style.color = '#ff9800';
    }
}

function stopCameraStream() {
    if (!cameraActive) return;
    cameraActive = false;
    
    console.log('[CAMERA] Stopping stream...');
    const img = document.getElementById('videoFeed');
    if (img) {
        img.src = '';
        img.style.display = 'none';
    }
    
    const status = document.getElementById('videoStatus');
    if (status) {
        status.textContent = '⏸️ Paused';
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
            
            const liveInfo = document.getElementById('videoLiveInfo');
            if (liveInfo) {
                liveInfo.textContent = `Live: ${data.config.resolution || '640×480'} @ ${data.config.fps || 12} FPS`;
            }
            
            const statusEl = document.getElementById('videoStatus');
            if (statusEl) {
                statusEl.textContent = cameraActive ? '🟢 Online' : '⏸️ Paused';
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

async function updateVideoSettings() {
    const resolution = document.getElementById('videoResolution').value;
    const fps = parseInt(document.getElementById('videoFps').value);
    const quality = parseInt(document.getElementById('videoQuality').value);
    
    const qualityLabel = document.getElementById('videoQualityLabel');
    const liveInfo = document.getElementById('videoLiveInfo');
    
    if (qualityLabel) qualityLabel.textContent = quality + '%';
    if (liveInfo) liveInfo.textContent = `Live: ${resolution.replace('x', '×')} @ ${fps} FPS`;
    
    try {
        const response = await fetch('/api/camera/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resolution, fps, quality })
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast('✅ Video settings updated', 'success');
            if (cameraActive) {
                refreshVideoFeed();
            }
        } else {
            showToast('❌ Failed: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error updating video settings:', error);
        showToast('❌ Network error', 'error');
    }
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
        )
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
            showToast('✅ Image settings updated', 'success');
        }

    } catch (error) {
        console.error(
            'Error updating camera image controls:',
            error
        );

        showToast(
            '❌ Image settings failed: ' + error.message,
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
    const defaults = {
        cameraBrightness: 0.0,
        cameraContrast: 1.0,
        cameraSaturation: 1.0,
        cameraSharpness: 1.0,
        cameraExposure: 0.0
    };

    Object.entries(defaults).forEach(([id, value]) => {
        const element = document.getElementById(id);

        if (element) {
            element.value = value;
        }
    });

    updateCameraControlLabels();
    await updateCameraImageControls(false);

    showToast(
        '✅ Neutral image settings restored',
        'success'
    );
}

async function takeScreenshot(source = 'video') {
    const btn = document.querySelector('.screenshot-btn');
    const originalText = btn.textContent;
    
    try {
        btn.disabled = true;
        btn.textContent = '⏳ Capturing...';
        
        const response = await fetch('/api/camera/screenshot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: source })
        });
        
        const data = await response.json();
        
        if (data.ok) {
            showToast('✅ Screenshot saved', 'success');
        } else {
            showToast('❌ Failed: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error taking screenshot:', error);
        showToast('❌ Network error', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

function stopVideoFeed() {
    const img = document.getElementById('videoFeed');
    if (img) {
        img.removeAttribute('src');
    }
}

function reconnectCameraFeed() {
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

        if (status) {
            status.textContent = '🔄 Applying settings...';
            status.style.color = '#ff9800';
        }

        let freezeFrame = null;

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

                const imageRect = img.getBoundingClientRect();
                const wrapRect = frameWrap.getBoundingClientRect();

                freezeFrame.style.left =
                    `${imageRect.left - wrapRect.left}px`;

                freezeFrame.style.top =
                    `${imageRect.top - wrapRect.top}px`;

                freezeFrame.style.width =
                    `${imageRect.width}px`;

                freezeFrame.style.height =
                    `${imageRect.height}px`;

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
        img.style.visibility = 'hidden';
        img.removeAttribute('src');

        cameraFeedRefreshTimer = setTimeout(() => {
            cameraFeedRefreshTimer = null;

            if (sequence !== cameraFeedRefreshSequence) {
                freezeFrame?.remove();
                resolve();
                return;
            }

            const finishReconnect = (online) => {
                if (sequence !== cameraFeedRefreshSequence) {
                    return;
                }

                img.style.visibility = 'visible';

                if (status) {
                    status.textContent = online
                        ? '🟢 Online'
                        : '🔴 Camera unavailable';

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
    reconnectCameraFeed();
}

// ============================================================
// SWITCH CAMERA MODE
// ============================================================
async function switchCameraMode(mode) {
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
                photoInfo.textContent = `Preview: ${res} (${currentPhotoQuality}%) • Save: ${photoSaveResolution.replace('x', '×')}`;
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
            showToast('❌ Failed to update photo settings', 'error');
            return;
        }

        if (showMessage) {
            showToast(`✅ Photo quality set to ${quality}%`, 'success');
        }

    } catch (error) {
        console.error('Error updating photo settings:', error);
        showToast('❌ Network error', 'error');
    }
}

async function capturePhotoPreview() {
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    
    try {
        if (status) {
            status.textContent = '⏳ Capturing preview...';
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
                status.textContent = `📷 Preview ready (${res.replace('x', '×')}, ${quality}%)`;
                status.style.color = '#2e7d32';
            }
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = '💾 Save';
            }
            currentPhotoData = data.image_data;
        } else {
            console.error('[PHOTO] Failed:', data.error);
            if (status) {
                status.textContent = '❌ Failed: ' + (data.error || 'Unknown error');
                status.style.color = '#c62828';
            }
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.textContent = '💾 Save';
            }
            if (placeholder) placeholder.style.display = 'flex';
        }
    } catch (error) {
        console.error('[PHOTO] Error:', error);
        if (status) {
            status.textContent = '❌ Network error';
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.textContent = '💾 Save';
        }
        if (placeholder) placeholder.style.display = 'flex';
    }
}

async function captureCameraPhoto() {
    const btn = document.querySelector('.camera-actions-block .screenshot-btn');
    const videoFeed = document.getElementById('videoFeed');

    try {
        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ Capture...';
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
            showToast(`✅ Captured: ${data.display_name || data.filename}`, 'success');
        } else {
            showToast('❌ Capture failed: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        console.error('Capture error:', error);
        showToast('❌ Network error', 'error');

    } finally {

    if (videoFeed) {
        videoFeed.classList.remove('camera-capturing');
    }

    setTimeout(() => {
        refreshVideoFeed();
    }, 1200);

    if (btn) {
        btn.disabled = false;
        btn.textContent = '📸 Capture';
        }
    }
}

async function savePhoto() {
    const display = document.getElementById('photoDisplay');
    const status = document.getElementById('photoStatus');
    const saveBtn = document.getElementById('photoSaveBtn');
    
    if (!display || display.style.display === 'none' || !currentPhotoData) {
        showToast('❌ No photo to save. Capture preview first!', 'error');
        return;
    }
    
    try {
        if (status) {
            status.textContent = '⏳ Capturing high-res photo...';
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
                status.textContent = '✅ Saved!';
                status.style.color = '#2e7d32';
            }
            showToast(`✅ Photo saved: ${data.filename} (${(data.size/1024).toFixed(1)} KB)`, 'success');
            
            setTimeout(() => {
                if (status) {
                    const res = photoPreviewResolution.replace('x', '×');
                    status.textContent = `📷 Preview ready (${res}, ${currentPhotoQuality}%)`;
                    status.style.color = '#2e7d32';
                }
                if (saveBtn) {
                    saveBtn.disabled = false;
                    saveBtn.textContent = '💾 Save';
                }
            }, 2000);
        } else {
            if (status) {
                status.textContent = '❌ Save failed';
                status.style.color = '#c62828';
            }
            showToast('❌ Failed to save: ' + (data.error || 'Unknown error'), 'error');
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = '💾 Save';
            }
        }
    } catch (error) {
        console.error('Error saving photo:', error);
        if (status) {
            status.textContent = '❌ Network error';
            status.style.color = '#c62828';
        }
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = '💾 Save';
        }
        showToast('❌ Network error', 'error');
    }
}

function refreshPhoto() {
    const status = document.getElementById('photoStatus');
    const display = document.getElementById('photoDisplay');
    const placeholder = document.getElementById('photoPlaceholder');
    
    if (status) {
        status.textContent = '⏳ Capturing...';
        status.style.color = '#ff9800';
    }
    if (display) display.style.display = 'none';
    if (placeholder) placeholder.style.display = 'flex';
    
    switchCameraMode('photo').then(() => {
        setTimeout(() => capturePhotoPreview(), 300);
    });
}

// ============================================================
// SCREENSHOTS GALLERY
// ============================================================
async function showScreenshots() {
    const modal = document.getElementById('screenshotsModal');
    const grid = document.getElementById('screenshotsGrid');

    if (!modal || !grid) return;

    modal.style.display = 'flex';
    grid.innerHTML = '<div class="loading">🖼️ Loading gallery...</div>';

    try {
        const response = await fetch('/api/camera/screenshots');
        const data = await response.json();
        const screenshots = data.screenshots || [];
        const storage = data.storage || {};
        const storageText = storage.free_gb !== undefined
            ? `Storage: ${storage.images || screenshots.length} images • ${storage.used_mb || 0} MB used • ${storage.free_gb} GB free`
            : `Storage: ${screenshots.length} images`;

        if (screenshots.length === 0) {
            grid.innerHTML = '<div class="loading">📭 No images yet</div>';
            return;
        }

        grid.innerHTML = `

            <div class="gallery-toolbar">

                <div class="gallery-info">

                    <div class="gallery-info-main">
                        📷 ${screenshots.length} images
                    </div>

                    <div class="gallery-info-sub">
                        💾 ${storage.used_mb || 0} MB used
                        &nbsp;&nbsp;•&nbsp;&nbsp;
                        🖥 ${storage.free_gb || 0} GB free
                        &nbsp;&nbsp;•&nbsp;&nbsp;
                        Newest first
                    </div>

                </div>

                <button class="gallery-delete-all-btn"
                        onclick="deleteAllScreenshots()">
                    🗑 Delete All
                </button>

            </div>

            ${screenshots.map(item => `
                <div class="screenshot-item" id="screenshot-${CSS.escape(item.filename)}">
                    <a href="${item.url}" target="_blank" rel="noopener">
                        <img src="${item.url}"
                             alt="${item.display_name || item.filename}"
                             title="${item.display_name || item.filename}"
                             onerror="this.style.display='none'; this.parentElement.parentElement.querySelector('.screenshot-error').style.display='block'">
                    </a>

                    <div class="screenshot-error" style="display:none;padding:20px;text-align:center;color:#999;">
                        ⚠️ Cannot load image
                    </div>

                    <div class="screenshot-info">
                        <div>
                            <div class="screenshot-time">${item.modified}</div>
                            <div class="screenshot-name">${item.display_name || item.filename.split('/').pop()}</div>
                        </div>

                        <div class="screenshot-actions">
                            <a class="screenshot-download-btn"
                            href="${item.url}"
                            download="${item.display_name || item.filename.split('/').pop()}"
                            title="Download">⬇</a>

                            <span class="screenshot-size">${(item.size / 1024).toFixed(1)} KB</span>

                            <button class="screenshot-delete-btn"
                                    onclick="deleteScreenshot('${item.filename}', event)"
                                    title="Delete">🗑</button>
                        </div>
                    </div>
                </div>
            `).join('')}
        `;

    } catch (error) {
        console.error('Error loading screenshots:', error);
        grid.innerHTML = `
            <div class="loading" style="color:#c62828;">
                ⚠️ Network error loading gallery<br>
                <button onclick="showScreenshots()" style="margin-top:8px;padding:6px 14px;border:none;border-radius:6px;background:#1a73e8;color:white;cursor:pointer;">
                    🔄 Retry
                </button>
            </div>
        `;
    }
}

async function deleteScreenshot(filename, event) {
    if (event) {
        event.stopPropagation();
    }

    if (!confirm(`Delete screenshot "${filename}"?`)) {
        return;
    }

    try {
        const response = await fetch(`/api/camera/screenshot/${filename}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            showToast('✅ Screenshot deleted', 'success');
            await showScreenshots();
        } else {
            showToast('❌ Failed to delete: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        console.error('Error deleting screenshot:', error);
        showToast('❌ Network error', 'error');
    }
}

async function deleteAllScreenshots() {
    if (!confirm('⚠️ Delete ALL screenshots?\n\nThis action cannot be undone!')) {
        return;
    }
    
    if (!confirm('Are you sure? All screenshots will be permanently deleted!')) {
        return;
    }
    
    try {
        const response = await fetch('/api/camera/screenshots', {
            method: 'DELETE'
        });
        
        if (response.ok) {
            showToast('✅ All screenshots deleted', 'success');
            const grid = document.getElementById('screenshotsGrid');
            if (grid) {
                grid.innerHTML = '<div class="loading">📭 No screenshots yet</div>';
            }
        } else {
            const data = await response.json();
            showToast('❌ Failed: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error deleting all screenshots:', error);
        showToast('❌ Network error', 'error');
    }
}

function closeScreenshots() {
    const modal = document.getElementById('screenshotsModal');
    if (modal) modal.style.display = 'none';
}

// ============================================================
// SWITCH MAIN TAB
// ============================================================
function switchMainTab(tab) {

//    if (radioHealthTimer) {
//        clearInterval(radioHealthTimer);
//        radioHealthTimer = null;
//    }

    if (tab === 'chats' && contextChatMode) {
        contextChatMode = false;
        contextBaseTab = null;
        document.body.classList.remove('context-chat-mode');

        document.getElementById('videoView').style.display = 'none';
        document.getElementById('systemView').style.display = 'none';
        document.getElementById('settingsView').style.display = 'none';

        document.getElementById('chatListContainer').style.display = currentChatId ? 'none' : 'block';
        document.getElementById('messagesView').style.display = currentChatId ? 'flex' : 'none';
        document.getElementById('chatHeader').style.display = currentChatId ? 'flex' : 'none';

        document.querySelectorAll('.main-content-tab').forEach(t => t.classList.remove('active'));
        document.getElementById('mainTabChats')?.classList.add('active');

        updateDockForTab?.('chats');
        return;
    }

    currentMainTab = tab;

    document.querySelectorAll('.main-content-tab').forEach(btn => {
        btn.classList.toggle(
            'active',
            btn.id === 'mainTab' + tab.charAt(0).toUpperCase() + tab.slice(1)
        );
    });

    const messagesView = document.getElementById('messagesView');
    const videoView = document.getElementById('videoView');
    const photoView = document.getElementById('photoView');
    const chatHeader = document.getElementById('chatHeader');
    const chatListContainer = document.getElementById('chatListContainer');
    const systemView = document.getElementById('systemView');    
    const settingsView = document.getElementById('settingsView');

    if (messagesView) messagesView.style.display = 'none';
    if (videoView) videoView.style.display = 'none';
    if (photoView) photoView.style.display = 'none';
    if (systemView) systemView.style.display = 'none';
    if (settingsView) settingsView.style.display = 'none';

    if (tab !== 'video') {
        stopVideoFeed();
    }

    if (tab === 'chats') {
        if (chatHeader) chatHeader.style.display = currentChatId ? 'flex' : 'none';

        if (currentChatId) {
            if (chatListContainer) chatListContainer.style.display = 'none';
            if (messagesView) messagesView.style.display = 'flex';
            startMessagePolling(currentChatId);
        } else {
            if (chatListContainer) chatListContainer.style.display = 'block';
            if (messagesView) messagesView.style.display = 'none';
            stopMessagePolling();
        }

        loadChatList();
        loadMessages();
        updateStatusDock('chats');

        if (!isInitialized) {
            switchCameraMode('video');
        }

    } else if (tab === 'video') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (videoView) videoView.style.display = 'flex';

        updateStatusDock('video');
        stopMessagePolling();

        switchCameraMode('video').then(() => {
            setTimeout(() => loadVideoSettings(), 100);
            setTimeout(() => loadPhotoSettings(), 150);
            setTimeout(() => refreshVideoFeed(), 200);
        });

    } else if (tab === 'photo') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (photoView) photoView.style.display = 'flex';

        updateStatusDock('photo');
        stopMessagePolling();

        switchCameraMode('photo').then(() => {
            setTimeout(() => loadPhotoSettings(), 100);
            setTimeout(() => capturePhotoPreview(), 300);
        });

    } else if (tab === 'system') {
        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';
        if (systemView) systemView.style.display = 'flex';

        loadSystemNetwork();
        loadSystemInfo();
        loadRadioHealth();

    } else if (tab === 'settings') {
        const btn = document.getElementById('mainTabSettings');
        if (btn) btn.classList.add('active');

        if (settingsView) settingsView.style.display = 'flex';

        if (chatHeader) chatHeader.style.display = 'none';
        if (chatListContainer) chatListContainer.style.display = 'none';
        if (messagesView) messagesView.style.display = 'none';

        updateStatusDock('settings');
    }
}

function updateStatusDock(tab) {
    const left = document.getElementById('dockLeft');
    const centerText = document.getElementById('dockStatusText');
    const right = document.getElementById('dockContextText');

    if (!left || !centerText || !right) return;

    if (tab === 'chats') {
        left.innerHTML = '💬 Chats';
        centerText.textContent = 'Mesh Online';
        right.textContent = 'Nodes';
    } else if (tab === 'video') {
        left.innerHTML = '📷 Camera';
        centerText.textContent = 'Camera Online';
        right.textContent = getCurrentVideoInfoText();
    } else if (tab === 'settings') {
        left.innerHTML = '⚙️ Settings';
        centerText.textContent = 'Ready';
        right.textContent = 'MeshCenter';
    } else {
        left.innerHTML = 'Workspace';
        centerText.textContent = 'Ready';
        right.textContent = 'MeshCenter';
    }
}

function getCurrentVideoInfoText() {
    const info = document.getElementById('videoLiveInfo');
    return info ? info.textContent.replace('Live: ', '') : 'Camera';
}

function syncVideoControlsToDock() {
    const srcRes = document.getElementById('videoResolution');
    const srcFps = document.getElementById('videoFps');
    const srcQuality = document.getElementById('videoQuality');

    const dockRes = document.getElementById('dockVideoResolution');
    const dockFps = document.getElementById('dockVideoFps');
    const dockQuality = document.getElementById('dockVideoQuality');
    const dockQualityLabel = document.getElementById('dockVideoQualityLabel');

    if (srcRes && dockRes) dockRes.value = srcRes.value;
    if (srcFps && dockFps) dockFps.value = srcFps.value;
    if (srcQuality && dockQuality) {
        dockQuality.value = srcQuality.value;
        if (dockQualityLabel) dockQualityLabel.textContent = srcQuality.value + '%';
    }
}

function syncDockVideoSettings() {
    const dockRes = document.getElementById('dockVideoResolution');
    const dockFps = document.getElementById('dockVideoFps');
    const dockQuality = document.getElementById('dockVideoQuality');
    const dockQualityLabel = document.getElementById('dockVideoQualityLabel');

    const srcRes = document.getElementById('videoResolution');
    const srcFps = document.getElementById('videoFps');
    const srcQuality = document.getElementById('videoQuality');

    if (dockRes && srcRes) srcRes.value = dockRes.value;
    if (dockFps && srcFps) srcFps.value = dockFps.value;
    if (dockQuality && srcQuality) {
        srcQuality.value = dockQuality.value;
        if (dockQualityLabel) dockQualityLabel.textContent = dockQuality.value + '%';
    }

    updateVideoSettings();
}

async function loadSystemNetwork() {
    try {
        const response = await fetch('/api/system/network');
        const data = await response.json();

        document.getElementById('systemWifiSsid').textContent = data.ssid || '--';

        document.getElementById('systemWifiSignal').textContent =
            data.signal_percent !== null && data.signal_percent !== undefined
                ? `${data.signal_percent}%`
                : '--';

        document.getElementById('systemWifiRssi').textContent =
            data.rssi_dbm !== null && data.rssi_dbm !== undefined
                ? `${data.rssi_dbm} dBm`
                : '--';

        // <<< ДОБАВИТЬ ЭТИ СТРОКИ >>>

        document.getElementById('systemRxRate').textContent =
            data.rx_bitrate || '--';

        document.getElementById('systemTxRate').textContent =
            data.tx_bitrate || '--';

        // <<< ДО КОНЦА >>>

        document.getElementById('systemWifiIp').textContent = data.ip || '--';
        document.getElementById('systemWifiGateway').textContent = data.gateway || '--';

        document.getElementById('systemInternet').textContent =
            data.internet ? '🟢 Connected' : '🔴 Radio Offline';

    } catch (error) {
        console.error('System network load error:', error);
        showToast('❌ Failed to load system network info', 'error');
    }
}

async function toggleWifiNetworks() {
    const panel = document.getElementById("wifiNetworksPanel");

    if (!panel) return;

    if (panel.style.display === "none") {
        panel.style.display = "block";
        await loadWifiNetworks();
    } else {
        panel.style.display = "none";
    }
}

async function loadWifiNetworks() {

    const list = document.getElementById("wifiNetworksList");

    list.innerHTML = "Scanning...";

    try {

        const response = await fetch("/api/system/wifi/scan");

        const data = await response.json();

        if (!data.ok) {
            list.innerHTML = "Scan failed";
            return;
        }

        if (data.networks.length === 0) {
            list.innerHTML = "No networks found";
            return;
        }

        list.innerHTML = "";

        data.networks.forEach(net => {

            const div = document.createElement("div");

            div.className = "wifi-network-item";

        const actionHtml = net.connected
            ? '<span class="wifi-connected">Connected</span>'
            : `
                <div class="wifi-actions">
                    <button class="wifi-connect-btn" data-ssid="${escapeHtml(net.ssid)}" data-saved="${net.saved ? '1' : '0'}">
                        Connect
                    </button>
                    ${net.saved ? `<button class="wifi-forget-btn" data-ssid="${escapeHtml(net.ssid)}">Forget</button>` : ''}
                </div>
            `;

        div.innerHTML = `
            <div class="wifi-name">
                ${net.connected ? "🟢" : "⚪"} ${net.ssid}
                ${net.saved && !net.connected ? '<span class="wifi-saved-badge">Saved</span>' : ''}
            </div>

            <div class="wifi-info">
                <span>${net.signal ?? '--'}%</span>
                <span>${net.signal_dbm ?? '--'} dBm</span>
                <span>${net.security || 'Open'}</span>
                ${actionHtml}
            </div>
        `;

            list.appendChild(div);

        });

        document.querySelectorAll('.wifi-connect-btn').forEach(btn => {
            btn.onclick = () => {
                const ssid = btn.dataset.ssid;
                const saved = btn.dataset.saved === '1';

                if (saved) {
                    connectWifi(ssid, '');
                } else {
                    openWifiConnectModal(ssid);
                }
            };
        });

        document.querySelectorAll('.wifi-forget-btn').forEach(btn => {
            btn.onclick = () => {
                forgetWifi(btn.dataset.ssid);
            };
        });

    } catch(e){

        console.error(e);

        list.innerHTML="Scan error";

    }

}

async function connectWifi(ssid, password) {
    try {
        showToast(`📶 Connecting to ${ssid}...`, 'success');

        const response = await fetch('/api/system/wifi/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid, password })
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            showToast(`✅ Connected to ${ssid}`, 'success');

            setTimeout(() => {
                loadSystemNetwork();
                loadWifiNetworks();
            }, 2500);
        } else {
            showToast('❌ Wi-Fi connect failed: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        console.error('Wi-Fi connect error:', error);
        showToast('❌ Wi-Fi connect network error', 'error');
    }
}

async function forgetWifi(ssid) {
    if (!confirm(`Forget Wi-Fi network "${ssid}"?`)) return;

    try {
        const response = await fetch('/api/system/wifi/forget', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid })
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            showToast(`🗑️ Forgotten: ${ssid}`, 'success');
            loadWifiNetworks();
        } else {
            showToast('❌ Forget failed: ' + (data.error || 'Unknown error'), 'error');
        }

    } catch (error) {
        console.error('Wi-Fi forget error:', error);
        showToast('❌ Wi-Fi forget network error', 'error');
    }
}

function escapeHtml(text) {
    return String(text)
        .replaceAll('&', '&amp;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
}

function exitSplitView() {
    const chatList = document.getElementById('chatListContainer');
    const messagesView = document.getElementById('messagesView');
    const videoView = document.getElementById('videoView');
    const systemView = document.getElementById('systemView');
    const settingsView = document.getElementById('settingsView');

    if (chatList) chatList.style.display = 'flex';
    if (messagesView) messagesView.style.display = 'none';
    if (videoView) videoView.style.display = 'none';
    if (systemView) systemView.style.display = 'none';
    if (settingsView) settingsView.style.display = 'none';

    document.querySelectorAll('.main-content-tab').forEach(tab => {
        tab.classList.remove('active');
    });

    document.getElementById('mainTabChats')?.classList.add('active');

    updateDockForTab?.('chats');
}

// ============================================================
// INIT
// ============================================================
async function init() {
    if (isInitialized) return;
    isInitialized = true;
    
    console.log('[INIT] Starting application...');
    
    await loadSettings();
    
    const statusEl = document.getElementById('statusText');
    if (statusEl) statusEl.innerHTML = '⏳ Loading...';
    
    try {
        // Загружаем настройки из localStorage
        const savedShowIgnored = localStorage.getItem('mesh_show_ignored');
        if (savedShowIgnored === 'true') {
            showIgnored = true;
            const checkbox = document.getElementById('showIgnoredToggle');
            if (checkbox) checkbox.checked = true;
        }
        
        const savedShowFavorites = localStorage.getItem('mesh_show_favorites');
        if (savedShowFavorites === 'true') {
            showFavorites = true;
            const checkbox = document.getElementById('showFavoritesToggle');
            if (checkbox) checkbox.checked = true;
        }
        
        // Загружаем все данные параллельно с таймаутами
        console.log('[INIT] Loading data in parallel...');
        
        // Загружаем чаты в первую очередь (самое важное)
        await loadChatList();
        
        // Остальное загружаем параллельно
        await Promise.allSettled([
            loadTelemetry(),
            loadBaseStatus(),
            loadSensors(),
            loadMessages()
        ]).then(results => {
            const names = ['Telemetry', 'BaseStatus', 'Sensors', 'Messages'];
            results.forEach((result, index) => {
                if (result.status === 'fulfilled') {
                    console.log(`[INIT] ${names[index]} loaded`);
                } else {
                    console.warn(`[INIT] ${names[index]} failed:`, result.reason);
                }
            });
        });
        
        // Переключаемся на вкладку чатов
        console.log('[INIT] Switching to chats tab...');
        switchMainTab('chats');
        
        if (statusEl) statusEl.innerHTML = '🟢 Mesh online';

        await loadRadioHealth();

        if (!radioHealthTimer) {
            radioHealthTimer = setInterval(loadRadioHealth, 5000);
        }

        console.log('[INIT] Application ready');
        
    } catch (error) {
        console.error('[INIT] Critical error:', error);
        const statusEl = document.getElementById('statusText');
        if (statusEl) statusEl.innerHTML = '🔴 Error loading - refresh page';
        
        const chatList = document.getElementById('chatList');
        if (chatList) {
            chatList.innerHTML = `
                <div class="loading" style="color:#c62828;">
                    ⚠️ Failed to load data<br>
                    <small style="font-size:12px;color:#999;">${error.message || 'Unknown error'}</small>
                    <br><br>
                    <button onclick="window.location.reload()" style="padding:8px 20px;border:none;border-radius:8px;background:#1a73e8;color:white;cursor:pointer;">
                        🔄 Refresh Page
                    </button>
                </div>
            `;
        }
    }
    
    // Периодические обновления
    setInterval(async () => {
        try {
            await Promise.all([
                loadMessages(),
                loadChatList(),
                loadSensors(),
                loadBaseStatus()
            ]);
        } catch (e) {
            console.error('Polling error:', e);
        }
    }, 10000);
    
    setInterval(loadTelemetry, 30000);

    const input = document.getElementById('messageInput');
    if (input) input.focus();
}

let selectedWifiSsid = null;

function openWifiConnectModal(ssid) {
    selectedWifiSsid = ssid;

    const modal = document.getElementById('wifiConnectModal');
    const ssidEl = document.getElementById('wifiConnectSsid');
    const passEl = document.getElementById('wifiConnectPassword');

    if (ssidEl) ssidEl.textContent = ssid;
    if (passEl) passEl.value = '';

    if (modal) modal.style.display = 'flex';
}

function closeWifiConnectModal() {
    const modal = document.getElementById('wifiConnectModal');
    if (modal) modal.style.display = 'none';
}

function toggleWifiPasswordVisible(cb) {
    const input = document.getElementById('wifiConnectPassword');
    if (input) input.type = cb.checked ? 'text' : 'password';
}

async function connectSelectedWifi() {
    const password = document.getElementById('wifiConnectPassword')?.value || '';

    if (!selectedWifiSsid) {
        showToast('❌ No Wi-Fi selected', 'error');
        return;
    }

    await connectWifi(selectedWifiSsid, password);
    closeWifiConnectModal();
}

async function loadSystemInfo() {
    try {
        const response = await fetch('/api/system/info');
        const data = await response.json();

        document.getElementById('systemHostname').textContent = data.hostname || '--';
        document.getElementById('systemUptime').textContent = data.uptime || '--';

        document.getElementById('systemCpuTemp').textContent =
            data.cpu_temp !== null && data.cpu_temp !== undefined
                ? `${data.cpu_temp}°C`
                : '--';

        document.getElementById('systemCpuLoad').textContent =
            data.load_avg !== null && data.load_avg !== undefined
                ? data.load_avg.toFixed(2)
                : '--';

        document.getElementById('systemRam').textContent =
            data.ram_used_mb !== null && data.ram_total_mb !== null
                ? `${data.ram_used_mb} / ${data.ram_total_mb} MB`
                : '--';

        document.getElementById('systemDisk').textContent =
            data.disk_used_gb !== null && data.disk_total_gb !== null
                ? `${data.disk_used_gb} / ${data.disk_total_gb} GB`
                : '--';

        document.getElementById('systemModel').textContent = data.model || '--';
        document.getElementById('systemOs').textContent = data.os || '--';
        document.getElementById('systemKernel').textContent = data.kernel || '--';

    } catch (error) {
        console.error('System info load error:', error);
        showToast('❌ Failed to load system info', 'error');
    }
}

function updateHeaderNodeStatus(data, reachable = true) {
    const headerStatus = document.getElementById('headerStatusText');
    if (!headerStatus) return;

    const labelEl = headerStatus.querySelector('.status-label');

    const status = String(data?.status || '').toUpperCase();
    const level = String(data?.level || '').toUpperCase();
    const listenerRunning = Boolean(data?.listener_running);

    let label = 'Disconnected';
    let stateClass = 'status-offline';

    if (!reachable) {
        label = 'Disconnected';
        stateClass = 'status-offline';

    } else if (!listenerRunning || status === 'LISTENER_DOWN') {
        label = 'Offline';
        stateClass = 'status-error';

    } else if (status === 'STARTING') {
        label = 'Starting';
        stateClass = 'status-warning';

    } else if (status === 'PAUSED') {
        label = 'Paused';
        stateClass = 'status-warning';

    } else if (status === 'NO_PACKETS') {
        label = 'No Signal';
        stateClass = 'status-error';

    } else if (status === 'IDLE' || level === 'WARNING') {
        label = status === 'IDLE' ? 'Idle' : 'Warning';
        stateClass = 'status-warning';

    } else if (level === 'ERROR') {
        label = 'Error';
        stateClass = 'status-error';

    } else if (level === 'OK' || status === 'OK') {
        label = 'Online';
        stateClass = 'status-ok';

    } else {
        label = status ? status.replaceAll('_', ' ') : 'Unknown';
        stateClass = 'status-warning';
    }

    headerStatus.classList.remove(
        'status-connecting',
        'status-ok',
        'status-warning',
        'status-error',
        'status-offline'
    );

    headerStatus.classList.add(stateClass);

    if (labelEl) {
        labelEl.textContent = label;
    }

    const packetText =
        data?.packet_age == null
            ? 'never'
            : `${data.packet_age} s ago`;

    const listenerText =
        listenerRunning
            ? 'running'
            : 'stopped';

    const reason =
        data?.status_reason ||
        data?.recommendation ||
        '';

    headerStatus.title = reachable
        ? `Radio: ${label} | Listener: ${listenerText} | Last packet: ${packetText}${reason ? ` | ${reason}` : ''} | Click to open System`
        : 'MeshCenter status API is unavailable. Click to open System';

    headerStatus.setAttribute(
        'aria-label',
        `${label}. Open System status`
    );
}

async function loadRadioHealth() {
    try {
        const healthResponse = await fetch('/api/radio_health', {
            cache: 'no-store'
        });

        if (!healthResponse.ok) {
            throw new Error(`Radio health HTTP ${healthResponse.status}`);
        }

        const data = await healthResponse.json();

        updateHeaderNodeStatus(data, true);

        let logData = {
            events: []
        };

        try {
            const logResponse = await fetch(
                '/api/system/log?limit=100',
                {
                    cache: 'no-store'
                }
            );

            if (logResponse.ok) {
                logData = await logResponse.json();
            }

        } catch (logError) {
            console.warn(
                'System log load error:',
                logError
            );
        }

        const statusEl = document.getElementById('radioHealthStatus');
        const levelEl = document.getElementById('radioHealthLevel');
        const listenerEl = document.getElementById('radioHealthListener');
        const packetEl = document.getElementById('radioHealthPacket');
        const telemetryEl = document.getElementById('radioHealthTelemetry');
        const sendEl = document.getElementById('radioHealthSend');
        const restartEl = document.getElementById('radioHealthRestart');
        const recommendationEl = document.getElementById('radioHealthRecommendation');
        const restartBtn = document.getElementById('restartListenerBtn');
        const historyEl = document.getElementById('radioHealthHistory');

        if (statusEl) statusEl.textContent = data.status || '--';

        const level = String(data.level || 'UNKNOWN').toUpperCase();
        let levelIcon = '⚪';
        let levelColor = '#777';

        if (level === 'OK') {
            levelIcon = '🟢';
            levelColor = '#249448';
        } else if (level === 'WARNING') {
            levelIcon = '🟡';
            levelColor = '#a66d00';
        } else if (level === 'ERROR') {
            levelIcon = '🔴';
            levelColor = '#c62828';
        }

        if (levelEl) {
            levelEl.textContent = `${levelIcon} ${level}`;
            levelEl.style.color = levelColor;
        }

        if (listenerEl) {
            listenerEl.textContent = data.listener_running ? '🟢 Running' : '🔴 Stopped';
        }

        if (packetEl) packetEl.textContent = data.packet_age == null ? 'Never' : `${data.packet_age} s ago`;
        if (telemetryEl) telemetryEl.textContent = data.telemetry_age == null ? 'Never' : `${data.telemetry_age} s ago`;
        if (sendEl) sendEl.textContent = data.send_age == null ? 'Never' : `${data.send_age} s ago`;
        if (restartEl) restartEl.textContent = data.restart_count ?? 0;

        if (recommendationEl) {
            recommendationEl.textContent = data.recommendation || data.status_reason || '--';
            recommendationEl.style.color = levelColor;
        }

        if (restartBtn) {
            restartBtn.disabled = false;
            restartBtn.textContent = '🔄 Restart Listener';
        }

        if (historyEl) {
            const history = Array.isArray(logData.events) ? logData.events.slice().reverse() : [];

            if (!history.length) {
                historyEl.innerHTML = '<div class="radio-history-empty">No events yet</div>';
            } else {
                historyEl.innerHTML = history.map(item => {
                    const itemLevel = String(item.level || 'INFO').toUpperCase();
                    let icon = '🔵';
                    let color = '#3974b9';

                    if (itemLevel === 'OK') {
                        icon = '🟢';
                        color = '#249448';
                    } else if (itemLevel === 'WARNING') {
                        icon = '🟡';
                        color = '#a66d00';
                    } else if (itemLevel === 'ERROR') {
                        icon = '🔴';
                        color = '#c62828';
                    } else if (itemLevel === 'ACTION') {
                        icon = '🟣';
                        color = '#7652a8';
                    }

                    const dateTime = item.date && item.time
                        ? `${item.date} ${item.time}`
                        : (item.datetime || item.time || '--');

                    const details = item.details
                        ? `<div class="radio-history-details">${escapeHtml(item.details)}</div>`
                        : '';

                    return `
                        <div class="radio-history-item">
                            <div class="radio-history-line">
                                <span class="radio-history-time">${escapeHtml(dateTime)}</span>
                                <span>${icon}</span>
                                <span class="radio-history-event" style="color:${color};">
                                    ${escapeHtml(item.event || 'Event')}
                                </span>
                                <span class="radio-history-source">${escapeHtml(item.source || 'system')}</span>
                            </div>
                            ${details}
                        </div>
                    `;
                }).join('');
            }
        }
    } catch (error) {
        updateHeaderNodeStatus(null, false);
        console.error('Radio health load error:', error);
    }
}

async function runSystemAction(action, button) {
    const config = {
        restart_meshcenter: {
            confirm: 'Restart MeshCenter service?\n\nThe web interface will be unavailable for a few seconds.',
            pending: 'Restarting MeshCenter...',
            success: 'MeshCenter restart requested.'
        },
        reboot: {
            confirm: 'Restart Raspberry Pi?\n\nMeshCenter and the radio connection will be temporarily unavailable.',
            pending: 'Restarting Raspberry Pi...',
            success: 'Raspberry Pi restart requested.'
        },
        shutdown: {
            confirm: 'Shut down Raspberry Pi?\n\nThe device must be powered on manually afterwards.',
            pending: 'Shutting down Raspberry Pi...',
            success: 'Raspberry Pi shutdown requested.'
        }
    };

    const selected = config[action];
    if (!selected || !confirm(selected.confirm)) return;

    const originalText = button?.textContent || '';
    if (button) {
        button.disabled = true;
        button.textContent = selected.pending;
    }

    try {
        const response = await fetch('/api/system/action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action })
        });
        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        showToast(`✅ ${selected.success}`, 'success');
        setTimeout(loadRadioHealth, 500);
    } catch (error) {
        showToast(`❌ ${error.message}`, 'error');
        if (button) {
            button.disabled = false;
            button.textContent = originalText;
        }
    }
}

function toggleRadioHealthHistory() {
    const panel = document.getElementById('radioHealthHistoryPanel');
    const arrow = document.getElementById('radioHistoryArrow');
    const button = document.getElementById('radioHistoryToggle');

    if (!panel) return;

    const opening = panel.style.display === 'none';

    panel.style.display = opening ? 'block' : 'none';

    if (arrow) {
        arrow.textContent = opening ? '▴' : '▾';
    }

    if (button) {
        button.setAttribute('aria-expanded', opening ? 'true' : 'false');
    }
}

// ============================================================
// ЭКСПОРТ В ГЛОБАЛЬНУЮ ОБЛАСТЬ
// ============================================================
window.loadChatList = loadChatList;
window.loadMessages = loadMessages;
window.openChat = openChat;
window.showChatList = showChatList;
window.toggleIgnore = toggleIgnore;
window.toggleFavorite = toggleFavorite;
window.selectNode = selectNode;
window.clearNodeSearch = clearNodeSearch;
window.rescanNodes = rescanNodes;
window.restartListener = restartListener;
window.mergeDuplicates = mergeDuplicates;
window.cleanupAllNodes = cleanupAllNodes;
window.toggleShowDuplicates = toggleShowDuplicates;
window.showExportOptions = showExportOptions;
window.showImportOptions = showImportOptions;
window.closeFormatMenus = closeFormatMenus;
window.exportNodesCSV = exportNodesCSV;
window.exportNodesJSON = exportNodesJSON;
window.importNodesCSV = importNodesCSV;
window.importNodesJSON = importNodesJSON;
window.showScreenshots = showScreenshots;
window.deleteScreenshot = deleteScreenshot;
window.deleteAllScreenshots = deleteAllScreenshots;
window.closeScreenshots = closeScreenshots;
window.switchMainTab = switchMainTab;
window.switchSidebarTab = switchSidebarTab;
window.refreshVideoFeed = refreshVideoFeed;
window.updateVideoSettings = updateVideoSettings;
window.takeScreenshot = takeScreenshot;
window.loadPhotoSettings = loadPhotoSettings;
window.updatePhotoSettings = updatePhotoSettings;
window.capturePhotoPreview = capturePhotoPreview;
window.savePhoto = savePhoto;
window.refreshPhoto = refreshPhoto;
window.showChatActions = showChatActions;
window.deleteCurrentChat = deleteCurrentChat;
window.clearCurrentChat = clearCurrentChat;
window.deleteAllDmChats = deleteAllDmChats;
window.executeDeleteChat = executeDeleteChat;
window.executeClearChat = executeClearChat;
window.executeDeleteAllDm = executeDeleteAllDm;
window.closeChatActions = closeChatActions;
window.closeConfirmDelete = closeConfirmDelete;
window.closeConfirmClear = closeConfirmClear;
window.closeDeleteAllDmModal = closeDeleteAllDmModal;
window.openTelemetryModal = openTelemetryModal;
window.closeTelemetryModal = closeTelemetryModal;
window.setTelemetryRange = setTelemetryRange;
window.updateTelemetryConfig = updateTelemetryConfig;
window.switchCameraMode = switchCameraMode;
window.startCameraStream = startCameraStream;
window.stopCameraStream = stopCameraStream;
window.loadSensors = loadSensors;
window.loadBaseStatus = loadBaseStatus;
window.loadTelemetry = loadTelemetry;
window.loadSettings = loadSettings;
window.setUnitSetting = setUnitSetting;
window.exportTelemetryData = exportTelemetryData;
window.closeTelemetryExportMenu = closeTelemetryExportMenu;
window.downloadTelemetryExport = downloadTelemetryExport;
window.toggleTelemetrySeries = toggleTelemetrySeries;
window.openCustomTelemetryExport = openCustomTelemetryExport;
window.closeCustomTelemetryExport = closeCustomTelemetryExport;
window.updateCustomExportMode = updateCustomExportMode;
window.runCustomTelemetryExport = runCustomTelemetryExport;
window.updateStatusDock = updateStatusDock;
window.syncDockVideoSettings = syncDockVideoSettings;
window.loadSystemNetwork = loadSystemNetwork;
window.openWifiConnectModal = openWifiConnectModal;
window.closeWifiConnectModal = closeWifiConnectModal;
window.toggleWifiPasswordVisible = toggleWifiPasswordVisible;
window.connectSelectedWifi = connectSelectedWifi;
window.loadSystemInfo = loadSystemInfo;
window.loadSystemInfo = loadSystemInfo;
window.exitSplitView = exitSplitView;
window.toggleRadioHealthHistory = toggleRadioHealthHistory;
window.runSystemAction = runSystemAction;
window.restartListener = restartListener;
window.updateCameraControlLabels = updateCameraControlLabels;
window.updateCameraImageControls = updateCameraImageControls;
window.restoreCameraImageDefaults = restoreCameraImageDefaults;
window.getAppSettings = function() {
    return appSettings;
};

// Экспортируем переменные
window.chatListCache = chatListCache;
window.currentChatId = currentChatId;
window.nodeCache = nodeCache;

console.log('[EXPORT] Все функции экспортированы в window');

// ============================================================
// ЗАПУСК
// ============================================================
console.log('[CHAT] Script loaded, calling init()...');
init();
