from flask import request, jsonify
import time
import subprocess


def register_chat_routes(
    app,
    state_lock,
    chats,
    nodes,
    messages,
    save_messages,
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
    prepare_radio_command,
    get_node_name,
    ensure_chat,
    add_message,
    reset_unread,
    get_node_info,
    save_nodes,
    now,
    radio_event,
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


    @app.route("/api/messages/delete", methods=["POST"])
    @handle_errors
    def api_delete_message():
        data = request.get_json(force=True)
        chat_id = str(data.get("chat_id", "")).strip()
        message_id = str(data.get("message_id", "")).strip()

        if not chat_id or not is_valid_node_id(chat_id):
            return jsonify({"ok": False, "error": "Invalid chat_id"}), 400

        if not message_id or len(message_id) > 128:
            return jsonify({"ok": False, "error": "Invalid message_id"}), 400

        deleted_message = None

        with state_lock:
            for index, message in enumerate(messages):
                if (
                    message.get("chat_id") == chat_id
                    and str(message.get("id", "")) == message_id
                ):
                    deleted_message = messages.pop(index)
                    break

            if deleted_message is None:
                return jsonify({"ok": False, "error": "Message not found"}), 404

            save_messages()

            remaining = [
                message for message in messages
                if message.get("chat_id") == chat_id
            ]

            if chat_id in chats:
                if remaining:
                    last_message = remaining[-1]
                    chats[chat_id]["last_message"] = last_message.get("text", "")
                    chats[chat_id]["last_time"] = last_message.get("time", "")
                else:
                    chats[chat_id]["last_message"] = ""
                    chats[chat_id]["last_time"] = ""

                chats[chat_id]["unread"] = 0
                save_chats()

        return jsonify({
            "ok": True,
            "chat_id": chat_id,
            "message_id": message_id
        })

    @app.route("/api/send", methods=["POST"])
    @handle_errors
    def api_send():
        data = request.get_json(force=True)

        text = sanitize_text(data.get("text", "").strip())
        target_node = data.get("target_node", "")
        chat_id = data.get("chat_id", "")
        reply_to = data.get("reply_to")

        if reply_to is not None and not isinstance(reply_to, dict):
            return jsonify({"ok": False, "error": "Invalid reply_to"}), 400

        if isinstance(reply_to, dict):
            raw_packet_id = reply_to.get("packet_id")
            try:
                reply_packet_id = int(raw_packet_id) if raw_packet_id is not None else None
            except (TypeError, ValueError):
                reply_packet_id = None

            reply_to = {
                "id": str(reply_to.get("id", ""))[:128],
                "packet_id": reply_packet_id,
                "sender": sanitize_text(str(reply_to.get("sender", "Unknown"))[:160]),
                "node_id": str(reply_to.get("node_id", ""))[:32],
                "text": sanitize_text(str(reply_to.get("text", ""))[:1000]),
                "time": str(reply_to.get("time", ""))[:32],
                "chat_id": str(reply_to.get("chat_id", chat_id))[:32],
                "chat_name": sanitize_text(str(reply_to.get("chat_name", ""))[:160])
            }

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

        reply_id = reply_to.get("packet_id") if isinstance(reply_to, dict) else None

        if isinstance(reply_to, dict) and reply_id is None:
            return jsonify({
                "ok": False,
                "error": "This message has no Meshtastic packet ID. Reply to a newly received message."
            }), 400

        try:
            print("[SEND] Preparing to send message", flush=True)
            print(
                f"[SEND] chat_type={chat_type}, final_chat_id={final_chat_id}, "
                f"receiver={receiver_name}, reply_id={reply_id}",
                flush=True
            )

            if not prepare_radio_command("/dev/ttyACM0", timeout=10):
                return jsonify({
                    "ok": False,
                    "error": "serial port busy: /dev/ttyACM0"
                }), 500

            sent_packet = None
            packet_id = None
            interface = None

            try:
                from meshtastic.serial_interface import SerialInterface

                with radio_lock:
                    interface = SerialInterface(devPath="/dev/ttyACM0")
                    destination = final_chat_id if chat_type == "dm" else "^all"

                    sent_packet = interface.sendText(
                        text=text,
                        destinationId=destination,
                        channelIndex=0,
                        replyId=reply_id,
                    )

                    packet_id = getattr(sent_packet, "id", None)
                    if packet_id is not None:
                        packet_id = int(packet_id)

                    print(
                        f"[SEND API] destination={destination}, packet_id={packet_id}, "
                        f"reply_id={reply_id}",
                        flush=True
                    )
            finally:
                if interface is not None:
                    try:
                        interface.close()
                    except Exception as close_error:
                        print(f"[SEND WARN] interface.close(): {close_error}", flush=True)

            radio_event("send")

            if chat_type == "dm" and final_chat_id not in chats:
                with state_lock:
                    ensure_chat(final_chat_id, chat_name, force=True)

            sender_name = (
                f"{LOCAL_NODE_NAME} → {receiver_name}"
                if chat_type == "dm"
                else LOCAL_NODE_NAME
            )

            with state_lock:
                add_message(
                    "me",
                    sender_name,
                    text,
                    LOCAL_NODE_ID,
                    final_chat_id,
                    chat_name,
                    reply_to=reply_to,
                    packet_id=packet_id
                )

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
                "packet_id": packet_id,
                "reply_id": reply_id
            })

        except subprocess.TimeoutExpired:
            radio_event(
                "send_error",
                "Meshtastic send timeout"
            )

            with state_lock:
                add_message(
                    "rx",
                    "SYSTEM ERROR",
                    "send timeout",
                    "",
                    CHANNEL_CHAT_ID
                )

            return jsonify({
                "ok": False,
                "error": "timeout"
            }), 500

        except Exception as e:
            radio_event(
                "send_error",
                str(e)
            )

            with state_lock:
                add_message(
                    "rx",
                    "SYSTEM ERROR",
                    f"send: {str(e)}",
                    "",
                    CHANNEL_CHAT_ID
                )

            return jsonify({
                "ok": False,
                "error": str(e)
            }), 500

        finally:
            time.sleep(2.0)
            pause_listen.clear()
            print("[SEND] Listener resumed", flush=True)