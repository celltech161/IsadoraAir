#!/usr/bin/env python3
"""Recovery-media discovery/validation -- IsadoraAir 1.2, r0045.

Establishes, from the ACTUAL E8 export layout this repo's own tooling
produces (`deploy/restore/build_offline_closure.py` for the apt/snap
closure, `deploy/backup_isadoraair.sh` for the backup archive, and the
git-mirror/wheelhouse steps documented in the E8 export procedure --
see docs/DISASTER_RECOVERY_STATUS.md), the one coherent "recovery
media" directory contract `restore.sh`'s interactive workflow drives
Stage 10/20/60/80 from, instead of six separate operator-supplied
paths:

    <recovery-media-root>/
      backups/isadoraair-backup-*.tar.gz     (the backup archive(s))
      offline/apt-repo/                       Packages(.gz) + .deb files
                                               (build_offline_closure.py
                                               `apt` subcommand's own
                                               --out-dir/apt-repo)
      offline/snaps/                          snap-manifest.json + .snap/
                                               .assert files
                                               (`snap` subcommand's own
                                               --out-dir/snaps)
      offline/manifests/                      apt-closure-manifest.json,
                                               direct-apt-packages.txt,
                                               snap-manifest.json
      offline/wheelhouse/                     Python wheels/sdists
                                               (pip --find-links target;
                                               not built by any script in
                                               this repo -- a documented
                                               manual/offline pip-download
                                               procedure, see the E8
                                               export README)
      repos/IsadoraAir.git                    bare mirror -- Stage 20's
                                               --repo-url
      repos/<companion>.git                   bare mirrors -- Stage 80's
                                               --repo-url-prefix

This module never mutates anything -- read-only discovery/validation
only. It never guesses a package selection either: `detect_apt_groups`
asks the CURRENT `deploy/packages-ubuntu-26.04.txt` (the same file
`10-packages.sh` itself sources) which optional group each closure
package belongs to, so the --with-* flags restore.sh derives always
match what THIS repo's own manifest says, not a hard-coded assumption
about one specific export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


REQUIRED_RELATIVE_DIRS = (
    "backups",
    "offline/apt-repo",
    "offline/snaps",
    "offline/manifests",
    "offline/wheelhouse",
    "repos",
)
OPTIONAL_GROUP_NAMES = (
    "OPTIONAL_CD_RIP",
    "OPTIONAL_KOKORO_TTS",
    "OPTIONAL_SYNDICATED_SELENIUM",
    "OPTIONAL_BACKUP_ENCRYPTION",
)
# Media-tree directory names this discovery walk is willing to consider
# as a candidate root -- deliberately narrow (never an arbitrary
# directory just because it happens to contain a file named
# isadoraair-backup-*.tar.gz somewhere underneath it).
_CANDIDATE_DIRNAMES = ("e8-inputs",)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_bare_git_repo(path: Path) -> bool:
    return path.is_dir() and (path / "HEAD").is_file() and (path / "objects").is_dir()


def validate_root(root: Path, *, archive: Path | None = None) -> dict:
    """Read-only structural validation against the layout documented
    above. `valid` reflects STRUCTURAL completeness only -- never
    partial-credited, True only when every required path is present
    and structurally sound. Whether `archive` specifically is present
    in this root's own backups/ is reported separately as
    `archive_match` (True/False/None), deliberately NOT folded into
    `valid`: `discover()` below needs to report a structurally-sound
    root that simply doesn't happen to contain THIS archive as a real,
    rankable (lower-priority) candidate, never silently drop it -- an
    ambiguous discovery must be shown honestly, not hidden. Callers
    that DO want "this root must contain this exact archive or it
    doesn't count" (an operator's own explicit --recovery-media-root)
    enforce that themselves by also checking `archive_match is not
    False` -- see cmd_validate below, the one CLI entry point
    restore.sh's own explicit-root validation actually uses."""

    problems: list[str] = []
    notes: list[str] = []
    root = Path(root)
    if not root.is_dir():
        return {"root": str(root), "valid": False, "problems": [f"{root} is not a directory"], "notes": []}

    for relative in REQUIRED_RELATIVE_DIRS:
        candidate = root / relative
        if not candidate.is_dir():
            problems.append(f"missing required directory: {relative}")

    apt_repo = root / "offline" / "apt-repo"
    if apt_repo.is_dir() and not any(apt_repo.glob("*.deb")):
        problems.append("offline/apt-repo has no .deb files")
    if apt_repo.is_dir() and not (apt_repo / "Packages").is_file():
        problems.append("offline/apt-repo is missing its Packages index")

    snaps_dir = root / "offline" / "snaps"
    if snaps_dir.is_dir() and not (snaps_dir / "snap-manifest.json").is_file():
        problems.append("offline/snaps is missing snap-manifest.json")

    wheelhouse = root / "offline" / "wheelhouse"
    if wheelhouse.is_dir() and not any(wheelhouse.iterdir()):
        problems.append("offline/wheelhouse is empty")

    isadoraair_git = root / "repos" / "IsadoraAir.git"
    if not _looks_like_bare_git_repo(isadoraair_git):
        problems.append("repos/IsadoraAir.git is missing or not a bare git repository")

    companion_repos = sorted(
        p.name for p in (root / "repos").glob("*.git")
        if p.name != "IsadoraAir.git" and _looks_like_bare_git_repo(p)
    ) if (root / "repos").is_dir() else []

    archive_match = None
    matched_archive_path = None
    backups_dir = root / "backups"
    if archive is not None and backups_dir.is_dir():
        archive = Path(archive)
        if archive.is_file():
            try:
                target_sha256 = _sha256_file(archive)
            except OSError as exc:
                problems.append(f"could not read --archive to compute its SHA256: {exc}")
                target_sha256 = None
            if target_sha256 is not None:
                archive_match = False
                for candidate in backups_dir.glob("*.tar.gz"):
                    try:
                        if _sha256_file(candidate) == target_sha256:
                            archive_match = True
                            matched_archive_path = str(candidate)
                            break
                    except OSError:
                        continue
                if not archive_match:
                    notes.append(
                        "the supplied --archive's SHA256 does not match any archive under backups/ "
                        "in this recovery-media root"
                    )
        else:
            problems.append(f"--archive does not exist: {archive}")

    return {
        "root": str(root),
        "valid": not problems,
        "problems": problems,
        "notes": notes,
        "apt_repo_dir": str(apt_repo),
        "snap_dir": str(snaps_dir),
        "wheelhouse_dir": str(wheelhouse),
        "manifests_dir": str(root / "offline" / "manifests"),
        "isadoraair_git": str(isadoraair_git),
        "companions_repo_prefix": str(root / "repos"),
        "companion_repo_names": companion_repos,
        "archive_match": archive_match,
        "matched_archive_path": matched_archive_path,
    }


