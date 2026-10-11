"""Lossless metadata stripping for uploads (app/image_strip.py).

JPEG, PNG and WebP are cleaned at the byte level: metadata goes, pixels stay identical, and a
huge image is never decoded. GIF/BMP/TIFF go through Pillow within a memory budget; one over it
is refused with a clear 413 instead of being stored with its metadata.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app.image_strip import ImageTooLarge, strip_image_metadata

SECRET = b"GPS-51.5N-0.12W"


def _jpeg(orientation: int | None = None, progressive: bool = False) -> bytes:
    img = Image.new("RGB", (64, 48), (200, 100, 50))
    exif = img.getexif()
    exif[270] = SECRET.decode()
    if orientation:
        exif[0x0112] = orientation
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif.tobytes(), comment=SECRET, icc_profile=b"ICC" * 20,
             progressive=progressive)
    return out.getvalue()


def _pixels(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.tobytes()


@pytest.mark.parametrize("progressive", [False, True])
def test_jpeg_metadata_goes_and_pixels_stay_identical(progressive):
    raw = _jpeg(progressive=progressive)
    clean = strip_image_metadata(raw, "image/jpeg")
    assert SECRET not in clean
    assert _pixels(clean) == _pixels(raw)
    assert Image.open(io.BytesIO(clean)).info.get("icc_profile") == b"ICC" * 20
    assert strip_image_metadata(clean, "image/jpeg") == clean  # idempotent


@pytest.mark.parametrize("orientation", [3, 6, 8])
def test_jpeg_orientation_survives_so_photos_stay_upright(orientation):
    clean = strip_image_metadata(_jpeg(orientation), "image/jpeg")
    assert SECRET not in clean
    exif = Image.open(io.BytesIO(clean)).getexif()
    assert dict(exif) == {0x0112: orientation}


def test_png_and_apng_text_and_exif_chunks_go():
    meta = PngInfo()
    meta.add_text("Source", SECRET.decode())
    meta.add_itxt("Where", SECRET.decode())
    frames = [Image.new("RGBA", (12, 12), (i * 60, 0, 0, 200)) for i in range(3)]
    out = io.BytesIO()
    frames[0].save(out, format="PNG", save_all=True, append_images=frames[1:], pnginfo=meta)
    raw = out.getvalue()
    clean = strip_image_metadata(raw, "image/png")
    assert SECRET not in clean
    assert Image.open(io.BytesIO(clean)).n_frames == 3
    assert _pixels(clean) == _pixels(raw)


def test_webp_exif_and_xmp_go():
    img = Image.new("RGB", (20, 20), (9, 9, 9))
    exif = img.getexif()
    exif[270] = SECRET.decode()
    out = io.BytesIO()
    img.save(out, format="WEBP", exif=exif.tobytes(), xmp=b"<x>" + SECRET + b"</x>", lossless=True)
    clean = strip_image_metadata(out.getvalue(), "image/webp")
    assert SECRET not in clean
    reopened = Image.open(io.BytesIO(clean))
    assert "exif" not in reopened.info and "xmp" not in reopened.info
    assert _pixels(clean) == _pixels(out.getvalue())


def test_a_huge_png_is_cleaned_without_decoding(monkeypatch):
    meta = PngInfo()
    meta.add_text("Source", SECRET.decode())
    out = io.BytesIO()
    Image.new("L", (7000, 7000)).save(out, format="PNG", pnginfo=meta)
    monkeypatch.setattr(Image.Image, "load", lambda self: pytest.fail("PNG was decoded"))
    clean = strip_image_metadata(out.getvalue(), "image/png")
    assert SECRET not in clean


def test_declared_type_does_not_decide_the_format():
    meta = PngInfo()
    meta.add_text("Source", SECRET.decode())
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(out, format="PNG", pnginfo=meta)
    assert SECRET not in strip_image_metadata(out.getvalue(), "image/jpeg")


def test_truncated_jpeg_is_returned_unchanged():
    raw = _jpeg()[:40]
    assert strip_image_metadata(raw, "image/jpeg") == raw


def test_an_oversized_bmp_is_refused():
    out = io.BytesIO()
    Image.new("RGB", (5000, 5000)).save(out, format="BMP")
    with pytest.raises(ImageTooLarge):
        strip_image_metadata(out.getvalue(), "image/bmp")


def test_upload_of_an_oversized_bmp_is_a_clear_413(admin_client, monkeypatch):
    from app import image_strip

    monkeypatch.setattr(image_strip, "_MAX_DECODE_BYTES", 100 * 100 * 4)
    pid = admin_client.get("/api/projects").json()[0]["id"]
    bug = admin_client.post("/api/bugs", json={"project_id": pid, "title": "Huge screenshot"}).json()
    out = io.BytesIO()
    Image.new("RGB", (200, 200)).save(out, format="BMP")
    res = admin_client.post(f"/api/bugs/{bug['id']}/attachments",
                            files={"file": ("big.bmp", out.getvalue(), "image/bmp")})
    assert res.status_code == 413, res.text
    assert "16 megapixels" in res.json()["detail"]
    assert admin_client.get(f"/api/bugs/{bug['id']}").json()["attachments"] == []
