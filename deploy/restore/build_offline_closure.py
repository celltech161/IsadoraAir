#!/usr/bin/env python3
"""Build the offline apt + snap closure Stage 10 (`10-packages.sh`) needs
for a fully offline E8 restore -- IsadoraAir 1.2 r0038.

Run this on a CONNECTED host (a production box, or a throwaway machine with
the same Ubuntu 26.04 release and the same package selection) to freeze a
reproducible, self-contained set of install inputs, then copy the output
directory to off-host recovery media. `10-packages.sh --snap-dir PATH`
(and, for apt, `--apt-repo-dir PATH`) are the only consumers -- this
script is never invoked during a restore itself, and needs real Internet
access to do its job; the restore host never needs any.

Two apt-closure defects and one snap-closure defect found by the genuine
E8 clean/offline acceptance run (plus a code-review pass that found the
first fix was still incomplete) motivate this tool's specific design
choices (see `docs/DISASTER_RECOVERY_RESTORE.md`'s "Offline package/snap
closure" section for the full narrative):

  1. `age` -- a DIRECT package -- was silently missing from a prior ad hoc
     apt closure because it was already installed on the source host, and
     a naive `apt-get --download-only install` does not re-fetch a
     package that is already installed.
  2. `bubblewrap` -- a TRANSITIVE dependency (`glycin-loaders ->
     bubblewrap`) -- was missing entirely: no .deb, no Packages stanza,
     no manifest entry.

  The FIRST fix attempt for (1) used `apt-get install --reinstall
  --download-only` against the builder's REAL dpkg status. That is
  insufficient for (2): `--reinstall` only forces re-fetching of packages
  named directly on the command line -- it does nothing for a transitive
  dependency that the builder's real dpkg status already reports as
  satisfied, so a builder host where `bubblewrap` happens to already be
  installed would still omit it, silently, exactly like `age` originally
  was. The actual, general fix (r0038 code review): resolve and download
  against an ISOLATED, EMPTY synthetic dpkg status
  (`-o Dir::State::status=<empty file>`), so apt computes the closure as
  a genuinely clean machine would need it -- completely independent of
  whatever this builder host happens to already have installed, for
  EVERY package in the graph, direct or transitive, not just the ones
  named on the command line. `-o Dir::Cache::archives=<dedicated temp
  dir>` is used for the same run, so no stale `.deb` left over from a
  previous run or the builder's own real `/var/cache/apt/archives` can
  influence the result. Real package availability/versions still come
  from the REAL configured apt sources (`Dir::State::lists` is left
  alone) -- only "what's already installed" is faked away.

  This tool never hand-curates the transitive set either way: it always
  takes apt's own dependency resolver's word for the full closure
  (`apt-get install --download-only -s`, a simulate/dry-run against that
  same isolated status, enumerating every `Inst <name>` line apt would
  act on) and then downloads EXACTLY that resolved name list -- not just
  the packages named in `deploy/packages-ubuntu-26.04.txt`.

  3. The snap closure needs more than `{snapd, chromium}` to avoid
     reaching for the Snap Store mid-sequence (a manifest missing e.g.
     `mesa-2404` would previously still pass verification and only fail
     later, potentially triggering a store lookup). And a coarse
     type-bucket/alphabetical install order is not an actual dependency
     graph. r0038 code review: this tool now always builds the full,
     fixed, PROVEN-offline Ubuntu 26.04/Chromium snap set and order --
     see `offline_snap_install.REQUIRED_SNAP_INSTALL_ORDER`, the single
     source of truth both tools share (imported here, never duplicated).
     Each snap is still captured at its ACTUAL locally-installed revision
     (`snap list <name>`) when available, rather than whatever the
     store's current stable channel happens to be, so the closure matches
     what production is really running rather than drifting from it
     between builder runs; only when a snap isn't installed locally does
     this fall back to downloading the given `--channel`.

Usage:
  build_offline_closure.py apt-closure --out-dir DIR
      [--packages-file deploy/packages-ubuntu-26.04.txt]
      [--groups CORE,AUDIO_GSTREAMER,BUILD_HEAAC,OPTIONAL_CD_RIP,OPTIONAL_KOKORO_TTS,OPTIONAL_SYNDICATED_SELENIUM,OPTIONAL_BACKUP_ENCRYPTION]

  build_offline_closure.py snap-closure --out-dir DIR [--channel latest/stable]

  build_offline_closure.py all --out-dir DIR [same flags as both above]

Requires on THIS (builder) host only: apt-get, dpkg-deb, dpkg-scanpackages
(package `dpkg-dev`), snap, gzip -- all standard on an Ubuntu 26.04 box.
Only `apt-get update` (refreshing this host's real package indices) runs
under sudo; every apt-closure resolution/download call uses an isolated
status+cache it owns and needs no root at all. Writes no secret material
-- there is none to write; package/snap closures are public software, not
credentials.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# offline_snap_install.REQUIRED_SNAP_INSTALL_ORDER is the one shared
# source of truth for the snap recovery contract's names/order -- never
# duplicated here. Works whether this file is run directly (its own
# directory is already sys.path[0]) or loaded dynamically by path (e.g.
# from a test) -- the explicit insert covers the latter.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_snap_install import REQUIRED_SNAP_INSTALL_ORDER  # noqa: E402

Runner = Callable[..., subprocess.CompletedProcess]

DEFAULT_GROUPS = (
    "CORE",
    "AUDIO_GSTREAMER",
    "BUILD_HEAAC",
    "OPTIONAL_CD_RIP",
    "OPTIONAL_KOKORO_TTS",
    "OPTIONAL_SYNDICATED_SELENIUM",
    "OPTIONAL_BACKUP_ENCRYPTION",
)
_GROUP_HEADER_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=\($")


class ClosureBuildError(Exception):
    pass


def _default_runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(argv, **kwargs)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------
# apt closure
# ---------------------------------------------------------------------

def parse_packages_file(path: Path) -> dict[str, list[str]]:
    """Parse deploy/packages-ubuntu-26.04.txt's sourceable-bash-array
    format (`GROUP=(\\n  pkg\\n  pkg  # comment\\n)`), without sourcing
    it as bash -- this tool must run standalone. Deliberately narrow: it
    only understands exactly this file's own established shape."""
    groups: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if current is None:
            match = _GROUP_HEADER_RE.match(line)
            if match:
                current = match.group(1)
                groups[current] = []
            continue
        if line == ")":
            current = None
            continue
        if not line or line.startswith("#"):
            continue
        pkg = line.split("#", 1)[0].strip()
        if pkg:
            groups[current].append(pkg)
    return groups


