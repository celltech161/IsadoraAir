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

Two closure defects found by the genuine E8 clean/offline acceptance run
motivate this tool's specific design choices (see
`docs/DISASTER_RECOVERY_RESTORE.md`'s "Offline package/snap closure"
section for the full narrative):

  1. `age` -- a DIRECT package -- was silently missing from a prior ad hoc
     apt closure because it was already installed on the source host, and
     a naive `apt-get --download-only install` does not re-fetch a
     package that is already installed. This tool always runs the real
     download step with `--reinstall`, which forces apt to fetch every
     package in the resolved dependency graph regardless of its current
     install state on the source host -- there is no special case for
     "already installed", because that is exactly the class of bug that
     caused the omission.
  2. `bubblewrap` -- a TRANSITIVE dependency (`glycin-loaders ->
     bubblewrap`) -- was missing entirely: no .deb, no Packages stanza,
     no manifest entry. This tool never hand-curates the transitive set;
     it always takes apt's own dependency resolver's word for the full
     closure (`apt-get install --reinstall --download-only -s`, i.e. a
     simulate/dry-run, enumerates every `Inst <name>` line apt would act
     on) and captures every one of them, not just the packages named in
     `deploy/packages-ubuntu-26.04.txt`.

For snaps, the same "capture reality, don't hand-curate" principle
applies to the one problem that is snap-specific: this tool always
includes the `snapd` system snap explicitly (a real production `mesa-2404`
install offline failed for exactly the lack of it -- see the
`10-packages.sh`/`offline_snap_install.py` headers for the full story),
and prefers to capture each snap at its ACTUAL locally-installed revision
(`snap list <name>`) rather than whatever the store's current stable
channel happens to be, so the closure matches what production is really
running rather than drifting from it between builder runs.

Usage:
  build_offline_closure.py apt-closure --out-dir DIR
      [--packages-file deploy/packages-ubuntu-26.04.txt]
      [--groups CORE,AUDIO_GSTREAMER,BUILD_HEAAC,OPTIONAL_CD_RIP,OPTIONAL_KOKORO_TTS,OPTIONAL_SYNDICATED_SELENIUM,OPTIONAL_BACKUP_ENCRYPTION]

  build_offline_closure.py snap-closure --out-dir DIR
      [--snaps chromium,gtk-common-themes,gnome-46-2404,cups,mesa-2404,core22,core24,bare]
      [--channel latest/stable]

  build_offline_closure.py all --out-dir DIR [same flags as both above]

