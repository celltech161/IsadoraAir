#!/usr/bin/env python3
"""deploy/restore/content_root_safety.py -- restore-layer path-safety
primitive for operator-configurable station-content roots (P0 1.2).

Why this exists: 40-station-content.sh historically read REPORTS_ROOT
straight out of the restored .env and then ran `sudo mkdir -p`,
`sudo chown`, an extraction and finally `sudo chown -R` against that
value with no validation at all. A wrong, stale or hostile value
(`/`, `/etc`, `/srv/isadoraair`, the application checkout, an ancestor
of the music library, a symlink to any of those) would have handed
ownership of a whole system or station tree to the service account.
WEATHER_DATA_DIR had the same defect in a non-recursive form: a restored
WEATHER_DATA_DIR=/etc produced `sudo chown $OWNER /etc` and
`sudo chmod 0755 /etc`. Both roots now go through this one primitive.

Contract (stdlib only, so it runs on a bare-metal host before Stage 60
has built any virtualenv):

  value          Print one key's effective value from the restored .env
                 with python-decouple's own semantics (last assignment
                 wins, one pair of surrounding quotes stripped; an
                 assigned-but-empty value stays empty, exactly as Django
                 sees it), or the managed default when unassigned.
  check          Resolve one root that way (or judge an explicit
                 --value the restore itself is about to write into .env),
                 canonicalize it, and fail closed unless it is a
                 dedicated content directory. Prints the single
                 effective path the caller must operate on. Never
                 creates or changes anything.
  establish      Create (if missing) exactly one already-checked root and
                 set its owner and/or mode, walking every path component
                 with O_NOFOLLOW directory file descriptors so neither
                 the root nor any ancestor can be a symlink redirecting
                 the change; ownership/mode are applied to the open
                 directory descriptor itself. Never recursive.
  members        Validate an archive's members under one top-level
                 prefix (relative names only, no `..`, no control
                 characters, regular files/directories only) and write
                 the exact NUL-separated relative member list.
  chown-members  (run as root) Change ownership of EXACTLY those
                 members beneath an already-validated root, walking
                 every path component with O_NOFOLLOW directory file
                 descriptors so no symlink -- pre-existing or raced in
                 -- can redirect the change outside the root. Never
                 recursive, never touches anything not in the list.

Refusal relations, judged on both the lexical (normalized) form and,
for a live restore, the realpath form of the candidate:

  * forbidden system trees: equal, inside, or containing -> refused;
  * anchors (/, /srv, /var/lib, /srv/isadoraair, $HOME, ...): equal
    or containing -> refused; a dedicated directory INSIDE one is fine
    (e.g. /var/lib/isadoraair/reports, /srv/isadoraair/reports, or a
    separately mounted /mnt/stationdata/reports);
  * protected roots (application checkouts, the tooling checkout, the
    protected updater runtime, every other managed station root):
    equal, inside, or containing -> refused.

Under --staging-root the LIVE value is judged exactly as a real restore
would judge it (so a staged rehearsal fails closed on the same .env),
and the staged path must additionally resolve inside the staging root.
"""

from __future__ import annotations

import argparse
import grp
import os
import posixpath
import pwd
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

# Equal / inside / containing are all refused.
FORBIDDEN_TREES = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32",
    "/proc", "/root", "/run", "/sbin", "/sys", "/usr", "/snap",
    "/var/backups", "/var/cache", "/var/log", "/var/mail", "/var/spool",
    "/var/lib/apt", "/var/lib/dpkg", "/var/lib/postgresql", "/var/lib/snapd",
    "/var/lib/systemd",
)

# Equal / containing are refused; a dedicated subdirectory is allowed.
ANCHORS = (
    "/", "/home", "/media", "/mnt", "/opt", "/srv", "/tmp", "/var",
    "/var/lib", "/var/opt", "/var/tmp",
    "/srv/isadoraair", "/var/lib/isadoraair",
)