def _candidate_roots_under(search_root: Path) -> list[Path]:
    if not search_root.is_dir():
        return []
    found = []
    for dirname in _CANDIDATE_DIRNAMES:
        # Bounded depth (this search root, and up to 2 levels under it) --
        # never an unbounded filesystem-wide walk.
        found.extend(search_root.glob(f"*/{dirname}"))
        found.extend(search_root.glob(f"{dirname}"))
        found.extend(search_root.glob(f"*/*/{dirname}"))
    # de-duplicate, preserve discovery order
    seen: set[str] = set()
    unique = []
    for path in found:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def discover(archive: Path, search_roots: list[Path]) -> list[dict]:
    """Returns every STRUCTURALLY VALID candidate root found beneath
    search_roots, each carrying its own validate_root() evidence
    (including whether it actually contains `archive` specifically).
    Archive-matching candidates sort first -- those are what "exactly
    one valid recovery-media tree can be discovered" (the interactive
    contract) actually means; a structurally valid but non-matching
    root is still reported (never silently dropped), just ranked
    lower, so an ambiguous case is shown honestly rather than guessed
    away."""

    archive = Path(archive)
    candidates: list[Path] = []

    # The archive's own immediate ancestry, if it already sits inside a
    # media tree (e.g. .../e8-inputs/backups/isadoraair-backup-*.tar.gz)
    # -- checked first since this is the common, "found near the
    # archive" case the interactive workflow's own step 1 describes.
    if archive.is_file() and archive.parent.name == "backups":
        candidates.append(archive.parent.parent)

    for root in search_roots:
        candidates.extend(_candidate_roots_under(Path(root)))

    seen: set[str] = set()
    results = []
    for candidate in candidates:
        resolved = str(Path(candidate).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        evidence = validate_root(candidate, archive=archive if archive.is_file() else None)
        if evidence["valid"]:
            results.append(evidence)

    results.sort(key=lambda e: (not e["archive_match"], e["root"]))
    return results


def detect_apt_groups(root: Path, packages_file: Path) -> dict[str, bool]:
    """Which optional apt package groups (as declared by
    deploy/packages-ubuntu-26.04.txt -- the SAME file 10-packages.sh
    itself sources, never a second/duplicated group definition) this
    recovery-media root's own frozen apt closure actually includes.
    Detected from offline/manifests/direct-apt-packages.txt (the exact
    package names build_offline_closure.py's apt subcommand recorded
    as directly requested) -- never hard-coded per export. A group
    counts as included if ANY one of its member packages is present
    (every 10-packages.sh --with-* flag pulls its whole group in via
    one apt-get call together, so one present member is already good
    evidence the whole group was closed over)."""

    direct_packages_file = Path(root) / "offline" / "manifests" / "direct-apt-packages.txt"
    if not direct_packages_file.is_file():
        return {name: False for name in OPTIONAL_GROUP_NAMES}
    direct_packages = {
        line.strip() for line in direct_packages_file.read_text(encoding="utf-8").splitlines() if line.strip()
    }

    # Ask bash itself what each group array contains -- this file is a
    # bash source file, never re-parsed with an ad hoc Python regex
    # that could silently drift from what 10-packages.sh's own `source
    # "$PACKAGES_FILE"` actually sees.
    script = "; ".join(f'source "$1"; printf "%s\\n" "${{{name}[*]}}"' for name in OPTIONAL_GROUP_NAMES)
    # Each group needs its own `source` in a fresh subshell-safe context
    # since bash won't let us cleanly separate array dumps otherwise --
    # simplest robust approach: one invocation per group.
    result: dict[str, bool] = {}
    for name in OPTIONAL_GROUP_NAMES:
        proc = subprocess.run(
            ["bash", "-c", f'source "$1"; printf "%s\\n" "${{{name}[@]}}"', "_", str(packages_file)],
            capture_output=True, text=True, timeout=10,
        )
        members = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        result[name] = bool(members & direct_packages)
    return result


def cmd_validate(args: argparse.Namespace) -> int:
    """The strict entry point restore.sh's own explicit
    --recovery-media-root path uses: structurally valid AND (no
    --archive given, or --archive is actually present in this root's
    backups/). An operator who explicitly names a root must get a hard
    failure if it doesn't even contain the archive they're restoring
    from -- discover() below is the one place a non-matching-but-valid
    root is still a legitimate (lower-priority) candidate to show."""
    evidence = validate_root(args.root, archive=args.archive)
    ok = evidence["valid"] and evidence["archive_match"] is not False
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0 if ok else 1


def cmd_discover(args: argparse.Namespace) -> int:
    results = discover(args.archive, args.search_root or [])
    print(json.dumps(results, sort_keys=True, separators=(",", ":")))
    return 0 if results else 1


def cmd_detect_apt_groups(args: argparse.Namespace) -> int:
    result = detect_apt_groups(args.root, args.packages_file)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(allow_abbrev=False)
    commands = root.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", allow_abbrev=False)
    validate.add_argument("--root", required=True, type=Path)
    validate.add_argument("--archive", type=Path)
    validate.set_defaults(handler=cmd_validate)

    discover_cmd = commands.add_parser("discover", allow_abbrev=False)
    discover_cmd.add_argument("--archive", required=True, type=Path)
    discover_cmd.add_argument("--search-root", action="append", type=Path)
    discover_cmd.set_defaults(handler=cmd_discover)

    detect = commands.add_parser("detect-apt-groups", allow_abbrev=False)
    detect.add_argument("--root", required=True, type=Path)
    detect.add_argument("--packages-file", required=True, type=Path)
    detect.set_defaults(handler=cmd_detect_apt_groups)

    return root


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except OSError as exc:
        print(f"recovery-media error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