Requires on THIS (builder) host only: apt-get, dpkg-deb, dpkg-scanpackages
(package `dpkg-dev`), snap, gzip -- all standard on an Ubuntu 26.04 box.
Both apt-get steps that actually touch the network/cache run under sudo
(the simulate/-s enumeration step does not need root and is never run
under sudo). Never runs anything as root beyond that. Writes no secret
material -- there is none to write; package/snap closures are public
software, not credentials.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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
DEFAULT_SNAPS = (
    "bare",
    "core22",
    "core24",
    "gtk-common-themes",
    "mesa-2404",
    "gnome-46-2404",
    "cups",
    "chromium",
)
ESSENTIAL_SNAP = "snapd"
CHROMIUM_SNAP = "chromium"
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
    own dependency resolver. Never hand-curated."""
    names: list[str] = []
    for line in simulate_stdout.splitlines():
        line = line.strip()
        if line.startswith("Inst "):
            parts = line.split()
            if len(parts) >= 2:
                names.append(parts[1])
    return names


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
            "`apt-get install --reinstall --download-only`, found none -- this indicates "
            "the real download step did not actually fetch a package the simulate step "
            "resolved; re-run with apt-get's own output visible to diagnose."
        )
    # Multiple cached versions (from a previous run) can coexist; the most
    # recently written one is the one this run's download step produced.
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
    archives_dir: Path = Path("/var/cache/apt/archives"),
    sudo: bool = True,
) -> AptClosureResult:
    if not direct_packages:
        raise ClosureBuildError("no direct packages resolved -- refusing to build an empty closure")

    sudo_prefix = ["sudo"] if sudo else []

    update = runner([*sudo_prefix, "apt-get", "update"])
    if update.returncode != 0:
        raise ClosureBuildError(f"apt-get update failed: {update.stderr}")

    # Simulate first (no root, no network/filesystem writes) purely to
    # enumerate the full resolved closure via apt's own resolver.
    simulate = runner(
        ["apt-get", "install", "--reinstall", "--download-only", "-s", "-y", *direct_packages]
    )
    if simulate.returncode != 0:
        raise ClosureBuildError(f"apt-get install --simulate failed to resolve the closure: {simulate.stderr}")
    closure_names = sorted(set(parse_simulate_inst_names(simulate.stdout)) | set(direct_packages))
    if not closure_names:
        raise ClosureBuildError("apt-get --simulate resolved an empty closure -- refusing to proceed")

    # Real download: --reinstall forces every package to be fetched
    # regardless of current install state (the `age` fix); --download-only
    # never unpacks/configures anything, so this step is safe to run
    # against a live source host.
    download = runner(
        [*sudo_prefix, "apt-get", "install", "--reinstall", "--download-only", "-y", *direct_packages]
    )
    if download.returncode != 0:
        raise ClosureBuildError(f"apt-get install --reinstall --download-only failed: {download.stderr}")

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

_SNAP_TYPE_RE = re.compile(r"^type:\s*(\S+)", re.MULTILINE)


def parse_installed_snap_revision(snap_list_stdout: str, name: str) -> str | None:
    """Parse `snap list <name>`'s own output for the ACTUAL locally
    installed revision (column 3), so the closure captures what
    production is really running rather than the store's current
    channel head. Returns None if the snap is not installed locally
    (caller falls back to the store's current revision for that one)."""
    lines = [ln for ln in snap_list_stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    for line in lines[1:]:
        columns = line.split()
        if columns and columns[0] == name and len(columns) >= 3:
            return columns[2]
    return None


def classify_snap_bucket(name: str, snap_info_stdout: str) -> int:
    """Dependency-safe install ordering, computed here (not hardcoded in
    the restore-side installer) so a manifest regeneration can react to a
    snap's type changing. `snapd` always installs first; `chromium`
    always installs last (its own prerequisite base/content snaps must
    already be present); everything else is ordered by declared type:
    base/os/kernel/gadget snaps before ordinary app/content snaps."""
    if name == ESSENTIAL_SNAP:
        return 0
    if name == CHROMIUM_SNAP:
        return 3
    match = _SNAP_TYPE_RE.search(snap_info_stdout)
    snap_type = match.group(1) if match else ""
    if snap_type in ("base", "os", "kernel", "gadget", "snapd"):
        return 1
    return 2


@dataclass(frozen=True)
class SnapClosureResult:
    manifest: dict
    snap_dir: Path
    manifest_path: Path


def build_snap_closure(
    names: list[str],
    out_dir: Path,
    runner: Runner = _default_runner,
    channel: str = "latest/stable",
) -> SnapClosureResult:
    all_names = sorted(set(names) | {ESSENTIAL_SNAP, CHROMIUM_SNAP})
    snap_dir = out_dir / "snaps"
    snap_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    buckets: dict[str, int] = {}
    for name in all_names:
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
            # apt/snap always writes as `<name>_<revision>.snap`.
            revision = snap_path.stem.rsplit("_", 1)[-1]

        info = runner(["snap", "info", name])
        buckets[name] = classify_snap_bucket(name, info.stdout if info.returncode == 0 else "")

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

    install_order = [e["name"] for e in sorted(entries, key=lambda e: (buckets[e["name"]], e["name"]))]

    manifests_dir = out_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "ubuntu_release": "26.04",
        "snaps": sorted(entries, key=lambda e: e["name"]),
        "install_order": install_order,
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

    snap_cmd = sub.add_parser("snap-closure", help="Build the offline snap closure + manifest.")
    snap_cmd.add_argument("--out-dir", required=True, type=Path)
    snap_cmd.add_argument("--snaps", type=_split_csv, default=list(DEFAULT_SNAPS))
    snap_cmd.add_argument("--channel", default="latest/stable")

    all_cmd = sub.add_parser("all", help="Run both apt-closure and snap-closure.")
    all_cmd.add_argument("--out-dir", required=True, type=Path)
    all_cmd.add_argument(
        "--packages-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "packages-ubuntu-26.04.txt",
    )
    all_cmd.add_argument("--groups", type=_split_csv, default=list(DEFAULT_GROUPS))
    all_cmd.add_argument("--snaps", type=_split_csv, default=list(DEFAULT_SNAPS))
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
            result = build_snap_closure(args.snaps, args.out_dir, channel=args.channel)
            print(f"snap-closure: {len(result.manifest['snaps'])} snap(s) -> {result.snap_dir}")
            print(f"snap-closure: install_order = {result.manifest['install_order']}")
    except ClosureBuildError as exc:
        print(f"build_offline_closure.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
