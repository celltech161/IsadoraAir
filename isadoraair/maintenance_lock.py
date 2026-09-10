"""One cross-process lock for database-credential-sensitive maintenance.

``.env.lock`` predates this module and remains the single lock inode. Normal
environment edits and credential rotation take it exclusively. Operations
which only consume the credentials (formal backup and Update Center job
admission) take it shared. Lock order is therefore simply: acquire this lock
before reading either credential file, and acquire no second maintenance lock.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time


class MaintenanceLockError(RuntimeError):
    pass


def database_maintenance_lock_path(environment_file: Path) -> Path:
    path = Path(environment_file)
    return path.with_name(path.name + ".lock")


def database_rotation_pending_path(environment_file: Path) -> Path:
    """Nonsecret durable gate visible to application-owned consumers."""
    path = Path(environment_file)
    return path.with_name(path.name + ".rotation-pending")


def database_rotation_is_pending(environment_file: Path) -> bool:
    """Fail closed on any filesystem object occupying the gate path."""
    try:
        os.lstat(database_rotation_pending_path(environment_file))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MaintenanceLockError("cannot inspect the database rotation gate") from exc
    return True


@contextmanager
def database_maintenance_lock(
    environment_file: Path,
    *,
    shared: bool,
    timeout: float = 30.0,
):
    """Acquire the station credential-maintenance lock.

    The lock file must have the same owner/group as the environment file and
    mode 0600. Root may create it, but immediately assigns the application
    file's ownership before attempting the lock. No secret data is stored in
    the lock file.
    """
    environment_file = Path(environment_file)
    lock_path = database_maintenance_lock_path(environment_file)
    if timeout < 0:
        raise ValueError("timeout must not be negative")
    try:
        env_info = environment_file.stat(follow_symlinks=False)
    except OSError as exc:
        raise MaintenanceLockError("cannot inspect the application environment file") from exc
    if not stat.S_ISREG(env_info.st_mode):
        raise MaintenanceLockError("application environment file is not a regular file")

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise MaintenanceLockError("cannot safely open the database maintenance lock") from exc
    try:
        lock_info = os.fstat(fd)
        if not stat.S_ISREG(lock_info.st_mode):
            raise MaintenanceLockError("database maintenance lock is not a regular file")
        if os.geteuid() == 0 and (lock_info.st_uid, lock_info.st_gid) != (env_info.st_uid, env_info.st_gid):
            os.fchown(fd, env_info.st_uid, env_info.st_gid)
            lock_info = os.fstat(fd)
        if (lock_info.st_uid, lock_info.st_gid) != (env_info.st_uid, env_info.st_gid):
            raise MaintenanceLockError("database maintenance lock ownership is invalid")
        os.fchmod(fd, 0o600)
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MaintenanceLockError("timed out waiting for the database maintenance lock")
                time.sleep(0.05)
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