# Equal / inside / containing are all refused.
# The defaults of the managed keys below (music, waveforms, reports,
# weather, encoders) are deliberately NOT listed here: they are protected
# through MANAGED_ROOT_DEFAULTS for every key except their own, so judging
# a key never refuses that key's own default location.
FIXED_PROTECTED_ROOTS = (
    "/opt/isadoraair",
    "/opt/isadoraair-runtime",
    "/var/lib/isadoraair-updater",
    "/var/lib/isadoraair-updater-bootstrap",
    "/srv/isadoraair/aircheck",
    "/srv/isadoraair/carts",
    "/srv/isadoraair/lost+found",
    "/srv/isadoraair/mitd_artbell",
    "/srv/isadoraair/rip_staging",
    "/srv/isadoraair/voicetracks",
    "/var/lib/isadoraair/restore",
    "/var/lib/isadoraair/runtime-recovery",
    "/var/lib/isadoraair/tts",
)

# Protected beneath the operator's $HOME (the backup/verify service user).
HOME_PROTECTED_RELATIVE = (
    ".local/state/isadoraair",  # backup-assurance receipts (isadoraair/backup_assurance.py)
)

# Every managed station root a restored .env can relocate (mirrors
# isadoraair/settings.py defaults). The key being checked is excluded
# from its own protected set.
MANAGED_ROOT_DEFAULTS = {
    "REPORTS_ROOT": "/var/lib/isadoraair/reports",
    "LIBRARY_ROOT": "/srv/isadoraair/music",
    "WAVEFORMS_DIR": "/srv/isadoraair/waveforms",
    "WEATHER_DATA_DIR": "/var/lib/isadoraair/weather",
    "ENCODER_STATE_ROOT": "/var/lib/isadoraair/encoders",
    # iPortal ProductionMedia store (production/root_policy.py DEFAULT_ROOT).
    # production/root_policy.py remains its runtime authority; listing it here
    # makes restore-time station-content roots (reports, weather, and the
    # media root itself) mutually protected, live and staged.
    "PRODUCTION_MEDIA_ROOT": "/srv/isadoraair/production-media",
}


class UnsafeRootError(Exception):
    pass


def read_decouple_env(path: Path) -> dict[str, str]:
    """Byte-for-byte the parse python-decouple's RepositoryEnv performs --
    the value Django will actually run with is the value judged here."""
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    with open(path, encoding="UTF-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and (
                (value[0] == "'" and value[-1] == "'") or (value[0] == '"' and value[-1] == '"')
            ):
                value = value[1:-1]
            data[key] = value
    return data


def effective_value(env: dict[str, str], key: str) -> str:
    """decouple's config(key, default=...): an assigned value -- even an
    empty one -- wins over the default."""
    return env[key] if key in env else MANAGED_ROOT_DEFAULTS[key]


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def normalize_absolute(value: str, *, label: str) -> str:
    if not value:
        raise UnsafeRootError(f"{label} is empty")
    if _has_control_chars(value):
        raise UnsafeRootError(f"{label} contains control characters")
    if not value.startswith("/"):
        raise UnsafeRootError(f"{label}={value!r} is not an absolute path")
    if ".." in value.split("/"):
        raise UnsafeRootError(f"{label}={value!r} contains a '..' component")
    normalized = posixpath.normpath(value)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def _relation(candidate: str, other: str) -> str | None:
    cand, oth = PurePosixPath(candidate), PurePosixPath(other)
    if cand == oth:
        return "is"
    if cand.is_relative_to(oth):
        return "is inside"
    if oth.is_relative_to(cand):
        return "contains"
    return None


def _forms(path: str, *, resolve: bool) -> set[str]:
    forms = {path}
    if resolve:
        forms.add(os.path.realpath(path))
    return forms


