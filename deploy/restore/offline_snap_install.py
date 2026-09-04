#!/usr/bin/env python3
"""Offline snap-closure verification/ordering -- IsadoraAir 1.2 r0038 (E8).

Stage 10 (`10-packages.sh`) needs to install a *complete* local snap
closure (snapd system snap, base/runtime/content snaps, Chromium) with no
Snap Store access at all, in a dependency-safe order, before the Ubuntu
`chromium-browser` transition package is unpacked -- see
`docs/DISASTER_RECOVERY_RESTORE.md`'s "Offline package/snap closure"
section for the full story of why (the E8 acceptance run's Stage 10
findings: a missing `age`/`bubblewrap` in the apt closure, and a missing
`snapd` system snap in the preserved snap closure, both caused a supposedly
offline restore to reach out to the network).

This stdlib-only helper (matching `restore_manage.py` / `runtime_recovery_
archive.py`'s "stdlib-only, orchestration glue, easy to unit-test" style)
owns exactly one job: turn a snap-closure directory (`.snap`/`.assert`
files plus an authoritative `snap-manifest.json`, produced by
`build_offline_closure.py`) into a verified, ordered installation plan --
or fail closed with a specific, actionable reason. It never calls `snap`,
`systemctl`, or `dpkg` itself, and never touches the network -- the
privileged install sequence (ack/install/stop-snapd/dpkg -i/restart-snapd)
stays in `10-packages.sh`, which already owns every other stage's
plan/apply/sudo idiom (see `lib.sh`). This script's whole reason to exist
is that manifest verification -- JSON schema, path confinement, SHA256
checks, and the full required-snap-set/install-order recovery contract
below -- is exactly the kind of logic that is easy to get subtly wrong
and easy to unit-test in Python, and hard to get right (or test) in bash.

## The required-snap-set/install-order contract

r0038 code review found the original "snapd and chromium must both be
present, and snapd no later than chromium" check insufficient: a
manifest missing `mesa-2404`, `core22`, `core24`, `gtk-common-themes`,
etc. would still PASS verification and only fail later during `snap
install` -- potentially still reaching for the Snap Store mid-sequence.
A coarse type-bucket/alphabetical install order is also not an actual
dependency graph and does not match what was proven to work by hand.

So this module hard-codes `REQUIRED_SNAP_INSTALL_ORDER` below as an
explicit, versioned recovery contract for the CURRENT Ubuntu 26.04 /
Chromium snap stack -- the exact sequence proven to install fully
offline during the E8 acceptance run (`bare`/`core22`/`core24` need
nothing else; `snapd` the system snap itself must exist locally before
`mesa-2404` -- which failed for exactly its absence -- or anything after
it; `chromium` goes last since it needs the rest already present). A
manifest's `snaps` must be exactly this set and its `install_order` must
be exactly this sequence -- not merely a permutation, not a superset,
not a looser ordering -- or verification fails closed before any
privileged action. snap REVISIONS remain entirely manifest-driven; only
the set of NAMES and their relative order is an explicit code contract.
If a future Ubuntu/Chromium snap stack needs a different closure, update
`REQUIRED_SNAP_INSTALL_ORDER` deliberately (a reviewed code change), not
silently accept whatever a manifest happens to declare.

Fail-closed by design: ANY problem with the manifest or the files it
references -- missing manifest, malformed JSON, missing/renamed file,
SHA256 mismatch, `snapd`/`chromium` absent, a bad `install_order` -- is a
hard error with a specific message and a distinct exit code. There is no
partial/best-effort mode and no fallback to the Snap Store; an incomplete
closure must never silently proceed.

Usage:
  offline_snap_install.py plan --snap-dir PATH

On success, prints one TSV line per snap to install, in
manifest-authoritative dependency order:

  SNAP<TAB>name<TAB>revision<TAB>/abs/path/to/name_rev.assert<TAB>/abs/path/to/name_rev.snap

`10-packages.sh` reads this (in BOTH --plan and --apply mode -- the
verification itself is read-only and needs no root) to display the plan
and, in --apply mode, to drive `snap ack`/`snap install` in that exact
order. On failure, prints a specific diagnostic to stderr and exits
nonzero -- nothing is printed to stdout in that case, so a caller that
(incorrectly) tried to consume partial stdout output would get nothing to
act on.

Exit codes:
  0  success -- plan printed to stdout
  2  manifest missing, unreadable, or fails schema validation
  3  a file the manifest references is missing, unsafe, or not a regular file
  4  a SHA256 in the manifest does not match the file's real contents
  5  one or more required snaps (see REQUIRED_SNAP_INSTALL_ORDER) are absent
  6  install_order does not exactly match REQUIRED_SNAP_INSTALL_ORDER
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "snap-manifest.json"

# The proven-offline Ubuntu 26.04 / Chromium snap recovery contract (E8
# acceptance run, r0038 review) -- see this module's own docstring for
# why this is a fixed, explicit sequence rather than a computed
# type-bucket/alphabetical order or a bare {snapd, chromium} minimum.
# bare/core22/core24 need nothing else; snapd (the system snap) must be
# present before mesa-2404 (which failed for exactly its absence) or
# anything after it; chromium goes last.
REQUIRED_SNAP_INSTALL_ORDER = (
    "bare",
    "core22",
    "core24",
    "snapd",
    "mesa-2404",
    "gtk-common-themes",
    "gnome-46-2404",
    "cups",
    "chromium",
)
_SHA256_RE_LEN = 64
_REQUIRED_ENTRY_KEYS = {"name", "revision", "snap_file", "snap_sha256", "assert_file", "assert_sha256"}


class SnapClosureError(Exception):
    exit_code = 1


class ManifestError(SnapClosureError):
    exit_code = 2


class MissingFileError(SnapClosureError):
    exit_code = 3


class HashMismatchError(SnapClosureError):
    exit_code = 4


class RequiredSnapMissingError(SnapClosureError):
    exit_code = 5


class InstallOrderError(SnapClosureError):
    exit_code = 6


@dataclass(frozen=True)
class SnapPlanEntry:
    name: str
    revision: str
    assert_path: Path
    snap_path: Path


def _is_safe_basename(value: object) -> bool:
    """Must be a plain filename -- no path separators, no `..`, not
    absolute, not empty. Mirrors lib.sh's ensure_confined_directory
    philosophy: a manifest-supplied filename must never be able to walk
    this helper outside --snap-dir."""
    if not isinstance(value, str) or not value:
        return False
    if value in (".", ".."):
        return False
    if "/" in value or "\\" in value:
        return False
    return True


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_RE_LEN
        and all(c in "0123456789abcdef" for c in value)
    )


def load_manifest(snap_dir: Path) -> dict:
    manifest_path = snap_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ManifestError(f"snap manifest not found: {manifest_path}")
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read snap manifest {manifest_path}: {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"snap manifest {manifest_path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ManifestError(f"snap manifest {manifest_path} must be a JSON object")
    return data


def _validate_schema(data: dict) -> tuple[list[dict], list[str]]:
    if data.get("schema_version") != 1:
        raise ManifestError("snap manifest: unsupported or missing schema_version (expected 1)")

    snaps = data.get("snaps")
    if not isinstance(snaps, list) or not snaps:
        raise ManifestError("snap manifest: 'snaps' must be a non-empty list")

    names_seen: set[str] = set()
    for entry in snaps:
        if not isinstance(entry, dict) or set(entry) != _REQUIRED_ENTRY_KEYS:
            raise ManifestError(
                f"snap manifest: each entry in 'snaps' must have exactly these keys: "
                f"{sorted(_REQUIRED_ENTRY_KEYS)} (got {entry!r})"
            )
        name = entry["name"]
        if not isinstance(name, str) or not name:
            raise ManifestError(f"snap manifest: invalid snap name: {entry.get('name')!r}")
        if name in names_seen:
            raise ManifestError(f"snap manifest: duplicate snap name: {name}")
        names_seen.add(name)
        if not isinstance(entry["revision"], str) or not entry["revision"]:
            raise ManifestError(f"snap manifest: {name}: 'revision' must be a non-empty string")
        for field in ("snap_file", "assert_file"):
            if not _is_safe_basename(entry[field]):
                raise ManifestError(
                    f"snap manifest: {name}: '{field}' must be a plain filename "
                    f"(no path separators), got {entry[field]!r}"
                )
        for field in ("snap_sha256", "assert_sha256"):
            if not _is_sha256(entry[field]):
                raise ManifestError(f"snap manifest: {name}: '{field}' is not a valid lowercase sha256 hex digest")

    install_order = data.get("install_order")
    if not isinstance(install_order, list) or not all(isinstance(x, str) for x in install_order):
        raise ManifestError("snap manifest: 'install_order' must be a list of snap names")

    return snaps, install_order


def _validate_required_snaps_present(names: set[str]) -> None:
    missing = [n for n in REQUIRED_SNAP_INSTALL_ORDER if n not in names]
    if missing:
        raise RequiredSnapMissingError(
            "snap manifest is missing required snap(s): "
            + ", ".join(missing)
            + " -- the current Ubuntu 26.04/Chromium offline recovery contract requires "
            "every one of " + ", ".join(REQUIRED_SNAP_INSTALL_ORDER) + "; see "
            "build_offline_closure.py's snap-closure step and this module's own docstring."
        )
    extra = sorted(names - set(REQUIRED_SNAP_INSTALL_ORDER))
    if extra:
        raise RequiredSnapMissingError(
            "snap manifest declares snap(s) outside the current recovery contract: "
            + ", ".join(extra)
            + " -- the manifest's 'snaps' must be EXACTLY " + ", ".join(REQUIRED_SNAP_INSTALL_ORDER)
            + ", not a superset; see this module's own docstring."
        )


def _validate_install_order(snap_names: list[str], install_order: list[str]) -> None:
    if sorted(install_order) != sorted(snap_names) or len(install_order) != len(set(install_order)):
        raise InstallOrderError(
            "snap manifest: 'install_order' must contain each snap in 'snaps' exactly once "
            f"(snaps={sorted(snap_names)}, install_order={install_order})"
        )
    expected = list(REQUIRED_SNAP_INSTALL_ORDER)
    if install_order != expected:
        raise InstallOrderError(
            "snap manifest: 'install_order' must exactly match the current Ubuntu 26.04/"
            f"Chromium offline recovery contract order {expected} -- got {install_order}. "
            "This is a fixed, proven-offline sequence, not a computed/alphabetical one; "
            "see this module's own docstring."
        )


def _validate_file(snap_dir: Path, filename: str, expected_sha256: str, snap_name: str, kind: str) -> Path:
    path = snap_dir / filename
    resolved_dir = snap_dir.resolve()
    resolved_path = path.resolve()
    if resolved_dir != resolved_path.parent or not resolved_path.is_file():
        raise MissingFileError(f"snap manifest: {snap_name}: {kind} file not found: {path}")
    digest = hashlib.sha256()
    with resolved_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise HashMismatchError(
            f"snap manifest: {snap_name}: {kind} file {path} SHA256 mismatch "
            f"(manifest says {expected_sha256}, actual is {actual}) -- refusing to install "
            "from a closure that does not match its own manifest"
        )
    return resolved_path


def build_plan(snap_dir: Path) -> list[SnapPlanEntry]:
    """The one entry point: validate everything, fail closed on the first
    problem, and return the ordered install plan. Never partially
    succeeds -- either every file is present/verified and the order is
    dependency-safe, or this raises."""
    data = load_manifest(snap_dir)
    snaps, install_order = _validate_schema(data)
    by_name = {entry["name"]: entry for entry in snaps}
    _validate_required_snaps_present(set(by_name))
    _validate_install_order(list(by_name), install_order)

    plan: list[SnapPlanEntry] = []
    for name in install_order:
        entry = by_name[name]
        assert_path = _validate_file(snap_dir, entry["assert_file"], entry["assert_sha256"], name, "assert")
        snap_path = _validate_file(snap_dir, entry["snap_file"], entry["snap_sha256"], name, "snap")
        plan.append(SnapPlanEntry(name=name, revision=entry["revision"], assert_path=assert_path, snap_path=snap_path))
    return plan


def render_plan_lines(plan: list[SnapPlanEntry]) -> list[str]:
    return [f"SNAP\t{e.name}\t{e.revision}\t{e.assert_path}\t{e.snap_path}" for e in plan]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offline_snap_install.py", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan", help="Verify the closure and print the ordered install plan (TSV).")
    plan_cmd.add_argument("--snap-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "plan":
        snap_dir: Path = args.snap_dir
        if not snap_dir.is_dir():
            print(f"offline_snap_install.py: --snap-dir is not a directory: {snap_dir}", file=sys.stderr)
            return 2
        try:
            plan = build_plan(snap_dir)
        except SnapClosureError as exc:
            print(f"offline_snap_install.py: {exc}", file=sys.stderr)
            return exc.exit_code
        for line in render_plan_lines(plan):
            print(line)
        return 0
    return 2  # unreachable: argparse enforces a valid subcommand


if __name__ == "__main__":
    raise SystemExit(main())