def resolve_direct_packages(groups: dict[str, list[str]], selected: list[str]) -> list[str]:
    unknown = [g for g in selected if g not in groups]
    if unknown:
        raise ClosureBuildError(f"unknown package group(s): {', '.join(unknown)} (known: {', '.join(sorted(groups))})")
    seen: dict[str, None] = {}
    for group in selected:
        for pkg in groups[group]:
            seen[pkg] = None
    return list(seen)


def parse_simulate_inst_names(simulate_stdout: str) -> list[str]:
    """apt-get's `-s`/`--simulate` output prefixes every package it would
    act on with `Inst <name> ...` -- this is the authoritative resolved
    closure (direct + every transitive dependency), straight from apt's
    own dependency resolver. Never hand-curated. Meaningful only when run
    against an isolated/empty dpkg status -- see `_isolation_opts`."""
    names: list[str] = []
    for line in simulate_stdout.splitlines():
        line = line.strip()
        if line.startswith("Inst "):
            parts = line.split()
            if len(parts) >= 2:
                names.append(parts[1])
    return names


def _isolation_opts(status_path: Path, archives_dir: Path) -> list[str]:
    """apt-get -o overrides that make dependency resolution/download
    depend ONLY on the real configured package sources (Dir::State::lists
    is deliberately left alone), never on this builder host's own
    installed-package state or any stale previously-downloaded .deb:
      - Dir::State::status: an isolated, empty dpkg status file, so apt
        resolves as if NOTHING is installed yet -- the fix for the
        bubblewrap-class defect (see module docstring).
      - Dir::Cache::archives: a dedicated, empty-at-start directory this
        run owns, so downloaded .deb files can never be confused with
        (or shadowed by) a previous run's or the real system's cache.
    Both overrides also mean these apt-get calls need no root at all."""
    return [
        "-o", f"Dir::State::status={status_path}",
        "-o", f"Dir::Cache::archives={archives_dir}",
    ]


