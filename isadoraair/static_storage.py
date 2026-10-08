"""STATIC_ROOT storage: collected static files are always readable by nginx.

nginx serves ``/static/`` straight from STATIC_ROOT as a different account, so
every directory there must be traversable and every file readable by others.
Update Center runs ``collectstatic`` under the updater's ``UMask=0077``, and
Django's default storage creates a directory that did not exist yet with the
process umask -- ``0700`` -- so a release that adds a new static directory
used to leave it unreadable to nginx (HTTP 403; r0107's iPortal scripts, and
the weather CSS/JS before them).

This storage, configured ONLY as the ``staticfiles`` backend in
``settings.STORAGES``, closes that in two ways, both confined to STATIC_ROOT:

* files and directories collectstatic creates get explicit modes
  (``0644`` / ``0755``) whatever the process umask -- including STATIC_ROOT
  itself and every intermediate directory;
* after every collectstatic run (its ``post_process`` step) the whole tree is
  reconciled, so directories and files an earlier run left too restrictive
  (and directories ``collectstatic --link`` creates, which bypass the
  storage) are repaired on the next update.

The reconcile only ADDS the read/traverse bits nginx needs; it never removes
a bit, changes ownership, follows a symlink, or touches anything outside
STATIC_ROOT. Uploads, ProductionMedia, reports and every other storage keep
their own (restrictive) permissions: Django's global FILE_UPLOAD_PERMISSIONS
and FILE_UPLOAD_DIRECTORY_PERMISSIONS are deliberately left unset.
"""
from __future__ import annotations

import os
import stat

from django.contrib.staticfiles.storage import StaticFilesStorage
from django.core.exceptions import ImproperlyConfigured

STATIC_DIRECTORY_MODE = 0o755
STATIC_FILE_MODE = 0o644


class StaticPermissionError(OSError):
    """An entry under STATIC_ROOT lacks access nginx needs and could not be fixed."""


def reconcile_static_permissions(root) -> list[str]:
    """Give every directory under ``root`` (``root`` included) at least
    ``0755`` and every regular file at least ``0644``, adding bits only.
    Symlinks and special files are left alone; nothing outside ``root`` is
    reached. Returns the paths it changed. Raises StaticPermissionError when
    an entry that needs a bit cannot be changed (it would stay unservable)."""
    root = os.fspath(root)
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ImproperlyConfigured(f"STATIC_ROOT {root} must be a real directory, not a symlink or file")
    changed = []
    _ensure(root, info, STATIC_DIRECTORY_MODE, changed)
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(directory, name)
            try:
                entry = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(entry.st_mode):
                _ensure(path, entry, STATIC_DIRECTORY_MODE, changed)
            elif stat.S_ISREG(entry.st_mode):
                _ensure(path, entry, STATIC_FILE_MODE, changed)
            # symlinks (collectstatic --link), sockets, fifos: never followed or changed
    return changed


def _ensure(path, info, required, changed):
    mode = stat.S_IMODE(info.st_mode)
    if mode & required == required:
        return
    try:
        try:
            # Never through a symlink, even one swapped in after the lstat:
            # Linux refuses a no-follow chmod of a symlink (NotImplementedError).
            os.chmod(path, mode | required, follow_symlinks=False)
        except NotImplementedError:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return                                  # replaced by a symlink meanwhile: not ours to change
            os.chmod(path, mode | required)             # a platform without no-follow chmod at all
    except OSError as exc:
        raise StaticPermissionError(
            f"{path} is mode {mode:04o}; nginx needs at least {required:04o} and it could not be changed: {exc}"
        ) from exc
    changed.append(path)


class StaticRootStorage(StaticFilesStorage):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("file_permissions_mode", STATIC_FILE_MODE)
        kwargs.setdefault("directory_permissions_mode", STATIC_DIRECTORY_MODE)
        super().__init__(*args, **kwargs)

    def post_process(self, paths, dry_run=False, **options):
        """collectstatic's last step: repair the whole STATIC_ROOT tree. Yields
        nothing (no file is renamed or rewritten)."""
        if not dry_run and self.location:
            reconcile_static_permissions(self.location)
        yield from ()
