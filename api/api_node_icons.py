from pathlib import Path
from flask import jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError


def register_node_icon_routes(app, data_dir, local_node_id, node_id_validator):
    icons_dir = Path(data_dir) / "node_icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    def icon_path(node_id):
        """Defense-in-depth, not a fix for a confirmed vulnerability: every
        caller already validates node_id with is_valid_node_id() (server.py,
        `re.fullmatch(r"![0-9a-fA-F]{8}", value)`) before this is ever
        reached, and that regex already rejects any traversal-shaped value
        - verified directly against '!../../etc/passwd', '!../secret',
        '!....//....//etc/passwd', '!b0f14d2a/../../secret', all REJECTED.
        This guard exists so a future loosening of that regex (for an
        unrelated reason, without realizing this route depends on its
        strictness) fails safe here too, instead of only at the one call
        site that happened to prompt the change."""
        safe_name = node_id.lstrip("!").lower()
        target = (icons_dir / f"{safe_name}.png").resolve()
        if not target.is_relative_to(icons_dir.resolve()):
            raise ValueError(f"resolved outside icons_dir: {node_id!r}")
        return target

    @app.route("/api/nodes/<node_id>/icon", methods=["GET"])
    def get_node_icon(node_id):
        normalized_id = node_id if node_id.startswith("!") else f"!{node_id}"
        if not node_id_validator(normalized_id):
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
        normalized_id = node_id if node_id.startswith("!") else f"!{node_id}"
        if not node_id_validator(normalized_id):
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
        normalized_id = node_id if node_id.startswith("!") else f"!{node_id}"
        if not node_id_validator(normalized_id):
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        try:
            path = icon_path(normalized_id)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400
        if path.exists():
            path.unlink()
        return jsonify({"ok": True, "node_id": normalized_id})

    # Test-only seam: icon_path()'s own traversal guard needs to be
    # exercised directly, bypassing the route-level regex check, to prove
    # it holds on its own - Flask's <node_id> URL converter already
    # rejects any "/"-containing payload before it would ever reach a
    # view function, so a real HTTP request can't reach this closure with
    # a traversal-shaped value to test it that way. Unused by server.py's
    # own call site (return value previously discarded), so this is
    # additive, not a behavior change.
    return icon_path
