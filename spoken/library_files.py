"""The projected library files of spoken assets -- the ONLY code that writes them.

A projected file lives at ``LIBRARY_ROOT/<category code>/spoken-<asset uuid>.flac``
(``SpokenAsset.library_relpath``; never derived from user input). Its bytes are
always the exact bytes of an immutable, validated ``ProductionMedia``
rendition -- copied from production storage, never re-rendered, never taken
from an upload.

Publication is atomic on the destination filesystem:

1. the category directory is reached from the configured LIBRARY_ROOT with
   ``O_NOFOLLOW`` directory descriptors (created if missing; a symlink or
   non-directory there is refused);
2. the rendition is copied into a STAGING file in that same directory --
   ``.spoken-<uuid>.<random>.partial``: hidden, not an audio extension (no
   library scanner picks it up), created ``O_CREAT|O_EXCL|O_NOFOLLOW`` --
   hashing the bytes as they are written; the copy is fsynced;
3. the staged file is re-read from disk and must hash to the rendition's
   SHA-256 at its size (rendition SHA == staged SHA);
4. ``renameat`` replaces the stable name in ONE step (same directory, so the
   same filesystem: absent -> complete file, or previous complete edition ->
   new complete edition; a reader never sees a partial file); the directory
   is fsynced. The final SHA is the verified staged SHA.

The source (production storage) and the library may be different
filesystems: bytes always cross by copy+hash into the destination directory,
and only the final rename -- always within one directory -- is relied on to
be atomic. Relocation to another category is the same publication into the
other directory, followed (after the database commits) by removal of the old
name.

The database is the authority for what should exist (spoken.delivery);
``reconcile`` converges these files to it.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from production.services import media_io

from .models import LIBRARY_RELPATH_PATTERN

_RELPATH = re.compile(LIBRARY_RELPATH_PATTERN)
_FINAL = re.compile(r"^spoken-(?P<hex>[0-9a-f]{32})\.flac$")
_STAGING = re.compile(r"^\.spoken-(?P<hex>[0-9a-f]{32})\.[0-9a-f]{16}\.partial$")
_CHUNK = 1024 * 1024
DIRECTORY_MODE = 0o755
FILE_MODE = 0o644


class LibraryFileError(Exception):
    def __init__(self, message, *, code="library_write_failed"):
        super().__init__(message)
        self.code = code


def _fault(point: str) -> None:
    """Failure-injection hook (tests replace it to crash at ``point``)."""


def library_root() -> Path:
    return Path(os.path.realpath(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")))


def split(relpath: str) -> tuple[str, str]:
    if not isinstance(relpath, str) or not _RELPATH.fullmatch(relpath):
        raise LibraryFileError(f"{relpath!r} is not a projected library path", code="invalid_library_path")
    category, name = relpath.split("/")
    return category, name


def absolute(relpath: str) -> Path:
    category, name = split(relpath)
    return library_root() / category / name


@contextmanager
def _category_dir(category: str, *, create: bool):
    """A no-follow descriptor of LIBRARY_ROOT/<category> (or None if it does
    not exist and ``create`` is false)."""
    root = os.open(library_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(category, flags, dir_fd=root)
        except FileNotFoundError:
            if not create:
                yield None
                return
            try:
                os.mkdir(category, DIRECTORY_MODE, dir_fd=root)
            except FileExistsError:
                pass
            os.fsync(root)
            fd = os.open(category, flags, dir_fd=root)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise LibraryFileError(f"library category {category!r} is not a plain directory",
                                       code="unsafe_library_path") from None
            raise
        try:
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(root)


def _hash_fd(fd: int) -> tuple[str, int]:
    digest, offset = hashlib.sha256(), 0
    while True:
        chunk = os.pread(fd, _CHUNK, offset)
        if not chunk:
            return digest.hexdigest(), offset
        digest.update(chunk)
        offset += len(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def publish(relpath: str, rendition) -> str:
    """Atomically make ``relpath`` hold exactly the rendition's bytes; returns
    the verified SHA-256 (== the rendition's). Raises LibraryFileError; on
    failure the stable name is untouched (the staging file is removed)."""
    category, name = split(relpath)
    asset_hex = _FINAL.fullmatch(name).group("hex")
    staging = f".spoken-{asset_hex}.{secrets.token_hex(8)}.partial"
    try:
        opened = media_io.open_media(rendition)
    except Exception as exc:                                  # purged / not valid / missing bytes
        raise LibraryFileError(f"the rendition cannot be read: {exc}", code="rendition_unusable") from exc
    with opened.file as source, _category_dir(category, create=True) as directory:
        fd = os.open(staging, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=directory)
        staged = True
        try:
            digest, size = hashlib.sha256(), 0
            for chunk in iter(lambda: source.read(_CHUNK), b""):
                digest.update(chunk)
                _write_all(fd, chunk)
                size += len(chunk)
            os.fsync(fd)
            os.fchmod(fd, FILE_MODE)
            if (digest.hexdigest(), size) != (opened.media.sha256, opened.media.byte_size):
                raise LibraryFileError("the rendition bytes do not match its SHA-256", code="sha_mismatch")
            _fault("staged")
            if _hash_fd(fd) != (opened.media.sha256, opened.media.byte_size):       # re-read from disk
                raise LibraryFileError("the staged copy does not hash to the rendition", code="sha_mismatch")
            _fault("verified")
            os.rename(staging, name, src_dir_fd=directory, dst_dir_fd=directory)
            staged = False
            os.fsync(directory)
            _fault("published")
            return opened.media.sha256
        except OSError as exc:
            raise LibraryFileError(f"could not write {relpath}: {exc}") from exc
        finally:
            os.close(fd)
            if staged:
                try:
                    os.unlink(staging, dir_fd=directory)
                except OSError:
                    pass


def current(relpath: str) -> tuple[str | None, int | None]:
    """(SHA-256, size) of what the stable name holds now, or (None, None)
    when absent. A symlink, directory or other non-regular file there is
    ('unsafe', None) -- never followed."""
    category, name = split(relpath)
    with _category_dir(category, create=False) as directory:
        if directory is None:
            return None, None
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EISDIR):
                return "unsafe", None
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "unsafe", None
            return _hash_fd(fd)
        finally:
            os.close(fd)


def exists(relpath: str) -> bool:
    category, name = split(relpath)
    with _category_dir(category, create=False) as directory:
        if directory is None:
            return False
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False


def remove(relpath: str) -> bool:
    """Remove the stable name (never following a link; a directory there is
    refused). True when something was removed."""
    category, name = split(relpath)
    with _category_dir(category, create=False) as directory:
        if directory is None:
            return False
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            return False
        except IsADirectoryError:
            raise LibraryFileError(f"{relpath} is a directory", code="unsafe_library_path") from None
        os.fsync(directory)
        return True


def category_dirs() -> list[str]:
    """Every plain (non-symlink) directory directly under LIBRARY_ROOT."""
    names = []
    try:
        with os.scandir(library_root()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
    except FileNotFoundError:
        return []
    return sorted(names)


def projected_names(category: str) -> list[tuple[str, str]]:
    """[(kind, name)] of the spoken files in a category directory: kind is
    'final' (spoken-<uuid>.flac) or 'staging' (.spoken-<uuid>.<r>.partial)."""
    found = []
    with _category_dir(category, create=False) as directory:
        if directory is None:
            return []
        for name in os.listdir(directory):
            if _FINAL.fullmatch(name):
                found.append(("final", name))
            elif _STAGING.fullmatch(name):
                found.append(("staging", name))
    return sorted(found)


def asset_hex(name: str) -> str | None:
    match = _FINAL.fullmatch(name) or _STAGING.fullmatch(name)
    return match.group("hex") if match else None


def remove_stale_staging(category: str, name: str, *, older_than_seconds: float) -> bool:
    """Remove a staging file of ours that is older than the threshold (an
    in-flight publication's staging is younger and is left alone)."""
    if not _STAGING.fullmatch(name):
        return False
    with _category_dir(category, create=False) as directory:
        if directory is None:
            return False
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or time.time() - info.st_mtime < older_than_seconds:
            return False
        os.unlink(name, dir_fd=directory)
        return True