def read_deb_control_fields(deb_path: Path, runner: Runner) -> dict[str, str]:
    result = runner(["dpkg-deb", "-f", str(deb_path), "Package", "Version", "Architecture"])
    if result.returncode != 0:
        raise ClosureBuildError(f"dpkg-deb -f failed for {deb_path}: {result.stderr}")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    for required in ("Package", "Version", "Architecture"):
        if required not in fields:
            raise ClosureBuildError(f"dpkg-deb -f did not report {required} for {deb_path}")
    return fields


def find_cached_deb(archives_dir: Path, package_name: str) -> Path:
    candidates = sorted(
        p for p in archives_dir.glob(f"{package_name}_*.deb")
        if fnmatch.fnmatchcase(p.name.split("_", 1)[0], package_name)
    )
    if not candidates:
        raise ClosureBuildError(
            f"expected a cached .deb for '{package_name}' in {archives_dir} after "
            "`apt-get install --download-only` against the isolated closure archive, found "
            "none -- this indicates the real download step did not actually fetch a package "
            "the simulate step resolved; re-run with apt-get's own output visible to diagnose."
        )
    # Multiple cached versions can coexist only in pathological cases
    # (this run owns a fresh, dedicated archives dir) -- the most
    # recently written one is authoritative either way.
    return max(candidates, key=lambda p: p.stat().st_mtime)


@dataclass(frozen=True)
class AptClosureResult:
    entries: list[dict]
    apt_repo_dir: Path
    manifest_path: Path
    direct_list_path: Path