def judge(candidate_forms: set[str], *, protected: dict[str, str], label: str) -> None:
    """Raise UnsafeRootError on the first refused relation."""
    for form in sorted(candidate_forms):
        for tree in FORBIDDEN_TREES:
            rel = _relation(form, tree)
            if rel:
                raise UnsafeRootError(f"{label} resolves to {form}, which {rel} system tree {tree}")
        for anchor in ANCHORS:
            rel = _relation(form, anchor)
            if rel in ("is", "contains"):
                raise UnsafeRootError(
                    f"{label} resolves to {form}, which {rel} {anchor} -- a content root must be "
                    "a dedicated directory, never a shared anchor or one of its ancestors"
                )
        for root, why in sorted(protected.items()):
            rel = _relation(form, root)
            if rel:
                raise UnsafeRootError(f"{label} resolves to {form}, which {rel} {why} {root}")


def build_protected(
    *, key: str, env: dict[str, str], target_root: str, tooling_root: str, home: str | None, resolve: bool
) -> tuple[dict[str, str], set[str]]:
    protected: dict[str, str] = {}
    extra_anchors: set[str] = set()

    def add(path: str, why: str) -> None:
        try:
            normalized = normalize_absolute(path, label=why)
        except UnsafeRootError:
            return  # an unusable OTHER value cannot be protected by path; it is not the root being judged
        for form in _forms(normalized, resolve=resolve):
            protected.setdefault(form, why)

    for path in FIXED_PROTECTED_ROOTS:
        add(path, "protected station root")
    add(target_root, "the restore target application root")
    add(tooling_root, "the restore tooling checkout")
    for other_key, default in MANAGED_ROOT_DEFAULTS.items():
        if other_key == key:
            continue
        add(effective_value(env, other_key), f"managed station root {other_key}")
        if effective_value(env, other_key) != default:
            add(default, f"default station root {other_key}")
    if home:
        try:
            normalized_home = normalize_absolute(home, label="HOME")
        except UnsafeRootError:
            normalized_home = None
        if normalized_home:
            extra_anchors.update(_forms(normalized_home, resolve=True))
            for relative in HOME_PROTECTED_RELATIVE:
                add(posixpath.join(normalized_home, relative), "protected service-user state root")
    return protected, extra_anchors


HOST_SPACE_WHY = ("the restore target application root", "the restore tooling checkout")


def judge_staged(
    *, key: str, effective: str, staging: str, protected: dict[str, str], host_roots: dict[str, str]
) -> None:
    """Managed-root separation inside a staging tree.

    Judging the LIVE value proves the configuration is acceptable; it does
    not prove the staged tree honours it -- a symlink inside the staging
    root can alias one staged managed root onto another (e.g.
    <staging>/mnt/stationdata/reports -> <staging>/mnt/stationdata/library).
    So the RESOLVED staged candidate is compared with the RESOLVED staged
    equivalent of every protected/managed root (equal, inside or
    containing -> refused), with the host-side target/tooling checkouts,
    and its staging-relative live equivalent is put back through the
    system-tree/anchor policy.
    """
    for root, why in sorted(protected.items()):
        if why in HOST_SPACE_WHY:
            continue  # host paths, not live-space paths -- compared directly below
        staged_root = os.path.realpath(os.path.join(staging, root.lstrip("/")))
        if not PurePosixPath(staged_root).is_relative_to(staging):
            continue  # resolves outside the staging tree; cannot alias a staged candidate
        rel = _relation(effective, staged_root)
        if rel:
            raise UnsafeRootError(
                f"staged {key} resolves to {effective}, which {rel} the staged {why} {root} ({staged_root})"
            )
    for root, why in host_roots.items():
        try:
            normalized = normalize_absolute(root, label=why)
        except UnsafeRootError:
            continue
        for form in _forms(normalized, resolve=True):
            rel = _relation(effective, form)
            if rel:
                raise UnsafeRootError(f"staged {key} resolves to {effective}, which {rel} {why} {form}")
    live_equivalent = "/" + os.path.relpath(effective, staging)
    judge({live_equivalent}, protected={}, label=f"staged {key} (live equivalent)")


