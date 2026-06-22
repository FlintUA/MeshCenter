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
let showDuplicatesOnly = false;

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

async function loadChatList() {
    try {
        const response = await fetch('/api/chats');
        const data = await response.json();
        chatListCache = data.chats || [];
        totalUnreadCount = data.total_unread || 0;

        const container = document.getElementById('chatList');
        if (!container) return;

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
            statusText = 'Offline';
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

    document.getElementById('chatListContainer').style.display = 'none';
    document.getElementById('messagesView').style.display = 'flex';
    document.getElementById('backToChatsBtn').style.display = 'block';
    document.getElementById('chatActionsBtn').style.display = 'block';
    
    document.getElementById('deleteAllDmHeaderBtn').style.display = 'none';
    document.getElementById('restoreDeletedDmBtn').style.display = 'none';

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

    document.getElementById('chatListContainer').style.display = 'block';
    document.getElementById('messagesView').style.display = 'none';
    document.getElementById('backToChatsBtn').style.display = 'none';
    document.getElementById('chatActionsBtn').style.display = 'none';
    
    document.getElementById('deleteAllDmHeaderBtn').style.display = 'block';
    document.getElementById('restoreDeletedDmBtn').style.display = 'block';

    const titleEl = document.getElementById('chatTitle');
    const subtitleEl = document.getElementById('chatSubtitle');
    
    const totalUnread = chatListCache.reduce((sum, c) => sum + (c.unread || 0), 0);
    if (totalUnread > 0) {
        titleEl.textContent = `💬 Chats (${totalUnread})`;
    } else {
        titleEl.textContent = '💬 Chats';
    }
    subtitleEl.textContent = '';

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

        const signature = chatId + '||' + messages.map(m => 
            [m.kind, m.sender, m.text, m.time].join('|')
        ).join('||');

        if (signature !== lastMessagesSignature) {
            lastMessagesSignature = signature;
            
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

            if (shouldScroll) {
                container.scrollTop = container.scrollHeight;
            }
        }

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

        const button = e.target.querySelector('button[type="submit"]');
        const originalHtml = button ? button.innerHTML : 'Send';

        if (button) {
            button.disabled = true;
            const currentWidth = button.offsetWidth;
            button.style.width = currentWidth + 'px';
            button.innerHTML = `<span style="display:inline-block;min-width:75px;text-align:left;">Sending<span class="dots"></span></span>`;
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
                lastMessagesSignature = '';
                loadChatMessages(currentChatId);
                loadChatList();
                
                if (button) {
                    button.innerHTML = '✓ Sent!';
                    button.style.background = '#4caf50';
                    button.style.borderColor = '#4caf50';
                    button.style.animation = '';
                    setTimeout(() => {
                        button.disabled = false;
                        button.style.width = '';
                        button.style.background = '';
                        button.style.borderColor = '';
                        button.innerHTML = originalHtml;
                    }, 1200);
                }
            } else {
                const error = await response.json();
                alert('Failed to send: ' + (error.error || 'Unknown error'));
                if (button) {
                    button.disabled = false;
                    button.style.width = '';
                    button.style.animation = '';
                    button.innerHTML = originalHtml;
                }
            }

        } catch (error) {
            console.error('Error sending message:', error);
            alert('Network error');
            if (button) {
                button.disabled = false;
                button.style.width = '';
                button.style.animation = '';
                button.innerHTML = originalHtml;
            }
        } finally {
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
            lastMessagesSignature = '';
            loadChatMessages(clearTargetChatId);
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
        
        if (currentChatId && currentChatType === 'dm') {
            updateChatHeaderStatus();
        }

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

// ===== ЭМОДЗИ =====
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
    ],
    flags: [
        '🏳️', '🏴', '🏁', '🚩', '🎌', '🇺🇳', '🇪🇺', '🏴‍☠️',
        '🇦🇫', '🇦🇱', '🇩🇿', '🇦🇩', '🇦🇴', '🇦🇬', '🇦🇷', '🇦🇲',
        '🇦🇺', '🇦🇹', '🇦🇿', '🇧🇸', '🇧🇭', '🇧🇩', '🇧🇧', '🇧🇾',
        '🇧🇪', '🇧🇿', '🇧🇯', '🇧🇹', '🇧🇴', '🇧🇦', '🇧🇼', '🇧🇷',
        '🇧🇳', '🇧🇬', '🇧🇫', '🇧🇮', '🇰🇭', '🇨🇲', '🇨🇦', '🇨🇻',
        '🇨🇫', '🇹🇩', '🇨🇱', '🇨🇳', '🇨🇴', '🇰🇲', '🇨🇬', '🇨🇩'
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

// ===== ПЕРЕКЛЮЧЕНИЕ ВКЛАДОК =====
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

// ===== УПРАВЛЕНИЕ НОДАМИ =====
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
                    <div>
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

// ===== ЭКСПОРТ CSV =====
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

// ===== ЭКСПОРТ JSON =====
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

// ===== ИМПОРТ CSV =====
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

// ===== ИМПОРТ JSON =====
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

// ===== ОБЪЕДИНЕНИЕ ДУБЛЕЙ =====
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

// ===== ПОКАЗАТЬ ДУБЛИ =====
function toggleShowDuplicates() {
    showDuplicatesOnly = !showDuplicatesOnly;
    const btn = document.querySelector('.nodes-tool-btn.show');
    if (btn) {
        btn.style.background = showDuplicatesOnly ? '#d0c0e0' : '';
        btn.textContent = showDuplicatesOnly ? '📋 Hide Duplicates' : '📋 Show Duplicates';
    }
    loadNodesManagement();
}

// ===== ОЧИСТКА ВСЕХ НОД =====
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

// ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
function escapeCsv(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/"/g, '""');
}

// ===== RESCAN NETWORK =====
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

// ===== УДАЛЕНИЕ ВСЕХ DM ЧАТОВ =====
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

// ===== ВОССТАНОВЛЕНИЕ УДАЛЕННЫХ DM =====
async function restoreDeletedDm() {
    if (!confirm('Restore all previously deleted DM chats?')) {
        return;
    }
    
    const btn = document.getElementById('restoreDeletedDmBtn');
    const originalText = btn.textContent;
    
    try {
        btn.disabled = true;
        btn.textContent = '⏳ Restoring...';
        
        const response = await fetch('/api/restore_deleted_dm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.ok) {
            await loadChatList();
            showToast('✅ Deleted DM chats restored', 'success');
        } else {
            showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Restore error:', error);
        showToast('❌ Network error', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// ===== УПРАВЛЕНИЕ МЕНЮ ЭКСПОРТА/ИМПОРТА =====
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

// Закрытие при клике вне
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