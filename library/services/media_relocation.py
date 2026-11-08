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
   relative to descriptors of the Library root and the Category directory (no-follow; ``link`` never
   overwrites).  The destination's expected identity is fixed by the creation itself -- the inode of the source
   descriptor this operation opened and keeps open -- and is never re-learned from a later look at the
   pathname (see "Threat model" below).  Strict callers refuse a cross-filesystem move (``allow_copy=False``):
   the link fails with ``EXDEV`` before anything is created.  Track Detail keeps its copy fallback, now bound to
   the descriptor of the file it created with ``O_EXCL`` and verified by reading the copy back (SHA-256 of
   source and copy).  The staged inode must be the witnessed recording.
4. **Write**: the caller's ``write(track, filepath)`` saves its row changes with the path to store (the
   destination, or the unchanged source); ``Track.save`` claims the destination path re-entrantly.  The row is
   read back, and immediately before commit ``StagedMediaRelocation.verify`` checks again that the source
   descriptor is unchanged and its path still names it, and that the destination directory and entry are
   still the staged object.
5. **Commit.**  Before commit any failure rolls back; the staged entry is removed only through the
   identity-safe removal (``remove_entry``), which withholds removal when it finds the entry to be the
   recording's last remaining name (a check, not an atomic guarantee: see "Limitation" below), and the original
   is never touched (``failed_before_commit``; whatever cannot be removed safely is kept and reported,
   ``destination_retained``).  A failure *at* commit leaves the outcome unknown: both entries are kept and the
   result is ``uncertain`` -- the database names one of them either way.
6. **Cleanup** after commit: in its own short transaction, holding the old path's lock (bounded), with no Track
   naming the old path and while the committed destination still names the relocated recording, the old entry
   is removed through ``remove_entry``.  Anything else keeps it and is reported as
   ``committed_cleanup_required`` (with the quarantine location of anything kept there): the move committed and
   is valid.  Nothing is ever retried automatically.

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
import secrets
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
CLEANUP_QUARANTINED = "retained_quarantined"            # kept, untouched, in the relocation quarantine
CLEANUP_RETAINED_UNSAFE = "retained_unsafe"             # identity-safe removal impossible: left in place
CLEANUP_DESTINATION_RETAINED = "destination_retained"   # rolled back, but the staged entry could not be removed
CLEANUP_BOTH_RETAINED = "both_retained"                 # uncertain commit: neither entry removed


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
    cleanup_location: str | None = None         # a retained object's place in the relocation quarantine
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
                "cleanup_location": library_audit.library_relative(self.cleanup_location)
                if self.cleanup_location else None,
                "attention": self.attention, **self.detail}


# --------------------------------------------------------------------------- staging and identity-safe removal
#
# Threat model.  Other filesystem writers -- ingest scripts, file managers, an operator's shell, a second
# IsadoraAir operation -- may create, rename, replace or delete entries in the Library's folders at any moment, and
# need not take IsadoraAir's PostgreSQL path locks.  Against them two rules hold:
#
# * **A destination's identity comes from its creation, never from a later look at its name.**  The expected
#   inode is that of a descriptor this operation holds open for its whole life: the source's own descriptor when
#   the destination is a hard link to it, or the descriptor ``O_CREAT | O_EXCL`` returned when Track Detail's
#   copy fallback created it.  An open inode cannot be freed and its number reused, so "this entry has the expected
#   (device, inode)" proves "this entry is the recording we authorized".  Every later observation of the pathname
#   is checked against that fixed identity; none ever replaces it.
# * **Nothing is unlinked because its name passed a check.**  An entry is removed only by moving it atomically
#   (``renameat2(RENAME_NOREPLACE)``) into a fresh private quarantine directory (``QUARANTINE_DIRNAME`` beneath
#   the Library root: mode 0700, owned by this user, a new random sub-directory per removal, all
#   descriptor-relative and no-follow), then examining *the moved object there* against the expected identity.
#   Only a match is unlinked -- inside the quarantine, where no other writer operates.  A mismatch (the name had
#   been replaced) is never deleted: it is moved back without overwriting anything, or, if its old name has been
#   taken meanwhile, kept in the quarantine with a ``RETAINED.json`` manifest and reported for the operator.  If
#   the quarantine cannot be used (no ``renameat2``, another filesystem, an unsafe quarantine directory), nothing
#   is removed and the entry is reported as retained.
#
# Out of scope, as for every other IsadoraAir component: a process running as the same user that deliberately
# operates inside IsadoraAir's private quarantine directory, or rewrites a recording's bytes and then restores its
# size and modification time before the link is made (the recorded six-field witness is metadata, not a hash).
#
# Limitation (no atomic guarantee is claimed).  Cooperating IsadoraAir operations are serialized by the
# transaction-scoped path locks (``library.services.source_paths``), and the removal above never knowingly deletes
# a substituted, unrelated file.  A writer that does not cooperate can independently delete a registered
# recording at any time.  The last-remaining-name check (``keep_last_link``) reads the link count of the moved
# entry and then unlinks it: if such a writer unlinks the recording's other name between that check and the
# unlink, the recording can be left with no filesystem name.  Linux offers no primitive that makes "unlink only if
# another name remains" atomic, and IsadoraAir does not claim one.