def build_apt_closure(
    direct_packages: list[str],
    out_dir: Path,
    runner: Runner = _default_runner,
    sudo: bool = True,
    work_dir: Path | None = None,
) -> AptClosureResult:
    """work_dir: an isolated scratch directory this function creates the
    synthetic dpkg status file and dedicated archive cache under. If not
    given, a temp directory is created and removed automatically; a
    caller-supplied work_dir is left in place for the caller to inspect/
    clean up (used by tests)."""
    if not direct_packages:
        raise ClosureBuildError("no direct packages resolved -- refusing to build an empty closure")

    sudo_prefix = ["sudo"] if sudo else []

    # Only step that touches real, shared system state -- refreshing
    # THIS host's real package indices (Dir::State::lists), which the
    # isolated-status calls below deliberately still read from.
    update = runner([*sudo_prefix, "apt-get", "update"])
    if update.returncode != 0:
        raise ClosureBuildError(f"apt-get update failed: {update.stderr}")

    owns_work_dir = work_dir is None
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="isadoraair-apt-closure-"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
    try:
        status_path = work_dir / "isolated-dpkg-status"
        status_path.touch()
        archives_dir = work_dir / "archives"
        archives_dir.mkdir(parents=True, exist_ok=True)
        iso = _isolation_opts(status_path, archives_dir)

        # Simulate against the isolated/empty status: apt resolves the
        # full recursive closure as a genuinely clean machine would need
        # it, independent of this builder's own real installed state.
        simulate = runner(
            ["apt-get", "install", "--download-only", "-s", "-y", *iso, *direct_packages]
        )
        if simulate.returncode != 0:
            raise ClosureBuildError(f"apt-get install --simulate failed to resolve the closure: {simulate.stderr}")
        closure_names = sorted(set(parse_simulate_inst_names(simulate.stdout)) | set(direct_packages))
        if not closure_names:
            raise ClosureBuildError("apt-get --simulate resolved an empty closure -- refusing to proceed")

        # Real download: same isolation, and EVERY name explicitly from
        # the exact resolved closure above (not just direct_packages) --
        # what gets downloaded is provably what was just resolved, never
        # re-derived a second time by a second dependency resolution pass.
        download = runner(
            ["apt-get", "install", "--download-only", "-y", *iso, *closure_names]
        )
        if download.returncode != 0:
            raise ClosureBuildError(f"apt-get install --download-only failed: {download.stderr}")

        apt_repo_dir = out_dir / "apt-repo"
        apt_repo_dir.mkdir(parents=True, exist_ok=True)

        entries = []
        direct_set = set(direct_packages)
        for name in closure_names:
            cached = find_cached_deb(archives_dir, name)
            fields = read_deb_control_fields(cached, runner)
            dest = apt_repo_dir / cached.name
            shutil.copy2(cached, dest)
            entries.append(
                {
                    "name": fields["Package"],
                    "version": fields["Version"],
                    "architecture": fields["Architecture"],
                    "filename": f"apt-repo/{dest.name}",
                    "sha256": sha256_file(dest),
                    "direct": fields["Package"] in direct_set,
                }
            )
    finally:
        if owns_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)

    scan = runner(["dpkg-scanpackages", ".", "/dev/null"], cwd=str(apt_repo_dir))
    if scan.returncode != 0:
        raise ClosureBuildError(f"dpkg-scanpackages failed: {scan.stderr}")
    packages_path = apt_repo_dir / "Packages"
    packages_path.write_text(scan.stdout, encoding="utf-8")
    with packages_path.open("rb") as src, gzip.open(apt_repo_dir / "Packages.gz", "wb") as dst:
        shutil.copyfileobj(src, dst)

    manifests_dir = out_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "packages": sorted(entries, key=lambda e: e["name"]),
    }
    manifest_path = manifests_dir / "apt-closure-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    direct_list_path = manifests_dir / "direct-apt-packages.txt"
    direct_list_path.write_text("\n".join(sorted(direct_set)) + "\n", encoding="utf-8")

    return AptClosureResult(
        entries=entries, apt_repo_dir=apt_repo_dir, manifest_path=manifest_path, direct_list_path=direct_list_path
    )


# ---------------------------------------------------------------------
# snap closure
# ---------------------------------------------------------------------

