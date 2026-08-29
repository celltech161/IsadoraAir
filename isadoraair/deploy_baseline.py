"""Runtime Foundation E6 -- deployment baseline consolidation.

One coherent read-only answer to: is this host structurally capable of
satisfying the station's runtime contract, and can a restored/clean host
establish the required system surfaces without relying on historical
one-off checks?

Combines, without re-implementing any of it:
  - legacy host/system checks that predate Runtime Foundation E and
    remain genuinely useful (Python, PostgreSQL client tools, live-only
    PostgreSQL connectivity,
    GStreamer + required elements, Liquidsoap, ALSA utils + snd-aloop,
    the canonical app-root/library-root/``/run/isadoraair`` directory
    checks) -- ported from the original
    ``monitoring.management.commands.check_deploy_baseline`` essentially
    verbatim, just returned as structured evidence instead of being
    printed directly;
  - package prerequisite evidence (:mod:`isadoraair.runtime_packages`);
  - Runtime Foundation E5 system-surface evidence
    (:mod:`isadoraair.runtime_surfaces`);
  - the pre-existing TTS scratch-surface evidence
    (:mod:`isadoraair.runtime_scratch`);
  - Runtime Foundation E1/E2 runtime-component evidence
    (:func:`isadoraair.runtime_validation.validate_current_runtime`),
    which already resolves current station requirements OR reports an
    explicit unresolved station-requirement state without crashing when
    no usable database exists -- this module reuses that fail-closed
    design rather than reinventing it.

``check_deploy_baseline.py`` (the operator-facing Django management
command) is a thin presentation layer over
:func:`evaluate_deployment_baseline` below -- this module has no Django
management-command machinery of its own and no side effects: it never
installs a package, builds anything, or repairs a system surface. It
only reports.

Structural vs. station baseline (see docs/RUNTIME_DEPLOY_BASELINE.md):

  STRUCTURAL tier -- evaluable with no station database at all: manifest
  validity, legacy host/system checks, package prerequisite PRESENCE
  (not yet whether the package is actually *needed* -- see below), E5
  system surfaces, the scratch surface.

  LIVE/STATION tier -- canonical / only; requires a working station database/configuration:
  which optional runtimes (Kokoro/Piper/fdkaac) are actually required,
  and whether they pass Foundation E's own E2 component validation. When
  the database can't be inspected, this tier reports UNRESOLVED, never a
  guessed PASS -- and package-prerequisite "required" evidence (e.g.
  "does this station need OPTIONAL_KOKORO_TTS") is UNRESOLVED right
  along with it, for the same reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from isadoraair.runtime_components import (
    RuntimeComponentContractError,
    load_runtime_components,
)
from isadoraair.runtime_packages import (
    PackagePrerequisiteEvidence,
    STATUS_FAIL as PACKAGE_STATUS_FAIL,
    STATUS_UNRESOLVED as PACKAGE_STATUS_UNRESOLVED,
    evaluate_package_prerequisite,
)
from isadoraair.runtime_scratch import (
    STATE_HEALTHY as SCRATCH_STATE_HEALTHY,
    STATE_UNRESOLVED_IDENTITY as SCRATCH_STATE_UNRESOLVED_IDENTITY,
    ScratchSurfaceEvidence,
    evaluate_scratch_surface,
)
from isadoraair.runtime_surfaces import (
    RuntimeSystemSurfaceManager,
    SystemSurfaceEvidence,
)
from isadoraair.runtime_provisioning import RuntimeProvisioningError
from isadoraair.runtime_validation import RuntimeEvidence, RuntimeValidator, validate_current_runtime


SCHEMA_VERSION = 1

RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_UNRESOLVED = "unresolved"

LEGACY_PASS = "PASS"
LEGACY_DEGRADED = "DEGRADED"
LEGACY_MISSING = "MISSING"
LEGACY_OPTIONAL = "OPTIONAL"

REQUIRED_GST_ELEMENTS = [
    "alsasrc", "alsasink", "audioconvert", "audiodynamic", "audiomixer",
    "audioresample", "audiotestsrc", "capsfilter", "concat", "decodebin",
    "fakesink", "filesrc", "input-selector", "level", "opusdec", "opusenc",
    "queue", "rglimiter", "rtpopusdepay", "rtpopuspay", "tee", "volume",
    "webrtcbin",
]
# Format-specific decode path -- see docs/GSTREAMER_ELEMENT_INVENTORY.md.
REQUIRED_GST_DECODE_ELEMENTS = [
    "flacparse", "flacdec", "qtdemux", "avdec_aac", "mpegaudioparse",
    "id3demux", "avdec_mp3", "aiffparse", "wavparse",
]
MIN_PYTHON = (3, 14)


@dataclass(frozen=True, slots=True)
class LegacyCheck:
    """One non-Foundation-E host/system deployment check -- same
    PASS/DEGRADED/MISSING/OPTIONAL vocabulary the original
    check_deploy_baseline command always used for this class of check.
    """

    label: str
    state: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"detail": self.detail, "label": self.label, "state": self.state}


# ---- legacy host/system checks -- ported ~verbatim from the pre-E6 ----
# ---- monitoring.management.commands.check_deploy_baseline. -----------
# fdkaac/Kokoro/Piper checks are deliberately NOT here anymore -- see
# the module docstring; Foundation E now owns that state authoritatively.

def _check_python() -> LegacyCheck:
    import sys

    v = sys.version_info
    actual = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        return LegacyCheck("Python", LEGACY_PASS, actual)
    return LegacyCheck("Python", LEGACY_MISSING, f"{actual} (need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})")


def _check_postgres_tools() -> LegacyCheck:
    missing = [b for b in ("psql", "pg_dump", "pg_restore") if shutil.which(b) is None]
    if missing:
        return LegacyCheck("PostgreSQL client tools", LEGACY_MISSING, f"missing: {', '.join(missing)}")
    return LegacyCheck("PostgreSQL client tools", LEGACY_PASS, "psql, pg_dump, pg_restore on PATH")


def _check_postgres_connection() -> LegacyCheck:
    try:
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute("SELECT version()")
            version_str = cur.fetchone()[0]
        return LegacyCheck("PostgreSQL connection", LEGACY_PASS, version_str.split(",")[0])
    except Exception as exc:  # host/DB reachability only -- never let this crash the baseline
        return LegacyCheck("PostgreSQL connection", LEGACY_MISSING, str(exc))


def _check_one_gst_element(gst_inspect: str, elem: str) -> LegacyCheck:
    try:
        result = subprocess.run([gst_inspect, elem], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return LegacyCheck(f"  element {elem}", LEGACY_MISSING, str(exc))
    if result.returncode != 0 or "No such element" in result.stdout + result.stderr:
        return LegacyCheck(f"  element {elem}", LEGACY_MISSING, "not found")
    return LegacyCheck(f"  element {elem}", LEGACY_PASS, None)


def _check_gstreamer() -> list[LegacyCheck]:
    gst_inspect = shutil.which("gst-inspect-1.0")
    if gst_inspect is None:
        return [LegacyCheck("GStreamer", LEGACY_MISSING, "gst-inspect-1.0 not found")] + [
            LegacyCheck(f"  element {e}", LEGACY_MISSING, "gst-inspect-1.0 unavailable")
            for e in REQUIRED_GST_ELEMENTS + REQUIRED_GST_DECODE_ELEMENTS
        ]
    try:
        out = subprocess.run([gst_inspect, "--version"], capture_output=True, text=True, timeout=10)
        ver_line = out.stdout.strip().splitlines()[0] if out.stdout else "unknown version"
    except Exception as exc:
        ver_line = f"version check failed: {exc}"
    results = [LegacyCheck("GStreamer", LEGACY_PASS, ver_line)]
    for elem in REQUIRED_GST_ELEMENTS + REQUIRED_GST_DECODE_ELEMENTS:
        results.append(_check_one_gst_element(gst_inspect, elem))
    return results


def _check_liquidsoap() -> LegacyCheck:
    liq = shutil.which("liquidsoap")
    if liq is None:
        return LegacyCheck("Liquidsoap", LEGACY_MISSING, "not found on PATH")
    try:
        out = subprocess.run([liq, "--version"], capture_output=True, text=True, timeout=10)
        first_line = out.stdout.strip().splitlines()[0] if out.stdout else "version unknown"
    except Exception as exc:
        first_line = f"version check failed: {exc}"
    return LegacyCheck("Liquidsoap", LEGACY_PASS, first_line)


def _check_alsa_utils() -> LegacyCheck:
    missing = [b for b in ("aplay", "arecord") if shutil.which(b) is None]
    if missing:
        return LegacyCheck("ALSA utils", LEGACY_MISSING, f"missing: {', '.join(missing)}")
    return LegacyCheck("ALSA utils", LEGACY_PASS, "aplay, arecord on PATH")


def _check_snd_aloop() -> list[LegacyCheck]:
    try:
        loaded = Path("/proc/modules").read_text()
        if "snd_aloop " not in loaded and not loaded.startswith("snd_aloop "):
            return [LegacyCheck("snd-aloop module", LEGACY_MISSING, "not loaded (modprobe snd-aloop)")]
    except Exception as exc:
        return [LegacyCheck("snd-aloop module", LEGACY_MISSING, f"could not read /proc/modules: {exc}")]
    results = [LegacyCheck("snd-aloop module", LEGACY_PASS, "loaded")]

    try:
        cards = Path("/proc/asound/cards").read_text()
    except Exception as exc:
        results.append(
            LegacyCheck("snd-aloop card layout", LEGACY_MISSING, f"could not read /proc/asound/cards: {exc}")
        )
        return results

    loopback_indices: set[int] = set()
    for line in cards.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        idx_str = line.split()[0]
        if "Loopback" in line:
            try:
                loopback_indices.add(int(idx_str))
            except ValueError:
                pass

    expected = {0, 3, 4}
    if loopback_indices == expected:
        results.append(
            LegacyCheck("snd-aloop card layout", LEGACY_PASS, f"3 instances at indices {sorted(loopback_indices)}")
        )
    elif len(loopback_indices) >= 1:
        results.append(
            LegacyCheck(
                "snd-aloop card layout",
                LEGACY_DEGRADED,
                f"found indices {sorted(loopback_indices)}, expected {sorted(expected)} "
                "(deploy/isadoraair-aloop.conf not installed, or a different layout in use)",
            )
        )
    else:
        results.append(LegacyCheck("snd-aloop card layout", LEGACY_MISSING, "no Loopback cards found"))
    return results


def _map_target_path(target_root: Path, canonical_path: Path) -> Path:
    if target_root == Path("/"):
        return canonical_path
    return target_root / canonical_path.relative_to("/")


def _check_directories(
    *, target_root: Path, project_root: Path, isa_user: str | None
) -> list[LegacyCheck]:
    results = []
    opt_root = _map_target_path(target_root, Path("/opt/isadoraair"))
    if opt_root.is_dir() and (opt_root / "manage.py").is_file():
        kind = "symlink" if opt_root.is_symlink() else "directory"
        results.append(LegacyCheck("/opt/isadoraair (canonical app root)", LEGACY_PASS, f"{kind}, manage.py present"))
    else:
        results.append(
            LegacyCheck("/opt/isadoraair (canonical app root)", LEGACY_MISSING, "missing, or manage.py not found under it")
        )

    canonical_library_root = Path(os.environ.get("LIBRARY_ROOT", "/srv/isadoraair/music"))
    library_root = _map_target_path(target_root, canonical_library_root)
    if library_root.is_dir():
        results.append(LegacyCheck("Library root", LEGACY_PASS, str(library_root)))
    else:
        results.append(LegacyCheck("Library root", LEGACY_MISSING, f"{library_root} does not exist"))

    run_dir = _map_target_path(target_root, Path("/run/isadoraair"))
    if run_dir.is_dir():
        results.append(LegacyCheck("/run/isadoraair", LEGACY_PASS, "present"))
    else:
        results.append(
            LegacyCheck(
                "/run/isadoraair",
                LEGACY_DEGRADED,
                "missing -- created by systemd-tmpfiles at boot (deploy/isadoraair-tmpfiles.conf); "
                "absence is normal if services haven't started yet",
            )
        )

    scratch_tmpfiles = _map_target_path(
        target_root, Path("/etc/tmpfiles.d/isadoraair.conf")
    )
    source_tmpfiles = project_root / "deploy" / "isadoraair-tmpfiles.conf"
    if not scratch_tmpfiles.is_file():
        results.append(
            LegacyCheck(
                "TTS scratch tmpfiles config",
                LEGACY_MISSING,
                f"{scratch_tmpfiles} is missing",
            )
        )
    elif isa_user is None:
        results.append(
            LegacyCheck(
                "TTS scratch tmpfiles config",
                LEGACY_DEGRADED,
                "present; content cannot be verified until --isa-user is supplied",
            )
        )
    else:
        try:
            expected = source_tmpfiles.read_text(encoding="utf-8").replace(
                "@@ISA_USER@@", isa_user
            )
            actual = scratch_tmpfiles.read_text(encoding="utf-8")
        except OSError as exc:
            results.append(
                LegacyCheck("TTS scratch tmpfiles config", LEGACY_MISSING, str(exc))
            )
        else:
            state = LEGACY_PASS if actual == expected else LEGACY_MISSING
            detail = "matches Git-owned authority" if state == LEGACY_PASS else "content mismatch"
            results.append(LegacyCheck("TTS scratch tmpfiles config", state, detail))
    return results


def legacy_checks(
    *, target_root: Path, project_root: Path, isa_user: str | None
) -> tuple[LegacyCheck, ...]:
    """Retained structural checks, with no live database access.

    Live executable/kernel checks are meaningful only for the booted root.
    An offline target gets target-mapped filesystem evidence instead of
    accidentally borrowing those capabilities from the installer host.
    """

    results: list[LegacyCheck] = []
    if target_root == Path("/"):
        results.extend((_check_python(), _check_postgres_tools()))
        results.extend(_check_gstreamer())
        results.append(_check_liquidsoap())
        results.append(_check_alsa_utils())
        results.extend(_check_snd_aloop())
    results.extend(
        _check_directories(
            target_root=target_root, project_root=project_root, isa_user=isa_user
        )
    )
    return tuple(results)


# ---- structural tier ---------------------------------------------------

@dataclass(frozen=True, slots=True)
class StructuralBaselineEvidence:
    legacy_checks: tuple[LegacyCheck, ...]
    package_prerequisites: tuple[PackagePrerequisiteEvidence, ...]
    system_surfaces: SystemSurfaceEvidence | None
    system_surfaces_error: str | None
    scratch_surface: ScratchSurfaceEvidence
    manifest_error: str | None = None

    @property
    def result(self) -> str:
        if self.manifest_error or self.system_surfaces_error:
            return RESULT_FAIL
        if any(c.state == LEGACY_MISSING for c in self.legacy_checks):
            return RESULT_FAIL
        if self.system_surfaces is not None and not self.system_surfaces.healthy:
            return RESULT_FAIL
        # BUILD-kind package prerequisites (e.g. fdkaac -> BUILD_HEAAC)
        # gate nothing here, deliberately -- autoconf/automake/libtool/
        # pkg-config only matter while actually BUILDING fdkaac. A
        # healthy, already-built canonical runtime must never be failed
        # by this check merely because build tooling isn't installed --
        # that would reintroduce the exact false-negative this baseline
        # consolidation exists to close. Only RUNTIME-kind prerequisites
        # (e.g. Kokoro -> OPTIONAL_KOKORO_TTS) gate the result; build-kind
        # evidence is still reported, just informationally.
        if any(
            p.status == PACKAGE_STATUS_FAIL for p in self.package_prerequisites if p.kind == "runtime"
        ):
            return RESULT_FAIL
        if self.scratch_surface.state not in (SCRATCH_STATE_HEALTHY, SCRATCH_STATE_UNRESOLVED_IDENTITY):
            return RESULT_FAIL
        if self.scratch_surface.state == SCRATCH_STATE_UNRESOLVED_IDENTITY:
            return RESULT_UNRESOLVED
        if any(
            p.status == PACKAGE_STATUS_UNRESOLVED and p.diagnostics
            for p in self.package_prerequisites
            if p.kind == "runtime"
        ):
            return RESULT_UNRESOLVED
        return RESULT_PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy_checks": [c.to_dict() for c in self.legacy_checks],
            "manifest_error": self.manifest_error,
            "package_prerequisites": [p.to_dict() for p in self.package_prerequisites],
            "result": self.result,
            "scratch_surface": self.scratch_surface.to_dict(),
            "system_surfaces": self.system_surfaces.to_dict() if self.system_surfaces else None,
            "system_surfaces_error": self.system_surfaces_error,
        }


def evaluate_structural_baseline(
    *,
    manifest: dict[str, Any] | None = None,
    target_root: str | Path = "/",
    project_root: str | Path | None = None,
    isa_user: str | None = None,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    include_legacy_checks: bool = True,
) -> StructuralBaselineEvidence:
    """Everything evaluable with no station database at all. Never
    raises for host/environment problems -- those become FAIL evidence
    entries instead, exactly like the rest of Foundation E's validators.
    """

    target_root = Path(target_root).absolute()
    active_project_root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent.parent
    try:
        active_manifest = manifest or load_runtime_components()
    except RuntimeComponentContractError as exc:
        return StructuralBaselineEvidence(
            legacy_checks=(),
            package_prerequisites=(),
            system_surfaces=None,
            system_surfaces_error=None,
            scratch_surface=evaluate_scratch_surface(
                isa_user=isa_user,
                target_root=target_root,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
            ),
            manifest_error=str(exc),
        )

    surfaces_evidence: SystemSurfaceEvidence | None = None
    surfaces_error: str | None = None
    try:
        manager = RuntimeSystemSurfaceManager(
            target_root=target_root,
            product_manifest=active_manifest,
            project_root=active_project_root,
        )
        surfaces_evidence = manager.current_evidence()
    except RuntimeProvisioningError as exc:
        surfaces_error = str(exc)

    # A mounted target is not a booted system: executing its packages or
    # borrowing installer-host dpkg state would both be misleading. The
    # package relationship/authority syntax has already been validated by
    # load_runtime_components(); installed-state evidence is collected only
    # for the live root.
    package_evidence = () if target_root != Path("/") else tuple(
        evaluate_package_prerequisite(
            active_manifest,
            name,
            kind=kind,
            required=None,
            reasons=("station configuration not inspected",),
            target_root=target_root,
        )
        for name, kind in (("kokoro", "runtime"), ("piper", "runtime"), ("fdkaac", "build"))
        if _component_declares_group(active_manifest, name, kind)
    )

    return StructuralBaselineEvidence(
        legacy_checks=(
            legacy_checks(
                target_root=target_root,
                project_root=active_project_root,
                isa_user=isa_user,
            )
            if include_legacy_checks
            else ()
        ),
        package_prerequisites=package_evidence,
        system_surfaces=surfaces_evidence,
        system_surfaces_error=surfaces_error,
        scratch_surface=evaluate_scratch_surface(
            isa_user=isa_user,
            target_root=target_root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        ),
        manifest_error=None,
    )


def _component_declares_group(manifest: dict[str, Any], name: str, kind: str) -> bool:
    from isadoraair.runtime_packages import component_package_group

    return component_package_group(manifest, name, kind=kind) is not None


# ---- full aggregate (structural + station) ------------------------------

@dataclass(frozen=True, slots=True)
class DeploymentBaselineEvidence:
    structural: StructuralBaselineEvidence
    station: RuntimeEvidence | None
    live_checks: tuple[LegacyCheck, ...] = ()
    station_package_prerequisites: tuple[PackagePrerequisiteEvidence, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @property
    def result(self) -> str:
        if self.structural.result == RESULT_FAIL:
            return RESULT_FAIL
        if any(check.state == LEGACY_MISSING for check in self.live_checks):
            return RESULT_FAIL
        if self.station is not None:
            if self.station.contract_errors:
                return RESULT_FAIL
            # Same build-vs-runtime distinction as StructuralBaselineEvidence
            # above -- BUILD_HEAAC never gates a healthy prebuilt fdkaac
            # runtime. This is the exact regression Runtime Foundation E6
            # exists to close (a healthy canonical E4 install, with no
            # pkg-config metadata published, must still baseline PASS).
            if any(
                p.status == PACKAGE_STATUS_FAIL
                for p in self.station_package_prerequisites
                if p.kind == "runtime"
            ):
                return RESULT_FAIL
            if self.station.requirement_errors:
                return RESULT_UNRESOLVED
            if self.station.result != "pass":
                return RESULT_FAIL
            if any(
                p.status == PACKAGE_STATUS_UNRESOLVED
                for p in self.station_package_prerequisites
                if p.kind == "runtime"
            ):
                return RESULT_UNRESOLVED
        if self.structural.result == RESULT_UNRESOLVED:
            return RESULT_UNRESOLVED
        return RESULT_PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "schema_version": self.schema_version,
            "live_checks": [check.to_dict() for check in self.live_checks],
            "station": self.station.to_dict() if self.station is not None else None,
            "station_package_prerequisites": [p.to_dict() for p in self.station_package_prerequisites],
            "structural": self.structural.to_dict(),
        }


def evaluate_deployment_baseline(
    *,
    manifest: dict[str, Any] | None = None,
    target_root: str | Path = "/",
    project_root: str | Path | None = None,
    isa_user: str | None = None,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    structural_only: bool = False,
    validator: RuntimeValidator | None = None,
) -> DeploymentBaselineEvidence:
    """The single aggregate entry point -- what
    ``manage.py check_deploy_baseline`` (and restore/installer tooling)
    should call. Read-only. Never provisions, installs a package, or
    repairs a system surface; only reports.
    """

    target_root = Path(target_root).absolute()
    structural = evaluate_structural_baseline(
        manifest=manifest,
        target_root=target_root,
        project_root=project_root,
        isa_user=isa_user,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    if structural_only or target_root != Path("/") or structural.manifest_error:
        return DeploymentBaselineEvidence(structural=structural, station=None)

    live_checks = (_check_postgres_connection(),)

    active_manifest = manifest or load_runtime_components()
    try:
        active_validator = validator or RuntimeValidator(manifest=active_manifest)
        station = validate_current_runtime(validator=active_validator)
    except RuntimeComponentContractError:
        station = None

    station_packages: tuple[PackagePrerequisiteEvidence, ...] = ()
    if station is not None:
        required_by_name = {
            name: component.required for name, component in _station_requirement_components(station)
        }
        # An unresolved station (requirement_errors non-empty) must not
        # silently read as "nothing required" -- every package
        # prerequisite that depends on station selection stays
        # UNRESOLVED right along with it, never a guessed PASS/OPTIONAL.
        unresolved_station = bool(station.requirement_errors)
        station_packages = tuple(
            evaluate_package_prerequisite(
                active_manifest,
                name,
                kind=kind,
                required=(None if unresolved_station else required_by_name.get(name, False)),
                reasons=(
                    ("station configuration could not be inspected",)
                    if unresolved_station
                    else _station_reasons(station, name)
                ),
                target_root=target_root,
            )
            for name, kind in (("kokoro", "runtime"), ("piper", "runtime"), ("fdkaac", "build"))
            if _component_declares_group(active_manifest, name, kind)
        )

    return DeploymentBaselineEvidence(
        structural=structural,
        station=station,
        live_checks=live_checks,
        station_package_prerequisites=station_packages,
    )


def _station_requirement_components(station: RuntimeEvidence):
    for name, evidence in station.components.items():
        yield name, evidence


def _station_reasons(station: RuntimeEvidence, name: str) -> tuple[str, ...]:
    component = station.components.get(name)
    return component.reasons if component is not None else ()