QUARANTINE_DIRNAME = ".relocation-quarantine"
RENAME_NOREPLACE = 1
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

# Removal outcomes (``remove_entry``).
ENTRY_REMOVED = "removed"                     # the expected object was removed
ENTRY_MISSING = "missing"                     # nothing was at the name
ENTRY_FOREIGN_RESTORED = "foreign_restored"   # another object was at the name; it was put back untouched
ENTRY_QUARANTINED = "quarantined"             # an object could not be put back: kept in the quarantine
ENTRY_NOT_MOVED = "not_moved"                 # the quarantine could not be used: nothing was moved or removed
ENTRY_GONE = (ENTRY_REMOVED, ENTRY_MISSING)


class CrossFilesystemRefused(OSError):
    """The destination is on another filesystem and this caller does not copy."""


class QuarantineUnavailable(OSError):
    """No atomic no-overwrite move into a private quarantine is possible here."""


class StagingUnwindIncomplete(OSError):
    """Staging failed and its destination entry could not be removed safely: it was kept (see the message)."""


def _load_renameat2():
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renameat2
    except (OSError, AttributeError, ImportError):
        return None, None
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    return function, ctypes


_RENAMEAT2, _CTYPES = _load_renameat2()


def _rename_noreplace(src_dir_fd, src_name, dst_dir_fd, dst_name) -> None:
    """``renameat2(..., RENAME_NOREPLACE)``: atomically move one directory entry; never overwrite."""
    if _RENAMEAT2 is None:
        raise QuarantineUnavailable(errno.ENOSYS, "renameat2 is not available")
    if _RENAMEAT2(src_dir_fd, os.fsencode(src_name), dst_dir_fd, os.fsencode(dst_name), RENAME_NOREPLACE) != 0:
        code = _CTYPES.get_errno()
        if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV):
            raise QuarantineUnavailable(code, f"no atomic no-overwrite move here ({os.strerror(code)})")
        raise OSError(code, os.strerror(code), os.fsdecode(src_name))


def _identity(info) -> tuple:
    return (info.st_dev, info.st_ino)


