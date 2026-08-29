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


def _copy_tree_verified(source_root: Path, destination_root: Path) -> None:
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
            _copy_regular(source_file, destination_file)


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
    components: dict[str, RecoveryComponentEvidence]
    piper_freshness: PiperFreshnessEvidence
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
        if self.piper_freshness.state == PIPER_FRESHNESS_STALE:
            return RESULT_FAIL
        return RESULT_PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": {name: self.components[name].to_dict() for name in sorted(self.components)},
            "manifest_error": self.manifest_error,
            "payload_id": self.payload_id,
            "piper_freshness": self.piper_freshness.to_dict(),
            "product_contract_match": self.product_contract_match,
            "result": self.result,
            "schema_version": self.schema_version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


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

    if manifest["schema_version"] != RECOVERY_SCHEMA_VERSION:
        raise RuntimeRecoveryError(f"runtime-recovery manifest schema_version must be {RECOVERY_SCHEMA_VERSION}")

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
    unknown_components = sorted(set(components) - {"tts", "native_fdkaac"})
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

    piper_digest = manifest.get("piper_selection_sha256")
    if piper_digest is not None and not isinstance(piper_digest, str):
        raise RuntimeRecoveryError("runtime-recovery manifest.piper_selection_sha256 must be a string")

    shell = _ManifestShell(
        payload_root=payload_root,
        payload_id=payload_id,
        product_contract_sha256=declared_product_hash,
        components=components,
        piper_selection_sha256=piper_digest,
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
    return bundle


def _load_native_component(shell: _ManifestShell, product_manifest: dict[str, Any]) -> NativeSourceEvidence:
    entry = shell.components["native_fdkaac"]
    native_root = shell.payload_root / entry["path"]
    evidence = verify_native_sources(native_root, product_manifest)
    _assert_strict_closure(native_root, {Path(archive.filename) for archive in evidence.archives})
    return evidence


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
    return RuntimeRecoveryPayload(
        root=shell.payload_root,
        payload_id=shell.payload_id,
        product_contract_sha256=shell.product_contract_sha256,
        tts_bundle=tts_bundle,
        native_source=native_source,
        piper_selection_digest=shell.piper_selection_sha256,
    )


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
            components={},
            piper_freshness=PiperFreshnessEvidence(checked=False, state=PIPER_FRESHNESS_NOT_CHECKED),
        )

    components: dict[str, RecoveryComponentEvidence] = {}
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
        components=components,
        piper_freshness=freshness,
    )


def validate_current_recovery_payload(
    root: str | Path, *, product_manifest: dict[str, Any] | None = None
) -> RuntimeRecoveryEvidence:
    """Convenience wrapper: resolves the LIVE station Piper selection
    (Runtime Foundation E1) and validates against it. Falls back to
    `not_checked` (never a guessed pass) if the station database cannot
    be inspected -- mirrors Runtime Foundation E6's own
    validate_current_runtime bootstrap-safe design."""

    try:
        digest = piper_selection_digest()
    except Exception:
        digest = None
    return validate_recovery_payload(
        root, product_manifest=product_manifest, current_piper_selection_digest=digest
    )


def _safe_message(value: object) -> str:
    return " ".join(str(value).split())[:512] or "recovery payload validation failed"


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

            manifest_dict = {
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "payload_id": resolved_payload_id,
                "product_contract_sha256": product_contract_digest(self.manifest),
                "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "components": components,
                "piper_selection_sha256": piper_selection_digest(resolved_piper_selection),
            }
            encoded = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
            _write_atomic(staging / RECOVERY_MANIFEST_FILENAME, encoded)

            # Re-validate the fully-staged payload before it becomes
            # visible at `output` -- proves the copy round-tripped
            # correctly, mirroring E3/E4's own post-publish verification.
            evidence = validate_recovery_payload(staging, product_manifest=self.manifest)
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

        final_evidence = validate_recovery_payload(resolved_output, product_manifest=self.manifest)
        return RuntimeRecoveryPreparationResult(
            payload_id=resolved_payload_id, output=str(resolved_output), evidence=final_evidence
        )
