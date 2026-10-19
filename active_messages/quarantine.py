"""Identity-preserving removal for D3E-C cleanup (final correction).

Why "verify the pathname, then unlink the pathname" is unsafe: ``unlink``
acts on whatever object a NAME denotes at the instant of the call. However
carefully the cleanup verified an opened descriptor (contents, size, inode)
and however late it re-checked that the name still denoted that inode, the
final ``unlink(name)`` is a separate system call: a file renamed onto the name
in between is the one removed, while the verified file survives elsewhere --
and its length is reported as reclaimed. Another check just moves the window;
Linux has no "unlink this descriptor".

So nothing here ever deletes through the shared pathname. Under the
publication's destination lock (held by the caller):

1. the caller re-establishes its authority (eligibility, Track ownership);
2. the file is opened through the verified directory descriptor (never a
   symlink) and proven to be a single-link regular file of the expected size
   and SHA-256 (hashed through that descriptor);
3. a PRIVATE operation directory is created exclusively (``mkdir`` never
   reuses an existing name) inside the application's quarantine
   ``<dir>/.am-quarantine`` (mode 0700, owned by this account, on the same
   filesystem, never a symlink): ``<name>.<128-bit random token>``;
4. an intent manifest recording the verified inode (device, inode, size,
   SHA-256) is written, fsync'd, and the directory fsync'd;
5. the name is moved into the operation directory as ``payload`` by
   ``renameat2(RENAME_NOREPLACE)`` -- atomic on one filesystem and refusing,
   never replacing, an existing destination -- and both directories fsync'd;
6. the MOVED object is verified: it must be the manifest's very inode (and
   still single-link, the expected size and SHA-256). A replacement that took
   the name between steps 2 and 5 is what moved: it is never deleted and
   never counted; it is put back with ``RENAME_NOREPLACE`` (so a newer
   occupant of the name is never overwritten) or, if the name is occupied
   again, kept in quarantine for manual recovery;
7. the caller's authority is re-checked; only then is ``payload`` removed
   from the private directory, and the removal is PROVEN: the verified
   descriptor's link count must drop to zero (had any other object been
   removed, it would not). Only that proven length is reported as reclaimed;
8. the manifest and the operation directory are removed.

``payload`` and ``intent.json`` carry no audio extension, so a library scan
never imports quarantined bytes as a Track. The persistent destination-lock
sidecars are never touched.

Trust boundary: the destination lock coordinates every SUPPORTED writer of
the reserved names (publish, the Track write guard, retention, recovery).
It cannot stop a hostile process running as the station account -- such a
process could unlink anything directly. The private directory (unpredictable
name, 0700, used only under the lock) is never written by a supported writer,
and the zero-link proof makes any foreign interference there visible: the
operation then reports ``quarantine_substituted`` and claims no bytes.

Crash safety: every interrupted state is a quarantine operation the next run
settles (``recover``) under the same lock, re-proving identity and authority
from scratch -- never deleting because an entry is old, never deleting an
object whose identity cannot be proven.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass

ROOT = ".am-quarantine"
PAYLOAD = "payload"
MANIFEST = "intent.json"
MANIFEST_MAX = 4096
MAX_OPERATIONS = 10000
RENAME_NOREPLACE = 1
OPERATION = re.compile(r"(?P<name>am-[0-9a-f]{32}\.flac|[0-9]{1,19}\.json)\.(?P<token>[0-9a-f]{32})")
# Authority answers that keep a verified payload pending where it is (retry
# later): the authority is unknown right now, or something references the
# quarantined object itself. Every other refusal puts the object back.
KEEP_AUTHORITY = frozenset({"ownership_unavailable", "authority_unavailable", "quarantine_owned_by_track"})
# Read-only classifications a sweep settles by itself (delete after re-proof,
# put back, or clear); every other one needs an operator.
SETTLEABLE = frozenset({"quarantine_pending", "quarantine_identity_mismatch", "quarantine_operation_leftover"})

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Expected:
    """What may be removed: the exact content when known (``sha256``,
    ``size``); both None for derived data whose authority is its name and
    ownership alone (a waveform) -- then the object observed in step 2 is the
    identity, re-proven by inode after the move."""
    sha256: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class Settled:
    operation: str
    state: str                  # deleted | restored | cleared | kept
    bytes_removed: int = 0
    problem: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Held:
    """A read-only observation of one quarantine operation."""
    operation: str
    name: str
    code: str                   # quarantine_pending | quarantine_identity_mismatch | quarantine_restore_blocked |
    size: int = 0               # quarantine_recovery_required | quarantine_operation_leftover |
                                # quarantine_entry_unexpected


# -- seams and primitives ------------------------------------------------------------------------------

def _checkpoint(stage: str) -> None:
    """A crash boundary (tests stop or kill a process here). Deliberately empty."""


_RENAMEAT2 = []


def _renameat2():
    if not _RENAMEAT2:
        try:
            function = ctypes.CDLL(None, use_errno=True).renameat2
            function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
        except (OSError, AttributeError):
            function = None
        _RENAMEAT2.append(function)
    return _RENAMEAT2[0]


def _rename_noreplace(src_dirfd: int, src: str, dst_dirfd: int, dst: str) -> None:
    """Atomically move ``src`` to ``dst``; FileExistsError -- never a
    replacement -- when ``dst`` exists. OSError(ENOSYS / EINVAL) where the
    kernel or filesystem cannot guarantee that (the caller then refuses)."""
    function = _renameat2()
    if function is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    if function(src_dirfd, os.fsencode(src), dst_dirfd, os.fsencode(dst), RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _fsync(fd: int) -> None:
    os.fsync(fd)


def digest_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _open_dir(name: str, dir_fd: int, *, device: int) -> int:
    """A private application directory: a real directory (never a symlink),
    the very object its name denotes, on ``device``, owned by this account
    and closed to everyone else. Refused otherwise."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError:
        raise Refused("quarantine_entry_unexpected") from None
    try:
        held = os.fstat(fd)
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino) or not stat.S_ISDIR(held.st_mode):
            raise Refused("quarantine_entry_unexpected")
        if held.st_dev != device:
            raise Refused("quarantine_not_same_filesystem")
        if held.st_uid != os.geteuid() or held.st_mode & 0o077:
            raise Refused("quarantine_not_private")
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_root(dir_fd: int, *, create: bool) -> int | None:
    """The quarantine of the directory ``dir_fd`` (created when asked), or
    None when absent and not created."""
    device = os.fstat(dir_fd).st_dev
    if create:
        try:
            os.mkdir(ROOT, 0o700, dir_fd=dir_fd)
            _fsync(dir_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise Refused(f"quarantine_{type(exc).__name__}") from None
    try:
        return _open_dir(ROOT, dir_fd, device=device)
    except FileNotFoundError:
        if create:
            raise Refused("quarantine_unavailable") from None
        return None


def operations(root_fd: int, name: str | None = None) -> tuple[list, list]:
    """(operations, unexpected entry names) of a quarantine, bounded."""
    found, unexpected = [], []
    for count, entry in enumerate(sorted(os.listdir(root_fd))):
        if count >= MAX_OPERATIONS:
            unexpected.append("(truncated)")
            break
        match = OPERATION.fullmatch(entry)
        if match is None:
            unexpected.append(entry)
        elif name is None or match["name"] == name:
            found.append(entry)
    return found, unexpected


def operation_name(operation: str) -> str:
    return OPERATION.fullmatch(operation)["name"]


def _write_manifest(op_fd: int, record: dict) -> None:
    data = json.dumps(record, sort_keys=True).encode()
    fd = os.open(MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=op_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        _fsync(fd)
    finally:
        os.close(fd)
    _fsync(op_fd)


def _read_manifest(op_fd: int, name: str) -> dict | None:
    """The intent record, or None when absent, unreadable or not this name's."""
    try:
        fd = os.open(MANIFEST, _FILE_FLAGS, dir_fd=op_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MANIFEST_MAX:
            return None
        data = os.read(fd, MANIFEST_MAX + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        record = json.loads(data)
    except ValueError:
        return None
    if (not isinstance(record, dict) or record.get("version") != 1 or record.get("name") != name
            or not all(isinstance(record.get(key), int) for key in ("dev", "ino", "size"))
            or not isinstance(record.get("sha256"), (str, type(None)))):
        return None
    return record


def _close(root_fd: int, operation: str, op_fd: int) -> str:
    """Remove a settled operation's manifest and directory. Never raises
    (it runs after bytes may already be proven removed): "" or a problem --
    an operation that could not be cleared is a leftover the next sweep
    clears; a directory holding anything else is left for an operator."""
    try:
        try:
            os.unlink(MANIFEST, dir_fd=op_fd)
        except FileNotFoundError:
            pass
        _fsync(op_fd)
    except OSError:
        return "quarantine_operation_leftover"
    try:
        os.rmdir(operation, dir_fd=root_fd)
    except OSError as exc:
        return "quarantine_recovery_required" if exc.errno == errno.ENOTEMPTY else "quarantine_operation_leftover"
    try:
        _fsync(root_fd)
    except OSError:
        return "quarantine_operation_leftover"
    return ""


# -- removal -----------------------------------------------------------------------------------------------

def _identity_problem(fd: int, info, expected: Expected) -> str:
    if not stat.S_ISREG(info.st_mode):
        return "file_not_regular"
    if info.st_nlink != 1:
        return "file_hard_linked"        # another name keeps the bytes: removing one reclaims nothing
    if expected.size is not None and info.st_size != expected.size:
        return "file_identity_mismatch"
    if expected.sha256 is not None and digest_fd(fd) != expected.sha256:
        return "file_identity_mismatch"
    return ""


def remove(dir_fd: int, name: str, *, expected: Expected, authorize) -> tuple[int, str]:
    """(bytes proven removed, problem) for the entry ``name`` of the verified
    directory ``dir_fd``. The caller holds the destination lock; ``authorize()``
    returns "" or a refusal code and is asked before the move and again before
    the deletion. problem "" with 0 bytes: nothing was there."""
    problem = authorize()
    if problem:
        return 0, problem
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return 0, ""
    except OSError as exc:
        return 0, "file_not_regular" if exc.errno == errno.ELOOP else f"file_{type(exc).__name__}"
    try:
        info = os.fstat(fd)
        problem = _identity_problem(fd, info, expected)
        if problem:
            return 0, problem
        _checkpoint("before_quarantine")
        try:
            root_fd = open_root(dir_fd, create=True)
        except Refused as exc:
            return 0, exc.code
        try:
            return _quarantine(dir_fd, root_fd, name, info, expected, authorize)
        finally:
            os.close(root_fd)
    except OSError as exc:
        return 0, f"file_{type(exc).__name__}"
    finally:
        os.close(fd)


def _quarantine(dir_fd, root_fd, name, info, expected, authorize) -> tuple[int, str]:
    operation = f"{name}.{secrets.token_hex(16)}"
    try:
        os.mkdir(operation, 0o700, dir_fd=root_fd)       # exclusive: never an existing directory
    except FileExistsError:
        return 0, "quarantine_conflict"
    except OSError as exc:
        return 0, f"quarantine_{type(exc).__name__}"
    _fsync(root_fd)
    try:
        op_fd = _open_dir(operation, root_fd, device=info.st_dev)
    except (Refused, FileNotFoundError):
        return 0, "quarantine_conflict"
    record = {"version": 1, "name": name, "dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
              "sha256": expected.sha256}
    try:
        _write_manifest(op_fd, record)
        _checkpoint("quarantine_created")
        _checkpoint("before_move")
        try:
            _rename_noreplace(dir_fd, name, op_fd, PAYLOAD)
        except FileNotFoundError:                         # removed by someone else meanwhile: nothing moved
            _close(root_fd, operation, op_fd)
            return 0, ""
        except FileExistsError:                           # a foreign object in a private directory: stop
            return 0, "quarantine_conflict"
        except OSError as exc:
            _close(root_fd, operation, op_fd)
            unsupported = exc.errno in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EXDEV)
            return 0, "quarantine_unsupported" if unsupported else f"quarantine_{type(exc).__name__}"
        _fsync(op_fd)
        _fsync(dir_fd)
        _checkpoint("after_move")
    finally:
        os.close(op_fd)
    settled = _settle(dir_fd, root_fd, operation, name, record, expected, authorize)
    return settled.bytes_removed, settled.problem


def _moved_problem(fd: int, moved, record: dict, expected: Expected | None) -> str:
    """"" only if the moved object is the verified one: the manifest's inode,
    single-link, its size -- and, when known, the expected content."""
    if not stat.S_ISREG(moved.st_mode) or (moved.st_dev, moved.st_ino) != (record["dev"], record["ino"]):
        return "quarantine_identity_mismatch"
    if moved.st_nlink != 1:
        return "file_hard_linked"
    if moved.st_size != record["size"]:
        return "quarantine_identity_mismatch"
    if expected is not None:
        if expected.size is not None and moved.st_size != expected.size:
            return "quarantine_identity_mismatch"
        if expected.sha256 is not None and (record["sha256"] != expected.sha256 or digest_fd(fd) != expected.sha256):
            return "quarantine_identity_mismatch"
    return ""


def _settle(dir_fd, root_fd, operation, name, record, expected, authorize) -> Settled:
    """Finish one operation: delete a proven payload, put back anything else
    (never over an occupant), or clear an operation that holds nothing."""
    try:
        op_fd = _open_dir(operation, root_fd, device=os.fstat(root_fd).st_dev)
    except FileNotFoundError:
        return Settled(operation, "cleared", detail="concluded")
    except Refused as exc:
        return Settled(operation, "kept", problem=exc.code)
    try:
        try:
            payload_fd = os.open(PAYLOAD, _FILE_FLAGS, dir_fd=op_fd)
        except FileNotFoundError:
            return _conclude(dir_fd, root_fd, operation, op_fd, name, record)
        except OSError:
            return Settled(operation, "kept", problem="quarantine_recovery_required")
        try:
            moved = os.fstat(payload_fd)
            if record is None:                            # nothing proves what this is: keep it
                return Settled(operation, "kept", problem="quarantine_recovery_required")
            reason = _moved_problem(payload_fd, moved, record, expected)
            if reason:
                return _restore(dir_fd, root_fd, operation, op_fd, name, reason)
            _checkpoint("verified")
            problem = authorize()
            if problem in KEEP_AUTHORITY:
                return Settled(operation, "kept", problem="quarantine_pending", detail=problem)
            if problem:
                return _restore(dir_fd, root_fd, operation, op_fd, name, problem)
            os.unlink(PAYLOAD, dir_fd=op_fd)
            if os.fstat(payload_fd).st_nlink != 0:        # not the verified object that went: claim nothing
                return Settled(operation, "kept", problem="quarantine_substituted")
            # Proven removed: from here nothing may lose the count.
            try:
                _fsync(op_fd)
            except OSError:
                pass
            _checkpoint("deleted")
            return Settled(operation, "deleted", moved.st_size, _close(root_fd, operation, op_fd))
        finally:
            os.close(payload_fd)
    finally:
        os.close(op_fd)


def _restore(dir_fd, root_fd, operation, op_fd, name, reason) -> Settled:
    """Put a payload that may not be deleted back under its name -- only if
    the name is free (RENAME_NOREPLACE); otherwise keep it for an operator."""
    _checkpoint("restoring")
    try:
        _rename_noreplace(op_fd, PAYLOAD, dir_fd, name)
    except FileExistsError:
        return Settled(operation, "kept", problem="quarantine_restore_blocked", detail=reason)
    except OSError:
        return Settled(operation, "kept", problem="quarantine_recovery_required", detail=reason)
    _fsync(dir_fd)
    _fsync(op_fd)
    _checkpoint("restored")
    leftover = _close(root_fd, operation, op_fd)
    return Settled(operation, "restored", problem=leftover or reason, detail=reason)


def _conclude(dir_fd, root_fd, operation, op_fd, name, record) -> Settled:
    """No payload: the move never happened (the name still holds the
    manifest's inode) or the operation already finished (deleted or
    restored). Nothing is held; the bytes, if any were removed, were counted
    by the run that removed them -- never again here."""
    detail = "concluded"
    if record is not None:
        try:
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (record["dev"], record["ino"]):
                detail = "abandoned_before_move"
        except OSError:
            pass
    leftover = _close(root_fd, operation, op_fd)
    if leftover:
        return Settled(operation, "kept", problem=leftover, detail=detail)
    return Settled(operation, "cleared", detail=detail)


def recover(dir_fd: int, name: str, *, expected: Expected | None, authorize) -> list:
    """Settle every interrupted operation of ``name`` (caller holds its
    destination lock), re-proving identity from the manifest's inode and
    ``expected`` and asking ``authorize`` again. ``expected`` None (no content
    authority now): a payload that is not the recorded inode is put back; one
    that is stays pending unless the authority answers. A payload with no
    valid manifest is never touched."""
    try:
        root_fd = open_root(dir_fd, create=False)
    except Refused as exc:
        return [Settled(ROOT, "kept", problem=exc.code)]
    if root_fd is None:
        return []
    try:
        found, _unexpected = operations(root_fd, name)
        results = []
        device = os.fstat(root_fd).st_dev
        for operation in found:
            try:
                op_fd = _open_dir(operation, root_fd, device=device)
            except FileNotFoundError:
                continue
            except Refused as exc:
                results.append(Settled(operation, "kept", problem=exc.code))
                continue
            try:
                record = _read_manifest(op_fd, name)
            finally:
                os.close(op_fd)
            _checkpoint("recovering")
            results.append(_settle(dir_fd, root_fd, operation, name, record, expected, authorize))
        return results
    finally:
        os.close(root_fd)


def reclaim(dir_fd: int, name: str, *, expected: Expected, authorize) -> tuple[int, list]:
    """Recover ``name``'s interrupted operations, then remove the entry
    itself -- unless an operation is still unresolved (then nothing new is
    started). (bytes proven removed, problems)."""
    removed, problems = 0, []
    settled = recover(dir_fd, name, expected=expected, authorize=authorize)
    for item in settled:
        removed += item.bytes_removed
        if item.problem:
            problems.append(item.problem)
    if any(item.state == "kept" for item in settled):
        return removed, problems
    gone, problem = remove(dir_fd, name, expected=expected, authorize=authorize)
    if problem:
        problems.append(problem)
    return removed + gone, problems


# -- read-only inspection ----------------------------------------------------------------------------------

def inspect(dir_fd: int, name: str | None = None) -> list:
    """Classify the quarantine of ``dir_fd`` WITHOUT changing anything (no
    hashing: metadata only). Returns Held observations."""
    try:
        root_fd = open_root(dir_fd, create=False)
    except Refused as exc:
        return [Held(ROOT, "", exc.code)]
    if root_fd is None:
        return []
    try:
        found, unexpected = operations(root_fd, name)
        held = [Held(entry, "", "quarantine_entry_unexpected") for entry in ([] if name else unexpected)]
        device = os.fstat(root_fd).st_dev
        for operation in found:
            held.append(_inspect_one(dir_fd, root_fd, operation, device))
        return held
    finally:
        os.close(root_fd)


def _inspect_one(dir_fd, root_fd, operation, device) -> Held:
    name = operation_name(operation)
    try:
        op_fd = _open_dir(operation, root_fd, device=device)
    except FileNotFoundError:
        return Held(operation, name, "quarantine_operation_leftover")
    except Refused:
        return Held(operation, name, "quarantine_entry_unexpected")
    try:
        record = _read_manifest(op_fd, name)
        try:
            moved = os.stat(PAYLOAD, dir_fd=op_fd, follow_symlinks=False)
        except FileNotFoundError:
            return Held(operation, name, "quarantine_operation_leftover")
        size = moved.st_size if stat.S_ISREG(moved.st_mode) else 0
        if record is None or not stat.S_ISREG(moved.st_mode):
            return Held(operation, name, "quarantine_recovery_required", size)
        if ((moved.st_dev, moved.st_ino) == (record["dev"], record["ino"]) and moved.st_nlink == 1
                and moved.st_size == record["size"]):
            return Held(operation, name, "quarantine_pending", size)
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            return Held(operation, name, "quarantine_restore_blocked", size)
        except FileNotFoundError:
            return Held(operation, name, "quarantine_identity_mismatch", size)
    except OSError:
        return Held(operation, name, "quarantine_recovery_required")
    finally:
        os.close(op_fd)
