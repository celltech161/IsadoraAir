"""Filesystem ownership assertions for paths this supervisor trusts.

An INDEPENDENT implementation of the same semantics as
deploy/updater_runtime/isadoraair_updater/security.py (the worker's own
copy) -- deliberately NOT imported from there (Correction 1: the
immutable supervisor must never import replaceable-worker-tree code).
Kept in parity by test_phase_d2_parity.py, not by sharing source."""
from __future__ import annotations

import os
from pathlib import Path
import stat


class ProtectionError(RuntimeError):
    pass


def assert_root_protected(path: Path, *, recursive: bool = False) -> None:
    """Production-only assertion; the installed supervisor entrypoint
    already requires root (see updater_bootstrapd.py). Tests run this
    unprivileged, so the check is inactive there -- no production
    bypass exists, since the real entrypoint refuses a non-root
    effective UID before this is ever reached."""
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
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ProtectionError(f"protected path is not a regular file or directory: {candidate}")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ProtectionError(f"protected path is not root-owned/non-writable: {candidate}")


def assert_root_protected_parents(path: Path) -> None:
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


def assert_no_symlink_in_tree(root: Path) -> None:
    """Beyond assert_root_protected's own top-level symlink check --
    walks an ENTIRE directory tree (e.g. a staged/published runtime
    slot) and refuses if any entry, at any depth, is a symlink or any
    non-regular/non-directory special file (FIFO, device, socket).
    Always active (not gated on euid==0) -- slot content integrity is
    checked at verification time regardless of privilege, since a test
    fixture must be able to prove this rule works without running as
    root."""
    root = Path(root)
    if not root.is_dir():
        raise ProtectionError(f"not a directory: {root}")
    for candidate in root.rglob("*"):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ProtectionError(f"symlink not permitted in a runtime slot: {candidate}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ProtectionError(f"special file not permitted in a runtime slot: {candidate}")
