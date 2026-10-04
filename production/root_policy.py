#!/usr/bin/env python3
"""THE canonical safety policy for PRODUCTION_MEDIA_ROOT.

One implementation, used everywhere the root is acted on destructively:

* the Django runtime (production.services.layout) -- directory creation,
  intake, reconciliation sweeps;
* the nightly backup (deploy/backup_isadoraair.sh + stage_production_media.sh);
* disaster-recovery restore (deploy/restore/40-station-content.sh).

It is deliberately stdlib-only (no Django) so the shell tooling can run it with
the system python3 before any virtualenv exists, and so all three agree on
exactly one rule set. CLI: ``python3 root_policy.py check|check-env ...``
(see ``main``); exit 0 prints the accepted root, exit 3 prints the refusal.

The configured root must be a DEDICATED production-media directory. It is
refused when, in either its lexically-normalized or its symlink-resolved form,
it:

* is not absolute, is empty, contains control characters or a ``..``
  component, or is ``/``;
* equals, contains, or lies inside a system tree (``/etc``, ``/usr``, ``/bin``,
  ``/sbin``, ``/lib*``, ``/boot``, ``/proc``, ``/sys``, ``/dev``, ``/run``,
  ``/snap``, ``/root``, ...);
* equals or contains a broad anchor (``/srv``, ``/srv/isadoraair``, ``/var``,
  ``/var/lib``, ``/var/lib/isadoraair``, ``/home``, ``/tmp``, ``/mnt``,
  ``/media``, ``/opt``, any account's home directory, ...). Living *inside*
  such an anchor is fine -- that is where a dedicated directory belongs (e.g.
  ``/srv/isadoraair/production-media`` or ``/mnt/data/production-media``);
* equals, contains, or lies inside protected station content or code: the
  music library, waveforms, reports, weather and encoder state, carts,
  voicetracks, aircheck, rip staging, the runtime-recovery payload, the
  application/repository root, the backup working directory, and any extra
  path the caller passes.

With ``dedicated_path`` it also refuses an EXISTING directory holding anything
other than the four managed subtrees (``media``, ``incoming``, ``work``,
``locks``; plus ``lost+found`` for a dedicated mount), or holding one of those
as a symlink / non-directory -- so an operator cannot point the root at a
populated directory and have the tooling adopt and chown it.
"""
from __future__ import annotations

import argparse
import os
import pwd
import stat
import sys
from pathlib import PurePosixPath

DEFAULT_ROOT = "/srv/isadoraair/production-media"
MANAGED_ENTRIES = ("media", "incoming", "work", "locks")
TOLERATED_ENTRIES = ("lost+found",)

# Never inside, never equal, never an ancestor.
SYSTEM_TREES = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32", "/proc",
    "/run", "/sbin", "/snap", "/sys", "/usr", "/root", "/lost+found", "/var/log",
    "/var/run", "/var/lock", "/var/cache", "/var/spool", "/var/mail",
)
# Never equal, never an ancestor (living inside is the normal case).
ANCHORS = (
    "/", "/home", "/mnt", "/media", "/opt", "/srv", "/srv/isadoraair", "/tmp", "/var",
    "/var/lib", "/var/lib/isadoraair", "/var/tmp", "/usr/local",
)
# .env key -> default, exactly as isadoraair/settings.py defines them.
STATION_PATH_SETTINGS = {
    "LIBRARY_ROOT": "/srv/isadoraair/music",
    "WAVEFORMS_DIR": "/srv/isadoraair/waveforms",
    "REPORTS_ROOT": "/var/lib/isadoraair/reports",
    "WEATHER_DATA_DIR": "/var/lib/isadoraair/weather",
    "ENCODER_STATE_ROOT": "/var/lib/isadoraair/encoders",
}
FIXED_STATION_PATHS = (
    "/srv/isadoraair/music", "/srv/isadoraair/waveforms", "/srv/isadoraair/carts",
    "/srv/isadoraair/voicetracks", "/srv/isadoraair/aircheck", "/srv/isadoraair/rip_staging",
    "/srv/isadoraair/mitd_artbell", "/var/lib/isadoraair/runtime-recovery",
    "/var/lib/isadoraair/reports", "/var/lib/isadoraair/weather", "/var/lib/isadoraair/encoders",
    "/opt/isadoraair",
)


class RootPolicyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# -- path helpers ----------------------------------------------------------

def _lexical(path: str) -> str:
    return os.path.normpath(path) if path else path


def _resolved(path: str) -> str:
    # realpath resolves every existing component (symlinks included) and
    # normalizes the rest; it never fails for a not-yet-existing path.
    return os.path.realpath(path)


def _forms(path: str) -> set[str]:
    return {_lexical(path), _resolved(path)}


def _is_within(child: str, parent: str) -> bool:
    """child == parent or child is inside parent (component-wise)."""
    child_parts, parent_parts = PurePosixPath(child).parts, PurePosixPath(parent).parts
    return child_parts[: len(parent_parts)] == parent_parts


def _account_homes() -> set[str]:
    homes = set()
    try:
        for entry in pwd.getpwall():
            if entry.pw_dir and entry.pw_dir.startswith("/") and entry.pw_dir not in ("/", "/nonexistent"):
                homes.add(_lexical(entry.pw_dir))
    except Exception:       # noqa: BLE001 -- an unreadable passwd db only removes extra anchors
        pass
    for value in (os.environ.get("HOME"), os.path.expanduser("~")):
        if value and value.startswith("/") and value != "/":
            homes.add(_lexical(value))
    return homes


