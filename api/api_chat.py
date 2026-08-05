from flask import request, jsonify
import time
import subprocess
import threading
import queue
import re


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
    get_meshtastic_port,
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
    is_radio_available,
    update_message_status,
):
    def active_serial_port():
        port = str(get_meshtastic_port() or "").strip()
        if not port:
            raise RuntimeError("Active Meshtastic serial port is not configured")
        return port

    # ------------------------------------------------------------------
    # Background send worker.
    #
    # Talking to the radio (opening a fresh SerialInterface, waiting for the
    # listener to release the port, the mandatory cooldown before resuming
    # --listen) reliably takes several seconds. /api/send used to do all of
    # that inline and only answer the HTTP request once it was done, so the
    # UI sat on a disabled "Sending..." button for the whole round trip.
    #
    # Now /api/send only validates the request, stores the message with
    # status="pending" and hands the actual transmission to this worker.
    # The frontend renders the message immediately and later sees it flip
    # to "sent"/"failed" through the existing message-polling endpoint.
    # ------------------------------------------------------------------
    send_queue = queue.Queue()

    def _mark_failed(message_id, chat_id, error_text):
        try:
            update_message_status(message_id, chat_id, "failed", error=error_text)
        except Exception as status_error:
            print(f"[SEND WORKER] Failed to update message status: {status_error}", flush=True)
        with state_lock:
            add_message("rx", "SYSTEM ERROR", f"send: {error_text}", "", CHANNEL_CHAT_ID)

    def _process_send_job(job):
        text = job["text"]
        final_chat_id = job["final_chat_id"]
        chat_type = job["chat_type"]
        channel_index = job["channel_index"]
        reply_id = job["reply_id"]
        message_id = job["message_id"]
        receiver_name = job["receiver_name"]

        if not is_radio_available():
            _mark_failed(message_id, final_chat_id, "radio released for external configuration")
            return

        try:
            if not prepare_radio_command(active_serial_port(), timeout=10):
                _mark_failed(message_id, final_chat_id, f"serial port busy: {active_serial_port()}")
                return

            sent_packet = None
            packet_id = None
            interface = None

            try:
                from meshtastic.serial_interface import SerialInterface

                with radio_lock:
                    interface = SerialInterface(devPath=active_serial_port())
                    destination = final_chat_id if chat_type == "dm" else "^all"

                    sent_packet = interface.sendText(
                        text=text,
                        destinationId=destination,
                        channelIndex=channel_index,
                        replyId=reply_id,
                    )

                    packet_id = getattr(sent_packet, "id", None)
                    if packet_id is not None:
                        packet_id = int(packet_id)

                    print(
                        f"[SEND WORKER] destination={destination}, channel_index={channel_index}, "
                        f"packet_id={packet_id}, reply_id={reply_id}",
                        flush=True
                    )
            finally:
                if interface is not None:
                    try:
                        interface.close()
                    except Exception as close_error:
                        print(f"[SEND WARN] interface.close(): {close_error}", flush=True)

            radio_event("send")
            update_message_status(message_id, final_chat_id, "sent", packet_id=packet_id)

            with state_lock:
                old = nodes.get(LOCAL_NODE_ID, {})
                info = get_node_info(LOCAL_NODE_ID)

                # Merge on top of the existing record instead of replacing it
                # outright, so telemetry collected between messages
                # (device_metrics / environment_metrics / power_metrics)
                # survives every outgoing send instead of being wiped here.
                node = dict(old)
                node.update({
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
                    "favorite": old.get("favorite", False),
                    "position": old.get("position"),
                })
                nodes[LOCAL_NODE_ID] = node
                save_nodes()

        except subprocess.TimeoutExpired:
            radio_event("send_error", "Meshtastic send timeout")
            _mark_failed(message_id, final_chat_id, "timeout")

        except Exception as e:
            radio_event("send_error", str(e))
            _mark_failed(message_id, final_chat_id, str(e))

        finally:
            time.sleep(2.0)
            pause_listen.clear()
            print("[SEND] Listener resumed", flush=True)

    def send_worker():
        while True:
            job = send_queue.get()
            try:
                if job is None:
                    continue
                _process_send_job(job)
            except Exception as worker_error:
                print(f"[SEND WORKER] Unexpected error: {worker_error}", flush=True)
                try:
                    _mark_failed(job.get("message_id"), job.get("final_chat_id"), str(worker_error))
                except Exception:
                    pass
            finally:
                send_queue.task_done()

    threading.Thread(target=send_worker, daemon=True, name="mc-send-worker").start()

    CHANNEL_CACHE_TTL_SECONDS = 300
    channel_cache = {"timestamp": 0.0, "channels": []}
    channel_cache_lock = threading.Lock()

    def channel_chat_id(index):
        return CHANNEL_CHAT_ID if int(index) == 0 else f"channel:{int(index)}"

    def is_channel_chat_id(value):
        return value == CHANNEL_CHAT_ID or bool(re.fullmatch(r"channel:[1-7]", str(value or "")))

    def is_radio_lock_busy():
        """Return True when another thread currently owns the shared radio lock.

        threading.RLock does not expose locked() on all supported Python
        versions, so use a non-blocking acquire test instead.
        """
        acquired = radio_lock.acquire(blocking=False)
        if acquired:
            radio_lock.release()
            return False
        return True

    def discover_radio_channels(force=False):
        """Read the active channel configuration from the connected radio.

        The short cache prevents the 10-second UI poll from reopening the serial
        connection on every request.  A failed refresh never replaces a known-good
        channel list, so a temporarily busy radio does not make channels disappear.
        """
        now_ts = time.time()
        with channel_cache_lock:
            cached_channels = [dict(item) for item in channel_cache["channels"]]
            cache_age = now_ts - channel_cache["timestamp"]
            # `force` used to be accepted but never actually consulted here,
            # so the frontend's "give me a fresh read on first load"
            # (?refresh_channels=1) request silently returned whatever was
            # already cached instead of re-reading the radio.
            if not force and cached_channels and cache_age < CHANNEL_CACHE_TTL_SECONDS:
                return cached_channels

        discovered = []
        interface = None
        discovery_error = None

        if not is_radio_available() or is_radio_lock_busy():
            if cached_channels:
                return cached_channels
            return [{
                "id": CHANNEL_CHAT_ID,
                "index": 0,
                "name": CHANNEL_CHAT_NAME,
                "type": "channel",
                "is_channel": True,
                "is_demo": False,
                "last_message": "",
                "last_time": "",
                "unread": 0,
            }]

        try:
            if not prepare_radio_command(active_serial_port(), timeout=10):
                raise RuntimeError("serial port busy")

            from meshtastic.serial_interface import SerialInterface
            with radio_lock:
                interface = SerialInterface(devPath=active_serial_port())

                # SerialInterface() returns as soon as the initial handshake
                # (myInfo) is in, but the channel list keeps trickling in
                # afterwards as part of the same config stream. Reading
                # interface.localNode.channels immediately after connecting
                # can catch it half-populated - primary (index 0) tends to
                # arrive first, so it looked fine while secondary channels
                # briefly reported role=DISABLED (their unset default) and
                # got filtered out below, only to show up on a later
                # discovery call once the full config had synced.
                wait_for_config = getattr(interface, "waitForConfig", None)
                if callable(wait_for_config):
                    try:
                        wait_for_config()
                    except Exception as wait_error:
                        print(f"[CHANNELS] waitForConfig() warning: {wait_error}", flush=True)

                raw_channels = getattr(getattr(interface, "localNode", None), "channels", None) or []

                for fallback_index, channel in enumerate(raw_channels):
                    index = getattr(channel, "index", fallback_index)
                    try:
                        index = int(index)
                    except (TypeError, ValueError):
                        index = fallback_index

                    # Meshtastic currently exposes channel slots 0 through 7.
                    if index < 0 or index > 7:
                        continue

                    settings_obj = getattr(channel, "settings", None)
                    name = getattr(settings_obj, "name", "") if settings_obj is not None else ""
                    role = getattr(channel, "role", None)
                    role_text = str(role or "").upper()
                    disabled = "DISABLED" in role_text or role == 0
                    if disabled:
                        continue

                    if not name:
                        name = CHANNEL_CHAT_NAME if index == 0 else f"Channel {index}"

                    discovered.append({
                        "id": channel_chat_id(index),
                        "index": index,
                        "name": str(name),
                        "type": "channel",
                        "is_channel": True,
                        "is_demo": False,
                        "last_message": "",
                        "last_time": "",
                        "unread": 0,
                    })
        except Exception as error:
            discovery_error = error
            print(f"[CHANNELS] Discovery warning: {error}", flush=True)
        finally:
            if interface is not None:
                try:
                    interface.close()
                except Exception:
                    pass
            if is_radio_available():
                pause_listen.clear()

        if discovered:
            # One item per slot, ordered exactly as on the radio.
            by_index = {item["index"]: item for item in discovered}
            discovered = [by_index[index] for index in sorted(by_index)]
            with channel_cache_lock:
                channel_cache["timestamp"] = now_ts
                channel_cache["channels"] = [dict(item) for item in discovered]
            return discovered

        # Keep the previous valid configuration during a temporary serial conflict.
        if cached_channels and discovery_error is not None:
            return cached_channels

        # A primary channel is always a safe final fallback on first startup.
        fallback = [{
            "id": CHANNEL_CHAT_ID,
            "index": 0,
            "name": CHANNEL_CHAT_NAME,
            "type": "channel",
            "is_channel": True,
            "is_demo": False,
            "last_message": "",
            "last_time": "",
            "unread": 0,
        }]
        with channel_cache_lock:
            channel_cache["timestamp"] = now_ts
            channel_cache["channels"] = [dict(item) for item in fallback]
        return fallback

    @app.route("/api/chats")
    def api_chats():
        chat_list, total_unread = get_chats_list()
        force_channel_refresh = request.args.get("refresh_channels", "").lower() in {"1", "true", "yes"}
        channels = discover_radio_channels(force=force_channel_refresh)
        chat_by_id = {item.get("id"): item for item in chat_list}
        for channel in channels:
            stored = chat_by_id.get(channel.get("id"), {})
            for key in ("last_message", "last_time", "unread", "last_sender"):
                if stored.get(key) not in (None, "", 0):
                    channel[key] = stored.get(key)
        return jsonify({
            "chats": chat_list,
            "channels": channels,
            "total_unread": total_unread
        })

    @app.route("/api/messages")
    def api_messages():
        chat_id = request.args.get("chat_id", "").strip()

        if chat_id and not (is_valid_node_id(chat_id) or is_channel_chat_id(chat_id)):
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

        if not chat_id or not (is_valid_node_id(chat_id) or is_channel_chat_id(chat_id)):
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
        if not is_radio_available():
            return jsonify({
                "ok": False,
                "error": "The radio is released for external configuration",
                "error_code": "radio_released"
            }), 409

        data = request.get_json(force=True)

        text = sanitize_text(data.get("text", "").strip())
        target_node = data.get("target_node", "")
        chat_id = data.get("chat_id", "")
        reply_to = data.get("reply_to")
        # Optional id generated by the frontend for its optimistic bubble.
        # Echoed back (and stored on the message) purely so the UI can match
        # its local placeholder to the authoritative server copy.
        client_id = str(data.get("client_id", "")).strip()[:64]

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

        if chat_id and not is_channel_chat_id(chat_id) and not is_valid_node_id(chat_id):
            return jsonify({"ok": False, "error": "Invalid chat_id"}), 400

        if target_node and not is_valid_node_id(target_node):
            return jsonify({"ok": False, "error": "Invalid target_node"}), 400

        if target_node and target_node.startswith("!") and target_node not in nodes:
            print(f"[SEND] Target node not in nodes cache, sending anyway: {target_node}", flush=True)

        final_chat_id = CHANNEL_CHAT_ID
        receiver_name = "Broadcast"
        chat_name = CHANNEL_CHAT_NAME
        chat_type = "channel"
        channel_index = 0

        if is_channel_chat_id(chat_id):
            final_chat_id = chat_id
            channel_index = 0 if chat_id == CHANNEL_CHAT_ID else int(chat_id.split(":", 1)[1])
            configured = next((c for c in discover_radio_channels() if c.get("id") == chat_id and not c.get("is_demo")), None)
            if configured is None:
                return jsonify({"ok": False, "error": "Channel is not configured on the radio"}), 400
            chat_name = configured.get("name", f"Channel {channel_index}")
        elif chat_id and chat_id != CHANNEL_CHAT_ID and chat_id.startswith("!"):
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

        print("[SEND] Queueing message", flush=True)
        print(
            f"[SEND] chat_type={chat_type}, final_chat_id={final_chat_id}, "
            f"receiver={receiver_name}, reply_id={reply_id}",
            flush=True
        )

        sender_name = (
            f"{LOCAL_NODE_NAME} → {receiver_name}"
            if chat_type == "dm"
            else LOCAL_NODE_NAME
        )

        # Everything below is in-memory bookkeeping only (no serial I/O), so
        # it stays on the request thread and returns fast. The message is
        # visible immediately (status="pending"); the actual radio
        # transmission happens on the background send_worker() thread.
        with state_lock:
            msg = add_message(
                "me",
                sender_name,
                text,
                LOCAL_NODE_ID,
                final_chat_id,
                chat_name,
                reply_to=reply_to,
                status="pending",
                client_id=client_id or None,
            )

            if final_chat_id in chats:
                reset_unread(final_chat_id)

        send_queue.put({
            "text": text,
            "final_chat_id": final_chat_id,
            "chat_type": chat_type,
            "channel_index": channel_index,
            "reply_id": reply_id,
            "message_id": msg["id"],
            "receiver_name": receiver_name,
        })

        return jsonify({
            "ok": True,
            "chat_id": final_chat_id,
            "chat_type": chat_type,
            "message_id": msg["id"],
            "client_id": client_id or None,
            "reply_id": reply_id,
            "status": "pending"
        }), 202