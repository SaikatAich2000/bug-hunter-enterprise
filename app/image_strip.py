"""Metadata stripper for uploaded images.

JPEG, PNG and WebP (nearly every screenshot and photo) are cleaned at the byte level: the
EXIF, XMP, IPTC, comment and text segments are dropped and everything else is copied
unchanged. Nothing is decoded, so the pixels keep their exact quality and a huge image costs no
more memory than its file. A JPEG's EXIF orientation is kept (as a minimal EXIF block holding
only that tag) so photos still display the right way up.

Other raster formats (GIF, BMP, TIFF) are re-encoded through Pillow with their metadata
cleared, within a pixel-memory budget and one decode at a time; an image over the budget is
refused with ImageTooLarge rather than stored with its metadata. Bytes that are not a readable
image are returned unchanged (fail open), so cleanup never blocks a legitimate upload.
"""
from __future__ import annotations

import io
import logging
import struct
import threading

logger = logging.getLogger("bug_hunter.image_strip")

_RASTER_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp",
                    "image/bmp", "image/tiff")

# Pillow path only: decoded pixel memory allowed for one image (16 MP at 4 bytes per pixel),
# and how many decodes may run at once.
_MAX_DECODE_BYTES = 64 * 1024 * 1024
_DECODES = threading.BoundedSemaphore(1)
_DECODE_WAIT_SECONDS = 60


class ImageTooLarge(ValueError):
    """The image cannot be cleaned within the server's memory budget."""


# --- JPEG -------------------------------------------------------------------------------

_SOI, _SOS, _EOI = 0xD8, 0xDA, 0xD9
_APP0, _APP1, _APP2, _APP14, _COM = 0xE0, 0xE1, 0xE2, 0xEE, 0xFE


def _jpeg_orientation(payload: bytes) -> int:
    """The orientation tag of an EXIF APP1 payload, or 1 when absent/unreadable."""
    if not payload.startswith(b"Exif\x00\x00") or len(payload) < 14:
        return 1
    tiff = payload[6:]
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None:
        return 1
    try:
        (ifd,) = struct.unpack_from(order + "I", tiff, 4)
        (count,) = struct.unpack_from(order + "H", tiff, ifd)
        for i in range(min(count, 512)):
            tag, typ, n = struct.unpack_from(order + "HHI", tiff, ifd + 2 + 12 * i)
            if tag == 0x0112 and typ == 3 and n == 1:
                (value,) = struct.unpack_from(order + "H", tiff, ifd + 2 + 12 * i + 8)
                return value if 1 <= value <= 8 else 1
    except struct.error:
        return 1
    return 1


def _orientation_segment(orientation: int) -> bytes:
    payload = (b"Exif\x00\x00" + b"MM\x00\x2a\x00\x00\x00\x08" + b"\x00\x01"
               + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + b"\x00\x00\x00\x00")
    return b"\xff" + bytes([_APP1]) + struct.pack(">H", len(payload) + 2) + payload


def _keep_jpeg_segment(marker: int, payload: bytes) -> bool:
    if marker in (_APP0, _APP14):  # JFIF header; Adobe colour transform
        return True
    if marker == _APP2:  # keep the ICC colour profile, drop FlashPix/MPF
        return payload.startswith(b"ICC_PROFILE\x00")
    return not (0xE0 <= marker <= 0xEF or marker == _COM)


def _strip_jpeg(data: bytes) -> bytes | None:
    out = bytearray(data[:2])
    orientation = 1
    pos = 2
    while pos < len(data):
        if data[pos] != 0xFF:
            return None
        while pos < len(data) and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= len(data):
            return None
        marker = data[pos]
        pos += 1
        if marker == _EOI or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += bytes([0xFF, marker])
            continue
        if pos + 2 > len(data):
            return None
        (length,) = struct.unpack_from(">H", data, pos)
        if length < 2 or pos + length > len(data):
            return None
        segment = data[pos - 2:pos + length]
        payload = data[pos + 2:pos + length]
        if marker == _SOS:
            # Scan data follows; everything from here on is copied as is.
            if orientation != 1:
                insert_at = 2
                if len(out) > 4 and out[2:4] == b"\xff\xe0":
                    insert_at = 4 + struct.unpack_from(">H", out, 4)[0]
                out[insert_at:insert_at] = _orientation_segment(orientation)
            return bytes(out) + data[pos - 2:]
        if marker == _APP1:
            orientation = _jpeg_orientation(payload) if orientation == 1 else orientation
        if _keep_jpeg_segment(marker, payload):
            out += segment
        pos += length
    return None