# -- the policy ------------------------------------------------------------

def check_root(candidate, *, protected=(), dedicated_path=None) -> str:
    """Return the lexically-normalized root, or raise RootPolicyError.

    ``protected``: extra paths that must never be equal to, contain, or contain
    the root (callers pass the configured station content and code roots; the
    fixed station defaults are always included). ``dedicated_path``: the real
    on-disk location to apply the dedicated-directory content rule to (the
    root itself at runtime; the staged location during a staged restore)."""
    raw = "" if candidate is None else str(candidate)
    if not raw.strip():
        raise RootPolicyError("root_empty", "the production media root is empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise RootPolicyError("root_control_characters", "the root contains control characters")
    if not raw.startswith("/"):
        raise RootPolicyError("root_not_absolute", f"the root must be absolute: {raw!r}")
    if ".." in PurePosixPath(raw).parts:
        raise RootPolicyError("root_traversal", f"the root must not contain '..': {raw!r}")
    root = _lexical(raw)
    candidates = _forms(raw)
    if "/" in candidates:
        raise RootPolicyError("root_is_filesystem_root", "the root resolves to /")

    protected_forms = set()
    for path in (*FIXED_STATION_PATHS, *protected):
        if path and str(path).startswith("/"):
            protected_forms |= _forms(str(path))
    anchor_forms = set()
    for path in (*ANCHORS, *_account_homes()):
        anchor_forms |= _forms(path)
    system_forms = set()
    for path in SYSTEM_TREES:
        system_forms |= _forms(path)

    for form in candidates:
        for system in system_forms:
            if _is_within(form, system) or _is_within(system, form):
                raise RootPolicyError(
                    "root_system_location", f"{raw!r} equals, contains or is inside system tree {system}",
                )
        for path in protected_forms:
            if _is_within(form, path) or _is_within(path, form):
                raise RootPolicyError(
                    "root_overlaps_protected", f"{raw!r} equals, contains or is inside protected path {path}",
                )
        for anchor in anchor_forms:
            if _is_within(anchor, form):
                raise RootPolicyError(
                    "root_is_broad_anchor", f"{raw!r} equals or contains {anchor}; use a dedicated directory",
                )

    if dedicated_path is not None:
        _check_dedicated(str(dedicated_path))
    return root


def _check_dedicated(path: str) -> None:
    try:
        info = os.stat(path)               # follows a symlinked root: judge the real directory
    except FileNotFoundError:
        return                             # will be created empty
    except OSError as exc:
        raise RootPolicyError("root_unreadable", f"cannot inspect {path}: {exc.strerror}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RootPolicyError("root_not_a_directory", f"{path} exists but is not a directory")
    try:
        entries = sorted(os.listdir(path))
    except OSError as exc:
        raise RootPolicyError("root_unreadable", f"cannot list {path}: {exc.strerror}") from exc
    foreign = [name for name in entries if name not in MANAGED_ENTRIES + TOLERATED_ENTRIES]
    if foreign:
        shown = ", ".join(foreign[:5]) + (" ..." if len(foreign) > 5 else "")
        raise RootPolicyError(
            "root_not_dedicated", f"{path} holds entries that are not production media: {shown}",
        )
    for name in MANAGED_ENTRIES:
        entry = os.path.join(path, name)
        try:
            entry_info = os.lstat(entry)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISDIR(entry_info.st_mode):
            raise RootPolicyError("root_managed_entry_invalid", f"{entry} is not a real directory")


# -- .env handling (python-decouple semantics: last assignment wins) --------

def read_env_file(path) -> dict:
    """Parse KEY=VALUE lines the way python-decouple's RepositoryEnv does:
    comments/blank/no-'=' lines skipped, key and value stripped, ONE pair of
    matching surrounding quotes removed, and a later assignment overriding an
    earlier one."""
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    return values


def station_paths_from_env(env: dict) -> list[str]:
    return [env.get(key) or default for key, default in STATION_PATH_SETTINGS.items()]


# -- CLI ----------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate a PRODUCTION_MEDIA_ROOT.", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "check-env"):
        cmd = sub.add_parser(name, allow_abbrev=False)
        if name == "check":
            cmd.add_argument("--root", required=True)
        else:
            cmd.add_argument("--env-file", required=True,
                             help="read PRODUCTION_MEDIA_ROOT (and station paths) from this .env if it exists")
        cmd.add_argument("--protected", action="append", default=[], help="extra protected path (repeatable)")
        cmd.add_argument("--app-root", action="append", default=[], help="application/repository root (repeatable)")
        cmd.add_argument("--dedicated-path", default=None,
                         help="apply the dedicated-directory rule to this real location")
        cmd.add_argument("--dedicated", action="store_true", help="apply the dedicated rule to the root itself")
    args = parser.parse_args(argv)

    env = {}
    if args.command == "check-env":
        if os.path.isfile(args.env_file):
            try:
                env = read_env_file(args.env_file)
            except (OSError, UnicodeDecodeError) as exc:
                print(f"root_env_unreadable: cannot read {args.env_file}: {exc}", file=sys.stderr)
                return 3
        root = env.get("PRODUCTION_MEDIA_ROOT") or DEFAULT_ROOT
    else:
        root = args.root
    protected = [*station_paths_from_env(env), *args.protected, *args.app_root]
    dedicated = args.dedicated_path if args.dedicated_path is not None else (root if args.dedicated else None)
    try:
        accepted = check_root(root, protected=protected, dedicated_path=dedicated)
    except RootPolicyError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 3
    print(accepted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
