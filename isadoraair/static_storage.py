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

* what collectstatic creates is created readable: each save runs under umask
  ``022``, so new directories are ``0755`` and new files ``0644`` from the
  moment they exist. No mode or group is ever changed afterwards BY PATH --
  Django's own post-create ``chmod``/``chown`` (``file_permissions_mode``,
  ``directory_permissions_mode``, the location-group fix-up) are disabled,
  because a path checked and then changed can be swapped for a symlink in
  between;
* after every collectstatic run (its ``post_process`` step) the whole tree is
  reconciled through FILE DESCRIPTORS (``reconcile_static_permissions``), so
  directories and files an earlier run left too restrictive -- and the
  directories ``collectstatic --link`` creates itself -- are repaired.

The reconcile only ADDS the read/traverse bits nginx needs; it never removes a
bit, changes ownership, follows a symlink, or reaches anything outside the
directory it pinned. Uploads, ProductionMedia, reports and every other storage
keep their own (restrictive) permissions: Django's global
FILE_UPLOAD_PERMISSIONS and FILE_UPLOAD_DIRECTORY_PERMISSIONS stay unset.
"""
from __future__ import annotations

import errno
import os
import stat

from django.contrib.staticfiles.storage import StaticFilesStorage
from django.core.exceptions import ImproperlyConfigured

STATIC_DIRECTORY_MODE = 0o755
STATIC_FILE_MODE = 0o644
CREATE_UMASK = 0o022                      # 0777/0666 & ~022 = 0755/0644
MAX_DEPTH = 64


class StaticPermissionError(OSError):
    """STATIC_ROOT could not be reconciled safely: an entry lacks access nginx
    needs and could not be fixed, or the safe mechanism is unavailable."""


def _descriptor_flags():
    """The open flags the reconcile needs, or StaticPermissionError when this
    platform cannot provide descriptor-relative, no-follow handling. There is
    deliberately no path-based fallback: it would reintroduce the race."""
    missing = [name for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK", "O_NOCTTY")
               if not hasattr(os, name)]
    if os.open not in os.supports_dir_fd:
        missing.append("open(dir_fd=)")
    if os.listdir not in os.supports_fd:
        missing.append("listdir(fd)")
    if not hasattr(os, "fchmod"):
        missing.append("fchmod")
    if missing:
        raise StaticPermissionError(
            f"static permissions cannot be reconciled safely on this platform (missing: {', '.join(missing)})")
    directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    # Any entry: never follows a final symlink (ELOOP), never blocks on a FIFO,
    # never becomes a controlling terminal. The object is then judged by fstat.
    entry = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
    return directory, entry


def reconcile_static_permissions(root) -> list[str]:
    """Give every directory under ``root`` (``root`` included) at least
    ``0755`` and every regular file at least ``0644``, adding bits only, and
    return the paths changed (labels only: nothing is ever opened by them).

    The root's final component is opened with O_NOFOLLOW|O_DIRECTORY relative
    to its parent and everything below is reached ONLY through that pinned
    descriptor: each child is opened relative to its parent's descriptor with
    O_NOFOLLOW, judged by fstat() of what was actually opened, and changed with
    fchmod() on that descriptor. Replacing any pathname -- the root, a
    directory, a file -- with a symlink at any moment can therefore never make
    the reconcile reach the link's target. Symlinks are skipped (collectstatic
    --link creates them); an entry that vanishes is skipped; anything that
    cannot be opened safely or repaired fails the run (StaticPermissionError).
    A symlinked or non-directory root is refused (ImproperlyConfigured).

    The root's ANCESTORS are resolved normally: they are the application's
    own layout (settings resolves BASE_DIR), outside the static tree."""
    directory_flags, entry_flags = _descriptor_flags()
    root = os.path.abspath(os.fspath(root))
    parent, name = os.path.split(root)
    if not name:
        raise ImproperlyConfigured("STATIC_ROOT must not be the filesystem root")
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    try:
        root_fd = os.open(name, directory_flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return []
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ImproperlyConfigured(f"STATIC_ROOT {root} must be a real directory, not a symlink or file") from exc
        raise StaticPermissionError(f"STATIC_ROOT {root} could not be opened safely: {exc}") from exc
    finally:
        os.close(parent_fd)
    changed = []
    try:
        _reconcile_directory(root_fd, root, entry_flags, changed, depth=0)
    finally:
        os.close(root_fd)
    return changed


def _reconcile_directory(dir_fd, label, entry_flags, changed, *, depth):
    if depth > MAX_DEPTH:
        raise StaticPermissionError(f"{label}: static tree deeper than {MAX_DEPTH} levels")
    info = os.fstat(dir_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise StaticPermissionError(f"{label} is not a directory")
    _ensure(dir_fd, info, STATIC_DIRECTORY_MODE, label, changed)
    for name in sorted(os.listdir(dir_fd)):
        child = os.path.join(label, name)
        try:
            fd = os.open(name, entry_flags, dir_fd=dir_fd)
        except FileNotFoundError:
            continue                                # removed meanwhile
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO):
                continue                            # a symlink (never followed) or a socket
            raise StaticPermissionError(f"{child} could not be opened safely: {exc}") from exc
        try:
            entry = os.fstat(fd)                    # what was ACTUALLY opened decides
            if stat.S_ISDIR(entry.st_mode):
                _reconcile_directory(fd, child, entry_flags, changed, depth=depth + 1)
            elif stat.S_ISREG(entry.st_mode):
                _ensure(fd, entry, STATIC_FILE_MODE, child, changed)
            # FIFOs and anything else: left alone
        finally:
            os.close(fd)


def _ensure(fd, info, required, label, changed):
    mode = stat.S_IMODE(info.st_mode)
    if mode & required == required:
        return
    try:
        os.fchmod(fd, mode | required)              # the opened object itself; never a path
    except (OSError, NotImplementedError) as exc:
        raise StaticPermissionError(
            f"{label} is mode {mode:04o}; nginx needs at least {required:04o} and it could not be changed: {exc}"
        ) from exc
    changed.append(label)


class StaticRootStorage(StaticFilesStorage):
    # Django would chmod/chown each saved file and new directory BY PATH after
    # creating it (a check/use race); modes come from CREATE_UMASK instead.
    @property
    def file_permissions_mode(self):
        return None

    @property
    def directory_permissions_mode(self):
        return None

    def _ensure_location_group_id(self, full_path):
        """No group fix-up by path: nginx reads static files through their
        "other" bits, so their group does not matter."""

    def _save(self, name, content):
        previous = os.umask(CREATE_UMASK)           # collectstatic is single-threaded
        try:
            return super()._save(name, content)
        finally:
            os.umask(previous)

    def post_process(self, paths, dry_run=False, **options):
        """collectstatic's last step: repair the whole STATIC_ROOT tree. Yields
        nothing (no file is renamed or rewritten)."""
        if not dry_run and self.location:
            reconcile_static_permissions(self.location)
        yield from ()
