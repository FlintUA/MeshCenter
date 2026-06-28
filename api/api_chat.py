from flask import request, jsonify


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
