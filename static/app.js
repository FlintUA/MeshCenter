
let selectedNodeId = null;

function renderNodeDetails(node) {
    const details = document.getElementById('nodeDetails');

    if (!node) {
        details.innerHTML = '';
        return;
    }

    details.innerHTML =
        '<div class="node-details">' +
        '<div class="node-details-title">' + node.clean_name + '</div>' +
        '<div>ID: ' + node.node_id + '</div>' +
        '<div>Short: ' + (node.short_name || '-') + '</div>' +
        '<div>Hardware: ' + (node.hw_model || '-') + '</div>' +
        '<div>Last seen: ' + node.age + '</div>' +
        '<div>RSSI: ' + (node.rssi || '-') + '</div>' +
        '<div>SNR: ' + (node.snr || '-') + '</div>' +
        '<div>Hops: ' + (node.hop_start || '-') + '</div>' +
        '<div>Relay: ' + (node.relay_node || '-') + '</div>' +
        '<div>Last message: ' + (node.last_text || '-') + '</div>' +
        '</div>';
}

async function loadMessages() {
    const r = await fetch('/api/messages');
    const data = await r.json();

    document.getElementById('status').textContent = data.status;

    const chat = document.getElementById('chat');
    const nearBottom = chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 80;

    chat.innerHTML = '';

    data.messages.forEach(m => {
        const row = document.createElement('div');
        row.className = 'row ' + m.kind;

        const bubble = document.createElement('div');
        bubble.className = 'bubble';

        const sender = document.createElement('div');
        sender.className = 'sender';
        sender.textContent = m.sender;

        const text = document.createElement('div');
        text.className = 'text';
        text.textContent = m.text;

        const time = document.createElement('div');
        time.className = 'time';
        time.textContent = m.time;

        bubble.appendChild(sender);
        bubble.appendChild(text);
        bubble.appendChild(time);
        row.appendChild(bubble);
        chat.appendChild(row);
    });

    if (nearBottom) {
        chat.scrollTop = chat.scrollHeight;
    }

    const nodesList = document.getElementById('nodesList');
    nodesList.innerHTML = '';

    document.getElementById('nodesTitle').textContent =
        'Nodes (' + data.nodes.length + ')';

    data.nodes.forEach(n => {
        const card = document.createElement('div');
        card.className = 'node-card';

        if (selectedNodeId === n.node_id) {
            card.className = 'node-card selected';
        }

        card.onclick = () => {
        selectedNodeId = n.node_id;
        renderNodeDetails(n);
        loadMessages();
        };

        const name = document.createElement('div');
        name.className = 'node-name';
        name.textContent = n.name;

        const id = document.createElement('div');
        id.className = 'node-id';
        id.textContent = n.node_id;

        const meta = document.createElement('div');
        meta.className = 'node-meta';
        meta.textContent = n.meta;

        const lastText = document.createElement('div');
        lastText.className = 'node-meta';
        lastText.textContent = n.last_text ? "Msg: " + n.last_text : "";

        card.appendChild(name);
        card.appendChild(id);
        card.appendChild(meta);
        card.appendChild(lastText);
        nodesList.appendChild(card);
    });
    const selectedNode = data.nodes.find(n => n.node_id === selectedNodeId);
    renderNodeDetails(selectedNode);
}

document.getElementById('sendForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const input = document.getElementById('text');
    const text = input.value.trim();
    if (!text) return;

    input.disabled = true;

    await fetch('/api/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text})
    });

    input.value = '';
    input.disabled = false;
    input.focus();
    loadMessages();
});

setInterval(loadMessages, 2000);
loadMessages();