def check_root(
    *,
    key: str,
    default: str,
    env_file: Path,
    target_root: str,
    tooling_root: str,
    staging_root: str | None,
    home: str | None,
    value: str | None = None,
) -> str:
    env = read_decouple_env(env_file)
    raw = value if value is not None else (env[key] if key in env else default)
    live = normalize_absolute(raw, label=key)
    live_mode = not staging_root
    protected, home_anchors = build_protected(
        key=key, env=env, target_root=target_root, tooling_root=tooling_root, home=home, resolve=live_mode
    )
    forms = _forms(live, resolve=live_mode)
    judge(forms, protected=protected, label=key)
    for form in sorted(forms):
        for anchor in sorted(home_anchors):
            rel = _relation(form, anchor)
            if rel in ("is", "contains"):
                raise UnsafeRootError(f"{key} resolves to {form}, which {rel} the operator home {anchor}")

    if live_mode:
        effective = os.path.realpath(live)
    else:
        staging = os.path.realpath(normalize_absolute(staging_root, label="--staging-root"))
        effective = os.path.realpath(os.path.join(staging, live.lstrip("/")))
        if not PurePosixPath(effective).is_relative_to(staging) or effective == staging:
            raise UnsafeRootError(
                f"staged {key} {effective} does not resolve strictly inside the staging root {staging}"
            )
        judge_staged(
            key=key, effective=effective, staging=staging, protected=protected,
            host_roots={target_root: "the restore target application root",
                        tooling_root: "the restore tooling checkout"},
        )
    if os.path.lexists(effective) and not os.path.isdir(effective):
        raise UnsafeRootError(f"{key} {effective} exists and is not a directory")
    return effective


def validate_members(archive: Path, prefix: str) -> list[str]:
    """``prefix`` is the archive-relative directory whose contents are restored
    (e.g. ``reports`` or ``srv-content/production-media/media``)."""
    prefix_parts = prefix.split("/") if prefix else []
    if (not prefix_parts or any(part in ("", ".", "..") for part in prefix_parts)
            or _has_control_chars(prefix)):
        raise UnsafeRootError(f"invalid member prefix {prefix!r}")
    found: set[str] = set()
    with tarfile.open(archive, "r|*") as tar:
        for member in tar:
            name = member.name
            stripped = name[2:] if name.startswith("./") else name
            if stripped != prefix and not stripped.startswith(prefix + "/"):
                continue
            rel = stripped[len(prefix):].strip("/")
            if not rel:
                if not member.isdir():
                    raise UnsafeRootError(f"archive member {name!r} must be a directory")
                continue
            if _has_control_chars(rel):
                raise UnsafeRootError(f"archive member {name!r} contains control characters")
            parts = rel.split("/")
            if any(part in ("", ".", "..") for part in parts):
                raise UnsafeRootError(f"archive member {name!r} is not a plain relative path")
            if not (member.isfile() or member.isdir()):
                raise UnsafeRootError(
                    f"archive member {name!r} is not a regular file or directory -- links and special "
                    "files are never restored into a managed content root"
                )
            found.add(rel)
    return sorted(found)


