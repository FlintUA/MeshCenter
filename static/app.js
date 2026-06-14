let selectedNodeId = null;
let selectedNodeName = null;
let lastNodesSignature = '';
let lastChatKey = '';
let nodeSearchTerm = '';

function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(value);
    return div.innerHTML;
}

function clearFilter() {
    selectedNodeId = null;
    selectedNodeName = null;
    updateFilterBar();
    loadMessages();
}

function updateFilterBar() {
    const bar = document.getElementById('filterBar');
    const text = document.getElementById('filterText');

    if (!selectedNodeId) {
        if (bar) bar.classList.remove('show');
        if (text) text.textContent = '';
        return;
    }

    if (bar) bar.classList.add('show');
    if (text) text.textContent = '💬 Filtered: ' + selectedNodeName;
}

function renderNodeDetails(node) {
    const details = document.getElementById('nodeDetails');
    if (!details) return;

    if (!node) {
        details.innerHTML = '';
        return;
    }

    details.innerHTML =
        '<div class="node-details">' +
        '<div class="node-details-title">' + escapeHtml(node.clean_name) + '</div>' +
        '<div>ID: ' + escapeHtml(node.node_id) + '</div>' +
        '<div>Short: ' + escapeHtml(node.short_name || '-') + '</div>' +
        '<div>Hardware: ' + escapeHtml(node.hw_model || '-') + '</div>' +
        '<div>Last seen: ' + escapeHtml(node.age || '-') + '</div>' +
        '<div>Signal: ' + escapeHtml(node.signal_quality || '-') + '</div>' +
        '<div>RSSI: ' + escapeHtml(node.rssi || '-') + ' dBm</div>' +
        '<div>SNR: ' + escapeHtml(node.snr || '-') + ' dB</div>' +
        '<div>Hops: ' + escapeHtml(node.hop_start || '-') + '</div>' +
        '<div>Relay: ' + escapeHtml(node.relay_node || '-') + '</div>' +
        '<div>Last message: ' + escapeHtml(node.last_text || '-') + '</div>' +
        '</div>';
}

