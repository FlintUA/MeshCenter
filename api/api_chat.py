from flask import request, jsonify
import time
import subprocess


def register_chat_routes(
    app,
    state_lock,
    chats,
    nodes,
    messages,
    save_chats,
    get_chats_list,
    get_chat_messages,
    get_nodes_list,
    is_valid_node_id,
    handle_errors,
    sanitize_text,
    CHANNEL_CHAT_ID,
    CHANNEL_CHAT_NAME,
    MESHTASTIC_CMD,
    LOCAL_NODE_ID,
    LOCAL_NODE_NAME,
    pause_listen,
    radio_lock,
    stop_listener,
    get_node_name,
    ensure_chat,
    add_message,
    reset_unread,
    get_node_info,
    save_nodes,
    now,
):
    @app.route("/api/chats")
    def api_chats():
        chat_list, total_unread = get_chats_list()
        return jsonify({
            "chats": chat_list,
            "total_unread": total_unread
        })

    @app.route("/api/messages")
    def api_messages():
        chat_id = request.args.get("chat_id", "").strip()

        if chat_id and not is_valid_node_id(chat_id):
            return jsonify({
                "ok": False,
                "error": "Invalid chat_id"
            }), 400

        if chat_id:
            chat_messages = get_chat_messages(chat_id)

            with state_lock:
                if (
                    chat_id.startswith("!")
                    and nodes.get(chat_id, {}).get("ignored", False)
                ):
                    chat_messages = [
                        m for m in chat_messages
                        if m.get("kind") == "me"
                        or "SYSTEM" in m.get("sender", "")
                    ]

                if chat_id in chats:
                    chats[chat_id]["unread"] = 0
                    save_chats()

                chat_info = chats.get(chat_id, {})

            return jsonify({
                "chat_id": chat_id,
                "messages": chat_messages,
                "chat_info": chat_info
            })

        return jsonify({
            "messages": messages,
            "nodes": get_nodes_list()
        })

    @app.route("/api/send", methods=["POST"])
    @handle_errors
    def api_send():
        data = request.get_json(force=True)

        text = sanitize_text(data.get("text", "").strip())
        target_node = data.get("target_node", "")
        chat_id = data.get("chat_id", "")

        if not text:
            return jsonify({"ok": False, "error": "empty or invalid message"}), 400

        if chat_id and chat_id != CHANNEL_CHAT_ID and not is_valid_node_id(chat_id):
            return jsonify({"ok": False, "error": "Invalid chat_id"}), 400

        if target_node and not is_valid_node_id(target_node):
            return jsonify({"ok": False, "error": "Invalid target_node"}), 400

        if target_node and target_node.startswith("!") and target_node not in nodes:
            print(f"[SEND] Target node not in nodes cache, sending anyway: {target_node}", flush=True)

        final_chat_id = CHANNEL_CHAT_ID
        receiver_name = "Broadcast"
        chat_name = CHANNEL_CHAT_NAME
        chat_type = "channel"

        if chat_id and chat_id != CHANNEL_CHAT_ID and chat_id.startswith("!"):
            final_chat_id = chat_id
            receiver_name = get_node_name(chat_id)
            chat_name = receiver_name
            chat_type = "dm"
        elif target_node and target_node.startswith("!"):
            final_chat_id = target_node
            receiver_name = get_node_name(target_node)
            chat_name = receiver_name
            chat_type = "dm"

        cmd = [MESHTASTIC_CMD, "--ch-index", "0"]

        if chat_type == "dm":
            cmd.extend(["--dest", final_chat_id])

        cmd.extend(["--sendtext", text])

        try:
            print("[SEND] Preparing to send message", flush=True)
            print(
                f"[SEND] chat_type={chat_type}, final_chat_id={final_chat_id}, receiver={receiver_name}",
                flush=True
            )

            pause_listen.set()
            time.sleep(1.0)

            stop_listener()

            time.sleep(2.0)

            with radio_lock:
                print("[SEND CMD]", cmd, flush=True)

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=45
                )

                print("[SEND RETURN]", result.returncode, flush=True)
                print("[SEND STDOUT]", result.stdout, flush=True)
                print("[SEND STDERR]", result.stderr, flush=True)

            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip() or "unknown send error"

                with state_lock:
                    add_message("rx", "SYSTEM ERROR", f"send: {err}", "", CHANNEL_CHAT_ID)

                return jsonify({
                    "ok": False,
                    "error": err,
                    "returncode": result.returncode
                }), 500

            send_output = (result.stdout or "") + "\n" + (result.stderr or "")

            known_nonfatal_warning = (
                "Error processing received packet" in send_output
                and "my_node_num" in send_output
                and "Connected to radio" in send_output
                and "Sending text message" in send_output
            )

            if (
                not known_nonfatal_warning
                and (
                    "Traceback" in send_output
                    or "ERROR" in send_output
                    or (
                        "Error" in send_output
                        and "Warning: Error processing received packet" not in send_output
                    )
                )
            ):
                err = send_output.strip() or "send command returned error text"

                with state_lock:
                    add_message("rx", "SYSTEM ERROR", f"send: {err}", "", CHANNEL_CHAT_ID)

                return jsonify({
                    "ok": False,
                    "error": err,
                    "returncode": result.returncode
                }), 500

            if chat_type == "dm" and final_chat_id not in chats:
                with state_lock:
                    ensure_chat(final_chat_id, chat_name, force=True)

            sender_name = (
                f"{LOCAL_NODE_NAME} → {receiver_name}"
                if chat_type == "dm"
                else LOCAL_NODE_NAME
            )

            with state_lock:
                add_message("me", sender_name, text, LOCAL_NODE_ID, final_chat_id, chat_name)

                if final_chat_id in chats:
                    reset_unread(final_chat_id)

                old = nodes.get(LOCAL_NODE_ID, {})
                info = get_node_info(LOCAL_NODE_ID)

                nodes[LOCAL_NODE_ID] = {
                    "name": LOCAL_NODE_NAME,
                    "node_id": LOCAL_NODE_ID,
                    "last_seen": time.time(),
                    "last_time": now(),
                    "rssi": old.get("rssi"),
                    "snr": old.get("snr"),
                    "hop_start": old.get("hop_start", ""),
                    "relay_node": old.get("relay_node", ""),
                    "last_text": (
                        f"sent to {receiver_name}: {text}"
                        if chat_type == "dm"
                        else f"sent: {text}"
                    ),
                    "short_name": info.get("short_name", old.get("short_name", "")),
                    "hw_model": info.get("hw_model", old.get("hw_model", "")),
                    "role": old.get("role", "CLIENT_BASE"),
                    "ignored": old.get("ignored", False),
                    "favorite": old.get("favorite", False)
                }

                save_nodes()

            return jsonify({
                "ok": True,
                "chat_id": final_chat_id,
                "chat_type": chat_type,
                "returncode": result.returncode
            })

        except subprocess.TimeoutExpired:
            with state_lock:
                add_message("rx", "SYSTEM ERROR", "send timeout", "", CHANNEL_CHAT_ID)

            return jsonify({"ok": False, "error": "timeout"}), 500

        except Exception as e:
            with state_lock:
                add_message("rx", "SYSTEM ERROR", f"send: {str(e)}", "", CHANNEL_CHAT_ID)

            return jsonify({"ok": False, "error": str(e)}), 500

        finally:
            time.sleep(2.0)
            pause_listen.clear()
            print("[SEND] Listener resumed", flush=True)