def _open_dir_nofollow(path: str) -> int:
    """Open an absolute directory, refusing a symlink at EVERY component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in PurePosixPath(path).parts[1:]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def establish_root(root: str, owner: str | None, mode: int | None) -> None:
    if root != posixpath.normpath(root) or not root.startswith("/") or root == "/":
        raise UnsafeRootError(f"--root {root!r} must be an absolute, normalized, non-root path")
    uid, gid = _resolve_owner(owner) if owner else (-1, -1)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in PurePosixPath(root).parts[1:]:
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        if owner:
            os.fchown(fd, uid, gid)
        if mode is not None:
            os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _resolve_owner(owner: str) -> tuple[int, int]:
    user, sep, group = owner.partition(":")
    if not user:
        raise UnsafeRootError(f"--owner {owner!r} has no user")
    try:
        entry = pwd.getpwuid(int(user)) if user.isdigit() else pwd.getpwnam(user)
    except (KeyError, ValueError):
        raise UnsafeRootError(f"--owner user {user!r} does not exist") from None
    uid = entry.pw_uid
    if not sep:
        return uid, -1
    if not group:
        return uid, entry.pw_gid
    try:
        gid = grp.getgrgid(int(group)).gr_gid if group.isdigit() else grp.getgrnam(group).gr_gid
    except (KeyError, ValueError):
        raise UnsafeRootError(f"--owner group {group!r} does not exist") from None
    return uid, gid


def chown_members(root: str, owner: str, members: list[str]) -> int:
    if root != posixpath.normpath(root) or not root.startswith("/") or root == "/":
        raise UnsafeRootError(f"--root {root!r} must be an absolute, normalized, non-root path")
    uid, gid = _resolve_owner(owner)
    root_fd = _open_dir_nofollow(root)
    changed = 0
    try:
        for rel in members:
            parts = rel.split("/")
            if any(part in ("", ".", "..") for part in parts) or _has_control_chars(rel):
                raise UnsafeRootError(f"member {rel!r} is not a plain relative path")
            fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    os.close(fd)
                    fd = nxt
                info = os.lstat(parts[-1], dir_fd=fd)
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise UnsafeRootError(f"{root}/{rel} is not a regular file or directory after extraction")
                os.chown(parts[-1], uid, gid, dir_fd=fd, follow_symlinks=False)
                changed += 1
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check")
    p_check.add_argument("--key", required=True, choices=sorted(MANAGED_ROOT_DEFAULTS))
    p_check.add_argument("--env-file", required=True, type=Path)
    p_check.add_argument("--target-root", required=True)
    p_check.add_argument("--tooling-root", required=True)
    p_check.add_argument("--staging-root")
    p_check.add_argument("--value", help="Judge this value (which the restore is about to write) instead of .env's.")

    p_value = sub.add_parser("value")
    p_value.add_argument("--key", required=True, choices=sorted(MANAGED_ROOT_DEFAULTS))
    p_value.add_argument("--env-file", required=True, type=Path)

    p_establish = sub.add_parser("establish")
    p_establish.add_argument("--root", required=True)
    p_establish.add_argument("--owner")
    p_establish.add_argument("--mode", type=lambda v: int(v, 8))

    p_members = sub.add_parser("members")
    p_members.add_argument("--archive", required=True, type=Path)
    p_members.add_argument("--prefix", required=True)
    p_members.add_argument("--output", required=True, type=Path)

    p_chown = sub.add_parser("chown-members")
    p_chown.add_argument("--root", required=True)
    p_chown.add_argument("--owner", required=True)
    p_chown.add_argument("--members-file", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            print(
                check_root(
                    key=args.key,
                    default=MANAGED_ROOT_DEFAULTS[args.key],
                    env_file=args.env_file,
                    target_root=args.target_root,
                    tooling_root=args.tooling_root,
                    staging_root=args.staging_root or None,
                    home=os.environ.get("HOME"),
                    value=args.value,
                )
            )
        elif args.command == "value":
            print(effective_value(read_decouple_env(args.env_file), args.key))
        elif args.command == "establish":
            establish_root(args.root, args.owner, args.mode)
        elif args.command == "members":
            members = validate_members(args.archive, args.prefix)
            args.output.write_bytes(b"".join(m.encode("utf-8") + b"\0" for m in members))
            print(len(members))
        else:
            raw = args.members_file.read_bytes()
            members = [m.decode("utf-8") for m in raw.split(b"\0") if m]
            print(chown_members(args.root, args.owner, members))
    except (UnsafeRootError, OSError, tarfile.TarError) as exc:
        print(f"content_root_safety: refusing: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
