"""Runtime Foundation E6 -- machine-readable bridge onto the existing
Ubuntu package authority (deploy/packages-ubuntu-26.04.txt).

Package MEMBERSHIP stays authoritative in that file, exactly as it
always has been for deploy/restore/10-packages.sh and
deploy/build_fdkaac.sh -- this module never duplicates a package list
into Python (no ``KOKORO_APT_PACKAGES = [...]`` here, and no second JSON
file either). What it adds:

  - a tiny, safe, read-only parser for that file's own established
    ``NAME=(\\n  pkg\\n  pkg\\n)`` bash-array format (never a bash
    interpreter -- it recognizes exactly that shape and nothing else);
  - a resolver from a runtime_components.json component to the named
    group it declares, keeping RUNTIME and BUILD-ONLY prerequisites
    distinct by which sub-block (``runtime``/``build``) names the group,
    never by an invented second field name -- see
    docs/RUNTIME_DEPLOY_BASELINE.md;
  - a three-state dpkg presence probe using the exact ``dpkg -s <pkg>``
    convention deploy/restore/10-packages.sh already uses, not
    ``command -v`` (a manually-placed binary could satisfy the latter
    without the package actually being installed), using trusted absolute
    executable paths and distinguishing probe failure from absence;
  - structured PASS/FAIL/UNRESOLVED/OPTIONAL_ABSENT/NOT_APPLICABLE
    package-prerequisite evidence for one component, consumed by
    isadoraair.deploy_baseline.
"""

from __future__ import annotations

import re
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGES_MANIFEST_PATH = PROJECT_ROOT / "deploy" / "packages-ubuntu-26.04.txt"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNRESOLVED = "unresolved"
STATUS_OPTIONAL_ABSENT = "optional_absent"
STATUS_NOT_APPLICABLE = "not_applicable"

_GROUP_START_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=\($")
_GROUP_END = ")"
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.:_-]*")
_DPKG_PROBE_TIMEOUT_SECONDS = 10.0
DPKG_EXECUTABLE_CANDIDATES = (Path("/usr/bin/dpkg"), Path("/bin/dpkg"))

DPKG_INSTALLED = "installed"
DPKG_NOT_INSTALLED = "not_installed"
DPKG_UNRESOLVED = "unresolved"


class RuntimePackageAuthorityError(ValueError):
    """The package authority file, or a referenced group name, is invalid."""


def parse_package_groups(path: Path) -> dict[str, tuple[str, ...]]:
    """Parse deploy/packages-ubuntu-26.04.txt's own bash-array format.

    Deliberately NOT a bash interpreter -- this only ever recognizes that
    file's specific, established shape: a line naming a group
    (``NAME=(``), followed by one package name (or a ``#``-prefixed
    comment, or a blank line) per line, closed by a lone ``)`` line.
    Outside a group, only comments, blank lines, and a group declaration
    are accepted. Inside a group, every member must be one complete,
    unquoted package token. Unsupported shell syntax is rejected rather
    than partially interpreted.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimePackageAuthorityError(
            f"package authority file is unavailable: {path}"
        ) from exc

    groups: dict[str, tuple[str, ...]] = {}
    current_name: str | None = None
    current_members: list[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if current_name is None:
            if not line or line.startswith("#"):
                continue
            match = _GROUP_START_RE.match(line)
            if match:
                current_name = match.group(1)
                current_members = []
                continue
            raise RuntimePackageAuthorityError(
                f"{path}:{lineno}: unsupported syntax outside a package group: {raw_line!r}"
            )
        if line == _GROUP_END:
            if current_name in groups:
                raise RuntimePackageAuthorityError(
                    f"{path}:{lineno}: duplicate package group '{current_name}'"
                )
            groups[current_name] = tuple(current_members)
            current_name = None
            current_members = []
            continue
        if not line or line.startswith("#"):
            continue
        token_match = _TOKEN_RE.fullmatch(line)
        if not token_match:
            raise RuntimePackageAuthorityError(
                f"{path}:{lineno}: unsupported package member syntax: {raw_line!r}"
            )
        package_name = token_match.group(0)
        if package_name in current_members:
            raise RuntimePackageAuthorityError(
                f"{path}:{lineno}: duplicate package member '{package_name}' "
                f"in group '{current_name}'"
            )
        current_members.append(package_name)
    if current_name is not None:
        raise RuntimePackageAuthorityError(
            f"{path}: package group '{current_name}' was never closed with ')'"
        )
    return groups


def package_group_members(group_name: str, *, path: Path | None = None) -> tuple[str, ...]:
    """Resolve one named group's package members from the authority file."""

    resolved_path = path or PACKAGES_MANIFEST_PATH
    groups = parse_package_groups(resolved_path)
    try:
        return groups[group_name]
    except KeyError as exc:
        raise RuntimePackageAuthorityError(
            f"unknown Ubuntu package group '{group_name}' -- not defined in {resolved_path}"
        ) from exc


