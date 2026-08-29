import io
from pathlib import Path
from flask import Flask
from PIL import Image
from api.api_node_icons import register_node_icon_routes


def _create_test_app(tmp_path):
    app = Flask(__name__)
    app.testing = True

    def validator(node_id):
        import re
        return bool(re.fullmatch(r"![0-9a-fA-F]{8}", node_id))

    register_node_icon_routes(app, str(tmp_path), "!12345678", validator)
    return app


def test_node_icon_get_invalid_id(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

    # Attempt invalid node IDs
    resp = client.get("/api/nodes/invalid_node/icon")
    assert resp.status_code == 400
    assert resp.json["ok"] is False
    assert resp.json["error"] == "Invalid node_id"

    resp = client.get("/api/nodes/!short/icon")
    assert resp.status_code == 400
    assert resp.json["ok"] is False
    assert resp.json["error"] == "Invalid node_id"


def test_node_icon_get_not_found(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

    resp = client.get("/api/nodes/!12345678/icon")
    assert resp.status_code == 404
    assert resp.json["ok"] is False
    assert resp.json["error"] == "Node icon not found"


def test_node_icon_upload_get_and_delete(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

    # Create dummy PNG image in memory
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    # Upload icon
    resp = client.post(
        "/api/nodes/!12345678/icon",
        data={"icon": (buf, "test.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.json["ok"] is True
    assert resp.json["node_id"] == "!12345678"

    # Fetch icon
    resp = client.get("/api/nodes/!12345678/icon")
    assert resp.status_code == 200
    assert resp.content_type == "image/png"

    # Delete icon
    resp = client.delete("/api/nodes/!12345678/icon")
    assert resp.status_code == 200
    assert resp.json["ok"] is True

    # Confirm deleted
    resp = client.get("/api/nodes/!12345678/icon")
    assert resp.status_code == 404
