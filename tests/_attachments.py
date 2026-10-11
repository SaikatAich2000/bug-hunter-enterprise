"""Attachment fixtures for tests that must reach states the upload allowlist now blocks.

Uploads are allowlisted, so rows holding HTML/SVG/JS (stored before the allowlist existed)
are written straight to the database. That keeps the download-time defences tested.
"""
from __future__ import annotations

# Smallest bytes the upload allowlist accepts as an mp4 and a png.
MP4_HEAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
PNG_HEAD = b"\x89PNG\r\n\x1a\n"


def insert_attachment(bug_id: int, filename: str, content_type: str, data: bytes) -> int:
    """Store an attachment row directly, bypassing the upload route; returns its id."""
    from app.database import SessionLocal
    from app.models import Attachment

    with SessionLocal() as db:
        att = Attachment(
            bug_id=bug_id, uploader_name="legacy", filename=filename,
            content_type=content_type, size_bytes=len(data), data=data,
        )
        db.add(att)
        db.commit()
        return att.id