def _inode_witness(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _stat_witness(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _entry_witness(directory_fd, name):
    return _stat_witness(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))


def _open_quarantine(root_fd):
    """The private quarantine directory beneath the Library root (created on first use), as a descriptor."""
    try:
        os.mkdir(QUARANTINE_DIRNAME, 0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    fd = os.open(QUARANTINE_DIRNAME, _DIRECTORY_FLAGS, dir_fd=root_fd)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise QuarantineUnavailable(errno.EPERM, "the relocation quarantine directory is not private to IsadoraAir")
    return fd


def _write_manifest(directory_fd, manifest) -> None:
    import json
    try:
        fd = os.open("RETAINED.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=directory_fd)
        with os.fdopen(fd, "w") as handle:
            json.dump(manifest, handle, indent=1, sort_keys=True)
    except OSError:
        log.warning("relocation quarantine manifest not written")


def remove_entry(root, root_fd, dir_fd, name, expected, *, context, keep_last_link=False) -> tuple[str, str, str | None]:
    """Remove the entry ``name`` of directory ``dir_fd`` only if it is the object ``expected`` (device, inode), which
    the caller holds open.  ``keep_last_link``: withhold the removal when the moved entry is found to be the
    recording's last remaining name (a hard-linked relocation whose other name another writer removed or
    replaced).  That is a check followed by an unlink, not an atomic guarantee: see "Limitation" in the threat
    model.  Returns (outcome, explanation, quarantine
    location or None); see the threat model."""
    try:
        quarantine_fd = _open_quarantine(root_fd)
    except OSError as exc:
        return ENTRY_NOT_MOVED, f"it was left in place: the relocation quarantine is unavailable ({exc})", None
    holder = f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(8)}"
    location = os.path.join(root, QUARANTINE_DIRNAME, holder, "entry")
    try:
        try:
            os.mkdir(holder, 0o700, dir_fd=quarantine_fd)
            holder_fd = os.open(holder, _DIRECTORY_FLAGS, dir_fd=quarantine_fd)
        except OSError as exc:
            return ENTRY_NOT_MOVED, f"it was left in place: no quarantine entry could be made ({exc})", None
        moved = False
        try:
            try:
                _rename_noreplace(dir_fd, name, holder_fd, "entry")
                moved = True
            except FileNotFoundError:
                return ENTRY_MISSING, "nothing was there any more", None
            except OSError as exc:
                return ENTRY_NOT_MOVED, f"it was left in place: it could not be moved aside safely ({exc})", None
            info = os.stat("entry", dir_fd=holder_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and _identity(info) == tuple(expected):
                if keep_last_link and info.st_nlink < 2:
                    try:                                    # the recording's only name: keep it where it was
                        _rename_noreplace(holder_fd, "entry", dir_fd, name)
                        moved = False
                        return ENTRY_NOT_MOVED, ("it was kept in place: it is the recording's only remaining name "
                                                 "(its other name was removed or replaced by another writer)"), None
                    except OSError as exc:
                        _write_manifest(holder_fd, {**context, "reason": f"the recording's only name; not restorable "
                                                                         f"({exc})",
                                                    "expected": {"dev": expected[0], "ino": expected[1]}})
                        return ENTRY_QUARANTINED, ("it is the recording's only remaining name and could not be put "
                                                   f"back; it was kept, untouched, at {location}"), location
                os.unlink("entry", dir_fd=holder_fd)        # the authorized object, in the private quarantine
                moved = False
                return ENTRY_REMOVED, "", None
            try:                                            # not ours: put it back, never overwriting anything
                _rename_noreplace(holder_fd, "entry", dir_fd, name)
                moved = False
                return ENTRY_FOREIGN_RESTORED, ("another file had replaced it; that file was not removed and is "
                                                "back in its place"), None
            except OSError as exc:
                _write_manifest(holder_fd, {**context, "reason": f"replaced by another file; not restorable ({exc})",
                                            "expected": {"dev": expected[0], "ino": expected[1]},
                                            "found": {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size}})
                return ENTRY_QUARANTINED, ("another file had replaced it and its name has been taken again; that file "
                                           f"was kept, untouched, at {location}"), location
        except OSError as exc:
            if moved:
                _write_manifest(holder_fd, {**context, "reason": f"removal interrupted ({exc})",
                                            "expected": {"dev": expected[0], "ino": expected[1]}})
                return ENTRY_QUARANTINED, f"its removal was interrupted ({exc}); it was kept at {location}", location
            return ENTRY_NOT_MOVED, f"it was left in place ({exc})", None
        finally:
            os.close(holder_fd)
            if not moved:
                try:
                    os.rmdir(holder, dir_fd=quarantine_fd)
                except OSError:
                    pass
    finally:
        os.close(quarantine_fd)


class StagedMediaRelocation:
    """A staged destination and the descriptors that fix its identity, retained until the transaction's outcome.

    ``source_identity`` is the inode of the open source descriptor.  ``destination_identity`` is the same inode for
    a hard link, or the inode of the exclusively created copy's open descriptor; neither is ever taken from a look
    at a pathname.  ``source_state`` / ``destination_state`` are (size, mtime, ctime) of those descriptors once
    staged: any later write, truncation, timestamp or permission change alters the ctime."""

    def __init__(self, *, source, destination, root, root_fd, source_parent_fd, destination_parent_fd, source_fd,
                 destination_fd, source_identity, destination_identity, source_state, destination_state):
        self.source, self.destination, self.root = source, destination, root
        self.root_fd, self.source_parent_fd, self.destination_parent_fd = root_fd, source_parent_fd, destination_parent_fd
        self.source_fd, self.destination_fd = source_fd, destination_fd
        self.source_identity, self.destination_identity = source_identity, destination_identity
        self.source_state, self.destination_state = source_state, destination_state
        self.copied = destination_fd is not None
        self.closed = False

    @staticmethod
    def _state(info):
        return (info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def verify(self):
        """Raise OSError unless the source is still the authorized recording at its path and the destination
        directory and entry still are the staged ones (checked against the fixed identities)."""
        if self._state(os.fstat(self.source_fd)) != self.source_state:
            raise OSError("the source media changed while it was being relocated")
        try:
            at_source = os.stat(self.source.name, dir_fd=self.source_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise OSError("the source path no longer names the source media") from None
        if not stat.S_ISREG(at_source.st_mode) or _identity(at_source) != self.source_identity:
            raise OSError("the source path no longer names the source media")
        if self.destination_fd is not None and self._state(os.fstat(self.destination_fd)) != self.destination_state:
            raise OSError("the staged copy changed during the Track update")
        anchored_parent = _inode_witness(os.fstat(self.destination_parent_fd))
        named_parent = _inode_witness(os.lstat(self.destination.parent))
        if named_parent != anchored_parent or named_parent[2] != stat.S_IFDIR:
            raise OSError("the destination category directory changed during the Track update")
        for observed in (os.stat(self.destination.name, dir_fd=self.destination_parent_fd, follow_symlinks=False),
                         _lstat_or_none(self.destination)):
            if observed is None or not stat.S_ISREG(observed.st_mode) or _identity(observed) != self.destination_identity:
                raise OSError("the staged destination is no longer the relocated recording")

    verify_destination_binding = verify

    def _context(self, role):
        return {"role": role, "source": str(self.source), "destination": str(self.destination),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    def remove_destination(self):
        """Roll back the staging: remove the destination entry only if it is still the staged object."""
        return remove_entry(self.root, self.root_fd, self.destination_parent_fd, self.destination.name,
                            self.destination_identity, context=self._context("staged destination (rolled back)"),
                            keep_last_link=not self.copied)

    def remove_source(self):
        """After commit: remove the old source entry only if it is still the source object, and only while the
        committed destination still names the relocated recording."""
        observed = _lstat_or_none(self.destination)
        if observed is None or _identity(observed) != self.destination_identity:
            return ENTRY_NOT_MOVED, ("it was left in place: the committed destination no longer names the relocated "
                                     "recording"), None
        return remove_entry(self.root, self.root_fd, self.source_parent_fd, self.source.name, self.source_identity,
                            context=self._context("old source (after commit)"), keep_last_link=not self.copied)

    def close(self):
        if not self.closed:
            for fd in (self.source_fd, self.destination_fd, self.source_parent_fd, self.destination_parent_fd,
                       self.root_fd):
                if fd is not None:
                    os.close(fd)
            self.closed = True


def _lstat_or_none(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def stage_no_replace(source: Path, destination: Path, *, library_root: Path, allow_copy: bool = True):
    """Stage a confined destination without following source or directory symlinks.

    The category directory is created/opened relative to an already-resolved root descriptor.  A hard link is
    the normal case: the created entry must be the open source's inode.  ``allow_copy`` (Track Detail only): on
    ``EXDEV`` the bytes are copied into a file created with ``O_EXCL`` and verified by reading it back through
    its own descriptor (SHA-256 of both); otherwise ``EXDEV`` raises ``CrossFilesystemRefused`` before anything is
    created.  The returned handle owns every descriptor until the caller commits or rolls back.  On failure the
    created entry is removed only through ``remove_entry`` (identity-safe); if that cannot be done the error is a
    ``StagingUnwindIncomplete`` saying what was kept.
    """
    import hashlib

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

    root_fd = os.open(root, _DIRECTORY_FLAGS)
    source_parent_fd = destination_parent_fd = source_fd = destination_fd = None
    created = None                 # (device, inode) of the destination entry this call created
    handed_over = False
    try:
        try:
            os.mkdir(category_code, mode=0o755, dir_fd=root_fd)
        except FileExistsError:
            pass
        destination_parent_fd = os.open(category_code, _DIRECTORY_FLAGS, dir_fd=root_fd)
        # Refuse a symlink/path substitution between the descriptor-relative open and its pathname.
        if _inode_witness(os.lstat(destination.parent)) != _inode_witness(os.fstat(destination_parent_fd)):
            raise OSError("the destination category directory is not a stable directory beneath the library root")

        source_parent_fd = os.open(source_parent, _DIRECTORY_FLAGS)
        source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_parent_fd)
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise OSError("the source media is not a regular file")
        if _entry_witness(source_parent_fd, source.name) != _stat_witness(source_info):
            raise OSError("the source media changed while relocation was staged")
        source_identity = _identity(source_info)

        try:
            os.link(source.name, destination.name, src_dir_fd=source_parent_fd, dst_dir_fd=destination_parent_fd,
                    follow_symlinks=False)
            linked = True
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            linked = False
        if linked:
            created = source_identity                # the entry this call made names the source's inode
            observed = os.stat(destination.name, dir_fd=destination_parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(observed.st_mode) or _identity(observed) != source_identity:
                raise OSError("the staged destination was replaced right after its creation")
            destination_identity = source_identity
        else:
            if not allow_copy:
                raise CrossFilesystemRefused(
                    errno.EXDEV, "the destination folder is on another filesystem; a cross-filesystem move is "
                                 "not supported here")
            destination_fd = os.open(destination.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     source_info.st_mode & 0o777, dir_fd=destination_parent_fd)
            created = destination_identity = _identity(os.fstat(destination_fd))
            before = os.fstat(source_fd)
            written, digest = 0, hashlib.sha256()
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    count = os.write(destination_fd, view)
                    view = view[count:]
                    written += count
            os.fsync(destination_fd)
            after = os.fstat(source_fd)
            if StagedMediaRelocation._state(before) != StagedMediaRelocation._state(after) or written != after.st_size:
                raise OSError("the source media changed while it was being copied")
            check, offset = hashlib.sha256(), 0
            while True:                              # the copy, read back through its own descriptor
                chunk = os.pread(destination_fd, 1024 * 1024, offset)
                if not chunk:
                    break
                check.update(chunk)
                offset += len(chunk)
            if offset != written or check.digest() != digest.digest():
                raise OSError("the copied media does not match the source")
            os.utime(destination_fd, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
            observed = os.stat(destination.name, dir_fd=destination_parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(observed.st_mode) or _identity(observed) != destination_identity:
                raise OSError("the staged copy was replaced while it was written")

        source_now = os.fstat(source_fd)
        if (source_now.st_size, source_now.st_mtime_ns) != (source_info.st_size, source_info.st_mtime_ns):
            raise OSError("the source media changed while relocation was staged")
        staged = StagedMediaRelocation(
            source=source, destination=destination, root=str(root), root_fd=root_fd,
            source_parent_fd=source_parent_fd, destination_parent_fd=destination_parent_fd, source_fd=source_fd,
            destination_fd=destination_fd, source_identity=source_identity, destination_identity=destination_identity,
            source_state=StagedMediaRelocation._state(source_now),
            destination_state=StagedMediaRelocation._state(os.fstat(destination_fd)) if destination_fd is not None
            else StagedMediaRelocation._state(source_now))
        staged.verify()
        handed_over = True
        return staged
    except BaseException as exc:
        if created is not None and destination_parent_fd is not None:
            outcome, explanation, _location = remove_entry(
                str(root), root_fd, destination_parent_fd, destination.name, created,
                context={"role": "staged destination (staging failed)", "source": str(source),
                         "destination": str(destination), "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            if outcome not in ENTRY_GONE and outcome != ENTRY_FOREIGN_RESTORED:
                raise StagingUnwindIncomplete(
                    errno.EIO, f"{exc}; the staged destination {destination} was not removed: {explanation}") from exc
        raise
    finally:
        if not handed_over:
            for fd in (source_fd, destination_fd, source_parent_fd, destination_parent_fd, root_fd):
                if fd is not None:
                    os.close(fd)


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

def _remove_old_entry(staged, source, using) -> tuple[str, str, str | None]:
    """After commit: remove the old directory entry under its path lock, only while no Track names that path, and
    only through the identity-safe removal (``StagedMediaRelocation.remove_source``)."""
    from library.models import Track
    from library.services import source_paths
    try:
        with transaction.atomic(using=using):
            try:
                source_paths.acquire(source, using, timeout_ms=CLEANUP_LOCK_TIMEOUT_MS)
            except OperationalError:
                return CLEANUP_RETAINED_BUSY, ("the old file entry was kept: another operation held its path; "
                                               "remove it by hand after checking no Track uses it"), None
            if Track._base_manager.using(using).filter(filepath=source).exists():
                return CLEANUP_RETAINED_OWNER, "the old file entry was kept: another Track now names that path", None
            try:
                outcome, explanation, location = staged.remove_source()
            except OSError as exc:
                return CLEANUP_RETAINED_CHANGED, f"the old file entry was kept: {exc}", None
    except Exception as exc:                  # never fail a committed move because its cleanup failed
        log.warning("relocation cleanup of %s failed: %s", source, type(exc).__name__)
        return CLEANUP_FAILED, f"the old file entry could not be removed ({type(exc).__name__}: {exc})", None
    if outcome in ENTRY_GONE:
        return CLEANUP_REMOVED, "", None
    if outcome == ENTRY_QUARANTINED:
        return CLEANUP_QUARANTINED, f"the old file entry: {explanation}", location
    if outcome == ENTRY_FOREIGN_RESTORED:
        return CLEANUP_RETAINED_CHANGED, f"the old file entry: {explanation}", None
    return CLEANUP_RETAINED_UNSAFE, f"the old file entry: {explanation}", None


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
                # The staged inode must be the witnessed recording (the source descriptor's identity and content
                # metadata; the link itself changes only its ctime and link count).
                if witness is not None and (staged.source_identity != (witness[0], witness[1])
                                            or staged.source_state[:2] != (witness[2], witness[3])):
                    raise Stop(STALE, "The Track's file was replaced while it was being moved.")
                staged.verify()
            extra = write(track, destination or source)
            if extra:
                result.detail.update(extra)
            stored = Track.objects.filter(pk=track_id).values_list("filepath", flat=True).first()
            if stored != (destination or source):
                raise RuntimeError("the Track row does not name the expected file path after the update")
            if staged is not None:
                # Immediately before commit: the source is still the authorized recording at its path, and the
                # destination directory and entry are still the staged object (fixed identities).
                staged.verify()
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
                    outcome, explanation, location = staged.remove_destination()
                except BaseException as restore_exc:
                    outcome, explanation, location = ENTRY_NOT_MOVED, f"it was left in place ({restore_exc})", None
                finally:
                    staged.close()
                if outcome not in ENTRY_GONE and outcome != ENTRY_FOREIGN_RESTORED:
                    result.cleanup = CLEANUP_DESTINATION_RETAINED
                    result.cleanup_location = location
                    result.cleanup_message = (f"the staged entry {staged.destination} was not removed: {explanation}; "
                                              f"the original remains at {staged.source}")
                staged = None
            elif isinstance(exc, StagingUnwindIncomplete):
                result.cleanup = CLEANUP_DESTINATION_RETAINED
                result.cleanup_message = str(exc)
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
            result.cleanup, result.cleanup_message, result.cleanup_location = _remove_old_entry(staged, source, using)
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
                "cleanup_location": library_audit.library_relative(result.cleanup_location)
                if result.cleanup_location else None,
                "reason": result.message[:300], **(extra or {}), **result.detail})
