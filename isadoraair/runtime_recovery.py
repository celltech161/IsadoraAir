"""Runtime Foundation E7A -- the durable, machine-readable disaster-recovery
runtime payload contract ("backup v3 runtime payload").

This module owns exactly one small, orchestration-only concern: a
self-contained container that can carry the immutable local material
Foundation E3 (offline Kokoro/Piper) and E4 (native fdkaac) provisioners
need to run completely offline, plus enough integrity/identity metadata
to prove -- read-only, without provisioning anything -- that the
container still matches this product's current runtime contract.

It deliberately does NOT re-implement anything E3/E4 already own:

  - the embedded TTS material is a real, ordinary Runtime Foundation E3
    bundle (isadoraair.runtime_bundle.load_runtime_bundle) -- this module
    never re-derives wheel/package/hash identity, it only points at an
    already-E3-valid bundle directory and re-validates it in place;
  - the embedded native material is exactly what
    isadoraair.runtime_native.verify_native_sources already knows how to
    check against the product manifest's own
    components.fdkaac.source_archives contract -- filenames, byte
    counts, and hashes stay owned there, never restated here;
  - "is this product contract" identity reuses
    isadoraair.runtime_bundle.product_contract_digest verbatim -- one
    product-contract digest function for the whole Foundation E family,
    not a second one;
  - "which Piper models does the station currently need" reuses
    isadoraair.runtime_requirements.resolve_current_runtime_requirements
    (E1) -- this module never invents its own station-selection query.

Historical-caller note (Runtime Foundation E1-E6 boundary): E1's own
station-requirement resolver only sees TTS demand that flows through
StationTTSVoice / WebRequestConfig.dedication_tts_voice_id /
RoadConditionsConfiguration.tts_voice_id. On THIS station today,
WebRequestConfig.enabled and RoadConditionsConfiguration.enabled are
both True, and both features synthesize via a hardcoded historical
Kokoro binary path (webrequests/services.py's and
road_conditions/synthesis.py's own KOKORO_BINARY constants) that never
consults StationTTSVoice at all -- so E1 currently resolves
kokoro.required=False even though this station operationally depends on
Kokoro right now. Because of this documented gap, Kokoro/native
inclusion in a recovery payload is deliberately OPERATOR-DECLARED here
(present because the operator supplied real material for it), never
gated on E1's `required` flag -- see build_recovery_payload's own
docstring. Piper has no equivalent gap (no hardcoded caller bypasses
StationTTSVoice for it anywhere in this codebase), so Piper staleness
detection safely DOES reuse E1's live resolution -- see
piper_selection_digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from isadoraair.runtime_bundle import (
    RuntimeBundle,
    RuntimeBundleError,
    load_runtime_bundle,
    product_contract_digest,
)
from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_native import NativeSourceEvidence, verify_native_sources
from isadoraair.runtime_provisioning import RuntimeProvisioningError
from isadoraair.runtime_requirements import (
    ComponentRequirement,
    RuntimeRequirements,
    resolve_current_runtime_requirements,
)


RECOVERY_MANIFEST_FILENAME = "runtime-recovery.json"
RECOVERY_SCHEMA_VERSION = 1
PHASE_D_RECOVERY_SCHEMA_VERSION = 2
PROTECTED_UPDATER_SUBDIR = "protected-updater"
TTS_BUNDLE_SUBDIR = "tts"
NATIVE_FDKAAC_SUBDIR = "native/fdkaac"
MAX_MANIFEST_BYTES = 1024 * 1024
_PAYLOAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

STATE_PRESENT = "present"
STATE_ABSENT = "absent"
STATE_INVALID = "invalid"

PIPER_FRESHNESS_CURRENT = "current"
PIPER_FRESHNESS_STALE = "stale"
PIPER_FRESHNESS_NOT_CHECKED = "not_checked"

RESULT_PASS = "pass"
RESULT_FAIL = "fail"


class RuntimeRecoveryError(ValueError):
    """A safe, operator-facing error in a runtime recovery payload --
    matches isadoraair.runtime_bundle.RuntimeBundleError's own plain
    ValueError pattern rather than coupling to a different module's
    exception hierarchy."""


class RecoveryPayloadNotConfiguredError(RuntimeRecoveryError):
    """Raised specifically when a persistent recovery-payload base
    root's `current` pointer does not exist at all -- the "this host
    has not adopted Runtime Foundation E7B yet" case, deliberately
    distinguished from every other failure (a configured-but-invalid/
    tampered/escaped pointer stays the base RuntimeRecoveryError).
    Callers (e.g. deploy/backup_isadoraair.sh, via
    validate_runtime_recovery_payload's exit code 2) may legitimately
    treat this one case as "not yet configured" rather than "broken" --
    never the reverse."""


# ---- filesystem safety primitives (mirrors runtime_provisioning.py's /
# ---- runtime_native.py's own established discipline; each Foundation E
# ---- module keeps its own copy rather than sharing a grab-bag utility
# ---- module -- see those modules' own equivalents). ----------------------

def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_no_symlink_ancestors(path: Path) -> None:
    cursor = path
    seen: list[Path] = []
    while True:
        seen.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for ancestor in reversed(seen):
        if ancestor.is_symlink():
            raise RuntimeRecoveryError(f"path has a symlinked ancestor: {ancestor}")


def _confined_relative_path(value: Any, location: str) -> str:
    """A manifest-declared component path must be a confined, relative,
    POSIX-shaped path -- never absolute, never containing `..` or a
    backslash. Mirrors isadoraair.runtime_bundle's own `_relative_path`
    confinement discipline; applied to every path a recovery manifest
    itself declares before it is ever joined onto payload_root."""

    if not isinstance(value, str) or not value.strip():
        raise RuntimeRecoveryError(f"{location} must be a non-empty string")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise RuntimeRecoveryError(f"{location} must be a confined relative path")
    return value


def _assert_existing_directory(path: Path, *, owner: int | None = None) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeRecoveryError(f"expected an existing non-symlink directory: {path}")
    if owner is not None and path.stat().st_uid != owner:
        raise RuntimeRecoveryError(f"directory is not owned by the caller: {path}")


def _write_atomic(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    temporary = path.parent / f".{path.name}.e7-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _copy_regular(source: Path, destination: Path, *, mode: int = 0o644) -> str:
    """Copy one real, non-symlink, non-hardlinked regular file, verified
    by re-reading what was actually written. Returns the SHA-256 of the
    copied content. Never follows a symlink at the source."""

    source_descriptor = -1
    destination_descriptor = -1
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_descriptor = os.open(
            source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise RuntimeRecoveryError(f"source is not a plain, single-link regular file: {source}")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(source_descriptor, "rb") as input_file, os.fdopen(
            destination_descriptor, "wb"
        ) as output_file:
            source_descriptor = -1
            destination_descriptor = -1
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(destination, mode)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeRecoveryError(f"could not copy {source}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    return digest.hexdigest()


def _copy_tree_verified(
    source_root: Path, destination_root: Path, *, preserve_modes: bool = False,
) -> None:
    """Copy an entire directory tree of plain regular files only --
    raises on the first symlink, non-regular file, or hardlink
    encountered anywhere in the source tree. Confined: every produced
    path is beneath destination_root."""

    for current, directories, filenames in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *filenames):
            if (current_path / name).is_symlink():
                raise RuntimeRecoveryError(
                    f"source tree contains a forbidden symlink: {(current_path / name)}"
                )
        for name in filenames:
            source_file = current_path / name
            relative = source_file.relative_to(source_root)
            destination_file = destination_root / relative
            mode = stat.S_IMODE(source_file.stat().st_mode) if preserve_modes else 0o644
            _copy_regular(source_file, destination_file, mode=mode)


def _normalize_payload_modes(root: Path) -> None:
    """Give a newly built payload the immutable-readable publication
    modes enforced by the persistent trust boundary."""

    os.chmod(root, 0o755)
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            os.chmod(current_path / name, 0o755)
        for name in filenames:
            os.chmod(current_path / name, 0o644)


def _normalize_directory_modes_only(root: Path) -> None:
    """The r0031 sibling of _normalize_payload_modes above -- normalizes
    every DIRECTORY under root to 0755 (the mode _assert_trusted_payload_tree/
    activate_recovery_payload require for every directory in an
    activatable payload tree), without touching any FILE's mode.
    _copy_tree_verified's own implicit `mkdir(parents=True, exist_ok=True)`
    directory creation depends on the calling process's umask, which is
    not guaranteed to produce 0755 -- unlike _normalize_payload_modes
    (used for the historical Foundation-E portion, whose files are
    deliberately flattened to a uniform 0644), attach_phase_d_recovery_
    component's protected-updater subtree must keep the EXACT file
    modes preserve_modes=True already copied in (0600 root-only
    config/state, 0755 executable entrypoints) -- only its directories
    need normalizing here."""

    os.chmod(root, 0o755)
    for current, directories, _filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            os.chmod(current_path / name, 0o755)


# ---- Piper station-selection digest (E1 reuse) ---------------------------

def piper_selection_digest(requirements: RuntimeRequirements | None = None) -> str:
    """Deterministic identity of exactly which Piper models the station
    currently requires, per Runtime Foundation E1's own resolver
    (isadoraair.runtime_requirements) -- never re-derived here. Piper has
    no historical-caller bypass (see this module's own docstring), so
    this digest is safe to use as an authoritative staleness signal,
    unlike Kokoro's `required` flag."""

    active = requirements or resolve_current_runtime_requirements()
    piper = active.components.get("piper")
    models = piper.piper_models if piper is not None else ()
    payload = sorted(
        (
            {
                "model_id": model.model_id,
                "model_sha256": model.model_sha256,
                "config_sha256": model.config_sha256,
            }
            for model in models
        ),
        key=lambda item: item["model_id"],
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def piper_bundle_selection_digest(bundle: RuntimeBundle) -> str:
    """Return the E1-compatible identity of the Piper models carried by
    an E3 bundle.  This is deliberately the same narrow identity used by
    :func:`piper_selection_digest`: model id plus model/config hashes.
    It lets recovery reject an internally valid but station-unrelated
    Piper bundle before publication."""

    component = bundle.components.get("piper")
    payload = [] if component is None else sorted(
        (
            {
                "model_id": model.model_id,
                "model_sha256": model.model.sha256,
                "config_sha256": model.config.sha256,
            }
            for model in component.piper_models.values()
        ),
        key=lambda item: item["model_id"],
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---- structured evidence -------------------------------------------------

@dataclass(frozen=True, slots=True)
class RecoveryComponentEvidence:
    name: str
    state: str
    path: str | None = None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnostics": list(self.diagnostics),
            "name": self.name,
            "path": self.path,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class PiperFreshnessEvidence:
    checked: bool
    state: str
    expected_digest: str | None = None
    observed_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "expected_digest": self.expected_digest,
            "observed_digest": self.observed_digest,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryEvidence:
    payload_id: str | None
    manifest_error: str | None
    product_contract_match: bool | None
    # The manifest's own declared digest -- present only once
    # product_contract_match is True (a payload that failed to load at
    # all, or whose declared digest didn't match, never exposes one).
    # Not a secret; recorded here so a caller (e.g. the backup script's
    # MANIFEST.txt) can cite it without a second file read.
    product_contract_sha256: str | None
    components: dict[str, RecoveryComponentEvidence]
    piper_freshness: PiperFreshnessEvidence
    # Which E3 components (kokoro/piper) the embedded tts bundle actually
    # carries -- () when tts is absent or invalid. Runtime Foundation E7B's
    # recovery-component policy (evaluate_recovery_policy) reads this to
    # answer "does this payload contain Kokoro/Piper", independent of
    # whether the top-level `tts` entry alone would say so.
    tts_components: tuple[str, ...] = ()
    schema_version: int = RECOVERY_SCHEMA_VERSION

    @property
    def result(self) -> str:
        if self.manifest_error:
            return RESULT_FAIL
        if self.product_contract_match is not True:
            return RESULT_FAIL
        if any(item.state == STATE_INVALID for item in self.components.values()):
            return RESULT_FAIL
        if not any(item.state == STATE_PRESENT for item in self.components.values()):
            return RESULT_FAIL
        if "piper" in self.tts_components and self.piper_freshness.state != PIPER_FRESHNESS_CURRENT:
            return RESULT_FAIL
        return RESULT_PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": {name: self.components[name].to_dict() for name in sorted(self.components)},
            "manifest_error": self.manifest_error,
            "payload_id": self.payload_id,
            "piper_freshness": self.piper_freshness.to_dict(),
            "product_contract_match": self.product_contract_match,
            "product_contract_sha256": self.product_contract_sha256,
            "result": self.result,
            "schema_version": self.schema_version,
            "tts_components": list(self.tts_components),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


# ---- recovery-component policy (Runtime Foundation E7B) ------------------
#
# E1's station-requirement resolver only sees TTS demand that flows
# through StationTTSVoice / WebRequestConfig.dedication_tts_voice_id /
# RoadConditionsConfiguration.tts_voice_id. On this station,
# WebRequestConfig.enabled and RoadConditionsConfiguration.enabled are
# both True, but both voice-id fields are None and there are zero
# StationTTSVoice rows -- yet both features already synthesize via the
# hardcoded historical Kokoro binary (webrequests/services.py's and
# road_conditions/synthesis.py's own KOKORO_BINARY constants), entirely
# bypassing StationTTSVoice. E1 therefore currently resolves
# kokoro.required=False even though Kokoro is operationally live here.
#
# This policy layer is deliberately independent of E1's `required` flag,
# generic (never a station-name literal), and explicit: an operator (or
# the backup script, via BACKUP_REQUIRED_RECOVERY_COMPONENTS) declares
# which component NAMES a recovery payload must positively contain --
# "kokoro"/"piper" (checked against the embedded E3 bundle's own
# component set) and/or "native_fdkaac" (checked against this payload's
# own top-level component). Nothing here infers policy from station
# configuration; it only checks payload evidence against an explicit,
# operator-supplied list.
RECOVERY_POLICY_COMPONENT_NAMES = frozenset({"kokoro", "piper", "native_fdkaac", "protected_updater"})


def parse_recovery_policy_components(value: str | None) -> frozenset[str]:
    """Parse the backup policy's strict comma-separated component list.

    An actually empty value means no policy.  Empty entries, surrounding
    whitespace, duplicates, and unknown names are configuration errors;
    none may silently weaken the operator's requested recovery contract.
    """

    if value is None or value == "":
        return frozenset()
    items = value.split(",")
    if any(not item or item != item.strip() or any(char.isspace() for char in item) for item in items):
        raise RuntimeRecoveryError(
            "recovery component policy must be a comma-separated list without empty or whitespace-containing entries"
        )
    duplicates = sorted({item for item in items if items.count(item) > 1})
    if duplicates:
        raise RuntimeRecoveryError(
            f"recovery component policy contains duplicate name(s): {', '.join(duplicates)}"
        )
    unknown = sorted(set(items) - RECOVERY_POLICY_COMPONENT_NAMES)
    if unknown:
        raise RuntimeRecoveryError(
            f"unknown recovery policy component name(s): {', '.join(unknown)}"
        )
    return frozenset(items)


@dataclass(frozen=True, slots=True)
class RecoveryPolicyEvidence:
    required: frozenset[str]
    missing: frozenset[str]

    @property
    def satisfied(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "missing": sorted(self.missing),
            "required": sorted(self.required),
            "satisfied": self.satisfied,
        }


def evaluate_recovery_policy(
    evidence: RuntimeRecoveryEvidence, required_components: frozenset[str] | set[str] | None
) -> RecoveryPolicyEvidence:
    """Pure, no-I/O check: does this ALREADY-COMPUTED evidence positively
    establish every operator-required component? An empty/None policy is
    trivially satisfied -- "not expected" components may remain absent
    (task's own "absence can remain optional" rule). `not_checked` Piper
    freshness is NEVER promoted to satisfied when Piper is
    policy-required -- an indeterminate DB-dependent check must fail
    closed, never advertise recoverability it could not positively
    confirm."""

    required = frozenset(required_components or ())
    unknown = required - RECOVERY_POLICY_COMPONENT_NAMES
    if unknown:
        raise RuntimeRecoveryError(
            f"unknown recovery policy component name(s): {', '.join(sorted(unknown))}"
        )
    missing: set[str] = set()
    for name in ("kokoro", "piper"):
        if name not in required:
            continue
        if evidence.components.get("tts") is None or evidence.components["tts"].state != STATE_PRESENT:
            missing.add(name)
            continue
        if name not in evidence.tts_components:
            missing.add(name)
            continue
        if name == "piper" and evidence.piper_freshness.state != PIPER_FRESHNESS_CURRENT:
            # Required AND present is not enough for Piper specifically --
            # policy-required Piper must be POSITIVELY confirmed current,
            # never left at STALE or (critically) NOT_CHECKED.
            missing.add(name)
    if "native_fdkaac" in required:
        component = evidence.components.get("native_fdkaac")
        if component is None or component.state != STATE_PRESENT:
            missing.add("native_fdkaac")
    if "protected_updater" in required:
        component = evidence.components.get("protected_updater")
        if component is None or component.state != STATE_PRESENT:
            missing.add("protected_updater")
    return RecoveryPolicyEvidence(required=required, missing=frozenset(missing))


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryPlan:
    action: str
    output: str
    includes_tts: bool
    includes_native: bool
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "errors": list(self.errors),
            "includes_native": self.includes_native,
            "includes_tts": self.includes_tts,
            "output": self.output,
            "ready": self.ready,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryPreparationResult:
    payload_id: str
    output: str
    evidence: RuntimeRecoveryEvidence

    def to_dict(self) -> dict[str, Any]:
        return {"evidence": self.evidence.to_dict(), "output": self.output, "payload_id": self.payload_id}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryPayload:
    root: Path
    payload_id: str
    product_contract_sha256: str
    tts_bundle: RuntimeBundle | None
    native_source: NativeSourceEvidence | None
    piper_selection_digest: str | None
    protected_updater_evidence: dict[str, Any] | None = None


# ---- manifest read/write --------------------------------------------------

def _default_payload_id() -> str:
    return "runtime-recovery-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / RECOVERY_MANIFEST_FILENAME
    try:
        manifest_mode = manifest_path.lstat().st_mode
        if manifest_path.is_symlink() or not stat.S_ISREG(manifest_mode):
            raise RuntimeRecoveryError(f"{RECOVERY_MANIFEST_FILENAME} must be a regular non-symlink file")
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise RuntimeRecoveryError(f"{RECOVERY_MANIFEST_FILENAME} is too large")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except RuntimeRecoveryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeRecoveryError(f"cannot load {RECOVERY_MANIFEST_FILENAME}") from exc
    if not isinstance(raw, dict):
        raise RuntimeRecoveryError("runtime-recovery manifest must be a JSON object")
    return raw


@dataclass(frozen=True, slots=True)
class _ManifestShell:
    """The top-level recovery manifest, parsed and structurally
    validated, but with neither the nested TTS bundle nor the native
    source directory loaded yet -- that per-component loading is
    deliberately split out so a caller (validate_recovery_payload) can
    attempt each component independently and still report the other
    component's own evidence when one of them is invalid."""

    payload_root: Path
    payload_id: str
    product_contract_sha256: str
    components: dict[str, dict[str, Any]]
    piper_selection_sha256: str | None
    schema_version: int


def _parse_manifest_shell(
    root: str | Path, product_manifest: dict[str, Any] | None
) -> tuple[_ManifestShell, dict[str, Any]]:
    """Payload-level structural validation only: existence, confinement,
    schema, payload_id, product-contract digest, and the shape of the
    components map. Never opens the nested TTS bundle or native source
    directory. Raises RuntimeRecoveryError on any problem -- this part
    of a payload is never "partially trusted"."""

    payload_root = Path(root)
    if not payload_root.is_absolute():
        payload_root = payload_root.absolute()
    if payload_root.is_symlink() or not payload_root.is_dir():
        raise RuntimeRecoveryError("recovery payload root must be an existing non-symlink directory")
    _assert_no_symlink_ancestors(payload_root)

    manifest = _read_manifest(payload_root)
    required = {"schema_version", "payload_id", "product_contract_sha256", "built_at", "components"}
    missing = sorted(required - manifest.keys())
    if missing:
        raise RuntimeRecoveryError(f"runtime-recovery manifest missing fields: {', '.join(missing)}")
    extra = sorted(manifest.keys() - required - {"piper_selection_sha256"})
    if extra:
        raise RuntimeRecoveryError(f"runtime-recovery manifest has unsupported fields: {', '.join(extra)}")

    schema_version = manifest["schema_version"]
    if schema_version not in {RECOVERY_SCHEMA_VERSION, PHASE_D_RECOVERY_SCHEMA_VERSION}:
        raise RuntimeRecoveryError(
            "runtime-recovery manifest schema_version must be 1 (historical) or 2 (Phase-D capable)"
        )

    payload_id = manifest["payload_id"]
    if not isinstance(payload_id, str) or not _PAYLOAD_ID_RE.fullmatch(payload_id):
        raise RuntimeRecoveryError("runtime-recovery manifest payload_id has an invalid stable identity")

    active_product_manifest = product_manifest or load_runtime_components()
    expected_product_hash = product_contract_digest(active_product_manifest)
    declared_product_hash = manifest["product_contract_sha256"]
    if declared_product_hash != expected_product_hash:
        raise RuntimeRecoveryError("recovery payload targets a different product runtime contract")

    components = manifest["components"]
    if not isinstance(components, dict) or not components:
        raise RuntimeRecoveryError("runtime-recovery manifest.components must be a non-empty object")
    if schema_version == PHASE_D_RECOVERY_SCHEMA_VERSION and "protected_updater" not in components:
        raise RuntimeRecoveryError(
            "runtime-recovery schema 2 must include the protected_updater component"
        )
    allowed_components = {"tts", "native_fdkaac"}
    if schema_version == PHASE_D_RECOVERY_SCHEMA_VERSION:
        allowed_components.add("protected_updater")
    unknown_components = sorted(set(components) - allowed_components)
    if unknown_components:
        raise RuntimeRecoveryError(f"runtime-recovery manifest has unsupported components: {', '.join(unknown_components)}")
    if "tts" in components:
        entry = components["tts"]
        if not isinstance(entry, dict) or set(entry) != {"path", "bundle_id", "manifest_sha256"}:
            raise RuntimeRecoveryError("runtime-recovery manifest.components.tts is malformed")
        _confined_relative_path(entry["path"], "components.tts.path")
    if "native_fdkaac" in components:
        entry = components["native_fdkaac"]
        if not isinstance(entry, dict) or set(entry) != {"path"}:
            raise RuntimeRecoveryError("runtime-recovery manifest.components.native_fdkaac is malformed")
        _confined_relative_path(entry["path"], "components.native_fdkaac.path")
    if "protected_updater" in components:
        entry = components["protected_updater"]
        if not isinstance(entry, dict) or set(entry) != {"path", "restore_manifest_sha256"}:
            raise RuntimeRecoveryError("runtime-recovery manifest.components.protected_updater is malformed")
        _confined_relative_path(entry["path"], "components.protected_updater.path")
        digest = entry["restore_manifest_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeRecoveryError("protected-updater restore manifest digest is invalid")

    piper_digest = manifest.get("piper_selection_sha256")
    if piper_digest is not None and not isinstance(piper_digest, str):
        raise RuntimeRecoveryError("runtime-recovery manifest.piper_selection_sha256 must be a string")

    shell = _ManifestShell(
        payload_root=payload_root,
        payload_id=payload_id,
        product_contract_sha256=declared_product_hash,
        components=components,
        piper_selection_sha256=piper_digest,
        schema_version=schema_version,
    )
    return shell, active_product_manifest


def _load_tts_component(shell: _ManifestShell, product_manifest: dict[str, Any]) -> RuntimeBundle:
    entry = shell.components["tts"]
    tts_root = shell.payload_root / entry["path"]
    bundle = load_runtime_bundle(tts_root, product_manifest)
    if bundle.bundle_id != entry["bundle_id"]:
        raise RuntimeRecoveryError("embedded TTS bundle_id does not match the recovery manifest")
    if bundle.manifest_sha256 != entry["manifest_sha256"]:
        raise RuntimeRecoveryError(
            "embedded TTS bundle's runtime-bundle.json was modified after the recovery payload was built"
        )
    if "piper" in bundle.components:
        if shell.piper_selection_sha256 is None:
            raise RuntimeRecoveryError("Piper recovery payload is missing station-selection identity")
        if piper_bundle_selection_digest(bundle) != shell.piper_selection_sha256:
            raise RuntimeRecoveryError(
                "embedded Piper model/config identity does not match the recovery manifest's station selection"
            )
    return bundle


def _load_native_component(shell: _ManifestShell, product_manifest: dict[str, Any]) -> NativeSourceEvidence:
    entry = shell.components["native_fdkaac"]
    native_root = shell.payload_root / entry["path"]
    evidence = verify_native_sources(native_root, product_manifest)
    _assert_strict_closure(native_root, {Path(archive.filename) for archive in evidence.archives})
    return evidence


def _load_protected_updater_component(shell: _ManifestShell) -> dict[str, Any]:
    from isadoraair.phase_d_recovery import MANIFEST_NAME, validate_phase_d_component

    entry = shell.components["protected_updater"]
    component_root = shell.payload_root / entry["path"]
    manifest_path = component_root / MANIFEST_NAME
    try:
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeRecoveryError("protected-updater restore manifest is unreadable") from exc
    if digest != entry["restore_manifest_sha256"]:
        raise RuntimeRecoveryError("protected-updater restore manifest was modified after payload assembly")
    try:
        return validate_phase_d_component(component_root)
    except ValueError as exc:
        raise RuntimeRecoveryError(f"protected-updater recovery validation failed: {exc}") from exc


def load_recovery_payload(
    root: str | Path, product_manifest: dict[str, Any] | None = None
) -> RuntimeRecoveryPayload:
    """Load, confine, and cross-check one immutable recovery payload.
    Raises RuntimeRecoveryError (or the underlying RuntimeBundleError /
    RuntimeProvisioningError) on any structural or integrity problem --
    never returns a partially-trusted result. Mirrors
    isadoraair.runtime_bundle.load_runtime_bundle's own contract. Use
    this for all-or-nothing loading (e.g. the builder's own post-copy
    verification); use validate_recovery_payload for per-component,
    never-raising structured evidence."""

    shell, active_product_manifest = _parse_manifest_shell(root, product_manifest)
    tts_bundle = _load_tts_component(shell, active_product_manifest) if "tts" in shell.components else None
    native_source = (
        _load_native_component(shell, active_product_manifest) if "native_fdkaac" in shell.components else None
    )
    protected_updater = (
        _load_protected_updater_component(shell) if "protected_updater" in shell.components else None
    )
    return RuntimeRecoveryPayload(
        root=shell.payload_root,
        payload_id=shell.payload_id,
        product_contract_sha256=shell.product_contract_sha256,
        tts_bundle=tts_bundle,
        native_source=native_source,
        piper_selection_digest=shell.piper_selection_sha256,
        protected_updater_evidence=protected_updater,
    )


def restore_protected_updater_component(
    root: str | Path, *, fake_root: str | Path, product_manifest: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Locate, integrity-check, and offline-restore one recovery
    payload's protected_updater component -- the restore-side sibling
    of _load_protected_updater_component (validate-only). Returns None
    if this payload declares no protected_updater component at all
    (the caller's own signal that there is nothing to restore, matching
    every other component's ABSENT state -- never an error on its own).

    Delegates the actual restore to isadoraair.phase_d_recovery.
    restore_phase_d_component, which alone owns the Phase-D trust/
    signature/descriptor verification and the offline, non-privileged,
    empty-fake-root materialization -- this function only resolves
    WHICH on-disk directory (inside the payload) is the component, the
    same restore-manifest-digest tamper check
    _load_protected_updater_component already performs, before handing
    off. Raises RuntimeRecoveryError on any structural or integrity
    problem, same contract as load_recovery_payload -- never returns a
    partially-trusted result."""

    from isadoraair.phase_d_recovery import MANIFEST_NAME, restore_phase_d_component

    shell, _ = _parse_manifest_shell(root, product_manifest)
    if "protected_updater" not in shell.components:
        return None
    entry = shell.components["protected_updater"]
    component_root = shell.payload_root / entry["path"]
    manifest_path = component_root / MANIFEST_NAME
    try:
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeRecoveryError("protected-updater restore manifest is unreadable") from exc
    if digest != entry["restore_manifest_sha256"]:
        raise RuntimeRecoveryError("protected-updater restore manifest was modified after payload assembly")
    try:
        return restore_phase_d_component(component_root=component_root, fake_root=Path(fake_root))
    except ValueError as exc:
        raise RuntimeRecoveryError(f"protected-updater recovery restore failed: {exc}") from exc


def _assert_strict_closure(root: Path, expected_relative: set[Path]) -> None:
    observed: set[Path] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *filenames):
            if (current_path / name).is_symlink():
                raise RuntimeRecoveryError(f"payload contains a forbidden symlink: {current_path / name}")
        for name in filenames:
            candidate = current_path / name
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeRecoveryError(f"payload contains a non-regular file: {candidate}")
            observed.add(candidate.relative_to(root))
    missing = sorted(str(p) for p in expected_relative - observed)
    extra = sorted(str(p) for p in observed - expected_relative)
    if missing:
        raise RuntimeRecoveryError(f"native source directory is missing declared file(s): {', '.join(missing)}")
    if extra:
        raise RuntimeRecoveryError(f"native source directory has undeclared file(s): {', '.join(extra)}")


# ---- read-only validation (never raises) ---------------------------------

def validate_recovery_payload(
    root: str | Path,
    *,
    product_manifest: dict[str, Any] | None = None,
    current_piper_selection_digest: str | None = None,
) -> RuntimeRecoveryEvidence:
    """Read-only, structured, fail-closed evidence for one recovery
    payload. Never raises for a bad/missing/tampered payload -- only for
    a genuinely unexpected internal error, matching every other
    Foundation E validator's contract. Pure/DB-independent: pass
    `current_piper_selection_digest` explicitly (e.g. from
    piper_selection_digest()) when live station context is available;
    omitted, Piper freshness is reported not_checked rather than
    guessed."""

    active_product_manifest = product_manifest or load_runtime_components()
    try:
        shell, active_product_manifest = _parse_manifest_shell(root, active_product_manifest)
    except (RuntimeRecoveryError, RuntimeBundleError, RuntimeProvisioningError) as exc:
        return RuntimeRecoveryEvidence(
            payload_id=None,
            manifest_error=_safe_message(exc),
            product_contract_match=None,
            product_contract_sha256=None,
            components={},
            piper_freshness=PiperFreshnessEvidence(checked=False, state=PIPER_FRESHNESS_NOT_CHECKED),
        )

    components: dict[str, RecoveryComponentEvidence] = {}
    tts_components: tuple[str, ...] = ()
    for name, loader in (("tts", _load_tts_component), ("native_fdkaac", _load_native_component)):
        if name not in shell.components:
            components[name] = RecoveryComponentEvidence(name=name, state=STATE_ABSENT)
            continue
        try:
            loaded = loader(shell, active_product_manifest)
        except (RuntimeRecoveryError, RuntimeBundleError, RuntimeProvisioningError) as exc:
            components[name] = RecoveryComponentEvidence(
                name=name, state=STATE_INVALID, diagnostics=(_safe_message(exc),)
            )
            continue
        path = str(loaded.root) if name == "tts" else loaded.source_dir
        components[name] = RecoveryComponentEvidence(name=name, state=STATE_PRESENT, path=path)
        if name == "tts":
            tts_components = tuple(sorted(loaded.components))
    if "protected_updater" not in shell.components:
        components["protected_updater"] = RecoveryComponentEvidence(
            name="protected_updater", state=STATE_ABSENT
        )
    else:
        try:
            protected_evidence = _load_protected_updater_component(shell)
        except RuntimeRecoveryError as exc:
            components["protected_updater"] = RecoveryComponentEvidence(
                name="protected_updater", state=STATE_INVALID, diagnostics=(_safe_message(exc),)
            )
        else:
            components["protected_updater"] = RecoveryComponentEvidence(
                name="protected_updater", state=STATE_PRESENT,
                path=str(shell.payload_root / shell.components["protected_updater"]["path"]),
                diagnostics=(
                    f"active_generation={protected_evidence['active_generation']}",
                    f"trust_threshold={protected_evidence['trust_threshold']}",
                ),
            )

    if current_piper_selection_digest is None:
        freshness = PiperFreshnessEvidence(checked=False, state=PIPER_FRESHNESS_NOT_CHECKED)
    else:
        stored = shell.piper_selection_sha256
        state = PIPER_FRESHNESS_CURRENT if stored == current_piper_selection_digest else PIPER_FRESHNESS_STALE
        freshness = PiperFreshnessEvidence(
            checked=True,
            state=state,
            expected_digest=current_piper_selection_digest,
            observed_digest=stored,
        )

    return RuntimeRecoveryEvidence(
        payload_id=shell.payload_id,
        manifest_error=None,
        product_contract_match=True,
        product_contract_sha256=shell.product_contract_sha256,
        components=components,
        piper_freshness=freshness,
        tts_components=tts_components,
        schema_version=shell.schema_version,
    )


def validate_current_recovery_payload(
    root: str | Path, *, product_manifest: dict[str, Any] | None = None
) -> RuntimeRecoveryEvidence:
    """Convenience wrapper: resolves the LIVE station Piper selection
    (Runtime Foundation E1) and validates against it. Falls back to
    `not_checked` (never a guessed pass) if the station database cannot
    be inspected -- mirrors Runtime Foundation E6's own
    validate_current_runtime bootstrap-safe design."""

    structural = validate_recovery_payload(root, product_manifest=product_manifest)
    if "piper" not in structural.tts_components:
        return structural
    try:
        digest = piper_selection_digest()
    except Exception:
        digest = None
    return validate_recovery_payload(
        root, product_manifest=product_manifest, current_piper_selection_digest=digest
    )


def _safe_message(value: object) -> str:
    return " ".join(str(value).split())[:512] or "recovery payload validation failed"


def attach_phase_d_recovery_component(
    *, existing_payload: str | Path, protected_updater_component: str | Path,
    output: str | Path, new_payload_id: str | None = None,
    product_manifest: dict[str, Any] | None = None,
) -> RuntimeRecoveryEvidence:
    """Assemble a schema-2 payload from a validated historical payload.

    This is the narrow compatibility bridge: schema-1 Foundation-E payloads
    remain valid forever, while a payload claiming Phase-D recovery upgrades
    explicitly to schema 2 and becomes fail-closed on the protected-updater
    component.  The source payload/component are never modified.

    `new_payload_id` (r0031): the manifest's own `payload_id` field is
    inherited verbatim from `existing_payload` when omitted (unchanged,
    default behavior -- every caller before r0031 gets exactly today's
    result). A caller that publishes the built payload under a NEW
    `payloads/<id>` directory basename (e.g.
    build_and_attach_installed_phase_d_payload below) should always pass
    the SAME string here, so the on-disk directory identity and the
    manifest's own reported payload_id (what
    validate_runtime_recovery_payload/backup-v3 metadata actually
    surfaces) can never silently diverge -- see
    activate_recovery_payload's own matching invariant, which refuses
    to activate a directory whose validated manifest payload_id
    disagrees with the requested directory name, so a caller that got
    this wrong here would fail closed there too, not just here.
    """

    from isadoraair.phase_d_recovery import MANIFEST_NAME, validate_phase_d_component

    source = Path(existing_payload).absolute()
    component = Path(protected_updater_component).absolute()
    destination = Path(output).absolute()
    if new_payload_id is not None and not _PAYLOAD_ID_RE.fullmatch(new_payload_id):
        raise RuntimeRecoveryError("new_payload_id has an invalid stable identity")
    active_product_manifest = product_manifest or load_runtime_components()
    source_evidence = validate_recovery_payload(source, product_manifest=active_product_manifest)
    if source_evidence.result != RESULT_PASS:
        raise RuntimeRecoveryError("existing recovery payload is not valid")
    try:
        validate_phase_d_component(component)
    except ValueError as exc:
        raise RuntimeRecoveryError(f"protected-updater component is not valid: {exc}") from exc
    if destination.exists():
        raise RuntimeRecoveryError(f"output already exists -- refusing to overwrite: {destination}")
    _assert_existing_directory(destination.parent, owner=os.geteuid())
    staging = destination.parent / f".{destination.name}.phase-d-building-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o755)
    try:
        _copy_tree_verified(source, staging)
        _normalize_payload_modes(staging)
        target_component = staging / PROTECTED_UPDATER_SUBDIR
        if target_component.exists():
            raise RuntimeRecoveryError("source payload already contains a protected-updater component")
        # Unlike historical Foundation-E payload material, the Phase-D
        # component has a signed/validated file-mode inventory: root-only
        # configuration and state remain 0600, while executable entrypoints
        # remain 0755. Preserve those modes exactly instead of flattening the
        # component to the historical payload-wide 0644 publication mode.
        _copy_tree_verified(component, target_component, preserve_modes=True)
        _normalize_directory_modes_only(target_component)
        manifest = _read_manifest(staging)
        manifest["schema_version"] = PHASE_D_RECOVERY_SCHEMA_VERSION
        if new_payload_id is not None:
            manifest["payload_id"] = new_payload_id
        manifest["components"]["protected_updater"] = {
            "path": PROTECTED_UPDATER_SUBDIR,
            "restore_manifest_sha256": hashlib.sha256(
                (target_component / MANIFEST_NAME).read_bytes()
            ).hexdigest(),
        }
        _write_atomic(
            staging / RECOVERY_MANIFEST_FILENAME,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        evidence = validate_recovery_payload(staging, product_manifest=active_product_manifest)
        if evidence.result != RESULT_PASS:
            raise RuntimeRecoveryError(
                f"schema-2 recovery payload failed validation: {evidence.to_json()}"
            )
        os.rename(staging, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return validate_recovery_payload(destination, product_manifest=active_product_manifest)


@dataclass(frozen=True, slots=True)
class InstalledPhaseDPublicationPlan:
    """Read-only report for `manage.py prepare_runtime_recovery_payload
    --plan --phase-d` -- everything an operator needs to see before
    building, with no filesystem write anywhere in its construction."""

    ready: bool
    errors: tuple[str, ...]
    base_root: str
    source_payload_id: str | None
    source_schema_version: int | None
    source_requires_refresh_derivation: bool
    new_payload_id: str
    output: str
    active_slot: str | None
    active_generation: int | None
    active_descriptor_sha256: str | None
    previous_slot: str | None
    previous_generation: int | None
    previous_descriptor_sha256: str | None
    activation_in_progress: bool
    trust_threshold: int | None
    public_signer_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "errors": list(self.errors),
            "base_root": self.base_root,
            "source_payload_id": self.source_payload_id,
            "source_schema_version": self.source_schema_version,
            "source_requires_refresh_derivation": self.source_requires_refresh_derivation,
            "new_payload_id": self.new_payload_id,
            "output": self.output,
            "active_slot": self.active_slot,
            "active_generation": self.active_generation,
            "active_descriptor_sha256": self.active_descriptor_sha256,
            "previous_slot": self.previous_slot,
            "previous_generation": self.previous_generation,
            "previous_descriptor_sha256": self.previous_descriptor_sha256,
            "activation_in_progress": self.activation_in_progress,
            "trust_threshold": self.trust_threshold,
            "public_signer_ids": list(self.public_signer_ids),
            # No private key material is ever read, held, or reported by
            # this plan or the apply it previews -- capture_phase_d_component
            # itself never touches a private key path at all (see
            # isadoraair/phase_d_recovery.py's own module docstring).
            "private_key_material_included": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def plan_installed_phase_d_publication(
    *, base_root: str | Path, new_payload_id: str | None = None,
    expected_owner_uid: int = 0, product_manifest: dict[str, Any] | None = None,
    enforce_root_ownership: bool = True,
    station_config_path: Path | None = None, bootstrap_config_path: Path | None = None,
) -> InstalledPhaseDPublicationPlan:
    """Entirely read-only -- never writes anything, including no
    scratch/temp directory. Resolves the same installed Phase-D state
    and current Foundation-E payload build_and_attach_installed_phase_d_payload
    (below) would use, and reports what an --apply would do, without
    doing it. `station_config_path`/`bootstrap_config_path` default to
    the real fixed constants (isadoraair.phase_d_recovery.STATION_CONFIG_PATH/
    BOOTSTRAP_CONFIG_PATH) when omitted -- overriding them is test-only,
    same as `enforce_root_ownership=False` -- see
    isadoraair.phase_d_recovery.load_installed_phase_d_state."""

    from isadoraair.phase_d_recovery import PhaseDRecoveryError, load_installed_phase_d_state

    resolved_base = Path(base_root)
    resolved_new_id = new_payload_id or f"phase-d-{_default_payload_id()}"
    errors: list[str] = []
    if new_payload_id is not None and not _PAYLOAD_ID_RE.fullmatch(new_payload_id):
        errors.append("new_payload_id has an invalid stable identity")

    source_payload_id = source_schema_version = None
    requires_refresh = False
    try:
        current_root = resolve_current_recovery_payload_root(resolved_base, expected_owner_uid=expected_owner_uid)
        current_evidence = validate_current_recovery_payload(current_root, product_manifest=product_manifest)
        if current_evidence.result != RESULT_PASS:
            errors.append(f"current recovery payload does not validate cleanly: {current_evidence.to_json()}")
        else:
            source_payload_id = current_evidence.payload_id
            source_schema_version = current_evidence.schema_version
            requires_refresh = current_evidence.schema_version == PHASE_D_RECOVERY_SCHEMA_VERSION
    except RuntimeRecoveryError as exc:
        errors.append(str(exc))

    active_slot = active_generation = active_descriptor = None
    previous_slot = previous_generation = previous_descriptor = None
    activation_in_progress = True  # fail closed if we never learn otherwise
    trust_threshold = None
    signer_ids: tuple[str, ...] = ()
    config_path_overrides: dict[str, Any] = {}
    if station_config_path is not None:
        config_path_overrides["station_config_path"] = station_config_path
    if bootstrap_config_path is not None:
        config_path_overrides["bootstrap_config_path"] = bootstrap_config_path
    try:
        installed = load_installed_phase_d_state(
            enforce_root_ownership=enforce_root_ownership, **config_path_overrides,
        )
        state = installed["runtime_state"]
        trust = installed["trust_policy"]
        active_slot = state.active_slot.value
        active_generation = state.active_generation
        active_descriptor = state.active_descriptor_sha256
        if state.previous_slot is not None:
            previous_slot = state.previous_slot.value
            previous_generation = state.previous_generation
            previous_descriptor = state.previous_descriptor_sha256
        activation_in_progress = state.activation is not None
        if activation_in_progress:
            errors.append("a protected-runtime activation is currently in progress -- refusing to capture")
        trust_threshold = trust.threshold
        signer_ids = tuple(sorted(signer.id for signer in trust.signers))
    except PhaseDRecoveryError as exc:
        errors.append(f"installed Phase-D state is unavailable: {exc}")

    return InstalledPhaseDPublicationPlan(
        ready=not errors,
        errors=tuple(errors),
        base_root=str(resolved_base),
        source_payload_id=source_payload_id,
        source_schema_version=source_schema_version,
        source_requires_refresh_derivation=requires_refresh,
        new_payload_id=resolved_new_id,
        output=str(resolved_base / PAYLOADS_SUBDIR / resolved_new_id),
        active_slot=active_slot, active_generation=active_generation,
        active_descriptor_sha256=active_descriptor,
        previous_slot=previous_slot, previous_generation=previous_generation,
        previous_descriptor_sha256=previous_descriptor,
        activation_in_progress=activation_in_progress,
        trust_threshold=trust_threshold, public_signer_ids=signer_ids,
    )


def build_and_attach_installed_phase_d_payload(
    *, base_root: str | Path, new_payload_id: str | None = None,
    expected_owner_uid: int = 0, product_manifest: dict[str, Any] | None = None,
    enforce_root_ownership: bool = True,
    station_config_path: Path | None = None, bootstrap_config_path: Path | None = None,
    bootstrap_root: Path | None = None, supervisor_service: Path | None = None,
    releases_dir: Path | None = None,
) -> RuntimeRecoveryEvidence:
    """The r0031 orchestration: capture this host's installed Phase-D
    state (observational only -- never modifies active/previous slots,
    runtime state, trust policy, or signer keys; never arms/disarms or
    starts/restarts anything; never creates a new protected-runtime
    generation) and attach it to the CURRENT Foundation-E recovery
    payload, publishing the result as a brand-new
    base_root/payloads/<new_payload_id> directory. Never activates it
    -- call activate_recovery_payload as a deliberate separate step.

    If the current payload is already schema 2 (a previous r0031-style
    refresh), a fresh schema-1 base is re-derived from ITS OWN embedded
    tts/native_fdkaac material first (attach_phase_d_recovery_component
    refuses to attach onto a source that already has a
    protected-updater component, by design) -- current's own tree is
    never read into, written into, or otherwise mutated either way.

    `enforce_root_ownership=False` and the five *_path/*_root/*_dir
    overrides all default to the real, fixed constants/enforcement and
    are test-only when overridden -- see
    isadoraair.phase_d_recovery.load_installed_phase_d_state and
    .resolve_installed_phase_d_capture_kwargs. releases_dir (r0034) is
    capture's one, capture-time-only dependency on the application's
    own git checkout, needed to synthesize each captured generation's
    binding.json -- see resolve_protected_runtime_binding."""

    from isadoraair.phase_d_recovery import (
        capture_phase_d_component, load_installed_phase_d_state,
        resolve_installed_phase_d_capture_kwargs,
    )

    resolved_base = Path(base_root)
    resolved_new_id = new_payload_id or f"phase-d-{_default_payload_id()}"
    if not _PAYLOAD_ID_RE.fullmatch(resolved_new_id):
        raise RuntimeRecoveryError("new_payload_id has an invalid stable identity")
    active_product_manifest = product_manifest or load_runtime_components()

    current_root = resolve_current_recovery_payload_root(resolved_base, expected_owner_uid=expected_owner_uid)
    current_evidence = validate_current_recovery_payload(current_root, product_manifest=active_product_manifest)
    if current_evidence.result != RESULT_PASS:
        raise RuntimeRecoveryError(f"current recovery payload does not validate cleanly: {current_evidence.to_json()}")

    output = resolved_base / PAYLOADS_SUBDIR / resolved_new_id

    config_path_overrides: dict[str, Any] = {}
    if station_config_path is not None:
        config_path_overrides["station_config_path"] = station_config_path
    if bootstrap_config_path is not None:
        config_path_overrides["bootstrap_config_path"] = bootstrap_config_path
    capture_input_overrides: dict[str, Any] = dict(config_path_overrides)
    if bootstrap_root is not None:
        capture_input_overrides["bootstrap_root"] = bootstrap_root
    if supervisor_service is not None:
        capture_input_overrides["supervisor_service"] = supervisor_service
    if releases_dir is not None:
        capture_input_overrides["releases_dir"] = releases_dir

    with tempfile.TemporaryDirectory(prefix="isadoraair-phase-d-publish-") as work_name:
        work = Path(work_name)

        installed = load_installed_phase_d_state(
            enforce_root_ownership=enforce_root_ownership, **config_path_overrides,
        )
        capture_kwargs = resolve_installed_phase_d_capture_kwargs(
            installed=installed, scratch_dir=work / "attestations", **capture_input_overrides,
        )
        captured_component = work / "captured-component"
        capture_phase_d_component(output=captured_component, **capture_kwargs)

        if current_evidence.schema_version == PHASE_D_RECOVERY_SCHEMA_VERSION:
            # Refresh case: current already carries a protected_updater
            # component -- re-derive a fresh schema-1 base from its OWN
            # already-validated tts/native_fdkaac material (never from
            # current's tree directly) rather than attach onto it.
            tts = current_evidence.components.get("tts")
            native = current_evidence.components.get("native_fdkaac")
            refreshed_base = RuntimeRecoveryBuilder(product_manifest=active_product_manifest).apply(
                tts_bundle=tts.path if tts and tts.state == STATE_PRESENT else None,
                native_source_dir=native.path if native and native.state == STATE_PRESENT else None,
                output=work / "refreshed-foundation-e-base",
                payload_id=f"{resolved_new_id}-foundation-e-base",
            )
            attach_source = refreshed_base.output
        else:
            attach_source = current_root

        return attach_phase_d_recovery_component(
            existing_payload=attach_source,
            protected_updater_component=captured_component,
            output=output,
            new_payload_id=resolved_new_id,
            product_manifest=active_product_manifest,
        )


# ---- preparation (plan / apply) ------------------------------------------

class RuntimeRecoveryBuilder:
    """Operator-facing plan/apply preparation of one runtime recovery
    payload from operator-SUPPLIED local material. Never fetches
    anything over the network, never mutates a canonical Foundation E
    path, never overwrites an existing payload. Mirrors the plan/apply
    shape Runtime Foundation E3/E4/E5 already established."""

    def __init__(self, *, product_manifest: dict[str, Any] | None = None) -> None:
        self.manifest = product_manifest or load_runtime_components()

    def _resolve_inputs(
        self,
        *,
        tts_bundle: str | Path | None,
        native_source_dir: str | Path | None,
        output: str | Path,
    ) -> tuple[Path | None, Path | None, Path]:
        if tts_bundle is None and native_source_dir is None:
            raise RuntimeRecoveryError(
                "at least one of tts_bundle or native_source_dir must be supplied"
            )
        resolved_tts = Path(tts_bundle).absolute() if tts_bundle is not None else None
        resolved_native = Path(native_source_dir).absolute() if native_source_dir is not None else None
        resolved_output = Path(output).absolute()
        return resolved_tts, resolved_native, resolved_output

    def plan(
        self,
        *,
        tts_bundle: str | Path | None = None,
        native_source_dir: str | Path | None = None,
        output: str | Path,
    ) -> RuntimeRecoveryPlan:
        """Entirely read-only. Never touches `output` beyond checking
        whether it already exists."""

        errors: list[str] = []
        try:
            resolved_tts, resolved_native, resolved_output = self._resolve_inputs(
                tts_bundle=tts_bundle, native_source_dir=native_source_dir, output=output
            )
        except RuntimeRecoveryError as exc:
            return RuntimeRecoveryPlan(
                action="blocked", output=str(output), includes_tts=False, includes_native=False,
                errors=(str(exc),),
            )

        if resolved_output.exists():
            errors.append(f"output already exists -- refusing to overwrite: {resolved_output}")

        if resolved_tts is not None:
            try:
                load_runtime_bundle(resolved_tts, self.manifest)
            except RuntimeBundleError as exc:
                errors.append(f"tts_bundle is not a valid Runtime Foundation E3 bundle: {exc}")

        if resolved_native is not None:
            try:
                verify_native_sources(resolved_native, self.manifest)
            except RuntimeProvisioningError as exc:
                errors.append(f"native_source_dir does not satisfy the fdkaac source contract: {exc}")

        return RuntimeRecoveryPlan(
            action="prepare" if not errors else "blocked",
            output=str(resolved_output),
            includes_tts=resolved_tts is not None,
            includes_native=resolved_native is not None,
            errors=tuple(errors),
        )

    def apply(
        self,
        *,
        tts_bundle: str | Path | None = None,
        native_source_dir: str | Path | None = None,
        output: str | Path,
        payload_id: str | None = None,
        piper_selection: RuntimeRequirements | None = None,
    ) -> RuntimeRecoveryPreparationResult:
        """Copies validated operator-supplied material into a brand-new
        output root. Builds under a same-parent temporary sibling and
        renames into place only after the copied payload re-validates
        cleanly -- a failed apply never leaves an apparently-valid
        payload at `output`."""

        plan = self.plan(tts_bundle=tts_bundle, native_source_dir=native_source_dir, output=output)
        if not plan.ready:
            raise RuntimeRecoveryError("; ".join(plan.errors))

        resolved_tts, resolved_native, resolved_output = self._resolve_inputs(
            tts_bundle=tts_bundle, native_source_dir=native_source_dir, output=output
        )
        _assert_no_symlink_ancestors(resolved_output)
        _assert_existing_directory(resolved_output.parent, owner=os.geteuid())

        staging = resolved_output.parent / f".{resolved_output.name}.e7-building-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o755)
        try:
            components: dict[str, Any] = {}
            staged_bundle: RuntimeBundle | None = None
            if resolved_tts is not None:
                tts_staged_root = staging / TTS_BUNDLE_SUBDIR
                _copy_tree_verified(resolved_tts, tts_staged_root)
                staged_bundle = load_runtime_bundle(tts_staged_root, self.manifest)
                components["tts"] = {
                    "path": TTS_BUNDLE_SUBDIR,
                    "bundle_id": staged_bundle.bundle_id,
                    "manifest_sha256": staged_bundle.manifest_sha256,
                }
            if resolved_native is not None:
                native_staged_root = staging / NATIVE_FDKAAC_SUBDIR
                _copy_tree_verified(resolved_native, native_staged_root)
                verify_native_sources(native_staged_root, self.manifest)
                components["native_fdkaac"] = {"path": NATIVE_FDKAAC_SUBDIR}

            resolved_payload_id = payload_id or _default_payload_id()
            if not _PAYLOAD_ID_RE.fullmatch(resolved_payload_id):
                raise RuntimeRecoveryError("payload_id has an invalid stable identity")

            # The Piper staleness digest only matters when the embedded
            # TTS bundle actually carries a `piper` component -- a
            # native-only or Kokoro-only payload has nothing for it to
            # track. Only in that one case, and only when the caller
            # didn't already supply an explicit piper_selection, do we
            # fall back to a live station-database query -- never as an
            # unconditional side effect of every apply() call (a
            # native-only payload must remain buildable with no station
            # database available at all).
            if piper_selection is not None:
                resolved_piper_selection = piper_selection
            elif staged_bundle is not None and "piper" in staged_bundle.components:
                resolved_piper_selection = None  # piper_selection_digest() resolves live
            else:
                resolved_piper_selection = RuntimeRequirements(
                    components={"piper": ComponentRequirement("piper")}
                )

            resolved_piper_digest = piper_selection_digest(resolved_piper_selection)
            if staged_bundle is not None and "piper" in staged_bundle.components:
                bundle_piper_digest = piper_bundle_selection_digest(staged_bundle)
                if bundle_piper_digest != resolved_piper_digest:
                    raise RuntimeRecoveryError(
                        "embedded Piper model/config identity does not match the selected station requirements"
                    )

            manifest_dict = {
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "payload_id": resolved_payload_id,
                "product_contract_sha256": product_contract_digest(self.manifest),
                "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "components": components,
                "piper_selection_sha256": resolved_piper_digest,
            }
            encoded = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
            _write_atomic(staging / RECOVERY_MANIFEST_FILENAME, encoded)
            _normalize_payload_modes(staging)

            # Re-validate the fully-staged payload before it becomes
            # visible at `output` -- proves the copy round-tripped
            # correctly, mirroring E3/E4's own post-publish verification.
            evidence = validate_recovery_payload(
                staging,
                product_manifest=self.manifest,
                current_piper_selection_digest=(
                    resolved_piper_digest
                    if staged_bundle is not None and "piper" in staged_bundle.components
                    else None
                ),
            )
            if evidence.result != RESULT_PASS:
                raise RuntimeRecoveryError(
                    f"newly-built recovery payload failed its own validation: {evidence.to_json()}"
                )

            if resolved_output.exists():
                raise RuntimeRecoveryError(f"output appeared during preparation: {resolved_output}")
            os.rename(staging, resolved_output)
            _fsync_directory(resolved_output.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        final_evidence = validate_recovery_payload(
            resolved_output,
            product_manifest=self.manifest,
            current_piper_selection_digest=(
                resolved_piper_digest
                if staged_bundle is not None and "piper" in staged_bundle.components
                else None
            ),
        )
        return RuntimeRecoveryPreparationResult(
            payload_id=resolved_payload_id, output=str(resolved_output), evidence=final_evidence
        )


# ---- persistent payload location (Runtime Foundation E7B) ----------------
#
# Convention (not established on any production host by this module --
# see docs/RUNTIME_BACKUP_PAYLOAD.md for the operator-facing writeup):
#
#   <base_root>/payloads/<payload-id>/     one immutable, RuntimeRecoveryBuilder-
#                                          built payload per directory; never
#                                          overwritten in place (apply() already
#                                          refuses an existing --output).
#   <base_root>/current -> payloads/<payload-id>
#                                          a single, explicit, atomically-swapped
#                                          pointer -- never a directory scan or
#                                          "pick newest by mtime" -- resolved by
#                                          resolve_current_recovery_payload_root
#                                          below, exactly one symlink hop,
#                                          confined to payloads/.
#
# Intended production ownership: base_root and everything under it
# root-owned, 0755 directories / 0644 files (matching Foundation E5's own
# /var/lib/isadoraair/tts convention) -- so the isadoraair service
# account (which every long-running production process actually runs
# as) can read but never write any of it. Only a deliberate, explicitly
# privileged prepare/activate run (this module's own API, run by an
# operator -- never a service) can ever create or repoint it. This
# module never creates base_root itself and never runs privileged on a
# caller's behalf; see prepare_runtime_recovery_payload's own docstring.
PAYLOADS_SUBDIR = "payloads"
CURRENT_POINTER_NAME = "current"


# r0035: protected-updater/ no longer needs the FULL six-value mode
# allowance r0031 originally introduced. Before r0035, capture preserved
# each protected config/state file's real installed mode (0600 for
# station.json/updater-bootstrap.json/runtime-state.json) straight into
# the payload -- that preservation is exactly what made the payload
# unreadable by the unprivileged backup process (isadoraair-backup.
# service runs as jreed, never root). capture_phase_d_component now
# stores those three files at the same uniform PHASE_D_STORAGE_MODE
# (0644) every other Foundation-E component already uses, recording
# each one's TRUE, deliberately-restrictive mode separately in the
# restore manifest's own restore_modes field (isadoraair.
# phase_d_recovery), reapplied explicitly at restore time -- never
# inferred from the payload's own on-disk mode.
#
# What remains: executable entrypoints (bootstrap/source/
# updater_bootstrapd.py, runtime-slots/{active,previous}/updaterd.py)
# are still deliberately 0755, not 0644 -- unrelated to the backup-
# readability problem (0755 is already world-readable) and independently,
# precisely cross-checked file-by-file against the descriptor's own
# signed mode field for the runtime-slots/ case
# (isadoraair_updater_bootstrap.descriptor.verify_descriptor_against_
# directory). This coarser tree-wide check only needs to accept the two
# values that legitimately occur now -- a real strengthening from the
# original six-value set (0600/0640/0700/0750 are no longer tolerated
# anywhere in the tree), not a loosening.
_PROTECTED_UPDATER_TRUSTED_FILE_MODES = frozenset({0o644, 0o755})


def _assert_trusted_mode_owner(
    path: Path, *, owner_uid: int, directory: bool, allowed_file_modes: frozenset[int] | None = None,
) -> None:
    metadata = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        kind = "directory" if directory else "regular file"
        raise RuntimeRecoveryError(f"trusted recovery path is not a {kind}: {path}")
    if metadata.st_uid != owner_uid:
        raise RuntimeRecoveryError(
            f"trusted recovery path has owner UID {metadata.st_uid}, expected {owner_uid}: {path}"
        )
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if directory:
        if actual_mode != 0o755:
            raise RuntimeRecoveryError(f"trusted recovery path has mode {actual_mode:04o}, expected 0755: {path}")
    else:
        allowed = allowed_file_modes or frozenset({0o644})
        if actual_mode not in allowed:
            expected_display = "/".join(f"{mode:04o}" for mode in sorted(allowed))
            raise RuntimeRecoveryError(
                f"trusted recovery path has mode {actual_mode:04o}, expected {expected_display}: {path}"
            )
    if not directory and metadata.st_nlink != 1:
        raise RuntimeRecoveryError(f"trusted recovery file is not single-link: {path}")


def _assert_trusted_payload_tree(root: Path, *, owner_uid: int) -> None:
    _assert_trusted_mode_owner(root, owner_uid=owner_uid, directory=True)
    protected_updater_root = root / PROTECTED_UPDATER_SUBDIR
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        under_protected_updater = current_path == protected_updater_root or protected_updater_root in current_path.parents
        for name in directories:
            candidate = current_path / name
            if candidate.is_symlink():
                raise RuntimeRecoveryError(f"trusted recovery tree contains a symlink: {candidate}")
            _assert_trusted_mode_owner(candidate, owner_uid=owner_uid, directory=True)
        for name in filenames:
            candidate = current_path / name
            if candidate.is_symlink():
                raise RuntimeRecoveryError(f"trusted recovery tree contains a symlink: {candidate}")
            _assert_trusted_mode_owner(
                candidate, owner_uid=owner_uid, directory=False,
                allowed_file_modes=_PROTECTED_UPDATER_TRUSTED_FILE_MODES if under_protected_updater else None,
            )


def resolve_current_recovery_payload_root(
    base_root: str | Path, *, expected_owner_uid: int = 0
) -> Path:
    """Safely resolve <base_root>/current to the payload directory it
    points at -- exactly one symlink hop, confined to
    <base_root>/payloads/, never a directory scan. Raises
    RuntimeRecoveryError for anything unsafe or absent: no pointer, the
    pointer is not a symlink, the pointer's target escapes
    <base_root>/payloads/, a symlinked ancestor anywhere in the chain,
    or a dangling target. Never validates the payload's OWN contents --
    call validate_recovery_payload on the result for that."""

    resolved_base = Path(base_root)
    if not resolved_base.is_absolute():
        resolved_base = resolved_base.absolute()
    _assert_no_symlink_ancestors(resolved_base)
    if resolved_base.is_symlink():
        raise RuntimeRecoveryError(f"recovery base root must be a non-symlink directory: {resolved_base}")
    if not resolved_base.exists():
        # Simply never set up on this host yet -- distinct from every
        # other failure mode below, all of which mean "configured, but
        # unsafe/broken."
        raise RecoveryPayloadNotConfiguredError(f"recovery base root does not exist: {resolved_base}")
    if not resolved_base.is_dir():
        raise RuntimeRecoveryError(f"recovery base root must be a directory: {resolved_base}")
    _assert_trusted_mode_owner(resolved_base, owner_uid=expected_owner_uid, directory=True)

    payloads_root = resolved_base / PAYLOADS_SUBDIR
    if payloads_root.is_symlink():
        raise RuntimeRecoveryError(f"recovery payloads directory must not be a symlink: {payloads_root}")
    if not payloads_root.exists():
        raise RecoveryPayloadNotConfiguredError(f"recovery payloads directory does not exist: {payloads_root}")
    _assert_trusted_mode_owner(payloads_root, owner_uid=expected_owner_uid, directory=True)

    pointer = resolved_base / CURRENT_POINTER_NAME
    try:
        pointer_stat = pointer.lstat()
    except OSError as exc:
        raise RecoveryPayloadNotConfiguredError(f"no current recovery payload is selected: {pointer}") from exc
    if not stat.S_ISLNK(pointer_stat.st_mode):
        raise RuntimeRecoveryError(f"current recovery payload pointer must be a symlink: {pointer}")
    if pointer_stat.st_uid != expected_owner_uid:
        raise RuntimeRecoveryError(
            f"current recovery payload pointer has owner UID {pointer_stat.st_uid}, expected {expected_owner_uid}: {pointer}"
        )

    # Exactly ONE symlink hop, read literally (os.readlink), never
    # Path.resolve() -- resolve() walks an UNBOUNDED chain of symlinks,
    # which would silently follow a symlink planted *inside* payloads/
    # too (e.g. current -> payloads/fake -> payloads/real) and defeat
    # the "immediate child must be a real directory" check below.
    raw_target = os.readlink(pointer)
    if os.path.isabs(raw_target) or "\x00" in raw_target:
        raise RuntimeRecoveryError(f"current recovery payload pointer must be a safe relative symlink: {pointer}")
    target = (resolved_base / raw_target).absolute()
    try:
        relative = target.relative_to(payloads_root)
    except ValueError:
        raise RuntimeRecoveryError(
            f"current recovery payload pointer escapes {payloads_root}: {pointer} -> {target}"
        ) from None
    if not relative.parts or ".." in relative.parts:
        raise RuntimeRecoveryError(f"current recovery payload pointer is malformed: {pointer} -> {target}")
    # The immediate child of payloads/ (the actual payload-id directory)
    # must itself be a real, non-symlink directory -- checked with
    # is_symlink()/is_dir(), neither of which follows further symlinks
    # past this one component, so a symlinked payload-id directory can
    # never be mistaken for a real, immutable one, and the pointer may
    # only ever name that directory directly (no deeper path segments).
    immediate = payloads_root / relative.parts[0]
    if immediate.is_symlink():
        raise RuntimeRecoveryError(f"recovery payload directory is an unexpected symlink: {immediate}")
    if not immediate.is_dir():
        raise RuntimeRecoveryError(f"current recovery payload target is not an existing directory: {immediate}")
    if len(relative.parts) != 1:
        raise RuntimeRecoveryError(f"current recovery payload pointer must name a direct child of {payloads_root}")
    _assert_trusted_payload_tree(immediate, owner_uid=expected_owner_uid)
    return immediate


def activate_recovery_payload(
    base_root: str | Path,
    payload_id: str,
    *,
    product_manifest: dict[str, Any] | None = None,
    expected_owner_uid: int = 0,
) -> Path:
    """Validate <base_root>/payloads/<payload_id> and, only if it
    validates cleanly, atomically repoint <base_root>/current at it.
    Never mutates the payload directory itself. Refuses a payload_id
    that isn't the exact directory basename already present (never
    creates one) -- this is a SELECTION action, not a build action;
    build with RuntimeRecoveryBuilder.apply() first."""

    resolved_base = Path(base_root)
    if not resolved_base.is_absolute():
        resolved_base = resolved_base.absolute()
    _assert_no_symlink_ancestors(resolved_base)
    payloads_root = resolved_base / PAYLOADS_SUBDIR
    _assert_trusted_mode_owner(resolved_base, owner_uid=expected_owner_uid, directory=True)
    _assert_trusted_mode_owner(payloads_root, owner_uid=expected_owner_uid, directory=True)

    if not _PAYLOAD_ID_RE.fullmatch(payload_id) or "/" in payload_id:
        raise RuntimeRecoveryError("payload_id has an invalid stable identity")
    target = payloads_root / payload_id
    if target.parent != payloads_root:
        raise RuntimeRecoveryError("payload_id must name a direct child of payloads/")
    _assert_trusted_payload_tree(target, owner_uid=expected_owner_uid)

    evidence = validate_current_recovery_payload(target, product_manifest=product_manifest)
    if evidence.result != RESULT_PASS:
        raise RuntimeRecoveryError(f"payload '{payload_id}' does not validate cleanly: {evidence.to_json()}")
    # r0031: the authoritative identity invariant. validate_runtime_recovery_payload
    # (and, from it, backup-v3's runtime-recovery-archive.json) reports
    # evidence.payload_id -- the MANIFEST's own internal field -- as
    # THE payload identity, not this directory's basename. Nothing
    # elsewhere cross-checks the two, so enforcing it exactly here, at
    # the one place a payload becomes `current`, is what makes "the
    # activated directory and the identity backup-v3 reports can never
    # diverge" true for every caller (this r0031 release included),
    # not just a convention a caller has to remember to uphold.
    # Existing, already-published payloads whose manifest payload_id
    # already equals their own payloads/<id> directory name (the
    # existing convention every RuntimeRecoveryBuilder.apply()-built
    # payload, including production's current e7c-real-acceptance-1,
    # already follows) are unaffected.
    if evidence.payload_id != payload_id:
        raise RuntimeRecoveryError(
            f"payload directory '{payload_id}' has manifest payload_id "
            f"'{evidence.payload_id}' -- refusing to activate a mismatched identity"
        )

    pointer = resolved_base / CURRENT_POINTER_NAME
    relative_target = os.path.relpath(target, resolved_base)
    temporary = resolved_base / f".{CURRENT_POINTER_NAME}.e7-{uuid.uuid4().hex}"
    try:
        os.symlink(relative_target, temporary)
        if temporary.lstat().st_uid != expected_owner_uid:
            raise RuntimeRecoveryError(
                f"temporary recovery pointer is not owned by expected UID {expected_owner_uid}: {temporary}"
            )
        os.replace(temporary, pointer)
        _fsync_directory(resolved_base)
    finally:
        if temporary.is_symlink():
            temporary.unlink(missing_ok=True)
    return target
