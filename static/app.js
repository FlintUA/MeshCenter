let currentChatId = null;
let currentChatName = null;
let currentChatType = null;
let lastMessagesSignature = '';
let nodeSearchTerm = '';
let directMessageTarget = null;
let chatListCache = [];
let messagePollInterval = null;
let showIgnored = false;
let showFavorites = false;
let nodeCache = [];
let deleteTargetChatId = null;
let clearTargetChatId = null;
let totalUnreadCount = 0;

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

function renderChatItem(chat) {
    const icon = chat.is_channel ? '📡' : '👤';
    const iconClass = chat.is_channel ? 'channel' : 'dm';
    const unreadClass = (chat.unread || 0) > 0 ? '' : 'zero';
    const lastMsg = chat.last_message || 'No messages yet';
    const time = chat.last_time || '';
    const ignored = chat.ignored ? '🚫 ' : '';
    const favorite = chat.favorite ? '⭐ ' : '';
    const unreadBadge = (chat.unread || 0) > 0 ? `<span class="chat-unread-badge">${chat.unread}</span>` : '';
    const hasUnread = (chat.unread || 0) > 0 ? 'has-unread' : '';

    return `
        <div class="chat-item ${hasUnread}" onclick="openChat('${escapeHtml(chat.id)}', '${escapeHtml(chat.name)}', '${escapeHtml(chat.type)}')">
            <div class="chat-icon ${iconClass}">${icon}</div>
            <div class="chat-info">
                <div class="chat-name">${ignored}${favorite}${escapeHtml(chat.name)}</div>
                <div class="chat-last-msg">${escapeHtml(truncateText(lastMsg, 60))}</div>
            </div>
            <div class="chat-meta">
                <div class="chat-time">${escapeHtml(time)}</div>
                ${unreadBadge}
            </div>
        </div>
    `;
}

async function loadChatList() {
    try {
        const response = await fetch('/api/chats');
        const data = await response.json();
        chatListCache = data.chats || [];
        totalUnreadCount = data.total_unread || 0;

        const container = document.getElementById('chatList');
        if (!container) return;

        const chatTitle = document.getElementById('chatTitle');
        if (chatTitle) {
            if (totalUnreadCount > 0) {
                chatTitle.textContent = `💬 Chats (${totalUnreadCount})`;
            } else {
                chatTitle.textContent = '💬 Chats';
            }
        }

        if (chatListCache.length === 0) {
            container.innerHTML = '<div class="loading">💬 No chats yet</div>';
            return;
        }

        const channelChat = chatListCache.find(c => c.is_channel);
        const dmChats = chatListCache.filter(c => !c.is_channel);

        let html = '';

        if (channelChat) {
            html += renderChatItem(channelChat);
        }

        if (dmChats.length > 0) {
            html += `<div class="chat-section-title">💬 Chats</div>`;
            html += dmChats.map(chat => renderChatItem(chat)).join('');
        } else if (!channelChat) {
            html = '<div class="loading">💬 No chats yet</div>';
        }

        container.innerHTML = html;

    } catch (error) {
        console.error('Error loading chats:', error);
        const container = document.getElementById('chatList');
        if (container) {
            container.innerHTML = '<div class="loading">⚠️ Error loading chats</div>';
        }
    }
}

async function checkNodeIgnored(nodeId) {
    try {
        const response = await fetch(`/api/node_status?node_id=${encodeURIComponent(nodeId)}`);
        const data = await response.json();
        return data.ignored || false;
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

function openChat(chatId, chatName, chatType) {
    currentChatId = chatId;
    currentChatName = chatName || chatId;
    currentChatType = chatType || 'dm';

    document.getElementById('chatListContainer').style.display = 'none';
    document.getElementById('messagesView').style.display = 'flex';
    document.getElementById('backToChatsBtn').style.display = 'block';
    document.getElementById('chatActionsBtn').style.display = 'block';

    document.getElementById('chatTitle').textContent = chatName;
    document.getElementById('chatSubtitle').textContent = chatType === 'channel' ? '' : '';

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

    lastMessagesSignature = '';
    loadChatMessages(chatId);
    startMessagePolling(chatId);
    
    if (chatType === 'dm' && chatId !== 'channel') {
        updateNodeDetails(chatId);
    } else {
        renderNodeDetails(null);
    }
    
    // Обновляем список чатов для сброса счетчика
    loadChatList();
}

function showChatList() {
    currentChatId = null;
    currentChatName = null;
    currentChatType = null;

    document.getElementById('chatListContainer').style.display = 'block';
    document.getElementById('messagesView').style.display = 'none';
    document.getElementById('backToChatsBtn').style.display = 'none';
    document.getElementById('chatActionsBtn').style.display = 'none';

    stopMessagePolling();
    loadChatList();
}

async function loadChatMessages(chatId) {
    if (!chatId) return;

    try {
        const response = await fetch(`/api/messages?chat_id=${encodeURIComponent(chatId)}`);
        const data = await response.json();

        const container = document.getElementById('messagesContainer');
        if (!container) return;

        const shouldScroll = container.scrollTop + container.clientHeight >= container.scrollHeight - 100;
        const messages = data.messages || [];

        const signature = messages.map(m => 
            [m.kind, m.sender, m.text, m.time].join('|')
        ).join('||');

        if (signature !== lastMessagesSignature) {
            if (messages.length === 0) {
                container.innerHTML = '<div class="loading">💬 No messages yet. Send the first one!</div>';
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

            lastMessagesSignature = signature;
            if (shouldScroll) {
                container.scrollTop = container.scrollHeight;
            }
        }

        // Обновляем список чатов для обновления счетчика непрочитанных
        loadChatList();

    } catch (error) {
        console.error('Error loading messages:', error);
        const container = document.getElementById('messagesContainer');
        if (container) {
            container.innerHTML = '<div class="loading">⚠️ Error loading messages</div>';
        }
    }
}

let messagePollingInterval = null;

function startMessagePolling(chatId) {
    stopMessagePolling();
    messagePollingInterval = setInterval(() => {
        if (currentChatId === chatId) {
            loadChatMessages(chatId);
        }
    }, 3000);
}

function stopMessagePolling() {
    if (messagePollingInterval) {
        clearInterval(messagePollingInterval);
        messagePollingInterval = null;
    }
}

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

        const button = e.target.querySelector('button');
        const originalText = button ? button.textContent : 'Send';

        if (button) {
            button.disabled = true;
            button.textContent = '📡 Sending...';
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
                lastMessagesSignature = '';
                loadChatMessages(currentChatId);
                loadChatList();
            } else {
                const error = await response.json();
                alert('Failed to send: ' + (error.error || 'Unknown error'));
            }

        } catch (error) {
            console.error('Error sending message:', error);
            alert('Network error');

        } finally {
            if (button) {
                button.disabled = false;
                button.textContent = originalText;
            }
            if (input) input.focus();
        }
    });
}

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