# --- PNG ---------------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}


def _strip_png(data: bytes) -> bytes | None:
    out = bytearray(_PNG_SIGNATURE)
    pos = len(_PNG_SIGNATURE)
    while pos + 12 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            return None
        if kind not in _PNG_DROP:
            out += data[pos:end]
        pos = end
        if kind == b"IEND":
            return bytes(out)
    return None


# --- WebP --------------------------------------------------------------------------------

_WEBP_DROP = {b"EXIF", b"XMP "}
_VP8X_EXIF_XMP_FLAGS = 0x08 | 0x04


def _strip_webp(data: bytes) -> bytes | None:
    chunks = bytearray()
    pos = 12
    while pos + 8 <= len(data):
        kind = data[pos:pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        end = pos + 8 + size + (size & 1)
        if pos + 8 + size > len(data):
            return None
        chunk = bytearray(data[pos:min(end, len(data))])
        if kind == b"VP8X" and size >= 1:
            chunk[8] &= ~_VP8X_EXIF_XMP_FLAGS & 0xFF
        if kind not in _WEBP_DROP:
            chunks += chunk
        pos = end
    if pos < len(data) - 1:
        return None
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WEBP" + bytes(chunks)


# --- Other formats (Pillow) --------------------------------------------------------------

def _bytes_per_pixel(mode: str) -> int:
    # Pillow keeps multi-band images at 4 bytes per pixel in memory.
    return {"1": 1, "L": 1, "P": 1, "I;16": 2, "LA": 4, "PA": 4}.get(mode, 4)


def _strip_with_pillow(data: bytes, ct: str) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return data  # No Pillow: the operator opted out.
    try:
        with io.BytesIO(data) as src:
            img = Image.open(src)
            width, height = img.size
            if width * height * _bytes_per_pixel(img.mode) > _MAX_DECODE_BYTES:
                raise ImageTooLarge(f"{width}×{height}")
            # Multi-frame images (animated GIF, multi-page TIFF) would lose frames on save.
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                return data
            if not _DECODES.acquire(timeout=_DECODE_WAIT_SECONDS):
                raise ImageTooLarge("busy")
            try:
                img.load()
                fmt = img.format
                if fmt is None:
                    return data
                # Keep palette transparency, or GIFs and palette images turn opaque.
                transparency = img.info.get("transparency")
                img.info = {}
                out = io.BytesIO()
                if transparency is not None:
                    img.save(out, format=fmt, transparency=transparency)
                else:
                    img.save(out, format=fmt)
                return out.getvalue()
            finally:
                _DECODES.release()
    except ImageTooLarge:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberate fail-open
        # Pillow raises a mix of OSError and bare Exception subclasses.
        logger.info("Image metadata strip skipped (%s): %s", ct, exc)
        return data


def strip_image_metadata(data: bytes, content_type: str | None) -> bytes:
    """A metadata-free copy of a raster image, else ``data`` unchanged. Idempotent.

    Raises ImageTooLarge for a GIF/BMP/TIFF that cannot be cleaned within the memory budget.
    """
    if not data or not content_type:
        return data
    ct = content_type.lower().split(";")[0].strip()
    if not any(ct.startswith(p) for p in _RASTER_PREFIXES):
        return data
    # Dispatch on the bytes, not the declared type: a PNG uploaded as image/jpeg is a PNG.
    if data.startswith(b"\xff\xd8\xff"):
        cleaned = _strip_jpeg(data)
    elif data.startswith(_PNG_SIGNATURE):
        cleaned = _strip_png(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        cleaned = _strip_webp(data)
    else:
        return _strip_with_pillow(data, ct)
    if cleaned is None:
        logger.info("Image metadata strip skipped (%s): unreadable structure", ct)
        return data
    return cleaned
