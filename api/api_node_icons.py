from pathlib import Path
from flask import jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError


def register_node_icon_routes(app, data_dir, local_node_id, node_id_validator):
    icons_dir = Path(data_dir) / "node_icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    def _normalize_and_validate(node_id):
        raw = str(node_id or "").strip()
        normalized = raw if raw.startswith("!") else f"!{raw}"
        if not node_id_validator(normalized):
            return None
        return normalized

    def icon_path(node_id):
        # Security: Defense-in-depth path traversal prevention
        safe_name = node_id.lstrip("!").lower()
        target = (icons_dir / f"{safe_name}.png").resolve()
        if not target.is_relative_to(icons_dir.resolve()):
            raise ValueError("Path traversal detected")
        return target

    @app.route("/api/nodes/<node_id>/icon", methods=["GET"])
    def get_node_icon(node_id):
        normalized_id = _normalize_and_validate(node_id)
        if not normalized_id:
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        try:
            path = icon_path(normalized_id)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        if not path.exists():
            return jsonify({"ok": False, "error": "Node icon not found"}), 404

        response = send_file(path, mimetype="image/png", conditional=True)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/api/nodes/<node_id>/icon", methods=["POST"])
    def upload_node_icon(node_id):
        normalized_id = _normalize_and_validate(node_id)
        if not normalized_id:
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        try:
            destination = icon_path(normalized_id)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        uploaded = request.files.get("icon")
        if uploaded is None or not uploaded.filename:
            return jsonify({"ok": False, "error": "No icon file supplied"}), 400

        uploaded.stream.seek(0, 2)
        size = uploaded.stream.tell()
        uploaded.stream.seek(0)
        if size > 2 * 1024 * 1024:
            return jsonify({"ok": False, "error": "Icon must be smaller than 2 MB"}), 413

        try:
            with Image.open(uploaded.stream) as source:
                source = ImageOps.exif_transpose(source).convert("RGBA")
                source.thumbnail((236, 236), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
                x = (256 - source.width) // 2
                y = (256 - source.height) // 2
                canvas.alpha_composite(source, (x, y))
                temporary = destination.with_suffix(".tmp")
                canvas.save(temporary, format="PNG", optimize=True)
                temporary.replace(destination)
        except (UnidentifiedImageError, OSError, ValueError):
            return jsonify({"ok": False, "error": "Unsupported or invalid image"}), 400

        return jsonify({
            "ok": True,
            "node_id": normalized_id,
            "icon_url": f"/api/nodes/{normalized_id}/icon?v={int(destination.stat().st_mtime)}",
        })

    @app.route("/api/nodes/<node_id>/icon", methods=["DELETE"])
    def delete_node_icon(node_id):
        normalized_id = _normalize_and_validate(node_id)
        if not normalized_id:
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        try:
            path = icon_path(normalized_id)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        if path.exists():
            path.unlink()
        return jsonify({"ok": True, "node_id": normalized_id})
