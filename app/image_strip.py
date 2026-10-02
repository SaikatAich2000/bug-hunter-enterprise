"""EXIF / metadata stripper for uploaded images.

Re-encodes raster uploads through Pillow with the ``info`` dict cleared,
dropping EXIF/XMP/ICC and PNG text chunks (GPS, serials, etc.) while keeping
pixel data. Fails open on any error so cleanup never blocks a legit upload.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger("bug_hunter.image_strip")

# Raster formats Pillow can safely re-encode. Add more as needed.
_RASTER_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp",
                    "image/bmp", "image/tiff")

# 50 MP decode ceiling — Pillow's own limits (~178/357 MP) can OOM a small server.
_MAX_IMAGE_PIXELS = 50_000_000


def strip_image_metadata(data: bytes, content_type: str | None) -> bytes:
    """Return a metadata-stripped copy of a recognised raster image, else
    ``data`` unchanged. Idempotent."""
    if not data or not content_type:
        return data
    ct = content_type.lower().split(";")[0].strip()
    if not any(ct.startswith(p) for p in _RASTER_PREFIXES):
        return data

    try:
        from PIL import Image
    except ImportError:
        # No Pillow = operator opted out; return original.
        return data

    try:
        with io.BytesIO(data) as src:
            img = Image.open(src)
            # Check dimensions before load() allocates the pixel buffer.
            width, height = img.size
            if width * height > _MAX_IMAGE_PIXELS:
                logger.info("EXIF strip skipped (%s): %d×%d px exceeds the decode budget",
                            ct, width, height)
                return data
            # Multi-frame (animated GIF/WebP, TIFF): save() keeps only frame 1,
            # so leave these untouched.
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                logger.info("EXIF strip skipped (%s): preserving multi-frame image", ct)
                return data
            img.load()
            fmt = img.format
            if fmt is None:
                return data
            # img.info holds EXIF/ICC/XMP/text chunks; clear before save. Preserve
            # palette transparency (tRNS) first, or GIFs/palette PNGs turn opaque.
            transparency = img.info.get("transparency")
            img.info = {}
            out = io.BytesIO()
            # exif=b"" guards against a future Pillow reading EXIF back from info.
            if fmt == "JPEG":
                img.save(out, format=fmt, exif=b"")
            elif transparency is not None:
                img.save(out, format=fmt, transparency=transparency)
            else:
                img.save(out, format=fmt)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - deliberate fail-open
        # Pillow raises a mix of OSError and bare Exception subclasses; a
        # narrow tuple would let some escape as a 500.
        logger.info("EXIF strip skipped (%s): %s", ct, exc)
        return data
