from flask import request, jsonify
import time
import subprocess
import threading
import queue
import re

# Meshtastic protobufs: Data.payload max_size (see meshtastic/protobufs
# mesh.options, mesh_pb2.Constants.DATA_PAYLOAD_LEN) - the hard protocol
# ceiling for a single message's payload, not a UI preference. The
# meshtastic library's sendText()/sendData() raises "Data payload too
# big" if exceeded rather than chunking or truncating, so this is
# validated here (byte length, not character count - UTF-8 text like
# Cyrillic uses more bytes per character) to give the user an honest
# rejection before a send attempt fails deep in the send worker.
MESHTASTIC_MAX_PAYLOAD_BYTES = 233


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
    radio_lock,
    radio_session,
    RadioBusyError,
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
        # Only update the message's own status/error field. This used to
        # also post a synthetic "SYSTEM ERROR" chat message into the
        # primary channel regardless of which chat/channel actually failed
        # - confusing (the error showed up in an unrelated conversation)
        # and redundant now that the frontend surfaces failures both on the
        # message bubble itself (pending/failed badge + Retry) and as an
        # entry in the Notifications log.
        try:
            update_message_status(message_id, chat_id, "failed", error=error_text)
        except Exception as status_error:
            print(f"[SEND WORKER] Failed to update message status: {status_error}", flush=True)

    def _send_one(interface, job):
        text = job["text"]
        final_chat_id = job["final_chat_id"]
        chat_type = job["chat_type"]
        channel_index = job["channel_index"]
        reply_id = job["reply_id"]
        message_id = job["message_id"]
        receiver_name = job["receiver_name"]

        try:
            destination = final_chat_id if chat_type == "dm" else "^all"
            # Broadcast has no per-recipient ACK in the Meshtastic protocol
            # itself - only direct messages can be meaningfully confirmed,
            # so only they ask for one. The short-lived SerialInterface used
            # here closes right after this call returns, long before any
            # ACK could arrive - actual delivery confirmation is picked up
            # later from --listen's own output (see process_routing_ack_line
            # in server.py), not from an onResponse callback on this object.
            want_ack = chat_type == "dm"

            sent_packet = interface.sendText(
                text=text,
                destinationId=destination,
                wantAck=want_ack,
                channelIndex=channel_index,
                replyId=reply_id,
            )

            packet_id = getattr(sent_packet, "id", None)
            if packet_id is not None:
                packet_id = int(packet_id)

            print(
                f"[SEND WORKER] destination={destination}, channel_index={channel_index}, "
                f"packet_id={packet_id}, reply_id={reply_id}, want_ack={want_ack}",
                flush=True
            )

            radio_event("send")
            update_message_status(
                message_id, final_chat_id, "sent",
                packet_id=packet_id, ack_requested=want_ack,
            )

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

    def _process_send_batch(batch):
        """Send every job already queued in one pause/connect/resume cycle.

        Pausing the listener (prepare_radio_command -> stop_listener) alone
        costs at least ~1.5s, plus wait_serial_release polling, plus the 2s
        cooldown before the listener resumes - all before any actual serial
        I/O. Doing that separately for every message in a quick burst (e.g.
        three messages sent back to back) repeatedly kills and restarts the
        --listen subprocess and reopens the serial connection in rapid
        succession, which is what produced occasional "Timed out waiting
        for connection completion" failures. Draining the queue and sharing
        a single SerialInterface across the whole batch avoids that.
        """
        if not batch:
            return

        if not is_radio_available():
            for job in batch:
                _mark_failed(job["message_id"], job["final_chat_id"], "radio released for external configuration")
            return

        try:
            with radio_session(device=active_serial_port(), timeout=10, cooldown=2.0):
                interface = None
                try:
                    from meshtastic.serial_interface import SerialInterface

                    interface = SerialInterface(devPath=active_serial_port())
                    for job in batch:
                        _send_one(interface, job)
                finally:
                    if interface is not None:
                        try:
                            interface.close()
                        except Exception as close_error:
                            print(f"[SEND WARN] interface.close(): {close_error}", flush=True)
        except RadioBusyError:
            for job in batch:
                _mark_failed(job["message_id"], job["final_chat_id"], f"serial port busy: {active_serial_port()}")
            return
        finally:
            print("[SEND] Listener resumed", flush=True)

    # How long to wait, after the first job in a new batch arrives, for
    # more jobs to land before committing to a batch. Draining with
    # get_nowait() right after the first get() only catches jobs that
    # happened to already be enqueued at that exact instant - a genuine
    # race against the HTTP request thread(s) doing the put(), so whether
    # two messages sent moments apart shared one pause/connect/resume
    # cycle depended on scheduling luck instead of working reliably.
    BATCH_ACCUMULATION_WINDOW_SECONDS = 0.1

    def send_worker():
        while True:
            job = send_queue.get()
            # One get() (blocking or via get_nowait() below) must be matched
            # by exactly one task_done() once that job's batch has actually
            # been processed - not immediately after get(), which would
            # mark the job "done" while it's still sitting in `batch`
            # waiting to be sent. Nothing currently calls send_queue.join(),
            # but task_done() bookkeeping should still reflect reality
            # rather than being a formality that happens to not matter yet.
            pending_jobs = [job]

            deadline = time.monotonic() + BATCH_ACCUMULATION_WINDOW_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    extra = send_queue.get(timeout=remaining)
                except queue.Empty:
                    break
                pending_jobs.append(extra)

            batch = [j for j in pending_jobs if j is not None]

            try:
                if batch:
                    _process_send_batch(batch)
            except Exception as worker_error:
                print(f"[SEND WORKER] Unexpected error: {worker_error}", flush=True)
                for failed_job in batch:
                    try:
                        _mark_failed(failed_job.get("message_id"), failed_job.get("final_chat_id"), str(worker_error))
                    except Exception:
                        pass
            finally:
                for _ in pending_jobs:
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
                "name": f"{CHANNEL_CHAT_NAME} [0]",
                "type": "channel",
                "is_channel": True,
                "is_demo": False,
                "last_message": "",
                "last_time": "",
                "unread": 0,
            }]

        try:
            with radio_session(device=active_serial_port(), timeout=10, cooldown=2.0):
                from meshtastic.serial_interface import SerialInterface
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

                # Give the radio extra time to sync secondary channel roles —
                # they can briefly still report role=DISABLED (their unset
                # default) right after waitForConfig() returns, before the
                # full config has actually finished trickling in.
                time.sleep(1.5)

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
                        "name": f"{name} [{index}]",
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
            # pause_listen/cooldown are handled by radio_session() itself;
            # only the SerialInterface needs closing here.
            if interface is not None:
                try:
                    interface.close()
                except Exception:
                    pass

        if discovered:
            # One item per slot, ordered exactly as on the radio.
            by_index = {item["index"]: item for item in discovered}
            discovered = [by_index[index] for index in sorted(by_index)]
            with channel_cache_lock:
                channel_cache["timestamp"] = now_ts
                channel_cache["channels"] = [dict(item) for item in discovered]

            # Auto-seed chats.json for newly discovered channels so they
            # appear in the UI immediately, without waiting for a first
            # incoming message. chats/state_lock/save_chats are already
            # available as closures from register_chat_routes()'s
            # parameters — no need to import them from server.
            try:
                with state_lock:
                    seeded = False
                    for ch in discovered:
                        chat_id = ch["id"]
                        if chat_id in chats:
                            continue
                        # ch["name"] is always "{base} [{index}]" (see the
                        # f-string above) — strip that exact suffix rather
                        # than a blind rsplit(" ["), which would mangle a
                        # channel name that itself legitimately contains " [".
                        suffix = f" [{ch['index']}]"
                        bare_name = ch["name"][:-len(suffix)] if ch["name"].endswith(suffix) else ch["name"]
                        chats[chat_id] = {
                            "id": chat_id,
                            "name": bare_name,
                            "type": "channel",
                            "last_message": "",
                            "last_time": "",
                            "unread": 0,
                        }
                        print(f"[CHANNELS] Auto-seeded chat: {chat_id} ({bare_name})", flush=True)
                        seeded = True
                    if seeded:
                        save_chats()
            except Exception as seed_error:
                print(f"[CHANNELS] Auto-seed warning: {seed_error}", flush=True)
                # Non-fatal — channel will appear after first incoming message

            return discovered

        # Keep the previous valid configuration during a temporary serial conflict.
        if cached_channels and discovery_error is not None:
            return cached_channels

        # A primary channel is always a safe final fallback on first startup.
        fallback = [{
            "id": CHANNEL_CHAT_ID,
            "index": 0,
            "name": f"{CHANNEL_CHAT_NAME} [0]",
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
                "error": "Invalid chat_id",
                "error_code": "invalid_chat_id"
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
            return jsonify({"ok": False, "error": "Invalid chat_id", "error_code": "invalid_chat_id"}), 400

        if not message_id or len(message_id) > 128:
            return jsonify({"ok": False, "error": "Invalid message_id", "error_code": "invalid_message_id"}), 400

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
                return jsonify({"ok": False, "error": "Message not found", "error_code": "message_not_found"}), 404

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
            return jsonify({"ok": False, "error": "Invalid reply_to", "error_code": "invalid_reply_to"}), 400

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
            return jsonify({"ok": False, "error": "empty or invalid message", "error_code": "empty_message"}), 400

        text_bytes = len(text.encode("utf-8"))
        if text_bytes > MESHTASTIC_MAX_PAYLOAD_BYTES:
            return jsonify({
                "ok": False,
                "error": f"Message too long: {text_bytes} bytes, max {MESHTASTIC_MAX_PAYLOAD_BYTES}",
                "error_code": "message_too_long",
                "error_params": {"bytes": text_bytes, "max": MESHTASTIC_MAX_PAYLOAD_BYTES},
            }), 400

        if chat_id and not is_channel_chat_id(chat_id) and not is_valid_node_id(chat_id):
            return jsonify({"ok": False, "error": "Invalid chat_id", "error_code": "invalid_chat_id"}), 400

        if target_node and not is_valid_node_id(target_node):
            return jsonify({"ok": False, "error": "Invalid target_node", "error_code": "invalid_target_node"}), 400

        if target_node and target_node.startswith("!") and target_node not in nodes:
            print(f"[SEND] Target node not in nodes cache, sending anyway: {target_node}", flush=True)

        final_chat_id = CHANNEL_CHAT_ID
        receiver_name = "Broadcast"
        chat_name = f"{CHANNEL_CHAT_NAME} [0]"
        chat_type = "channel"
        channel_index = 0

        if is_channel_chat_id(chat_id):
            final_chat_id = chat_id
            channel_index = 0 if chat_id == CHANNEL_CHAT_ID else int(chat_id.split(":", 1)[1])
            configured = next((c for c in discover_radio_channels() if c.get("id") == chat_id and not c.get("is_demo")), None)
            if configured is None:
                return jsonify({"ok": False, "error": "Channel is not configured on the radio", "error_code": "channel_not_configured"}), 400
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
                "error": "This message has no Meshtastic packet ID. Reply to a newly received message.",
                "error_code": "reply_missing_packet_id"
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

    @app.route("/api/send/retry", methods=["POST"])
    @handle_errors
    def api_send_retry():
        """Re-attempt sending a message that already exists server-side with
        status="failed", reusing its own id instead of the /api/send path,
        which would create a second, independent message record for the
        same content - the retry button used to leave both the old failed
        bubble and a new one in the chat history.
        """
        if not is_radio_available():
            return jsonify({
                "ok": False,
                "error": "The radio is released for external configuration",
                "error_code": "radio_released"
            }), 409

        data = request.get_json(force=True)
        chat_id = str(data.get("chat_id", "")).strip()
        message_id = str(data.get("message_id", "")).strip()

        if not chat_id or not (is_valid_node_id(chat_id) or is_channel_chat_id(chat_id)):
            return jsonify({"ok": False, "error": "Invalid chat_id", "error_code": "invalid_chat_id"}), 400
        if not message_id or len(message_id) > 128:
            return jsonify({"ok": False, "error": "Invalid message_id", "error_code": "invalid_message_id"}), 400

        with state_lock:
            target = None
            for message in reversed(messages):
                if message.get("chat_id") == chat_id and str(message.get("id", "")) == message_id:
                    target = message
                    break

            if target is None:
                return jsonify({"ok": False, "error": "Message not found", "error_code": "message_not_found"}), 404
            if target.get("kind") != "me":
                return jsonify({"ok": False, "error": "Only outgoing messages can be retried", "error_code": "retry_not_outgoing"}), 400
            if target.get("status") != "failed":
                return jsonify({"ok": False, "error": "Only failed messages can be retried", "error_code": "retry_not_failed"}), 400

            text = str(target.get("text", ""))
            reply_to = target.get("reply_to")

            if not text:
                return jsonify({"ok": False, "error": "Message has no text to resend", "error_code": "retry_empty_text"}), 400

            target["status"] = "pending"
            target.pop("error", None)
            save_messages()

        if is_channel_chat_id(chat_id):
            chat_type = "channel"
            channel_index = 0 if chat_id == CHANNEL_CHAT_ID else int(chat_id.split(":", 1)[1])
            receiver_name = "Broadcast"
        else:
            chat_type = "dm"
            channel_index = 0
            receiver_name = get_node_name(chat_id)

        reply_id = reply_to.get("packet_id") if isinstance(reply_to, dict) else None

        send_queue.put({
            "text": text,
            "final_chat_id": chat_id,
            "chat_type": chat_type,
            "channel_index": channel_index,
            "reply_id": reply_id,
            "message_id": message_id,
            "receiver_name": receiver_name,
        })

        return jsonify({
            "ok": True,
            "chat_id": chat_id,
            "message_id": message_id,
            "status": "pending"
        }), 202