def component_package_group(
    manifest: dict[str, Any], component_name: str, *, kind: str
) -> str | None:
    """The named package-authority group `component_name` declares for
    `kind` ("runtime" or "build"), or None if it defines no such
    prerequisite at all -- e.g. Piper's runtime block deliberately has
    none (docs/PIPER_PROVENANCE.md: self-contained, nothing from apt)."""

    if kind not in ("runtime", "build"):
        raise ValueError("kind must be 'runtime' or 'build'")
    component = manifest.get("components", {}).get(component_name, {})
    block = component.get(kind)
    if not isinstance(block, dict):
        return None
    group = block.get("ubuntu_packages_group")
    return group if isinstance(group, str) and group else None


DpkgProbe = Callable[[str], str | bool]


def _trusted_dpkg_executable() -> Path | None:
    for candidate in DPKG_EXECUTABLE_CANDIDATES:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _dpkg_status(package_name: str, *, target_root: Path = Path("/")) -> str:
    executable = _trusted_dpkg_executable()
    if executable is None:
        return DPKG_UNRESOLVED
    command = [str(executable)]
    if target_root != Path("/"):
        command.append(f"--root={target_root}")
    command.extend(("-s", package_name))
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_DPKG_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DPKG_UNRESOLVED
    if result.returncode == 0:
        return DPKG_INSTALLED
    if result.returncode == 1:
        return DPKG_NOT_INSTALLED
    return DPKG_UNRESOLVED


DEFAULT_DPKG_PROBE: DpkgProbe = _dpkg_status


@dataclass(frozen=True, slots=True)
class PackagePrerequisiteEvidence:
    component: str
    kind: str
    group: str | None
    members: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    required: bool | None = None
    reasons: tuple[str, ...] = ()
    status: str = STATUS_NOT_APPLICABLE
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "diagnostics": list(self.diagnostics),
            "group": self.group,
            "kind": self.kind,
            "members": list(self.members),
            "missing": list(self.missing),
            "reasons": list(self.reasons),
            "required": self.required,
            "status": self.status,
        }


def evaluate_package_prerequisite(
    manifest: dict[str, Any],
    component_name: str,
    *,
    kind: str,
    required: bool | None,
    reasons: tuple[str, ...] = (),
    packages_path: Path | None = None,
    dpkg_probe: DpkgProbe = DEFAULT_DPKG_PROBE,
    target_root: Path = Path("/"),
) -> PackagePrerequisiteEvidence:
    """Structured PASS/FAIL/UNRESOLVED/OPTIONAL_ABSENT/NOT_APPLICABLE
    evidence for one component's package prerequisite of the given
    `kind`. `required=None` means the caller could not resolve whether
    the owning component is actually required (e.g. no station database
    available) -- this always yields UNRESOLVED, never a guessed PASS.

    No package installation happens here, ever -- this is read-only,
    dpkg-state evidence only.
    """

    group = component_package_group(manifest, component_name, kind=kind)
    if group is None:
        return PackagePrerequisiteEvidence(
            component=component_name, kind=kind, group=None, required=required, reasons=reasons,
            status=STATUS_NOT_APPLICABLE,
        )
    try:
        members = package_group_members(group, path=packages_path)
    except RuntimePackageAuthorityError as exc:
        return PackagePrerequisiteEvidence(
            component=component_name, kind=kind, group=group, required=required, reasons=reasons,
            status=STATUS_FAIL, diagnostics=(str(exc),),
        )
    probe_results: list[tuple[str, str]] = []
    for package in members:
        if dpkg_probe is DEFAULT_DPKG_PROBE:
            raw_result = _dpkg_status(package, target_root=target_root)
        else:
            raw_result = dpkg_probe(package)
        # Preserve the established bool seam for existing tests and
        # downstream callers while the product probe itself is three-state.
        if raw_result is True:
            result = DPKG_INSTALLED
        elif raw_result is False:
            result = DPKG_NOT_INSTALLED
        else:
            result = raw_result
        probe_results.append((package, result))

    missing = tuple(pkg for pkg, result in probe_results if result == DPKG_NOT_INSTALLED)
    unresolved = tuple(pkg for pkg, result in probe_results if result == DPKG_UNRESOLVED)
    diagnostics = (
        ("unable to determine package status because the trusted dpkg probe failed for: "
         + ", ".join(unresolved)),
    ) if unresolved else ()
    if unresolved:
        status = STATUS_UNRESOLVED
    elif required is None:
        status = STATUS_UNRESOLVED
    elif required:
        status = STATUS_FAIL if missing else STATUS_PASS
    else:
        status = STATUS_OPTIONAL_ABSENT if missing else STATUS_PASS
    return PackagePrerequisiteEvidence(
        component=component_name,
        kind=kind,
        group=group,
        members=members,
        missing=missing,
        required=required,
        reasons=reasons,
        status=status,
        diagnostics=diagnostics,
    )
