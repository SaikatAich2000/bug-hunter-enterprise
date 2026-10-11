"""Attachment allowlist: a file is accepted only when its extension is listed AND its
leading bytes match what that extension claims. The client-sent content type is never
trusted; the stored type comes from this table.

A denylist cannot keep up with executable formats, so anything not named here is refused.
SVG is absent on purpose: it is scriptable.
"""
from __future__ import annotations

import codecs
from typing import Callable, Optional

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FTYP_OFFSET = slice(4, 8)
_QUICKTIME_ATOMS = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot")
# Control bytes that appear in real text and logs (tab, LF, FF, CR, ESC for ANSI colour).
_TEXT_CONTROLS = frozenset(b"\t\n\x0c\r\x1b")


def _starts(*prefixes: bytes) -> Callable[[bytes], bool]:
    return lambda data: data.startswith(prefixes)


def _riff(kind: bytes) -> Callable[[bytes], bool]:
    return lambda data: data[:4] == b"RIFF" and data[8:12] == kind


def _ftyp(data: bytes) -> bool:
    return data[_FTYP_OFFSET] == b"ftyp"


def _quicktime(data: bytes) -> bool:
    return data[_FTYP_OFFSET] in _QUICKTIME_ATOMS


def _pdf(data: bytes) -> bool:
    # Readers tolerate junk before the header, within the first KiB.
    return b"%PDF-" in data[:1024]


def _text(data: bytes) -> bool:
    """Plain text: no NUL bytes and almost no other control bytes. UTF-16 is accepted by BOM."""
    head = data[:8192]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:  # incremental: the 8 KiB slice may end mid-character
            codecs.getincrementaldecoder("utf-16")().decode(head, final=False)
        except UnicodeDecodeError:
            return False
        return True
    if b"\x00" in head:
        return False
    controls = sum(1 for b in head if b < 0x20 and b not in _TEXT_CONTROLS)
    return controls * 100 <= len(head)


_ZIP = _starts(*_ZIP_MAGIC)
_OLE = _starts(_OLE_MAGIC)

# extension -> (stored content type, signature check)
ALLOWED_UPLOADS: dict[str, tuple[str, Callable[[bytes], bool]]] = {
    "png": ("image/png", _starts(b"\x89PNG\r\n\x1a\n")),
    "jpg": ("image/jpeg", _starts(b"\xff\xd8\xff")),
    "jpeg": ("image/jpeg", _starts(b"\xff\xd8\xff")),
    "gif": ("image/gif", _starts(b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", _riff(b"WEBP")),
    "bmp": ("image/bmp", _starts(b"BM")),
    "tif": ("image/tiff", _starts(b"II*\x00", b"MM\x00*")),
    "tiff": ("image/tiff", _starts(b"II*\x00", b"MM\x00*")),
    "heic": ("image/heic", _ftyp),
    "heif": ("image/heif", _ftyp),
    "avif": ("image/avif", _ftyp),
    "pdf": ("application/pdf", _pdf),
    "mp4": ("video/mp4", _quicktime),
    "m4v": ("video/x-m4v", _quicktime),
    "mov": ("video/quicktime", _quicktime),
    "3gp": ("video/3gpp", _ftyp),
    "webm": ("video/webm", _starts(b"\x1a\x45\xdf\xa3")),
    "mkv": ("video/x-matroska", _starts(b"\x1a\x45\xdf\xa3")),
    "avi": ("video/x-msvideo", _riff(b"AVI ")),
    "ogv": ("video/ogg", _starts(b"OggS")),
    "mpg": ("video/mpeg", _starts(b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")),
    "mpeg": ("video/mpeg", _starts(b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")),
    "wmv": ("video/x-ms-wmv", _starts(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")),
    "doc": ("application/msword", _OLE),
    "xls": ("application/vnd.ms-excel", _OLE),
    "ppt": ("application/vnd.ms-powerpoint", _OLE),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _ZIP),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _ZIP),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", _ZIP),
    "odt": ("application/vnd.oasis.opendocument.text", _ZIP),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", _ZIP),
    "odp": ("application/vnd.oasis.opendocument.presentation", _ZIP),
    "rtf": ("application/rtf", _starts(b"{\\rtf")),
    "txt": ("text/plain", _text),
    "log": ("text/plain", _text),
    "csv": ("text/csv", _text),
    "zip": ("application/zip", _ZIP),
}

ALLOWED_SUMMARY = "images, PDF, video, Office documents, and txt, csv, log or zip files"


def upload_extension(filename: str) -> str:
    """Lower-cased extension, after dropping trailing dots and spaces (Windows ignores
    them on download, so "evil.exe." is an .exe). Empty when there is none."""
    name = (filename or "").strip().rstrip(". \t")
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].strip().lower()


def check_extension(filename: str) -> Optional[str]:
    """Return an error message when the extension is not allowed, else None."""
    ext = upload_extension(filename)
    if ext in ALLOWED_UPLOADS:
        return None
    label = f"Files of type .{ext}" if ext else "Files without an extension"
    return f"{label} can't be attached. Allowed: {ALLOWED_SUMMARY}."


def verify_upload(filename: str, data: bytes) -> str:
    """Return the content type to store, or raise ValueError with a user-facing reason."""
    problem = check_extension(filename)
    if problem:
        raise ValueError(problem)
    ext = upload_extension(filename)
    mime, matches = ALLOWED_UPLOADS[ext]
    if not matches(data):
        raise ValueError(f"This file isn't a valid .{ext} file, so it can't be attached.")
    return mime
