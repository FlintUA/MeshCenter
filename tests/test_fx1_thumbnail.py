"""FX1: the thumbnail of a Files card - made in memory, size capped, magic bytes verified, nothing stored."""
import io

import pytest
from PIL import Image

from meshsrv.attachments import thumbnail
from meshsrv.attachments.thumbnail import ThumbnailError, make_thumbnail

import test_mca_api_attachments as base       # the API test helpers (fake facade, vault fixtures)
from test_mca_api_attachments import vault_dir  # noqa: F401  (a fixture, used by name below)


def _image_bytes(fmt, size=(640, 480), mode="RGB", color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format=fmt)
    return out.getvalue()


def _thumb_size(data):
    image = Image.open(io.BytesIO(data))
    return image.size, image.format


@pytest.mark.parametrize("fmt, mime, mode, color", [
    ("JPEG", "image/jpeg", "RGB", (10, 200, 30)),
    ("PNG", "image/png", "RGBA", (10, 200, 30, 128)),
    ("WEBP", "image/webp", "RGB", (10, 200, 30)),
])
def test_an_image_becomes_a_small_jpeg_of_at_most_160_px(fmt, mime, mode, color):
    out = make_thumbnail(_image_bytes(fmt, (640, 480), mode, color), mime)
    size, out_format = _thumb_size(out)
    assert out_format == "JPEG" and max(size) <= thumbnail.THUMB_EDGE and size[0] > size[1], "the aspect ratio is kept"
    assert len(out) < 20_000


def test_the_magic_bytes_must_match_the_declared_type():
    png = _image_bytes("PNG")
    with pytest.raises(ThumbnailError) as info:
        make_thumbnail(png, "image/jpeg")                       # a sender-controlled header says jpeg, the bytes are a png
    assert info.value.code == "type_mismatch"
    with pytest.raises(ThumbnailError) as info:
        make_thumbnail(b"<html><script>alert(1)</script></html>" * 10, "image/png")
    assert info.value.code == "type_mismatch"
    with pytest.raises(ThumbnailError) as info:
        make_thumbnail(png, "application/pdf")
    assert info.value.code == "not_an_image"


def test_a_picture_beyond_the_pixel_cap_is_refused_before_it_is_decoded():
    big = _image_bytes("PNG", (3200, 3000))                      # 9.6 MP: a small file, a large bitmap
    with pytest.raises(ThumbnailError) as info:
        make_thumbnail(big, "image/png")
    assert info.value.code == "too_large"


def test_a_jpeg_is_decoded_at_a_fraction_of_its_size(monkeypatch):
    seen = {}
    from PIL import JpegImagePlugin
    real = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        seen.setdefault("draft", (mode, size))      # the first call is ours; Pillow's own thumbnail() calls draft again
        return real(self, mode, size)
    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    make_thumbnail(_image_bytes("JPEG", (2400, 1800)), "image/jpeg")
    assert seen["draft"][0] == "RGB", "Pillow's draft mode keeps the decoder from building the full bitmap"


def test_a_corrupt_image_is_undecodable_not_a_server_error():
    data = _image_bytes("JPEG")[:200] + b"\x00" * 40
    with pytest.raises(ThumbnailError) as info:
        make_thumbnail(data, "image/jpeg")
    assert info.value.code in ("undecodable", "type_mismatch")


def test_at_most_two_thumbnails_are_made_at_once(monkeypatch):
    monkeypatch.setattr(thumbnail, "WAIT_SECONDS", 0.05)
    held = [thumbnail._slots.acquire() for _ in range(thumbnail.MAX_CONCURRENT)]
    try:
        with pytest.raises(ThumbnailError) as info:
            make_thumbnail(_image_bytes("JPEG"), "image/jpeg")
        assert info.value.code == "busy"
    finally:
        for _ in held:
            thumbnail._slots.release()
    assert make_thumbnail(_image_bytes("JPEG"), "image/jpeg")


# ---- the route -------------------------------------------------------------------------------------------------------

def _plain_facade(tmp_path, data, mime, locator="files/photo.jpg", disposition=None):
    f = tmp_path / "photo.bin"
    f.write_bytes(data)
    facade = base._FakeFacade(
        snapshot=base._snapshot([base._attachment(
            direction="received", state="AVAILABLE", file_name="photo.jpg", mime_type=mime, plain_size=len(data),
            descriptor=base._descriptor(mime_type=mime, locator=locator, plain_size=len(data), disposition=disposition),
        )]),
        content_files={locator: f},
    )
    return facade


def test_the_route_serves_a_small_jpeg_with_the_security_headers(monkeypatch, tmp_path):
    facade = _plain_facade(tmp_path, _image_bytes("JPEG"), "image/jpeg")
    c = base._client(monkeypatch, facade)
    resp = c.get("/api/attachments/%s/thumbnail" % base.ATTACHMENT_ID)
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg"
    assert resp.headers["X-Content-Type-Options"] == "nosniff" and resp.headers["Content-Security-Policy"] == "sandbox"
    assert resp.headers["Cache-Control"].startswith("private")
    assert max(_thumb_size(resp.data)[0]) <= 160


def test_the_route_refuses_what_is_not_a_verified_image(monkeypatch, tmp_path):
    c = base._client(monkeypatch, _plain_facade(tmp_path, b"%PDF-1.4 hello", "application/pdf"))
    assert c.get("/api/attachments/%s/thumbnail" % base.ATTACHMENT_ID).status_code == 404      # downloads only
    c = base._client(monkeypatch, _plain_facade(tmp_path, b"<svg onload=alert(1)/>" * 8, "image/png"))
    assert c.get("/api/attachments/%s/thumbnail" % base.ATTACHMENT_ID).status_code == 415      # the header lies
    c = base._client(monkeypatch, _plain_facade(tmp_path, _image_bytes("PNG", (3200, 3000)), "image/png"))
    assert c.get("/api/attachments/%s/thumbnail" % base.ATTACHMENT_ID).status_code == 413


def test_the_route_needs_a_copy_on_this_node(monkeypatch):
    facade = base._FakeFacade(snapshot=base._snapshot([base._attachment(direction="received", state="AVAILABLE")]))
    c = base._client(monkeypatch, facade)
    assert c.get("/api/attachments/%s/thumbnail" % base.ATTACHMENT_ID).status_code == 404


def test_under_the_vault_the_thumbnail_is_made_from_the_encrypted_file_and_is_never_cached(monkeypatch, vault_dir, tmp_path):
    from _vault_spy import no_plaintext_writes
    data = _image_bytes("JPEG", (800, 600))
    rt, facade, codec_obj, stored = base._vault_content(monkeypatch, vault_dir, tmp_path, mime="image/jpeg", file_name="photo.jpg",
                                                       data=data, locator="cache/incoming/x.mcv")
    c = base._client(monkeypatch, facade)
    before = sorted(p.name for p in tmp_path.iterdir())
    with no_plaintext_writes():
        resp = c.get("/api/attachments/%s/thumbnail" % base.ATTACHMENT_ID)
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"
    assert max(_thumb_size(resp.data)[0]) <= 160
    assert sorted(p.name for p in tmp_path.iterdir()) == before, "nothing was written"
    rt.lock()
    assert c.get("/api/attachments/%s/thumbnail" % base.ATTACHMENT_ID).status_code == 423, "locked: no picture at all"