function selectNode(nodeId, nodeName) {
    if (selectedNodeId === nodeId) {
        clearFilter();
        return;
    }

    selectedNodeId = nodeId;
    selectedNodeName = nodeName;
    updateFilterBar();
    lastChatKey = '';
    lastNodesSignature = '';
    loadMessages();
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

async function loadBaseStatus() {
    try {
        const response = await fetch('/api/base_status');
        const data = await response.json();

        const card = document.getElementById('baseCard');
        if (!card) return;

        const battery = data.real_battery !== null && data.real_battery !== undefined
            ? '~' + data.real_battery + '%'
            : (data.battery_level !== null && data.battery_level !== undefined
            ? data.battery_level + '%'
            : '--%');

        const voltage = data.voltage !== null && data.voltage !== undefined
            ? Number(data.voltage).toFixed(3) + ' V'
            : '-- V';

        const channel = data.channel_utilization !== null && data.channel_utilization !== undefined
            ? Number(data.channel_utilization).toFixed(2) + '%'
            : '--%';

        const airTx = data.air_util_tx !== null && data.air_util_tx !== undefined
            ? Number(data.air_util_tx).toFixed(2) + '%'
            : '--%';

        const uptime = data.uptime_seconds !== null && data.uptime_seconds !== undefined
            ? formatUptime(data.uptime_seconds)
            : '--';

        card.innerHTML =
            '<div class="base-card-title">📡 Flint Base</div>' +
            '<div class="base-status-grid">' +
                '<div><span>⚡</span><b>' + escapeHtml(voltage) + '</b><small> Voltage</small></div>' +
                '<div><span>🔋</span><b>' + escapeHtml(battery) + '</b><small> Battery</small></div>' +
                '<div><span>📶</span><b>' + escapeHtml(channel) + '</b><small> Channel Util</small></div>' +
                '<div><span>📡</span><b>' + escapeHtml(airTx) + '</b><small> Air TX</small></div>' +
            '</div>' +
            '<div class="base-card-footer">⏱ Uptime: ' + escapeHtml(uptime) + ' | 🕒 ' + escapeHtml(data.last_update || '--') + '</div>';

    } catch (error) {
        console.error('Error loading base status:', error);
        const card = document.getElementById('baseCard');
        if (card) {
            card.innerHTML = '<div class="base-card-title">📡 Flint Base</div><div class="base-card-line">⚠️ Status unavailable</div>';
        }
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

// Sensors data (card is hidden, but we can still load data for future use)
async function loadSensors() {
    try {
        const response = await fetch('/api/sensors');
        const data = await response.json();
        
        const sensorsCard = document.getElementById('sensorsCard');
        if (sensorsCard && (data.temperature !== null || data.voltage !== null)) {
            sensorsCard.style.display = 'block';
            
            const tempEl = document.getElementById('tempValue');
            const humEl = document.getElementById('humValue');
            const presEl = document.getElementById('presValue');
            const voltEl = document.getElementById('voltValue');
            const currEl = document.getElementById('currValue');
            const powEl = document.getElementById('powValue');
            
            if (tempEl) tempEl.textContent = data.temperature !== null ? data.temperature.toFixed(1) : '--';
            if (humEl) humEl.textContent = data.humidity !== null ? data.humidity.toFixed(1) : '--';
            if (presEl) presEl.textContent = data.pressure !== null ? Math.round(data.pressure) : '--';
            if (voltEl) voltEl.textContent = data.voltage !== null ? data.voltage.toFixed(2) : '--';
            if (currEl) currEl.textContent = data.current !== null ? Math.round(data.current) : '--';
            if (powEl) powEl.textContent = data.power !== null ? Math.round(data.power) : '--';
            
            if (data.battery_percent !== null) {
                const batteryIndicator = document.getElementById('batteryIndicator');
                if (batteryIndicator) batteryIndicator.style.display = 'block';
                const percent = Math.min(100, Math.max(0, data.battery_percent));
                const fillEl = document.getElementById('batteryFill');
                const percentEl = document.getElementById('batteryPercent');
                if (fillEl) fillEl.style.width = percent + '%';
                if (percentEl) percentEl.textContent = percent + '%';
            }
            
            const updateEl = document.getElementById('sensorUpdate');
            if (updateEl && data.last_update) {
                updateEl.textContent = `Last update: ${data.last_update}`;
            }
        }
    } catch (error) {
        console.error('Error loading sensors:', error);
    }
}

async function loadMessages() {
    let url = '/api/messages';
    if (selectedNodeId) {
        url += '?node_id=' + encodeURIComponent(selectedNodeId);
    }

    try {
        const response = await fetch(url);
        const data = await response.json();

        const statusEl = document.getElementById('statusText');
        const nodeCountEl = document.getElementById('nodeCount');
        const nodesCountBadge = document.getElementById('nodesCountBadge');
        
        if (statusEl) statusEl.innerHTML = data.status === 'radio: listening' ? '🟢 Radio active' : '🟡 Sending...';
        if (nodeCountEl) nodeCountEl.innerHTML = '📡 ' + data.nodes.length + ' nodes';
        if (nodesCountBadge) nodesCountBadge.textContent = '(' + data.nodes.length + ')';
        
        const container = document.getElementById('messagesContainer');
        if (!container) return;
        
        const shouldScroll = container.scrollTop + container.clientHeight >= container.scrollHeight - 100;

        const lastMsg = data.messages.length > 0 ? data.messages[data.messages.length - 1] : null;

        const chatKey = lastMsg
            ? [data.messages.length, lastMsg.kind, lastMsg.sender, lastMsg.node_id, lastMsg.text, lastMsg.time].join('|')
            : 'empty';

        if (chatKey !== lastChatKey) {
            if (data.messages.length === 0) {
                container.innerHTML = '<div class="loading">💬 No messages yet. Be the first to send one!</div>';
            } else {
                container.innerHTML = data.messages.map(msg => `
                    <div class="message ${escapeHtml(msg.kind)}">
                        <div class="bubble">
                            <div class="sender">${escapeHtml(msg.sender)}</div>
                            <div class="text">${escapeHtml(msg.text)}</div>
                            <div class="time">${escapeHtml(msg.time)}</div>
                        </div>
                    </div>
                `).join('');
            }
            lastChatKey = chatKey;
            if (shouldScroll) container.scrollTop = container.scrollHeight;
        }
        
        const nodesList = document.getElementById('nodesList');
        if (!nodesList) return;
        
        let filteredNodes = data.nodes;
        if (nodeSearchTerm) {
            filteredNodes = data.nodes.filter(node => 
                node.clean_name.toLowerCase().includes(nodeSearchTerm.toLowerCase()) ||
                node.node_id.toLowerCase().includes(nodeSearchTerm.toLowerCase())
            );
        }

        const nodesSignature = filteredNodes.map(node => 
            [node.node_id, node.clean_name, node.last_text, node.signal_quality, selectedNodeId === node.node_id ? 'selected' : ''].join('|')
        ).join('||');

        if (nodesSignature !== lastNodesSignature) {
            if (filteredNodes.length === 0 && nodeSearchTerm) {
                nodesList.innerHTML = '<div class="loading" style="padding: 20px; text-align: center;">🔍 No nodes found</div>';
            } else {
                nodesList.innerHTML = filteredNodes.map(node => {
                    const nodeId = escapeHtml(node.node_id);
                    const cleanName = escapeHtml(node.clean_name);
                    const selected = selectedNodeId === node.node_id ? 'selected' : '';
                    const badgeClass = signalBadgeClass(node.signal_quality);
                    const badgeText = signalBadgeText(node.signal_quality);
                    const lastText = node.last_text
                        ? `<div class="node-last-text">📝 ${escapeHtml(node.last_text.substring(0, 70))}${node.last_text.length > 70 ? '...' : ''}</div>`
                        : '';

                    return `
                        <div class="node-card ${selected}" onclick="selectNode('${nodeId}', '${cleanName}')">
                            <div class="node-name">
                                ${escapeHtml(node.name)}
                                <span class="badge ${badgeClass}" title="Signal quality: ${node.signal_quality || 'unknown'}">${badgeText}</span>
                            </div>
                            <div class="node-id">${nodeId}</div>
                            <div class="node-meta">${escapeHtml(node.meta)}</div>
                            ${lastText}
                        </div>
                    `;
                }).join('');
            }
            lastNodesSignature = nodesSignature;
        }

        const selectedNode = data.nodes.find(node => node.node_id === selectedNodeId);
        if (selectedNode) {
            selectedNodeName = selectedNode.clean_name;
            renderNodeDetails(selectedNode);
        } else {
            renderNodeDetails(null);
        }
        updateFilterBar();
    } catch (error) {
        console.error('Error loading messages:', error);
        const statusEl = document.getElementById('statusText');
        if (statusEl) statusEl.innerHTML = '🔴 Connection error';
    }
}

// Send form handler
const sendForm = document.getElementById('sendForm');
if (sendForm) {
    sendForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const input = document.getElementById('messageInput');
        const text = input ? input.value.trim() : '';
        if (!text) return;

        const button = e.target.querySelector('button');
        const originalText = button ? button.textContent : 'Send';
        if (button) {
            button.disabled = true;
            button.textContent = '📡 Sending...';
        }

        try {
            const response = await fetch('/api/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text})
            });
            if (response.ok) {
                if (input) input.value = '';
                lastChatKey = '';
                loadMessages();
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

// Search functionality
const searchInput = document.getElementById('nodeSearchInput');
if (searchInput) {
    searchInput.addEventListener('input', (e) => {
        nodeSearchTerm = e.target.value;
        lastNodesSignature = '';
        loadMessages();
    });
}

// Auto-refresh intervals
setInterval(loadMessages, 3000);
setInterval(loadBaseStatus, 30000);
setInterval(loadSensors, 10000);

// Initial loads
loadMessages();
loadBaseStatus();
loadSensors();

// Focus input on load
setTimeout(() => {
    const input = document.getElementById('messageInput');
    if (input) input.focus();
}, 100);