def parse_installed_snap_revision(snap_list_stdout: str, name: str) -> str | None:
    """Parse `snap list <name>`'s own output for the ACTUAL locally
    installed revision (column 3), so the closure captures what
    production is really running rather than the store's current
    channel head. Returns None if the snap is not installed locally
    (caller falls back to the store's current --channel for that one)."""
    lines = [ln for ln in snap_list_stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    for line in lines[1:]:
        columns = line.split()
        if columns and columns[0] == name and len(columns) >= 3:
            return columns[2]
    return None


@dataclass(frozen=True)
class SnapClosureResult:
    manifest: dict
    snap_dir: Path
    manifest_path: Path


def build_snap_closure(
    out_dir: Path,
    runner: Runner = _default_runner,
    channel: str = "latest/stable",
) -> SnapClosureResult:
    """Always builds exactly REQUIRED_SNAP_INSTALL_ORDER -- the fixed,
    proven-offline Ubuntu 26.04/Chromium recovery contract (imported from
    offline_snap_install, the single source of truth both tools share).
    There is no `names` parameter: this is not an arbitrary snap list
    any more, see the module docstring for why a computed/alphabetical
    order was rejected in favor of this explicit contract."""
    snap_dir = out_dir / "snaps"
    snap_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for name in REQUIRED_SNAP_INSTALL_ORDER:
        listed = runner(["snap", "list", name])
        revision = parse_installed_snap_revision(listed.stdout, name) if listed.returncode == 0 else None

        download_argv = ["snap", "download", name]
        if revision is not None:
            download_argv += ["--revision", revision]
        else:
            download_argv += ["--channel", channel]
        download = runner(download_argv, cwd=str(snap_dir))
        if download.returncode != 0:
            raise ClosureBuildError(f"snap download failed for '{name}': {download.stderr}")

        snap_matches = sorted(snap_dir.glob(f"{name}_*.snap"))
        assert_matches = sorted(snap_dir.glob(f"{name}_*.assert"))
        if not snap_matches or not assert_matches:
            raise ClosureBuildError(
                f"snap download for '{name}' did not produce both a .snap and a .assert file in {snap_dir}"
            )
        snap_path = max(snap_matches, key=lambda p: p.stat().st_mtime)
        assert_path = max(assert_matches, key=lambda p: p.stat().st_mtime)

        if revision is None:
            # Recover the actual downloaded revision from the filename
            # snap always writes as `<name>_<revision>.snap`.
            revision = snap_path.stem.rsplit("_", 1)[-1]

        entries.append(
            {
                "name": name,
                "revision": revision,
                "snap_file": snap_path.name,
                "snap_sha256": sha256_file(snap_path),
                "assert_file": assert_path.name,
                "assert_sha256": sha256_file(assert_path),
            }
        )

    manifests_dir = out_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "ubuntu_release": "26.04",
        "snaps": sorted(entries, key=lambda e: e["name"]),
        "install_order": list(REQUIRED_SNAP_INSTALL_ORDER),
    }
    manifest_path = manifests_dir / "snap-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # offline_snap_install.py reads snap-manifest.json directly out of
    # --snap-dir, not out of manifests/ -- keep both in sync so the same
    # closure directory works as both --snap-dir and a manifest archive.
    shutil.copy2(manifest_path, snap_dir / "snap-manifest.json")

    return SnapClosureResult(manifest=manifest, snap_dir=snap_dir, manifest_path=manifest_path)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_offline_closure.py", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    apt_cmd = sub.add_parser("apt-closure", help="Build the offline apt repo + manifest.")
    apt_cmd.add_argument("--out-dir", required=True, type=Path)
    apt_cmd.add_argument(
        "--packages-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "packages-ubuntu-26.04.txt",
    )
    apt_cmd.add_argument("--groups", type=_split_csv, default=list(DEFAULT_GROUPS))

    snap_cmd = sub.add_parser(
        "snap-closure",
        help="Build the offline snap closure + manifest (always the full required set -- see REQUIRED_SNAP_INSTALL_ORDER).",
    )
    snap_cmd.add_argument("--out-dir", required=True, type=Path)
    snap_cmd.add_argument("--channel", default="latest/stable")

    all_cmd = sub.add_parser("all", help="Run both apt-closure and snap-closure.")
    all_cmd.add_argument("--out-dir", required=True, type=Path)
    all_cmd.add_argument(
        "--packages-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "packages-ubuntu-26.04.txt",
    )
    all_cmd.add_argument("--groups", type=_split_csv, default=list(DEFAULT_GROUPS))
    all_cmd.add_argument("--channel", default="latest/stable")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command in ("apt-closure", "all"):
            groups = parse_packages_file(args.packages_file)
            direct = resolve_direct_packages(groups, args.groups)
            result = build_apt_closure(direct, args.out_dir)
            print(f"apt-closure: {len(result.entries)} package(s) -> {result.apt_repo_dir}")
            print(f"apt-closure: manifest -> {result.manifest_path}")
        if args.command in ("snap-closure", "all"):
            result = build_snap_closure(args.out_dir, channel=args.channel)
            print(f"snap-closure: {len(result.manifest['snaps'])} snap(s) -> {result.snap_dir}")
            print(f"snap-closure: install_order = {result.manifest['install_order']}")
    except ClosureBuildError as exc:
        print(f"build_offline_closure.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
