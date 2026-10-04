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

Root safety: every use goes through production.root_policy -- the SAME rules
the backup and restore tooling apply. ``media_root()`` enforces the path rules
(never /, a system tree, a broad anchor, station content or code);
``media_root(dedicated=True)`` -- used by every destructive entry point
(directory creation, intake, sweeps) -- also refuses an existing root holding
anything but the managed subtrees.

Durability: see ``ensure_durable_dir`` and production.services.intake.
"""
from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .. import root_policy
from ..errors import IntakeError

SUBDIRECTORIES = ("media", "incoming", "work", "locks")
DIR_MODE = 0o750
PART_MODE = 0o600           # an upload in progress: owner only
MEDIA_FILE_MODE = 0o440     # permanent bytes: read-only even to the owner
STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{32}$")
PART_NAME_RE = re.compile(r"^[0-9a-f]{32}\.part$")
WORK_NAME_RE = re.compile(r"^[0-9a-f]{32}$")


def _protected_paths() -> list[str]:
    """Station content and code the root must never equal, contain or sit in."""
    values = [getattr(settings, name, None) for name in root_policy.STATION_PATH_SETTINGS]
    values += [getattr(settings, name, None) for name in ("BASE_DIR", "STATIC_ROOT", "MEDIA_ROOT")]
    return [str(value) for value in values if value]


def media_root(*, dedicated: bool = False) -> Path:
    """The validated production-media root (see production.root_policy)."""
    raw = getattr(settings, "PRODUCTION_MEDIA_ROOT", "")
    try:
        accepted = root_policy.check_root(
            raw, protected=_protected_paths(), dedicated_path=(raw if dedicated else None),
        )
    except root_policy.RootPolicyError as exc:
        raise ImproperlyConfigured(f"unsafe PRODUCTION_MEDIA_ROOT ({exc.code}): {exc.message}") from exc
    return Path(accepted)


def media_dir() -> Path:
    return media_root() / "media"


def incoming_dir() -> Path:
    return media_root() / "incoming"


def work_root() -> Path:
    return media_root() / "work"


def locks_dir() -> Path:
    return media_root() / "locks"


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_durable_dir(path: Path) -> None:
    """Make ``path`` an existing, real, 0750 directory whose ENTRY is durable.

    Creates missing ancestors one level at a time; for every directory it
    creates or adopts it sets the mode BEFORE syncing, fsyncs the directory and
    then fsyncs its parent (so the directory entry itself survives power loss).
    Already-existing directories get the same parent fsync -- an earlier
    process may have created one and died before syncing it -- which is cheap
    and makes the result independent of who created what. Never touches a
    directory owned by someone else beyond reading it; refuses a symlink or a
    non-directory."""
    path = Path(path)
    missing = []
    probe = path
    while not os.path.lexists(probe):
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, DIR_MODE)
        except FileExistsError:
            pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise IntakeError(f"{path.name} exists but is not a directory", code="layout_invalid")
    if info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) != DIR_MODE:
        os.chmod(path, DIR_MODE)
    for directory in reversed(missing[1:]):        # created ancestors, outermost first
        fsync_directory(directory)
        fsync_directory(directory.parent)
    fsync_directory(path)
    fsync_directory(path.parent)


def ensure_dir(path: Path) -> None:
    """Backwards-compatible name for ensure_durable_dir."""
    ensure_durable_dir(path)


def ensure_layout() -> Path:
    root = media_root(dedicated=True)
    ensure_durable_dir(root)
    for name in SUBDIRECTORIES:
        ensure_durable_dir(root / name)
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
