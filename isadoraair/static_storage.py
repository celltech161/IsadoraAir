"""STATIC_ROOT storage: every destination operation is descriptor-confined.

nginx serves ``/static/`` straight from STATIC_ROOT as a different account, so
every directory there must be traversable and every file readable by others.
Update Center runs ``collectstatic`` under the updater's ``UMask=0077``; with
Django's pathname-based storage a new static directory came out ``0700``
(HTTP 403: r0107's iPortal scripts, the weather CSS/JS before them).

**Invariant (r0108).** No collectstatic destination operation trusts a mutable
pathname. The application account can rename or replace the directories
above STATIC_ROOT (``/home/jreed``, the checkout) and anything inside it, so a
pathname resolved twice -- checked, then used -- can be swapped for a symlink
in between and steer a create, write, delete, rename or chmod onto private
files elsewhere (Codex reproduced each class). So:

* STATIC_ROOT is reached from ``/`` -- the one trusted anchor -- one component
  at a time, each opened relative to the previous directory descriptor with
  ``O_DIRECTORY|O_NOFOLLOW`` and verified with ``fstat()``; a symlink anywhere
  on the way fails closed (StaticConfinementError). Only the final component
  may be created (``mkdir`` relative to its pinned parent, then re-opened
  no-follow, verified and ``fchmod``-ed);
* a destination name becomes a relative component list (no absolute names,
  ``..`` or NUL) walked the same way from the pinned root: intermediate
  directories are opened -- or created -- relative to their pinned parent;
* existence, size and modification time are ``fstatat(AT_SYMLINK_NOFOLLOW)``
  of the leaf relative to its pinned parent; a symlink leaf is reported as
  existing but without a usable time, so collectstatic deletes and replaces
  the ENTRY;
* a file is written to a fresh ``O_CREAT|O_EXCL|O_NOFOLLOW`` temporary in the
  pinned parent, given its mode with ``fchmod`` on that descriptor, and
  renamed over the leaf with ``renameat`` inside the same pinned directory --
  a symlink leaf is replaced as an entry, never written through;
* deletion is ``unlinkat``/``rmdir`` relative to the pinned parent (a symlink's
  referent is never touched; directories are never deleted recursively);
* ``path()`` is not offered: collectstatic then treats this storage as
  non-local and never runs its own pathname code against the destination
  (``os.path.islink``/``os.unlink``/``os.makedirs`` on full paths, and
  ``--link``, which is refused);
* after every run (collectstatic's ``post_process``) the whole tree is
  reconciled through descriptors (``reconcile_static_permissions``).

Directories become ``0755`` and files ``0644`` -- set on the opened object,
adding bits only. Uploads, ProductionMedia, reports and every other storage
keep their own restrictive permissions: FILE_UPLOAD_PERMISSIONS and
FILE_UPLOAD_DIRECTORY_PERMISSIONS stay unset. Without descriptor-relative,
no-follow primitives everything fails closed: there is no pathname fallback.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.staticfiles.utils import check_settings
from django.core.exceptions import ImproperlyConfigured, SuspiciousFileOperation
from django.core.files import File
from django.core.files.storage import Storage
from django.utils.encoding import filepath_to_uri

STATIC_DIRECTORY_MODE = 0o755
STATIC_FILE_MODE = 0o644
MAX_DEPTH = 64
TEMP_PREFIX = ".isadoraair-static-"
_NOT_A_DIRECTORY = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)


class StaticPermissionError(OSError):
    """STATIC_ROOT could not be handled safely: an entry lacks access nginx
    needs and could not be fixed, or the safe mechanism is unavailable."""


class StaticConfinementError(StaticPermissionError):
    """A destination pathname component is a symlink or not a directory: it is
    never followed."""


def _primitives():
    """The open flags, or StaticPermissionError when this platform cannot
    provide descriptor-relative, no-follow handling (no pathname fallback)."""
    missing = [name for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK", "O_NOCTTY", "O_EXCL")
               if not hasattr(os, name)]
    for function in (os.open, os.mkdir, os.unlink, os.rmdir, os.rename, os.stat):
        if function not in os.supports_dir_fd:
            missing.append(f"{function.__name__}(dir_fd=)")
    if os.stat not in os.supports_follow_symlinks:
        missing.append("stat(follow_symlinks=False)")
    if os.listdir not in os.supports_fd:
        missing.append("listdir(fd)")
    if not hasattr(os, "fchmod"):
        missing.append("fchmod")
    if missing:
        raise StaticPermissionError(
            f"STATIC_ROOT cannot be handled safely on this platform (missing: {', '.join(missing)})")
    return SimpleNamespace(
        anchor=os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        directory=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        # Any entry: never follows a final symlink (ELOOP), never blocks on a
        # FIFO, never becomes a controlling terminal; judged by fstat after.
        entry=os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC,
        create=os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
    )


def _refusal(label, exc):
    if exc.errno in _NOT_A_DIRECTORY:
        return StaticConfinementError(exc.errno, f"{label} is a symlink or not a directory; refusing to follow it")
    return StaticPermissionError(exc.errno, f"{label} could not be opened safely: {exc.strerror}")


def _add_mode(fd, info, required, label):
    """Add ``required`` bits to the opened object. True when it changed."""
    mode = stat.S_IMODE(info.st_mode)
    if mode & required == required:
        return False
    try:
        os.fchmod(fd, mode | required)              # the opened object itself; never a path
    except (OSError, NotImplementedError) as exc:
        raise StaticPermissionError(
            f"{label} is mode {mode:04o}; nginx needs at least {required:04o} and it could not be changed: {exc}"
        ) from exc
    return True


def _open_directory(parent_fd, name, label, flags, *, create, static):
    """Open ``name`` relative to ``parent_fd`` as a directory, never following
    a symlink. Absent: None, or -- with ``create`` -- made with mkdir relative
    to the same pinned parent and re-opened (another process winning the race
    is fine; whatever is there is validated the same way). ``static``
    directories (STATIC_ROOT and below) get at least 0755 when ``create``."""
    try:
        fd = os.open(name, flags.directory, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)          # private until fchmod below
        except FileExistsError:
            pass
        except OSError as exc:
            raise StaticPermissionError(exc.errno, f"{label} could not be created: {exc.strerror}") from exc
        try:
            fd = os.open(name, flags.directory, dir_fd=parent_fd)
        except OSError as exc:
            raise _refusal(label, exc) from exc
    except OSError as exc:
        raise _refusal(label, exc) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise StaticConfinementError(errno.ENOTDIR, f"{label} is not a directory")
        if create and static:
            _add_mode(fd, info, STATIC_DIRECTORY_MODE, label)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _root_components(root):
    root = os.fspath(root)
    if not os.path.isabs(root):
        raise ImproperlyConfigured(f"STATIC_ROOT must be an absolute path (got {root!r})")
    parts = [part for part in root.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or any("\x00" in part for part in parts):
        raise ImproperlyConfigured(f"STATIC_ROOT {root!r} is not a plain absolute directory path")
    return parts


def open_static_root(root, *, create=False):
    """A descriptor pinning STATIC_ROOT, reached from ``/`` (the trusted anchor)
    one component at a time with O_DIRECTORY|O_NOFOLLOW; None when it does not
    exist and ``create`` is false. Only the final component is ever created;
    a missing ancestor, a symlink or a non-directory anywhere fails closed."""
    flags = _primitives()
    parts = _root_components(root)
    fd = os.open("/", flags.anchor)
    try:
        for index, part in enumerate(parts):
            label = "/" + "/".join(parts[:index + 1])
            last = index == len(parts) - 1
            child = _open_directory(fd, part, label, flags, create=create and last, static=last)
            if child is None:
                if create:
                    raise StaticPermissionError(errno.ENOENT, f"{label} does not exist")
                return None
            os.close(fd)
            fd = child
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def reconcile_static_permissions(root) -> list[str]:
    """Give every directory under ``root`` (``root`` included) at least
    ``0755`` and every regular file at least ``0644``, adding bits only, and
    return the paths changed (labels only: nothing is opened by them).

    The root is pinned by open_static_root; everything below is reached ONLY
    through that descriptor: each child is opened relative to its parent's
    descriptor with O_NOFOLLOW, judged by fstat() of what was actually
    opened, and changed with fchmod() on that descriptor. Symlinks are skipped
    (never followed), an entry that vanishes is skipped; anything that cannot
    be opened safely or repaired fails the run."""
    flags = _primitives()
    root_fd = open_static_root(root, create=False)
    if root_fd is None:
        return []
    changed = []
    try:
        _reconcile_directory(root_fd, os.fspath(root), flags.entry, changed, depth=0)
    finally:
        os.close(root_fd)
    return changed


def _reconcile_directory(dir_fd, label, entry_flags, changed, *, depth):
    if depth > MAX_DEPTH:
        raise StaticPermissionError(f"{label}: static tree deeper than {MAX_DEPTH} levels")
    info = os.fstat(dir_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise StaticPermissionError(f"{label} is not a directory")
    if _add_mode(dir_fd, info, STATIC_DIRECTORY_MODE, label):
        changed.append(label)
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
                if _add_mode(fd, entry, STATIC_FILE_MODE, child):
                    changed.append(child)
            # FIFOs and anything else: left alone
        finally:
            os.close(fd)


def destination_components(name, *, allow_root=False):
    """A destination name as relative components beneath STATIC_ROOT. Absolute
    names, ``..``, NUL and (unless ``allow_root``) the root itself are refused;
    empty and ``.`` components are dropped."""
    name = os.fsdecode(os.fspath(name))
    if "\x00" in name:
        raise SuspiciousFileOperation("a static destination name contains NUL")
    if name.startswith("/"):
        raise SuspiciousFileOperation(f"absolute static destination name {name!r}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise SuspiciousFileOperation(f"static destination name {name!r} leaves STATIC_ROOT")
    if not parts and not allow_root:
        raise SuspiciousFileOperation("empty static destination name")
    return parts


class StaticRootStorage(Storage):
    """The ``staticfiles`` backend (settings.STORAGES). Deliberately NOT a
    FileSystemStorage: it never offers or uses an absolute destination path
    (see the module docstring for the invariant)."""

    def __init__(self, location=None, base_url=None):
        self._location = location
        self._base_url = base_url
        check_settings(base_url if base_url is not None else settings.STATIC_URL)

    # -- configuration -----------------------------------------------------------------
    @property
    def location(self):
        """Display/configuration only -- never used to reach a file by path."""
        location = self._location if self._location is not None else settings.STATIC_ROOT
        if not location:
            raise ImproperlyConfigured("You're using the staticfiles app without having set the STATIC_ROOT "
                                       "setting to a filesystem path.")
        return os.fspath(location)

    @property
    def base_url(self):
        base_url = self._base_url if self._base_url is not None else settings.STATIC_URL
        if base_url is not None and not base_url.endswith("/"):
            base_url += "/"
        return base_url

    def url(self, name):
        if self.base_url is None:
            raise ValueError("This file is not accessible via a URL.")
        url = filepath_to_uri(name)
        if url is not None:
            url = url.lstrip("/")
        return urljoin(self.base_url, url)

    def path(self, name):
        raise NotImplementedError("STATIC_ROOT is reached only through pinned directory descriptors; "
                                  "this storage offers no absolute destination path")

    # -- descriptor walk ---------------------------------------------------------------------
    @contextmanager
    def _directory(self, parts, *, create):
        """The pinned directory STATIC_ROOT/<parts> (None when absent and not
        ``create``); every component opened relative to the previous one."""
        flags = _primitives()
        fd = open_static_root(self.location, create=create)
        try:
            label = self.location
            for part in parts:
                if fd is None:
                    break
                label = os.path.join(label, part)
                child = _open_directory(fd, part, label, flags, create=create, static=True)
                os.close(fd)
                fd = child
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    def _leaf_stat(self, name):
        parts = destination_components(name, allow_root=True)
        with self._directory(parts[:-1], create=False) as directory:
            if directory is None:
                raise FileNotFoundError(errno.ENOENT, f"{name}: no such static file")
            if not parts:
                return os.fstat(directory)
            return os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)

    def _regular_stat(self, name):
        info = self._leaf_stat(name)
        if stat.S_ISLNK(info.st_mode):
            # Never reported as the link's target: collectstatic, seeing an
            # error here, deletes the ENTRY and writes a real file in its place.
            raise StaticConfinementError(errno.ELOOP, f"{name} is a symlink in STATIC_ROOT; it will be replaced")
        return info

    # -- Storage API used by collectstatic -------------------------------------------------
    def exists(self, name):
        try:
            self._leaf_stat(name)
        except FileNotFoundError:
            return False
        return True

    def get_modified_time(self, name):
        tz = dt_timezone.utc if settings.USE_TZ else None
        return datetime.fromtimestamp(self._regular_stat(name).st_mtime, tz=tz)

    def size(self, name):
        return self._regular_stat(name).st_size

    def listdir(self, path):
        parts = destination_components(path, allow_root=True)
        with self._directory(parts, create=False) as directory:
            if directory is None:
                raise FileNotFoundError(errno.ENOENT, f"{path}: no such static directory")
            directories, files = [], []
            for name in os.listdir(directory):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                (directories if stat.S_ISDIR(info.st_mode) else files).append(name)
            return directories, files

    def delete(self, name):
        parts = destination_components(name)
        with self._directory(parts[:-1], create=False) as directory:
            if directory is None:
                return
            try:
                os.unlink(parts[-1], dir_fd=directory)       # a symlink: the entry, never its referent
            except FileNotFoundError:
                pass
            except IsADirectoryError:
                try:
                    os.rmdir(parts[-1], dir_fd=directory)    # empty directories only; never recursive
                except FileNotFoundError:
                    pass

    def get_available_name(self, name, max_length=None):
        """Static destinations are replaced, never renamed aside."""
        name = "/".join(destination_components(name))
        if max_length is not None and len(name) > max_length:
            raise SuspiciousFileOperation(f"static destination name {name!r} is longer than {max_length}")
        return name

    def _open(self, name, mode="rb"):
        if set(mode) - set("rb"):
            raise ValueError("the static storage opens files for reading only")
        flags = _primitives()
        parts = destination_components(name)
        with self._directory(parts[:-1], create=False) as directory:
            if directory is None:
                raise FileNotFoundError(errno.ENOENT, f"{name}: no such static file")
            try:
                fd = os.open(parts[-1], flags.entry, dir_fd=directory)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise StaticConfinementError(errno.ELOOP, f"{name} is a symlink in STATIC_ROOT") from exc
                raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StaticConfinementError(errno.EINVAL, f"{name} is not a regular file")
            return File(os.fdopen(fd, "rb"), name)
        except BaseException:
            os.close(fd)
            raise

    def _save(self, name, content):
        flags = _primitives()
        parts = destination_components(name)
        with self._directory(parts[:-1], create=True) as directory:
            temporary = f"{TEMP_PREFIX}{secrets.token_hex(16)}"
            fd = os.open(temporary, flags.create, 0o600, dir_fd=directory)
            try:
                try:
                    for chunk in content.chunks():
                        view = memoryview(chunk.encode() if isinstance(chunk, str) else chunk)
                        while view:
                            view = view[os.write(fd, view):]
                    _add_mode(fd, os.fstat(fd), STATIC_FILE_MODE, name)
                finally:
                    os.close(fd)
                # Replaces the leaf ENTRY within this pinned directory: a symlink
                # there is replaced, never written through; a directory refuses.
                os.rename(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
                raise
        return "/".join(parts)

    def post_process(self, paths, dry_run=False, **options):
        """collectstatic's last step: repair the whole STATIC_ROOT tree. Yields
        nothing (no file is renamed or rewritten)."""
        if not dry_run:
            reconcile_static_permissions(self.location)
        yield from ()
