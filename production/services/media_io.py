"""Safe, identity-only access to stored media bytes.

``open_media`` is the ONLY way application code obtains a file handle for a
ProductionMedia. It takes a media identity (never a path), re-reads the row,
resolves the system-generated storage key, refuses anything purged, missing,
unvalidated (by default) or inconsistent with the row's recorded evidence, and
never falls back to any other path. Phase B's HTTP range-streaming endpoint is
built from ``open_media`` + ``parse_byte_range`` + ``iter_range``; this phase
exposes no HTTP surface.

The cheap integrity check on every open is the byte size. A full SHA-256
re-verification is ``verify_integrity`` (used by diagnostics/reconciliation),
not done on every preview.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from django.core.exceptions import ValidationError

from .. import formats
from ..errors import MediaInconsistent, MediaNotValidated, MediaPurged, RangeNotSatisfiable
from ..models import ProductionMedia
from . import layout

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass
class OpenedMedia:
    media: ProductionMedia
    file: BinaryIO
    size: int
    content_type: str

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _fetch(media) -> ProductionMedia:
    pk = getattr(media, "pk", media)
    try:
        return ProductionMedia.objects.get(pk=pk)
    except (ProductionMedia.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise MediaInconsistent("no such production media", code="unknown_media") from exc


def _open_verified(row: ProductionMedia):
    """(fd, size) for a present row's bytes, or MediaInconsistent. Never
    follows a symlink and never leaves media/."""
    path = layout.resolve_storage_path(row.storage_key)
    media_root = os.path.realpath(layout.media_dir())
    if os.path.dirname(os.path.dirname(os.path.realpath(path))) != media_root:
        raise MediaInconsistent("media path escapes the media store", code="path_escape")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise MediaInconsistent("media bytes are missing", code="missing_bytes") from exc
    except OSError as exc:
        code = "not_a_regular_file" if exc.errno == errno.ELOOP else "unreadable_bytes"
        raise MediaInconsistent("media bytes cannot be opened", code=code) from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise MediaInconsistent("media bytes are not a regular file", code="not_a_regular_file")
    if info.st_size != row.byte_size:
        os.close(fd)
        raise MediaInconsistent("media bytes do not match the recorded size", code="size_mismatch")
    return fd, info.st_size


def content_type_of(row: ProductionMedia) -> str:
    """From the VALIDATED container, never the declared type."""
    return formats.content_type_for(row.container) if row.is_valid else "application/octet-stream"


def open_media(media, *, require_valid=True) -> OpenedMedia:
    row = _fetch(media)
    if not row.is_present:
        raise MediaPurged("media bytes have been purged")
    if require_valid and not row.is_valid:
        raise MediaNotValidated("media has not been validated as valid")
    fd, size = _open_verified(row)
    return OpenedMedia(media=row, file=os.fdopen(fd, "rb"), size=size, content_type=content_type_of(row))


def verify_integrity(media) -> None:
    """Re-hash the stored bytes against the row's SHA-256 (slow: reads it all)."""
    row = _fetch(media)
    if not row.is_present:
        raise MediaPurged("media bytes have been purged")
    fd, _size = _open_verified(row)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != row.sha256:
        raise MediaInconsistent("media bytes do not match the recorded SHA-256", code="sha_mismatch")


def parse_byte_range(header: str | None, size: int):
    """Parse a single ``Range: bytes=a-b`` header against ``size``.

    Returns None when there is no header, or one that must be ignored (a
    multi-range, or an invalid spec such as last < first, per RFC 9110) -- the
    caller serves the whole file -- or ``(start, end)`` inclusive. Raises
    RangeNotSatisfiable for a valid spec the file cannot satisfy."""
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:                                   # suffix: last N bytes
        count = int(last)
        if count == 0:
            raise RangeNotSatisfiable("empty suffix range")
        return max(0, size - count), size - 1
    start = int(first)
    if last and int(last) < start:
        return None                                  # invalid spec: ignore the header
    if start >= size:
        raise RangeNotSatisfiable("range is outside the file")
    end = int(last) if last else size - 1
    return start, min(end, size - 1)


def iter_range(handle: BinaryIO, start: int, end: int, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield bytes ``start..end`` inclusive in bounded chunks."""
    handle.seek(start)
    remaining = end - start + 1
    while remaining > 0:
        chunk = handle.read(min(chunk_size, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk
