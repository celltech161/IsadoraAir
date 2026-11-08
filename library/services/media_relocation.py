"""Guarded relocation of one registered Track's source file between managed Category folders (roadmap 3.11).

The one implementation of moving a Track's audio, for every caller:

* **Track Detail** -- a primary-category change moves the file to the new Category's folder (since r0112).
* **Library Reconciliation** -- category-drift repair (``library_category_drift``): move the file into its
  primary Category's folder, or change the primary Category to the folder's (no file operation).
* **The /library/ bulk editor** -- a primary-category change with the explicit "move files" policy
  (``track_bulk_edit``).

It provides the *mechanics*; each caller keeps its own authority and evidence (a Track Detail edit, a scan
finding and a previewed bulk edit are not interchangeable) and passes it in as ``check`` (what must still be
true, rechecked under the locks) and ``write`` (its own row changes).  The service never decides which
Category is right.

``execute`` -- one Track, one short transaction (``durable``: never inside another), in this order:

1. **Path locks, then the Track row** -- the canonical order of ``library.services.source_paths``: the
   source's and the destination's registration locks (sorted), then the Track's row (``SELECT ... FOR
   UPDATE``).  Every lock wait of the transaction together is bounded (``lock_budget_seconds``,
   ``_LockBudget``); a lock not obtained in time is ``busy`` and nothing changes.  PostgreSQL aborts a
   deadlock, which is reported the same way.
2. **Recheck** under the locks: the Track still names the expected source path.  For strict callers
   (reconciliation, bulk edit) the source must be a canonical, non-empty regular file inside the Library root
   (no symlink anywhere on its path) with the expected six-field witness (device, inode, size, mtime, ctime,
   link count), with no other hard link, no change within the last minute
   (``library_reconciliation.FRESH_SECONDS``: delivery or replacement in progress), readable, and owned by no
   other subsystem (spoken projection, Active Messages, remote-hosted).  Track Detail (not strict) keeps its
   r0112 rules: the staging's own confinement (no-follow opens beneath the root) refuses an unsafe source.
   The destination -- ``LIBRARY_ROOT/<Category code>/<the same file name>``, a valid single
   directory component -- must not exist and no other Track may name it (``collision``: never an overwrite,
   rename or numeric suffix).  Then the caller's ``check``.
3. **Stage** (``stage_no_replace``): a second directory entry for the same inode at the destination, made
   relative to descriptors of the Library root and the Category directory (``O_NOFOLLOW``, ``O_EXCL``
   semantics of ``link``), its identity verified against the open source.  Strict callers refuse a
   cross-filesystem move (``allow_copy=False``): the link fails with ``EXDEV`` before anything is created.
   Track Detail keeps its accepted copy fallback.  The staged inode must still be the witnessed one.
4. **Write**: the caller's ``write(track, filepath)`` saves its row changes with the path to store (the
   destination, or the unchanged source); ``Track.save`` claims the destination path re-entrantly.  The row is
   read back and the destination binding verified again.
5. **Commit.**  Before commit any failure rolls back and removes only the staged destination entry (its witness
   must be unchanged); the original is never touched (``failed_before_commit``; if the staged entry cannot be
   removed safely it is kept and reported).  A failure *at* commit leaves the outcome unknown: both entries
   are kept and the result is ``uncertain`` -- the database names one of them either way.
6. **Cleanup** after commit: in its own short transaction, holding the old path's lock (bounded), with no Track
   naming the old path, and only while the old entry is still the staged inode, the old directory entry is
   unlinked.  Anything else keeps it and is reported as ``committed_cleanup_required``: the move committed
   and is valid; only an extra directory entry remains.  Nothing is ever retried automatically.

The audio is never decoded, re-encoded, tagged or hashed: the inode moves, so its content is unchanged.  Every
outcome is audited (``library_audit``, ``relocation.track``) with the operation's origin.

Waveform caches: a cache records the path and the six-field witness of the file it was decoded from
(``waveform_cache``).  After a move both differ (the path, and the ctime that link/unlink change), so the cache
stays displayable but becomes *unverified* until Reanalyze Track decodes the file again; Repick and Reset Cue
Points are unavailable meanwhile.  Cue points, their locks, revisions and approvals are not touched, playback
is unaffected, and no cache is rewritten here.
"""
from __future__ import annotations

import errno
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, OperationalError, router, transaction, connections

log = logging.getLogger(__name__)

# Outcomes of one Track's operation.
MOVED = "moved"                                         # the file moved (and the row was written)
UPDATED = "updated"                                     # the row was written; no file was moved
UNCHANGED = "unchanged"                                 # nothing needed doing
STALE = "stale"                                         # something changed since the review
REFUSED = "refused"                                     # not allowed or not safe here
COLLISION = "collision"                                 # the destination exists or is another Track's
BUSY = "busy"                                           # a lock was not obtained in time
FAILED_BEFORE_COMMIT = "failed_before_commit"           # rolled back; the original is untouched
COMMITTED_CLEANUP_REQUIRED = "committed_cleanup_required"   # committed; the old entry needs attention
UNCERTAIN = "uncertain"                                 # the commit's outcome is unknown; both entries kept
COMMITTED = (MOVED, UPDATED, COMMITTED_CLEANUP_REQUIRED)
#: Outcomes an operator must look at before anything else is done (a browser batch stops on them).
ATTENTION = (COMMITTED_CLEANUP_REQUIRED, UNCERTAIN)

ORIGINS = ("track_detail", "reconciliation", "bulk_edit")
#: Default bound on every lock wait of one Track's transaction together (path locks, row lock, writes).
DEFAULT_LOCK_BUDGET_SECONDS = 3.0
#: The after-commit removal of the old directory entry waits this long for its path lock.
CLEANUP_LOCK_TIMEOUT_MS = 2000

# Cleanup states.
CLEANUP_REMOVED = "removed"
CLEANUP_NOT_NEEDED = "not_needed"
CLEANUP_RETAINED_OWNER = "retained_live_owner"
CLEANUP_RETAINED_BUSY = "retained_busy"
CLEANUP_RETAINED_CHANGED = "retained_changed"
CLEANUP_FAILED = "failed"
CLEANUP_DESTINATION_RETAINED = "destination_retained"   # rolled back, but the staged entry could not be removed
CLEANUP_BOTH_RETAINED = "both_retained"                 # uncertain commit: neither entry removed


class CrossFilesystemRefused(OSError):
    """The destination is on another filesystem and this caller does not copy."""


class Stop(Exception):
    """End one Track's operation with ``outcome`` (raised by the service's checks or a caller's ``check``)."""

    def __init__(self, outcome, message):
        super().__init__(message)
        self.outcome = outcome
        self.message = message


@dataclass
class Result:
    outcome: str
    message: str
    track_id: int
    source: str = ""
    destination: str | None = None
    committed: bool = False
    cleanup: str = ""
    cleanup_message: str = ""
    error: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def attention(self) -> bool:
        return self.outcome in ATTENTION or self.cleanup == CLEANUP_DESTINATION_RETAINED

    def as_dict(self) -> dict:
        from library.services import library_audit
        return {"outcome": self.outcome, "message": self.message, "track_id": self.track_id,
                "source": library_audit.library_relative(self.source),
                "destination": library_audit.library_relative(self.destination) if self.destination else None,
                "committed": self.committed, "cleanup": self.cleanup, "cleanup_message": self.cleanup_message,
                "attention": self.attention, **self.detail}


# --------------------------------------------------------------------------- staging (moved from library.views)

def _stat_witness(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _inode_witness(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _content_witness(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _entry_witness(directory_fd, name):
    return _stat_witness(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))


class StagedMediaRelocation:
    """Two descriptor-anchored directory entries retained until transaction outcome."""

    def __init__(self, source, destination, source_parent_fd, destination_parent_fd,
                 source_witness, destination_witness):
        self.source = source
        self.destination = destination
        self.source_parent_fd = source_parent_fd
        self.destination_parent_fd = destination_parent_fd
        self.source_witness = source_witness
        self.destination_witness = destination_witness
        self.closed = False

    def verify_destination_binding(self):
        """The pathname saved in the database must still name our anchored file and directory."""
        anchored_parent = _inode_witness(os.fstat(self.destination_parent_fd))
        named_parent = _inode_witness(os.lstat(self.destination.parent))
        if named_parent != anchored_parent or named_parent[2] != stat.S_IFDIR:
            raise OSError("the destination category directory changed during the Track update")
        anchored = _entry_witness(self.destination_parent_fd, self.destination.name)
        try:
            named = _stat_witness(os.lstat(self.destination))
        except FileNotFoundError:
            named = None
        if anchored != self.destination_witness or named != self.destination_witness:
            raise OSError("the staged destination changed during the Track update")

    def _remove(self, directory_fd, path, witness):
        try:
            current = _entry_witness(directory_fd, path.name)
        except FileNotFoundError:
            return
        if current != witness:
            raise OSError(f"refusing to remove {path}: it changed during the Track update")
        os.unlink(path.name, dir_fd=directory_fd)

    def remove_destination(self):
        self._remove(self.destination_parent_fd, self.destination, self.destination_witness)

    def remove_source(self):
        self._remove(self.source_parent_fd, self.source, self.source_witness)

    def close(self):
        if not self.closed:
            os.close(self.source_parent_fd)
            os.close(self.destination_parent_fd)
            self.closed = True


def stage_no_replace(source: Path, destination: Path, *, library_root: Path, allow_copy: bool = True):
    """Stage a confined destination without following source or directory symlinks.

    The category directory is created/opened relative to an already-resolved root descriptor and
    remains open through the database transaction. Both the hard-link path and EXDEV copy fallback
    use that descriptor, O_EXCL and no-follow source opens. The returned handle owns the directory
    descriptors until the caller commits or rolls back.  ``allow_copy=False``: a cross-filesystem
    destination raises ``CrossFilesystemRefused`` before anything is created.
    """
    root = Path(library_root).resolve(strict=True)
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    category_code = destination.parent.name
    validate_category_storage_component(category_code)
    if destination.parent.parent != root or destination.name != source.name:
        raise OSError("the proposed media destination is not a direct child of the library root")

    source_parent = source.parent
    try:
        source_parent.relative_to(root)
    except ValueError:
        raise OSError("the source media is outside the library root") from None
    if source_parent.resolve(strict=True) != source_parent:
        raise OSError("the source media directory contains a symlink")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(root, directory_flags)
    source_parent_fd = destination_parent_fd = source_fd = None
    destination_created = False
    try:
        try:
            os.mkdir(category_code, mode=0o755, dir_fd=root_fd)
        except FileExistsError:
            pass
        destination_parent_fd = os.open(category_code, directory_flags, dir_fd=root_fd)
        # Refuse a symlink/path substitution between the descriptor-relative open and its pathname.
        if _inode_witness(os.lstat(destination.parent)) != _inode_witness(os.fstat(destination_parent_fd)):
            raise OSError("the destination category directory is not a stable directory beneath the library root")

        source_parent_fd = os.open(source_parent, directory_flags)
        source_fd = os.open(
            source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_parent_fd,
        )
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise OSError("the source media is not a regular file")
        if _entry_witness(source_parent_fd, source.name) != _stat_witness(source_info):
            raise OSError("the source media changed while relocation was staged")

        try:
            os.link(
                source.name, destination.name,
                src_dir_fd=source_parent_fd, dst_dir_fd=destination_parent_fd,
                follow_symlinks=False,
            )
            destination_created = True
            linked = os.stat(destination.name, dir_fd=destination_parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(linked.st_mode) or (linked.st_dev, linked.st_ino) != (
                    source_info.st_dev, source_info.st_ino):
                raise OSError("the source media changed while its hard link was created")
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            if not allow_copy:
                raise CrossFilesystemRefused(
                    errno.EXDEV, "the destination folder is on another filesystem; a cross-filesystem move is "
                                 "not supported here") from None
            destination_fd = os.open(
                destination.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                source_info.st_mode & 0o777,
                dir_fd=destination_parent_fd,
            )
            destination_created = True
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(destination_fd, view):]
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            os.utime(
                destination.name,
                ns=(source_info.st_atime_ns, source_info.st_mtime_ns),
                dir_fd=destination_parent_fd,
                follow_symlinks=False,
            )

        source_witness = _entry_witness(source_parent_fd, source.name)
        if not stat.S_ISREG(source_witness[2]) or _content_witness(
                os.stat(source.name, dir_fd=source_parent_fd, follow_symlinks=False)) != _content_witness(source_info):
            raise OSError("the source media changed while relocation was staged")
        destination_witness = _entry_witness(destination_parent_fd, destination.name)
        if not stat.S_ISREG(destination_witness[2]) or destination_witness[3] != source_info.st_size:
            raise OSError("the staged destination is not a regular file")

        staged = StagedMediaRelocation(
            source, destination, source_parent_fd, destination_parent_fd,
            source_witness, destination_witness,
        )
        try:
            staged.verify_destination_binding()
        except BaseException:
            staged.remove_destination()
            staged.close()
            source_parent_fd = destination_parent_fd = None
            destination_created = False
            raise
        source_parent_fd = destination_parent_fd = None
        return staged
    except BaseException:
        if destination_created and destination_parent_fd is not None:
            try:
                os.unlink(destination.name, dir_fd=destination_parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if source_parent_fd is not None:
            os.close(source_parent_fd)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)
        os.close(root_fd)


def validate_category_storage_component(value):
    from library.models import validate_category_storage_component as validate
    return validate(value)


# --------------------------------------------------------------------------- paths and the source

def library_root() -> str:
    """The real (symlink-free) Library root; raises Stop(REFUSED) when the storage is unavailable."""
    configured = getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")
    try:
        root = os.path.realpath(configured)
    except (OSError, ValueError):
        root = None
    if not root or not os.path.isdir(root):
        raise Stop(REFUSED, "The Library storage is unavailable.")
    return root


def category_folder(path, root) -> str:
    """The first directory under the Library root that ``path`` sits in ("" for the root itself or outside)."""
    if not isinstance(path, str) or not path.startswith(root + os.sep):
        return ""
    parts = os.path.relpath(path, root).split(os.sep)
    return parts[0] if len(parts) > 1 else ""


def destination_for(source, category_code, root) -> str:
    """``root/<category_code>/<the source's file name>``; Stop(REFUSED) when the code is not one directory name."""
    try:
        validate_category_storage_component(category_code)
    except ValidationError:
        raise Stop(REFUSED, f"The Category code {category_code!r} cannot be used as a Library folder name.") from None
    return os.path.join(root, category_code, os.path.basename(source))


def _fresh(info) -> bool:
    from library.services import library_reconciliation
    return time.time_ns() - max(info.st_mtime_ns, info.st_ctime_ns) < \
        library_reconciliation.FRESH_SECONDS * 1_000_000_000


def check_source(path, *, root, expected=None, strict=True, protection=None, track=None) -> list:
    """The source's six-field witness (``library_reconciliation.stat_witness``) when it may be relocated;
    otherwise raises Stop (STALE for a changed or vanished file, REFUSED for one that is not safe to move)."""
    from library.services import library_reconciliation
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise Stop(REFUSED, "The Track's stored file path is not an absolute path.")
    if os.path.normpath(path) != path or not path.startswith(root + os.sep):
        raise Stop(REFUSED, "The Track's file is not inside the managed Library.")
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        real = None
    if real != path:
        raise Stop(REFUSED, "The Track's file path goes through a symbolic link (it is not its canonical path).")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise Stop(STALE, "The Track's file no longer exists at its path.") from None
    except OSError as exc:
        raise Stop(REFUSED, f"The Track's file cannot be checked ({exc.strerror or type(exc).__name__}).") from None
    if not stat.S_ISREG(info.st_mode):
        raise Stop(REFUSED, "The Track's file is not a regular file.")
    if info.st_size == 0:
        raise Stop(REFUSED, "The Track's file is empty (0 bytes).")
    witness = library_reconciliation.stat_witness(info)
    if expected is not None and witness != list(expected):
        raise Stop(STALE, "The Track's file was modified or replaced since it was reviewed.")
    if strict:
        if info.st_nlink != 1:
            raise Stop(REFUSED, f"The Track's file has {info.st_nlink} directory entries (hard links: a shared "
                                "source); it is not moved here.")
        if _fresh(info):
            raise Stop(REFUSED, "The Track's file changed within the last minute (delivery or replacement in "
                                "progress?); try again later.")
        if not os.access(path, os.R_OK):
            raise Stop(REFUSED, "The Track's file cannot be read.")
    if protection is not None:
        path_owner, track_owner = protection
        owner = (track_owner(track.pk) if track is not None else None) or (
            "remote-hosted Track" if track is not None and getattr(track, "remote_url", "") else None) or \
            path_owner(path, os.path.relpath(path, root), path)
        if owner:
            raise Stop(REFUSED, f"Protected: {owner}.")
    return witness


def check_destination(source, destination, *, root, track_id, allow_copy) -> None:
    """The destination must be free (no entry, no Track names it) and, for a non-copying caller, on the source's
    filesystem.  Raises Stop."""
    from library.models import Track
    if Track.objects.filter(filepath=destination).exclude(pk=track_id).exists():
        raise Stop(COLLISION, f"Another Track already names {os.path.relpath(destination, root)}.")
    if os.path.lexists(destination):
        raise Stop(COLLISION, f"A file already exists at {os.path.relpath(destination, root)}; it is never "
                              "overwritten or renamed. Resolve it by hand first.")
    folder = os.path.dirname(destination)
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise Stop(REFUSED, f"The destination folder cannot be checked ({exc.strerror}).") from None
    if info is not None and not stat.S_ISDIR(info.st_mode):
        raise Stop(REFUSED, "The destination Category folder is not a plain directory (a symbolic link or a file).")
    if not allow_copy:
        try:
            device = (info or os.lstat(root)).st_dev
            if device != os.lstat(source).st_dev:
                raise Stop(REFUSED, "The destination folder is on another filesystem; a cross-filesystem move is "
                                    "not supported here.")
        except OSError as exc:
            raise Stop(REFUSED, f"The filesystems cannot be compared ({exc.strerror}).") from None


# --------------------------------------------------------------------------- playout protection

#: LogItems of the local hours from the current one through this many later are "imminent" (the engine
#: loads the current hour's log and looks ahead at the next one, holding their Track rows in memory).
IMMINENT_LATER_HOURS = 1


def playout_conflicts(track, *, now, engine, engine_problem="") -> tuple[list, list]:
    """(blockers, warnings) for changing the file path or primary Category of a Track the engine may be
    playing or about to play.  ``engine``: a checked engine state (``library_duplicate_resolution._engine_state``),
    or None with ``engine_problem`` -- then nothing is allowed."""
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from library.models import LogItem
    from library.services.library_duplicate_resolution import _on_air

    blockers, warnings = [], []
    if engine is None:
        blockers.append("the playout engine's current state cannot be confirmed: "
                        f"{engine_problem or 'it is unavailable'}; nothing is changed until it can be")
    blockers.extend(f"it may be on air: {reason}" for reason in _on_air(track, now, engine))
    start = timezone.localtime(now).replace(minute=0, second=0, microsecond=0)
    hours = [start + timedelta(hours=i) for i in range(IMMINENT_LATER_HOURS + 1)]
    window = Q(pk__in=[])
    for h in hours:
        window |= Q(playlist_log__date=h.date(), playlist_log__hour=h.hour)
    active_log = (engine or {}).get("log_id")
    if isinstance(active_log, int) and not isinstance(active_log, bool):
        window |= Q(playlist_log_id=active_log)
    items = LogItem.objects.filter(track_id=track.pk)
    if items.filter(window, played_at__isnull=True).exists():
        blockers.append("it is scheduled in the current or next log hour (or the engine's active log), which the "
                        "engine may already hold in memory; retry after that hour has played")
    last = hours[-1]
    later = Q(playlist_log__date__gt=last.date()) | Q(playlist_log__date=last.date(), playlist_log__hour__gt=last.hour)
    future = items.filter(later).values("playlist_log_id").distinct().count()
    if future:
        warnings.append(f"It is scheduled in {future} later log hour(s); those entries stay as they are (no log is "
                        "changed or rebuilt) and will use the Track as it is when they play.")
    return blockers, warnings


def future_log_hours(track_id, now) -> int:
    """How many log hours from the current one on schedule this Track (display only)."""
    from django.db.models import Q

    from library.models import LogItem
    from library.services.library_duplicate_resolution import _local_hour
    today, hour = _local_hour(now)
    return (LogItem.objects.filter(track_id=track_id)
            .filter(Q(playlist_log__date__gt=today) | Q(playlist_log__date=today, playlist_log__hour__gte=hour))
            .values("playlist_log_id").distinct().count())


# --------------------------------------------------------------------------- the operation

def _remove_old_entry(staged, source, using) -> tuple[str, str]:
    """After commit: unlink the old directory entry under its path lock, only while no Track names it."""
    from library.models import Track
    from library.services import source_paths
    try:
        with transaction.atomic(using=using):
            try:
                source_paths.acquire(source, using, timeout_ms=CLEANUP_LOCK_TIMEOUT_MS)
            except OperationalError:
                return CLEANUP_RETAINED_BUSY, ("the old file entry was kept: another operation held its path; "
                                               "remove it by hand after checking no Track uses it")
            if Track._base_manager.using(using).filter(filepath=source).exists():
                return CLEANUP_RETAINED_OWNER, "the old file entry was kept: another Track now names that path"
            try:
                staged.remove_source()
            except OSError as exc:
                return CLEANUP_RETAINED_CHANGED, f"the old file entry was kept: {exc}"
    except Exception as exc:                  # never fail a committed move because its cleanup failed
        log.warning("relocation cleanup of %s failed: %s", source, type(exc).__name__)
        return CLEANUP_FAILED, f"the old file entry could not be removed ({type(exc).__name__}: {exc})"
    return CLEANUP_REMOVED, ""


def execute(*, track_id, source, destination, origin, user, write, check=None, expected_witness=None,
            strict=True, allow_copy=False, lock_budget_seconds=DEFAULT_LOCK_BUDGET_SECONDS, protection=None,
            audit=None) -> Result:
    """Relocate (``destination``) or only rewrite (``destination=None``) one Track; see the module docstring.

    ``source``: the path the caller expects the Track to name.  ``write(track, filepath)``: saves the caller's
    row changes with ``filepath`` (returns a dict merged into the result, or None).  ``check(track)``: the
    caller's own rechecks under the locks (raise ``Stop``).  ``audit``: extra audit detail (operation identity,
    scan and finding, direction...)."""
    from library.models import Track, TrackWriteRefused
    from library.services import source_paths
    from library.services.library_duplicate_resolution import _LockBudget

    if origin not in ORIGINS:
        raise ValueError(f"unknown relocation origin {origin!r}")
    result = Result(outcome=FAILED_BEFORE_COMMIT, message="", track_id=track_id, source=source,
                    destination=destination)
    staged = None
    body_done = False
    committed = [False]
    using = router.db_for_write(Track)
    try:
        root = library_root()
        if destination is not None:
            if os.path.dirname(os.path.dirname(destination)) != root or \
                    os.path.basename(destination) != os.path.basename(source or ""):
                raise Stop(REFUSED, "The destination is not the source's file name in a Category folder directly "
                                    "beneath the Library root.")
            destination_for(source, os.path.basename(os.path.dirname(destination)), root)
            if destination == source:
                destination = result.destination = None
        budget = _LockBudget(lock_budget_seconds)
        with transaction.atomic(using=using, durable=True), connections[using].execute_wrapper(budget):
            transaction.on_commit(lambda: committed.__setitem__(0, True), using=using)
            # Path locks first (sorted), then the Track row: the order every path-registering write uses.
            for path in sorted({source} | ({destination} if destination else set())):
                remaining = max(1, int((budget.deadline - time.monotonic()) * 1000))
                source_paths.acquire(path, using, timeout_ms=remaining)
            track = Track.objects.select_for_update(of=("self",)).filter(pk=track_id).first()
            if track is None:
                raise Stop(STALE, "The Track no longer exists.")
            if track.filepath != source:
                raise Stop(STALE, "The Track's file path changed since it was reviewed.")
            witness = None
            if strict:
                # Track Detail (not strict) keeps its r0112 behaviour: the staging's own confinement decides.
                witness = check_source(source, root=root, expected=expected_witness, strict=True,
                                       protection=protection, track=track)
            if destination is not None:
                check_destination(source, destination, root=root, track_id=track_id, allow_copy=allow_copy)
            if check is not None:
                check(track)
            if destination is not None:
                staged = stage_no_replace(Path(source), Path(destination), library_root=Path(root),
                                          allow_copy=allow_copy)
                linked = staged.source_witness
                if witness is not None and (linked[0], linked[1], linked[3], linked[4]) != (
                        witness[0], witness[1], witness[2], witness[3]):
                    raise Stop(STALE, "The Track's file was replaced while it was being moved.")
                staged.verify_destination_binding()
            extra = write(track, destination or source)
            if extra:
                result.detail.update(extra)
            stored = Track.objects.filter(pk=track_id).values_list("filepath", flat=True).first()
            if stored != (destination or source):
                raise RuntimeError("the Track row does not name the expected file path after the update")
            if staged is not None:
                # Catch deterministic directory/path substitution after staging but before commit.
                staged.verify_destination_binding()
            body_done = True
    except BaseException as exc:
        if body_done and not committed[0]:
            # The failure happened at COMMIT: whether it applied is unknown.  Keep both entries (the database
            # names one of them either way) and say so; nothing is retried.
            result.outcome, result.committed = UNCERTAIN, False
            result.message = ("The database did not confirm the update; whether it was saved is unknown. Both "
                              "file entries were kept. Check the Track before doing anything else.")
            result.cleanup = CLEANUP_BOTH_RETAINED if staged is not None else CLEANUP_NOT_NEEDED
            result.error = f"{type(exc).__name__}: {exc}"
            if staged is not None:
                staged.close()
            log.error("relocation track=%s origin=%s uncertain commit: %s", track_id, origin, type(exc).__name__)
            if not isinstance(exc, Exception):
                raise
            _audit(result, origin, user, audit)
            return result
        if not committed[0]:
            if staged is not None:
                try:
                    staged.remove_destination()
                except BaseException as restore_exc:
                    result.cleanup = CLEANUP_DESTINATION_RETAINED
                    result.cleanup_message = (f"the staged entry {staged.destination} could not be removed safely "
                                              f"({restore_exc}); the original remains at {staged.source}")
                finally:
                    staged.close()
                staged = None
            if not isinstance(exc, Exception):
                raise
            result.outcome, result.message = _failure(exc, destination)
            result.error = f"{type(exc).__name__}: {exc}"
            _audit(result, origin, user, audit)
            return result
        # Committed, then an on_commit callback raised: the move stands; clean up and report it.
        result.error = f"post-commit: {type(exc).__name__}: {exc}"
        log.warning("relocation track=%s origin=%s post-commit callback failed: %s", track_id, origin,
                    type(exc).__name__)
        if not isinstance(exc, Exception):
            if staged is not None:
                staged.close()
            raise
    result.committed = True
    if staged is None:
        result.outcome = UPDATED
        result.cleanup = CLEANUP_NOT_NEEDED
        result.message = result.message or "Updated; the file was not moved."
    else:
        try:
            result.cleanup, result.cleanup_message = _remove_old_entry(staged, source, using)
        finally:
            staged.close()
        if result.cleanup == CLEANUP_REMOVED:
            result.outcome, result.message = MOVED, "Moved."
        else:
            result.outcome = COMMITTED_CLEANUP_REQUIRED
            result.message = ("The move committed: the Track now names its new path and the file is there. The old "
                              f"directory entry needs attention ({result.cleanup_message}). It is not retried.")
    _audit(result, origin, user, audit)
    return result


def _failure(exc, destination) -> tuple[str, str]:
    from library.models import TrackWriteRefused
    if isinstance(exc, Stop):
        return exc.outcome, exc.message
    if isinstance(exc, TrackWriteRefused):
        return REFUSED, f"Protected: {exc}"
    if isinstance(exc, CrossFilesystemRefused):
        return REFUSED, "The destination folder is on another filesystem; a cross-filesystem move is not supported here."
    if isinstance(exc, FileExistsError):
        return COLLISION, "The destination was created by another operation meanwhile; nothing was changed."
    if isinstance(exc, OperationalError):
        return BUSY, ("The Track or one of its paths is busy (another operation holds it, or the database was "
                      "unavailable); nothing was changed. Try again.")
    if isinstance(exc, DatabaseError):
        return FAILED_BEFORE_COMMIT, f"Not saved ({type(exc).__name__}); nothing was changed."
    if isinstance(exc, OSError):
        return FAILED_BEFORE_COMMIT, f"The file could not be staged safely ({exc}); nothing was changed."
    return FAILED_BEFORE_COMMIT, f"Failed before commit ({type(exc).__name__}: {exc}); nothing was changed."


def _audit(result, origin, user, extra) -> None:
    from library.services import library_audit
    level = "info" if result.outcome in (MOVED, UPDATED, UNCHANGED) else "warning"
    if result.attention:
        level = "error"
    library_audit.record(
        "relocation.track", user=user, outcome=result.outcome, level=level,
        title=f"Library Track {result.track_id}: {result.outcome.replace('_', ' ')} ({origin})"[:200],
        detail={"origin": origin, "track_id": result.track_id,
                "from": library_audit.library_relative(result.source),
                "to": library_audit.library_relative(result.destination) if result.destination else None,
                "committed": result.committed, "cleanup": result.cleanup or None,
                "reason": result.message[:300], **(extra or {}), **result.detail})
