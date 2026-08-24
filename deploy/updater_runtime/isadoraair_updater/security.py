"""Filesystem ownership assertions for paths the root runtime trusts."""
from __future__ import annotations

import os
from pathlib import Path
import stat


class ProtectionError(RuntimeError):
    pass


def assert_root_protected(path: Path, *, recursive: bool = False):
    """Production-only assertion; the installed entry point already requires root.

    Unit tests run the pure runtime unprivileged under temporary directories, so
    the root-specific ownership check is inactive there.  No production bypass
    exists because ``updaterd.py`` refuses a non-root effective UID.
    """
    if os.geteuid() != 0:
        return
    path = Path(path)
    candidates = [path]
    if recursive and path.is_dir():
        candidates.extend(path.rglob("*"))
    for candidate in candidates:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ProtectionError(f"protected path contains a symlink: {candidate}")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ProtectionError(f"protected path is not root-owned/non-writable: {candidate}")


def assert_root_protected_parents(path: Path):
    if os.geteuid() != 0:
        return
    absolute = Path(path).absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:-1]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ProtectionError(f"protected path parent is not a real directory: {current}")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ProtectionError(f"protected path parent is writable or not root-owned: {current}")
