let selectedNodeId = null;
let selectedNodeName = null;
let lastNodesSignature = '';
let lastMessagesSignature = '';
let lastChatKey = '';

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
        bar.classList.remove('show');
        text.textContent = '';
        return;
    }

    bar.classList.add('show');
    text.textContent = '💬 Filtered: ' + selectedNodeName + ' (' + selectedNodeId + ')';
}

function renderNodeDetails(node) {
    const details = document.getElementById('nodeDetails');

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
        '<div>RSSI: ' + escapeHtml(node.rssi || '-') + '</div>' +
        '<div>SNR: ' + escapeHtml(node.snr || '-') + '</div>' +
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
    if (signalQuality === 'good') return 'good';
    if (signalQuality === 'medium') return 'mid';
    if (signalQuality === 'weak') return 'weak';
    return 'idle';
}

async function loadMessages() {
    let url = '/api/messages';

    if (selectedNodeId) {
        url += '?node_id=' + encodeURIComponent(selectedNodeId);
    }

    try {
        const response = await fetch(url);
        const data = await response.json();

        document.getElementById('statusText').innerHTML =
            data.status === 'radio: listening' ? '🟢 Radio active' : '🟡 Sending...';

        document.getElementById('nodeCount').innerHTML =
            '📡 ' + data.nodes.length + ' nodes';

        document.getElementById('nodesCountBadge').textContent =
            '(' + data.nodes.length + ')';
            
        const container = document.getElementById('messagesContainer');
        const shouldScroll = container.scrollTop + container.clientHeight >= container.scrollHeight - 100;

        const lastMsg = data.messages.length > 0 ? data.messages[data.messages.length - 1] : null;

        const chatKey = lastMsg
            ? [
                data.messages.length,
                lastMsg.kind,
                lastMsg.sender,
                lastMsg.node_id,
                lastMsg.text,
                lastMsg.time
            ].join('|')
            : 'empty';

        if (chatKey !== lastChatKey) {
            if (data.messages.length === 0) {
                container.innerHTML = '<div class="loading">💬 No messages for this view.</div>';
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

            if (shouldScroll) {
                container.scrollTop = container.scrollHeight;
            }
        }
        const nodesList = document.getElementById('nodesList');

        const nodesSignature = data.nodes.map(node => {
            return [
                node.node_id,
                node.clean_name,
                node.last_text,
                node.signal_quality,
                node.rssi,
                node.snr,
                node.hop_start,
                node.relay_node,
                selectedNodeId === node.node_id ? 'selected' : ''
            ].join('|');
        }).join('||');

        if (nodesSignature !== lastNodesSignature) {
            nodesList.innerHTML = data.nodes.map(node => {
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
                            <span class="badge ${badgeClass}">${badgeText}</span>
                        </div>
                        <div class="node-id">${nodeId}</div>
                        <div class="node-meta">${escapeHtml(node.meta)}</div>
                        ${lastText}
                    </div>
                `;
            }).join('');

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
        document.getElementById('statusText').innerHTML = '🔴 Connection error';
    }
}

document.getElementById('sendForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const input = document.getElementById('messageInput');
    const text = input.value.trim();

    if (!text) return;

    const button = e.target.querySelector('button');
    button.disabled = true;
    button.textContent = 'Sending...';

    try {
        const response = await fetch('/api/send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text})
        });

        if (response.ok) {
            input.value = '';
            lastChatKey = '';
            loadMessages();
        }
    } catch (error) {
        console.error('Error sending message:', error);
    } finally {
        button.disabled = false;
        button.textContent = 'Send 📡';
        input.focus();
    }
});

setInterval(loadMessages, 3000);
loadMessages();

setTimeout(() => {
    document.getElementById('messageInput').focus();
}, 100);

async function loadBaseStatus() {
    try {
        const response = await fetch('/api/base_status');
        const data = await response.json();

        const card = document.getElementById('baseCard');
        if (!card) return;

        const battery = data.battery_level !== null && data.battery_level !== undefined
            ? data.battery_level + ' %'
            : '-';

        const voltage = data.voltage !== null && data.voltage !== undefined
            ? Number(data.voltage).toFixed(3) + ' V'
            : '-';

        const channel = data.channel_utilization !== null && data.channel_utilization !== undefined
            ? Number(data.channel_utilization).toFixed(2) + ' %'
            : '-';

        const airTx = data.air_util_tx !== null && data.air_util_tx !== undefined
            ? Number(data.air_util_tx).toFixed(2) + ' %'
            : '-';

        const uptime = data.uptime_seconds !== null && data.uptime_seconds !== undefined
            ? formatUptime(data.uptime_seconds)
            : '-';

        card.innerHTML =
            '<div class="base-card-title">📡 Flint Base</div>' +
            '<div class="base-card-line">🔋 Battery: ' + escapeHtml(battery) + '</div>' +
            '<div class="base-card-line">⚡ Voltage: ' + escapeHtml(voltage) + '</div>' +
            '<div class="base-card-line">📶 Channel: ' + escapeHtml(channel) + '</div>' +
            '<div class="base-card-line">📡 Air TX: ' + escapeHtml(airTx) + '</div>' +
            '<div class="base-card-line">⏱ Uptime: ' + escapeHtml(uptime) + '</div>' +
            '<div class="base-card-line">🕒 Update: ' + escapeHtml(data.last_update || '-') + '</div>';

    } catch (error) {
        console.error('Error loading base status:', error);
    }
}

function formatUptime(seconds) {
    seconds = Number(seconds);

    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);

    if (h > 0) {
        return h + ' h ' + m + ' min';
    }

    return m + ' min';
}

setInterval(loadBaseStatus, 30000);
loadBaseStatus();