// ===== УДАЛЕНИЕ ЧАТА =====
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

// ===== ОЧИСТКА ЧАТА =====
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
            // Сбрасываем кэш сообщений
            lastMessagesSignature = '';
            // Принудительно обновляем сообщения в чате
            loadChatMessages(clearTargetChatId);
            // Обновляем список чатов
            loadChatList();
            // Обновляем список узлов
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

function setDirectMessage(nodeId, nodeName) {
    if (directMessageTarget === nodeName) {
        directMessageTarget = null;
        document.querySelectorAll('.node-title-btn').forEach(btn => {
            btn.style.background = 'linear-gradient(135deg, #4a5a7a 0%, #3a4a6a 100%)';
            btn.style.boxShadow = 'none';
        });
        document.getElementById('messageInput')?.focus();
        return;
    }

    directMessageTarget = nodeName;
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
                lastMessagesSignature = '';
                loadChatMessages(nodeId);
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
    const isActive = directMessageTarget === node.clean_name;

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
                        <span class="value">${escapeHtml(node.node_id)}</span>
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
                        ${isFavorite ? 'Favorite' : 'Favorite'}
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

async function loadMessages() {
    try {
        const response = await fetch('/api/messages');
        const data = await response.json();

        nodeCache = data.nodes || [];

        const statusEl = document.getElementById('statusText');
        const nodeCountEl = document.getElementById('nodeCount');

        if (statusEl) statusEl.innerHTML = '🟢 Mesh online';
        
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
        console.error('Error loading messages:', error);
        const statusEl = document.getElementById('statusText');
        if (statusEl) statusEl.innerHTML = '🔴 Connection error';
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

function selectNode(nodeId, nodeName) {
    if (currentChatId === nodeId) return;
    openChat(nodeId, nodeName, 'dm');
    updateNodeDetails(nodeId);
}

async function loadSensors() {
    try {
        const response = await fetch('/api/sensors');
        const data = await response.json();

        const sensorsCard = document.getElementById('sensorsCard');
        if (sensorsCard && (data.temperature !== null || data.voltage !== null)) {
            sensorsCard.style.display = 'block';

            document.getElementById('tempValue').textContent = data.temperature !== null ? data.temperature.toFixed(1) : '--';
            document.getElementById('humValue').textContent = data.humidity !== null ? data.humidity.toFixed(1) : '--';
            document.getElementById('presValue').textContent = data.pressure !== null ? Math.round(data.pressure) : '--';
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

        const battery = data.real_battery !== null ? '~' + data.real_battery + '%' :
                       data.battery_level !== null ? data.battery_level + '%' : '--%';
        const voltage = data.voltage !== null ? Number(data.voltage).toFixed(3) + ' V' : '-- V';
        const channel = data.channel_utilization !== null ? Number(data.channel_utilization).toFixed(2) + '%' : '--%';
        const airTx = data.air_util_tx !== null ? Number(data.air_util_tx).toFixed(2) + '%' : '--%';
        const uptime = data.uptime_seconds !== null ? formatUptime(data.uptime_seconds) : '--';

        card.innerHTML = `
            <div class="base-card-title">
                <span>📡 Flint Base</span>
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

async function init() {
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
    
    await loadBaseStatus();
    await loadSensors();
    await loadMessages();
    await loadChatList();
    showChatList();

    setInterval(loadMessages, 10000);
    setInterval(loadChatList, 5000);
    setInterval(loadBaseStatus, 30000);
    setInterval(loadSensors, 10000);

    const input = document.getElementById('messageInput');
    if (input) input.focus();
}

init();