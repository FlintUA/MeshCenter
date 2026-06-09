from flask import Flask, request, jsonify, render_template_string
import subprocess
import threading
import time
import re

APP_HOST = "0.0.0.0"
APP_PORT = 5000
MESHTASTIC_CMD = "/home/flint/.local/bin/meshtastic"
LOCAL_NODE_NAME = "Flint Base"

app = Flask(__name__)

messages = []
seen_ids = set()
seen_recent_texts = {}

listen_process = None
radio_lock = threading.Lock()
pause_listen = threading.Event()

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Meshnode Web Chat</title>
<style>
html, body {
    height: 100%;
    margin: 0;
    overflow: hidden;
}
body {
    font-family: Arial, sans-serif;
    background: #eeeeee;
    display: flex;
    flex-direction: column;
}
.header {
    flex: 0 0 auto;
    padding: 12px 18px;
    background: white;
    font-size: 24px;
    font-weight: bold;
    border-bottom: 1px solid #ddd;
}
.status {
    flex: 0 0 auto;
    padding: 4px 18px;
    background: white;
    color: #777;
    font-size: 12px;
}
#chat {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 12px;
    background: #eeeeee;
}
.row {
    display: flex;
    margin-bottom: 8px;
}
.row.me {
    justify-content: flex-end;
}
.row.rx {
    justify-content: flex-start;
}
.bubble {
    max-width: 70%;
    padding: 7px 11px;
    border-radius: 12px;
    background: white;
    border: 1px solid #ddd;
}
.row.me .bubble {
    background: #dcf8c6;
}
.sender {
    font-size: 12px;
    color: #555;
    font-weight: bold;
    margin-bottom: 2px;
}
.text {
    font-size: 18px;
    white-space: pre-wrap;
    word-break: break-word;
}
.time {
    font-size: 11px;
    color: #777;
    text-align: right;
    margin-top: 3px;
}
form {
    flex: 0 0 auto;
    height: 54px;
    display: flex;
    gap: 8px;
    padding: 8px;
    background: white;
    border-top: 1px solid #ddd;
    box-sizing: border-box;
}
input {
    flex: 1;
    padding: 8px 10px;
    font-size: 17px;
}
button {
    padding: 8px 22px;
    font-size: 17px;
}
</style>
</head>
<body>
<div class="header">Meshnode Web Chat - LongFast Channel 0</div>
<div class="status" id="status">loading...</div>
<div id="chat"></div>

<form id="sendForm">
<input id="text" autocomplete="off" placeholder="Введите сообщение..." />
<button type="submit">Send</button>
</form>

<script>
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
</script>
</body>
</html>
"""

def now():
    return time.strftime("%H:%M:%S")

def add_message(kind, sender, text):
    messages.append({
        "kind": kind,
        "sender": sender,
        "text": text,
        "time": now()
    })
    messages[:] = messages[-300:]

def extract_packet_id(line):
    m = re.search(r"'id':\s*(\d+)", line)
    if m:
        return m.group(1)

    m = re.search(r"\bid:\s*(\d+)", line)
    if m:
        return m.group(1)

    return None

def extract_sender(line):
    m = re.search(r"'fromId':\s*'([^']+)'", line)
    if m:
        return m.group(1)

    m = re.search(r"'from':\s*(\d+)", line)
    if m:
        return "node " + m.group(1)

    m = re.search(r"\bfrom:\s*(\d+)", line)
    if m:
        return "node " + m.group(1)

    return "RX"

def extract_text_message(line):
    if "TEXT_MESSAGE_APP" not in line and "'text':" not in line and '"text":' not in line:
        return None

    m = re.search(r"'text':\s*'([^']*)'", line)
    if m:
        return m.group(1).strip()

    m = re.search(r'"text":\s*"([^"]*)"', line)
    if m:
        return m.group(1).strip()

    return None

def is_duplicate_text(sender, text):
    cleaned_text = text.strip()
    if not cleaned_text:
        return True

    current_time = time.time()

    # Удаляем старые записи, чтобы словарь не рос бесконечно
    old_keys = []
    for key, ts in seen_recent_texts.items():
        if current_time - ts > 60:
            old_keys.append(key)

    for key in old_keys:
        del seen_recent_texts[key]

    # Дубликаты от meshtastic --listen часто отличаются только sender,
    # поэтому ключ делаем по тексту, а не по sender+text.
    key = cleaned_text

    old_time = seen_recent_texts.get(key)
    if old_time and current_time - old_time < 15:
        return True

    seen_recent_texts[key] = current_time
    return False

def stop_listener():
    global listen_process

    if listen_process is not None:
        try:
            listen_process.terminate()
            time.sleep(1)

            if listen_process.poll() is None:
                listen_process.kill()
                time.sleep(1)

        except Exception:
            pass

        listen_process = None

def listen_meshtastic():
    global listen_process

    while True:
        if pause_listen.is_set():
            time.sleep(0.5)
            continue

        try:
            with radio_lock:
                if pause_listen.is_set():
                    continue

                listen_process = subprocess.Popen(
                    [MESHTASTIC_CMD, "--listen"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    errors="ignore"
                )

            for line in listen_process.stdout:
                if pause_listen.is_set():
                    break

                line = line.strip()
                if not line:
                    continue

                text = extract_text_message(line)
                if not text:
                    continue

                pid = extract_packet_id(line)
                if pid:
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)

                sender = extract_sender(line)

                if is_duplicate_text(sender, text):
                    continue

                add_message("rx", sender, text)

        except Exception as e:
            add_message("rx", "SYSTEM ERROR", "listen: " + str(e))

        time.sleep(2)

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/messages")
def api_messages():
    status = "radio: sending..." if pause_listen.is_set() else "radio: listening"
    return jsonify({
        "status": status,
        "messages": messages
    })

@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True)
    text = data.get("text", "").strip()

    if not text:
        return jsonify({"ok": False, "error": "empty message"}), 400

    pause_listen.set()

    with radio_lock:
        try:
            stop_listener()
            time.sleep(2)

            result = subprocess.run(
                [MESHTASTIC_CMD, "--ch-index", "0", "--sendtext", text],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                add_message("me", LOCAL_NODE_NAME, text)
                return jsonify({"ok": True})

            err = result.stderr.strip() or result.stdout.strip() or "unknown send error"
            add_message("rx", "SYSTEM ERROR", "send: " + err)
            return jsonify({"ok": False, "error": err}), 500

        except Exception as e:
            add_message("rx", "SYSTEM ERROR", "send: " + str(e))
            return jsonify({"ok": False, "error": str(e)}), 500

        finally:
            time.sleep(2)
            pause_listen.clear()

if __name__ == "__main__":
    t = threading.Thread(target=listen_meshtastic, daemon=True)
    t.start()
    app.run(host=APP_HOST, port=APP_PORT)
    