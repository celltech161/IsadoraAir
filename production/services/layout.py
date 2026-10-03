"""The production-media directory layout and path-generation rules.

    <PRODUCTION_MEDIA_ROOT>/
        media/      permanent immutable ProductionMedia bytes      (backed up)
        incoming/   in-progress uploads, <32hex>.part              (NOT backed up)
        work/       transient processing workspaces, <32hex>/      (NOT backed up)
        locks/      lock sidecars if a consumer needs them         (NOT backed up)

Every permanent path is generated here from a UUID. Nothing a client sends --
filename, title, username, MIME type, URL, form field -- ever becomes a path
component. ``resolve_storage_path`` is the ONLY function that turns a stored
key into a path, and it refuses anything that is not exactly the
system-generated shape.

The root is read from settings at call time (never cached at import) so tests
and operators can repoint it safely. The root is not served by nginx.
"""
from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from ..errors import IntakeError

SUBDIRECTORIES = ("media", "incoming", "work", "locks")
DIR_MODE = 0o750
PART_MODE = 0o600           # an upload in progress: owner only
MEDIA_FILE_MODE = 0o440     # permanent bytes: read-only even to the owner
STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{32}$")
PART_NAME_RE = re.compile(r"^[0-9a-f]{32}\.part$")
WORK_NAME_RE = re.compile(r"^[0-9a-f]{32}$")


def media_root() -> Path:
    raw = getattr(settings, "PRODUCTION_MEDIA_ROOT", "")
    root = Path(str(raw)) if raw else None
    if root is None or not root.is_absolute() or ".." in root.parts:
        raise ImproperlyConfigured("PRODUCTION_MEDIA_ROOT must be a clean absolute path")
    return root


def media_dir() -> Path:
    return media_root() / "media"


def incoming_dir() -> Path:
    return media_root() / "incoming"


def work_root() -> Path:
    return media_root() / "work"


def locks_dir() -> Path:
    return media_root() / "locks"


def ensure_dir(path: Path) -> None:
    """Create ``path`` (0750) if needed; tighten a directory WE own whose
    mode is wider than 0750. Never touches a directory owned by someone else."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise IntakeError(f"{path.name} exists but is not a directory", code="layout_invalid")
    if info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) != DIR_MODE:
        path.chmod(DIR_MODE)


def ensure_layout() -> Path:
    root = media_root()
    ensure_dir(root)
    for name in SUBDIRECTORIES:
        ensure_dir(root / name)
    return root


def new_media_id() -> uuid.UUID:
    return uuid.uuid4()


def storage_key_for(media_id: uuid.UUID) -> str:
    """``<2 hex>/<32 hex>`` derived from the media's own UUID: extensionless,
    so no untrusted metadata is needed to name the permanent file and the name
    never has to change once validation establishes the container."""
    hexid = media_id.hex
    return f"{hexid[:2]}/{hexid}"


def resolve_storage_path(storage_key: str) -> Path:
    """The permanent path for a stored key. Pure (no filesystem access).

    Raises IntakeError for any key that is not exactly the system-generated
    shape, so a corrupted or hostile value can never traverse out of media/."""
    if not isinstance(storage_key, str) or not STORAGE_KEY_RE.fullmatch(storage_key):
        raise IntakeError("storage key is not a system-generated key", code="invalid_storage_key")
    return media_dir() / storage_key


def new_part_path() -> Path:
    return incoming_dir() / f"{uuid.uuid4().hex}.part"


def create_work_dir() -> Path:
    """A fresh, empty, system-named scratch directory under work/."""
    ensure_layout()
    path = work_root() / uuid.uuid4().hex
    path.mkdir(mode=DIR_MODE)
    return path


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
