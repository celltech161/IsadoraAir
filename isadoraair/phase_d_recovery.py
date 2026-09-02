"""Phase-D protected-updater component for the Foundation-E recovery payload.

The component is self-contained, offline and contains public trust evidence
only.  It never captures a private key, socket, mutable application checkout,
or database content.  Capture and restore are explicit-path APIs so tests can
use a fake root and production tooling can apply its own root boundary later.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any


SCHEMA_VERSION = 1
MANIFEST_NAME = "restore-manifest.json"
COMPONENT_DIRECTORY = "protected-updater"
MAX_FILES = 1024
MAX_BYTES = 128 * 1024 * 1024

# r0031: fixed, trusted constants for this station's real installed
# Phase-D bootstrap -- verified directly against the live supervisor
# (systemctl show updater-bootstrapd.service --property=ExecStart,
# FragmentPath). Never operator-configurable and never read from any
# config document: a config field naming these would let a compromised
# config file alone redirect what capture reads, the exact same
# reasoning isadoraair_updater_bootstrap.config's own module docstring
# already gives for why the supervisor's entrypoint path is a compiled
# constant, not a config field. See docs/RUNTIME_BACKUP_PAYLOAD.md's
# "Publishing a schema-2 Phase-D recovery payload" section.
STATION_CONFIG_PATH = Path("/etc/isadoraair/station.json")
BOOTSTRAP_CONFIG_PATH = Path("/etc/isadoraair/updater-bootstrap.json")
INSTALLED_BOOTSTRAP_SOURCE_ROOT = Path("/usr/local/libexec/isadoraair-updater-bootstrap")
INSTALLED_SUPERVISOR_SERVICE = Path("/etc/systemd/system/updater-bootstrapd.service")


class PhaseDRecoveryError(ValueError):
    pass


def _runtime_paths() -> None:
    import sys

    repository = Path(__file__).resolve().parents[1]
    for relative in ("deploy/updater_bootstrap", "deploy/updater_runtime"):
        candidate = str(repository / relative)
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _safe_relative(value: str) -> Path:
    candidate = PurePosixPath(value)
    if (
        not value or value.startswith("/") or "\\" in value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise PhaseDRecoveryError(f"unsafe recovery path: {value!r}")
    return Path(*candidate.parts)


def _copy_plain(source: Path, destination: Path, *, mode: int | None = None) -> None:
    try:
        info = source.lstat()
    except OSError as exc:
        raise PhaseDRecoveryError(f"required recovery input is missing: {source}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PhaseDRecoveryError(f"recovery input is not a plain single-link file: {source}")
    publication_mode = stat.S_IMODE(info.st_mode) if mode is None else mode
    if publication_mode not in {0o600, 0o640, 0o644, 0o700, 0o750, 0o755}:
        raise PhaseDRecoveryError(f"recovery input has an unsupported or writable mode: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            publication_mode,
        )
    except FileExistsError as exc:
        # Every caller of _copy_plain/_copy_tree relies on this refusing
        # rather than silently overwriting -- capture_phase_d_component/
        # restore_phase_d_component never expect to hit this (their own
        # destination is always freshly created immediately beforehand),
        # but publish_phase_d_component's whole safety contract depends
        # on it: a pre-existing file at the real/staging target must stop
        # the publish cold, with a clear error, not a raw OSError leaking
        # past every PhaseDRecoveryError handler upstream (management
        # command, restore stage).
        raise PhaseDRecoveryError(f"refusing to overwrite existing destination: {destination}") from exc
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            descriptor = -1
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise PhaseDRecoveryError(f"recovery tree input is not a non-symlink directory: {source}")
    for current, directories, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise PhaseDRecoveryError(f"recovery input contains symlink: {directory}")
        for name in filenames:
            file_path = current_path / name
            relative = file_path.relative_to(source)
            _copy_plain(file_path, destination / relative)


def _inventory(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    total = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise PhaseDRecoveryError(f"recovery component contains a symlink: {current_path / name}")
        for name in filenames:
            path = current_path / name
            if path.name == MANIFEST_NAME and path.parent == root:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise PhaseDRecoveryError(f"recovery component contains non-regular file: {path}")
            content = path.read_bytes()
            total += len(content)
            entries.append({
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            })
    if not entries or len(entries) > MAX_FILES or total > MAX_BYTES:
        raise PhaseDRecoveryError("protected-updater recovery inventory is empty or exceeds safety bounds")
    return sorted(entries, key=lambda entry: entry["path"])


def _read_json(path: Path, *, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhaseDRecoveryError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PhaseDRecoveryError(f"{label} must be an object")
    return value


def _attestation_assertions(directory: Path):
    _runtime_paths()
    from isadoraair_updater_bootstrap.trust import SignatureAssertion

    assertions = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "binding.json":
            continue
        value = _read_json(path, label=f"attestation {path.name}")
        if set(value) != {"schema_version", "signer_id", "signature_base64"} or value["schema_version"] != 1:
            raise PhaseDRecoveryError(f"attestation {path.name} has an invalid schema")
        try:
            signature = base64.b64decode(value["signature_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise PhaseDRecoveryError(f"attestation {path.name} has invalid base64") from exc
        assertions.append(SignatureAssertion(value["signer_id"], signature))
    return assertions


def load_installed_phase_d_state(
    *, station_config_path: Path = STATION_CONFIG_PATH,
    bootstrap_config_path: Path = BOOTSTRAP_CONFIG_PATH,
    enforce_root_ownership: bool = True,
) -> dict[str, Any]:
    """Read-only. Loads and validates this host's real, installed
    station and Phase-D bootstrap configuration, live runtime state,
    and trust policy -- exactly the sources capture_phase_d_component()'s
    own path arguments (slots_root, runtime_state, signer_root,
    trust_policy) are derived from, and everything an operator-facing
    plan report needs (active/previous generation+slot+descriptor
    identity, trust threshold/signer identities, whether an activation
    is currently in progress). Requires read access to both config
    paths (0600 root:root on a real station) -- raises
    PhaseDRecoveryError with a clear message if either is unreadable or
    invalid, never a partial/guessed report.

    Returns the parsed dataclass objects themselves (not just paths),
    so a caller (plan report, or resolve_installed_phase_d_capture_kwargs
    below) never re-parses the same documents twice:
      {"station_config": StationConfig, "bootstrap_config": BootstrapConfig,
       "runtime_state": RuntimeState, "trust_policy": TrustPolicy}

    `enforce_root_ownership=False` is the same "inactive under an
    unprivileged test process" convention isadoraair_updater_bootstrap.
    config.validate_config_dict's own parameter already establishes --
    kept explicit here, threaded through to BOTH the station and
    bootstrap config loaders, so a test can exercise this function
    end-to-end without running as root. Real production callers must
    never pass False."""

    _runtime_paths()
    from isadoraair_updater.config import ConfigError as StationConfigError
    from isadoraair_updater.config import load_config as load_station_config
    from isadoraair_updater_bootstrap.config import ConfigError as BootstrapConfigError
    from isadoraair_updater_bootstrap.config import validate_config_dict as validate_bootstrap_config
    from isadoraair_updater_bootstrap.state import StateError, parse_runtime_state_dict
    from isadoraair_updater_bootstrap.trust import TrustPolicyError, parse_trust_policy_dict

    try:
        station_config = load_station_config(
            Path(station_config_path), enforce_protection=enforce_root_ownership,
        )
    except StationConfigError as exc:
        raise PhaseDRecoveryError(f"installed station configuration is invalid or unreadable: {exc}") from exc

    bootstrap_raw = _read_json(Path(bootstrap_config_path), label="installed bootstrap configuration")
    try:
        bootstrap_config = validate_bootstrap_config(
            bootstrap_raw, application_root=station_config.application_root,
            enforce_root_ownership=enforce_root_ownership,
        )
    except BootstrapConfigError as exc:
        raise PhaseDRecoveryError(f"installed bootstrap configuration is invalid: {exc}") from exc

    state_raw = _read_json(bootstrap_config.runtime_state_path, label="installed runtime state")
    try:
        runtime_state = parse_runtime_state_dict(state_raw)
    except StateError as exc:
        raise PhaseDRecoveryError(f"installed runtime state is invalid: {exc}") from exc

    trust_raw = _read_json(bootstrap_config.trust_policy_path, label="installed trust policy")
    try:
        trust_policy = parse_trust_policy_dict(trust_raw, signer_directory=bootstrap_config.signer_root)
    except TrustPolicyError as exc:
        raise PhaseDRecoveryError(f"installed trust policy is invalid: {exc}") from exc

    return {
        "station_config": station_config,
        "bootstrap_config": bootstrap_config,
        "runtime_state": runtime_state,
        "trust_policy": trust_policy,
    }


def _prepare_attestations_root(
    *, slots_root: Path, active_slot: str, previous_slot: str | None, scratch_dir: Path,
) -> Path:
    """The one capture input that needs real preparation, not just a
    direct pointer: the real on-disk convention names attestation
    evidence by slot LETTER (slots_root/.staging/attestations-<A|B>,
    the same convention deploy/updater_bootstrap/isadoraair_updater_bootstrap/
    slots.py, deploy/updater_runtime/isadoraair_updater/runtime_handoff.py,
    and updaterd.py's own descriptor-staging convention already agree
    on), while capture_phase_d_component expects a tree pre-organized
    by ROLE (active/previous). This performs that one small, read-
    only-source remapping copy into a fresh scratch_dir -- never
    modifies slots_root/.staging itself. scratch_dir must not already
    exist."""

    destination = Path(scratch_dir)
    if destination.exists():
        raise PhaseDRecoveryError(f"attestations scratch directory must not already exist: {destination}")
    destination.mkdir(parents=True)
    staging_root = Path(slots_root) / ".staging"
    _copy_tree(staging_root / f"attestations-{active_slot}", destination / "active")
    if previous_slot is not None:
        _copy_tree(staging_root / f"attestations-{previous_slot}", destination / "previous")
    return destination


def resolve_installed_phase_d_capture_kwargs(
    *, installed: dict[str, Any], scratch_dir: Path,
    station_config_path: Path = STATION_CONFIG_PATH,
    bootstrap_config_path: Path = BOOTSTRAP_CONFIG_PATH,
    bootstrap_root: Path = INSTALLED_BOOTSTRAP_SOURCE_ROOT,
    supervisor_service: Path = INSTALLED_SUPERVISOR_SERVICE,
) -> dict[str, Path]:
    """Builds the exact kwargs capture_phase_d_component() needs, all
    derived from `installed` (the result of load_installed_phase_d_state)
    plus fixed trusted constants -- the operator never supplies a
    protected filesystem path. `scratch_dir` hosts the one prepared
    input (attestations, see _prepare_attestations_root above); it must
    not already exist, and the caller owns cleaning it up afterward
    (capture_phase_d_component only reads from it)."""

    bootstrap_config = installed["bootstrap_config"]
    runtime_state = installed["runtime_state"]
    attestations_root = _prepare_attestations_root(
        slots_root=bootstrap_config.slots_root,
        active_slot=runtime_state.active_slot.value,
        previous_slot=runtime_state.previous_slot.value if runtime_state.previous_slot else None,
        scratch_dir=Path(scratch_dir),
    )
    return {
        "bootstrap_root": Path(bootstrap_root),
        "supervisor_service": Path(supervisor_service),
        "slots_root": bootstrap_config.slots_root,
        "runtime_state": bootstrap_config.runtime_state_path,
        "station_config": Path(station_config_path),
        "bootstrap_config": Path(bootstrap_config_path),
        "trust_policy": bootstrap_config.trust_policy_path,
        "signer_root": bootstrap_config.signer_root,
        "descriptors_root": bootstrap_config.slots_root / ".staging",
        "attestations_root": attestations_root,
    }


def capture_phase_d_component(
    *, output: Path, bootstrap_root: Path, supervisor_service: Path,
    slots_root: Path, runtime_state: Path, station_config: Path,
    bootstrap_config: Path, trust_policy: Path, signer_root: Path,
    descriptors_root: Path, attestations_root: Path,
) -> dict:
    """Build one new component atomically from explicit local inputs."""

    _runtime_paths()
    from isadoraair_updater_bootstrap.descriptor import parse_descriptor_dict
    from isadoraair_updater_bootstrap.state import parse_runtime_state_dict

    destination = Path(output)
    if destination.exists():
        raise PhaseDRecoveryError(f"refusing to overwrite recovery component: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.building-", dir=destination.parent))
    try:
        _copy_tree(Path(bootstrap_root), staging / "bootstrap" / "source")
        service_name = Path(supervisor_service).name
        if service_name != "updater-bootstrapd.service":
            raise PhaseDRecoveryError("unexpected Phase-D supervisor service identity")
        _copy_plain(Path(supervisor_service), staging / "bootstrap" / service_name)
        state_value = _read_json(Path(runtime_state), label="runtime state")
        state = parse_runtime_state_dict(state_value)
        if state.activation is not None:
            raise PhaseDRecoveryError("cannot capture Phase-D recovery while an activation is in progress")

        for label, slot in (("active", state.active_slot.value), ("previous", state.previous_slot.value if state.previous_slot else None)):
            if slot is not None:
                _copy_tree(Path(slots_root) / slot, staging / "runtime-slots" / label)
        _copy_plain(Path(runtime_state), staging / "runtime-state.json")
        _copy_plain(Path(station_config), staging / "station.json")
        _copy_plain(Path(bootstrap_config), staging / "updater-bootstrap.json")
        _copy_plain(Path(trust_policy), staging / "trust-policy.json")
        _copy_tree(Path(signer_root), staging / "signer-public-keys")
        _copy_tree(Path(descriptors_root), staging / "runtime-descriptors")
        _copy_tree(Path(attestations_root), staging / "runtime-attestations")

        descriptors: dict[str, dict[str, Any]] = {}
        for descriptor_path in sorted((staging / "runtime-descriptors").glob("*.json")):
            raw = descriptor_path.read_bytes()
            descriptor = parse_descriptor_dict(json.loads(raw.decode("utf-8")), label=descriptor_path.name)
            descriptors[hashlib.sha256(raw).hexdigest()] = {
                "path": f"runtime-descriptors/{descriptor_path.name}",
                "generation": descriptor.generation,
                "runtime_version": descriptor.runtime_version,
                "manifest_protocol_version": descriptor.manifest_protocol_version,
                "supported_wire_protocols": list(descriptor.supported_wire_protocols),
            }
        for digest in filter(None, (state.active_descriptor_sha256, state.previous_descriptor_sha256)):
            if digest not in descriptors:
                raise PhaseDRecoveryError(f"runtime state descriptor {digest} is absent from recovery descriptors")

        trust_value = _read_json(staging / "trust-policy.json", label="trust policy")
        signer_ids = [entry.get("id") for entry in trust_value.get("signers", []) if isinstance(entry, dict)]
        if not signer_ids or not isinstance(trust_value.get("threshold"), int):
            raise PhaseDRecoveryError("trust policy lacks signer identities/threshold")

        inventory = _inventory(staging)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "active": {
                "slot": state.active_slot.value,
                "generation": state.active_generation,
                "descriptor_sha256": state.active_descriptor_sha256,
            },
            "previous": None if state.previous_slot is None else {
                "slot": state.previous_slot.value,
                "generation": state.previous_generation,
                "descriptor_sha256": state.previous_descriptor_sha256,
            },
            "bootstrap_protocol": _read_json(staging / "updater-bootstrap.json", label="bootstrap config").get("bootstrap_protocol_version"),
            "supervisor_service": service_name,
            "runtime_state_sha256": hashlib.sha256((staging / "runtime-state.json").read_bytes()).hexdigest(),
            "trust_threshold": trust_value["threshold"],
            "public_signer_ids": sorted(signer_ids),
            "runtime_identities": descriptors,
            "files": inventory,
        }
        (staging / MANIFEST_NAME).write_bytes(_canonical(manifest))
        (staging / MANIFEST_NAME).chmod(0o644)
        evidence = validate_phase_d_component(staging)
        os.rename(staging, destination)
        return evidence
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_phase_d_component(root: Path) -> dict:
    """Reusable backup-v3 and restore validator for one component."""

    _runtime_paths()
    from isadoraair_updater_bootstrap.descriptor import parse_descriptor_dict, verify_descriptor_against_directory
    from isadoraair_updater_bootstrap.state import parse_runtime_state_dict
    from isadoraair_updater_bootstrap.trust import parse_trust_policy_dict
    from isadoraair_updater_bootstrap.verification import verify_candidate_bundle

    component = Path(root)
    if component.is_symlink() or not component.is_dir():
        raise PhaseDRecoveryError("protected-updater recovery component is not a plain directory")
    manifest = _read_json(component / MANIFEST_NAME, label="Phase-D restore manifest")
    required = {
        "schema_version", "active", "previous", "bootstrap_protocol", "supervisor_service",
        "runtime_state_sha256", "trust_threshold", "public_signer_ids", "runtime_identities", "files",
    }
    if set(manifest) != required or manifest["schema_version"] != SCHEMA_VERSION:
        raise PhaseDRecoveryError("Phase-D restore manifest has an unsupported schema")
    if manifest["supervisor_service"] != "updater-bootstrapd.service":
        raise PhaseDRecoveryError("Phase-D supervisor service identity is not recognized")
    observed = _inventory(component)
    if observed != manifest["files"]:
        raise PhaseDRecoveryError("Phase-D recovery file inventory/hash/mode does not match restore manifest")
    state_bytes = (component / "runtime-state.json").read_bytes()
    if hashlib.sha256(state_bytes).hexdigest() != manifest["runtime_state_sha256"]:
        raise PhaseDRecoveryError("runtime-state digest does not match restore manifest")
    state = parse_runtime_state_dict(json.loads(state_bytes.decode("utf-8")))
    if state.activation is not None:
        raise PhaseDRecoveryError("recovery runtime state must not contain an in-flight activation")

    trust_raw = _read_json(component / "trust-policy.json", label="trust policy")
    signer_directory = component / "signer-public-keys"
    rewritten = json.loads(json.dumps(trust_raw))
    for signer in rewritten.get("signers", []):
        source_name = Path(signer.get("public_key_path", "")).name
        signer["public_key_path"] = str((signer_directory / source_name).resolve())
    trust = parse_trust_policy_dict(rewritten, signer_directory=signer_directory.resolve(), label="recovered trust policy")
    if trust.threshold != manifest["trust_threshold"] or sorted(s.id for s in trust.signers) != manifest["public_signer_ids"]:
        raise PhaseDRecoveryError("recovered trust authority differs from restore metadata")

    def verify_slot(label: str, generation: int, digest: str, previous_generation: int | None) -> dict:
        identity = manifest["runtime_identities"].get(digest)
        if identity is None or identity.get("generation") != generation:
            raise PhaseDRecoveryError(f"{label} descriptor identity is missing or inconsistent")
        descriptor_path = component / _safe_relative(identity["path"])
        descriptor_bytes = descriptor_path.read_bytes()
        descriptor = parse_descriptor_dict(json.loads(descriptor_bytes.decode("utf-8")), label=f"{label} descriptor")
        disk_failures = verify_descriptor_against_directory(descriptor, component / "runtime-slots" / label)
        if disk_failures:
            raise PhaseDRecoveryError(f"{label} slot does not match descriptor: {disk_failures!r}")
        assertions = _attestation_assertions(component / "runtime-attestations" / label)
        # Recovery attestations carry one binding file for each generation.
        binding = _read_json(component / "runtime-attestations" / label / "binding.json", label=f"{label} binding")
        assertions = [item for item in assertions if item.signer_id != ""]
        result = verify_candidate_bundle(
            release_id=binding["release_id"], previous_release_id=binding["previous_release_id"],
            previous_generation=previous_generation, descriptor_bytes=descriptor_bytes,
            bundle_root=component / "runtime-slots" / label, trust_policy=trust,
            assertions=assertions, current_bootstrap_protocol_version=manifest["bootstrap_protocol"],
            current_wire_protocol_version=descriptor.supported_wire_protocols[0],
            candidate_minimum_bootstrap_protocol_version=manifest["bootstrap_protocol"],
            require_policy_file="protected-policy.json",
        )
        if not result.ok:
            raise PhaseDRecoveryError(f"{label} runtime signature/inventory validation failed: {result.reasons!r}")
        return identity

    # Previous is validated as the first generation relative to its recorded
    # predecessor in the binding; active advances previous when present.
    previous_identity = None
    if manifest["previous"] is not None:
        previous = manifest["previous"]
        previous_binding = _read_json(component / "runtime-attestations" / "previous" / "binding.json", label="previous binding")
        previous_identity = verify_slot(
            "previous", previous["generation"], previous["descriptor_sha256"],
            previous_binding.get("previous_generation"),
        )
    active_previous_generation = manifest["previous"]["generation"] if manifest["previous"] else None
    active_identity = verify_slot(
        "active", manifest["active"]["generation"], manifest["active"]["descriptor_sha256"],
        active_previous_generation,
    )
    if state.active_generation != manifest["active"]["generation"] or state.active_descriptor_sha256 != manifest["active"]["descriptor_sha256"]:
        raise PhaseDRecoveryError("runtime-state active identity differs from restore metadata")
    if manifest["previous"] is None:
        if state.previous_generation is not None:
            raise PhaseDRecoveryError("runtime-state unexpectedly contains a previous generation")
    elif state.previous_generation != manifest["previous"]["generation"] or state.previous_descriptor_sha256 != manifest["previous"]["descriptor_sha256"]:
        raise PhaseDRecoveryError("runtime-state previous identity differs from restore metadata")

    return {
        "result": "pass",
        "active_generation": state.active_generation,
        "active_slot": state.active_slot.value,
        "active_descriptor_sha256": state.active_descriptor_sha256,
        "previous_generation": state.previous_generation,
        "bootstrap_protocol": manifest["bootstrap_protocol"],
        "runtime_version": active_identity["runtime_version"],
        "manifest_protocol_version": active_identity["manifest_protocol_version"],
        "supported_wire_protocols": active_identity["supported_wire_protocols"],
        "public_signer_ids": manifest["public_signer_ids"],
        "trust_threshold": manifest["trust_threshold"],
        "previous_identity": previous_identity,
    }


def restore_phase_d_component(*, component_root: Path, fake_root: Path) -> dict:
    """Offline, non-privileged restore into an empty fake root."""

    evidence = validate_phase_d_component(component_root)
    _runtime_paths()
    from isadoraair_updater.config import validate_config_dict as validate_station_config
    from isadoraair_updater_bootstrap.config import validate_config_dict as validate_bootstrap_config
    from isadoraair_updater_bootstrap.state import parse_runtime_state_dict

    destination = Path(fake_root)
    if destination.exists():
        raise PhaseDRecoveryError("fake restore root must not already exist")
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    try:
        _copy_tree(Path(component_root), staging / "var/lib/isadoraair/recovery/protected-updater")
        payload = staging / "var/lib/isadoraair/recovery/protected-updater"
        station_value = _read_json(payload / "station.json", label="station config")
        station = validate_station_config(station_value, allow_local_repository=True)
        bootstrap_value = _read_json(payload / "updater-bootstrap.json", label="bootstrap config")
        bootstrap = validate_bootstrap_config(
            bootstrap_value,
            application_root=station.application_root,
            enforce_root_ownership=False,
        )
        state = parse_runtime_state_dict(_read_json(payload / "runtime-state.json", label="runtime state"))
        restore_manifest = _read_json(payload / MANIFEST_NAME, label="restore manifest")

        def restored(path: Path) -> Path:
            path = Path(path)
            if not path.is_absolute() or ".." in path.parts:
                raise PhaseDRecoveryError(f"restore destination is not a safe absolute path: {path}")
            return staging.joinpath(*path.parts[1:])

        # Materialize the exact protected surfaces named by the validated
        # root-owned configuration. The payload stores slots by logical role
        # (active/previous); restore maps those roles back to their recorded
        # A/B identities and republishes each descriptor at the supervisor's
        # fixed staging convention so the real worker can verify itself.
        _copy_tree(payload / "bootstrap" / "source", staging / "usr/local/libexec/isadoraair-updater-bootstrap")
        service_name = _read_json(payload / MANIFEST_NAME, label="restore manifest")["supervisor_service"]
        _copy_plain(payload / "bootstrap" / service_name, staging / "etc/systemd/system" / service_name)
        _copy_plain(payload / "station.json", staging / "etc/isadoraair/station.json")
        _copy_plain(payload / "updater-bootstrap.json", staging / "etc/isadoraair/updater-bootstrap.json")
        _copy_plain(payload / "trust-policy.json", restored(bootstrap.trust_policy_path))
        _copy_tree(payload / "signer-public-keys", restored(bootstrap.signer_root))

        slot_mappings = [("active", state.active_slot, state.active_descriptor_sha256)]
        if state.previous_slot is not None:
            slot_mappings.append(("previous", state.previous_slot, state.previous_descriptor_sha256))
        for label, slot, descriptor_digest in slot_mappings:
            _copy_tree(payload / "runtime-slots" / label, restored(bootstrap.slots_root / slot.value))
            identity = restore_manifest["runtime_identities"].get(descriptor_digest)
            if identity is None:
                raise PhaseDRecoveryError(f"{label} descriptor identity is absent from restore manifest")
            descriptor_source = payload / _safe_relative(identity["path"])
            descriptor_destination = restored(
                bootstrap.slots_root / ".staging" / f"descriptor-{slot.value}.json"
            )
            _copy_plain(descriptor_source, descriptor_destination)
        _copy_plain(payload / "runtime-state.json", restored(bootstrap.runtime_state_path))
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # This unprivileged restore proves materialization and cryptographic
    # identity only.  The real worker deliberately refuses non-root startup
    # and root-owned ancestry cannot be represented honestly in a user-owned
    # fake root.  Keep the result explicit so recovery evidence cannot be
    # mistaken for the later privileged DISARMED/readiness acceptance.
    return {
        **evidence,
        "restore_root": str(destination),
        "network_used": False,
        "worker_started": False,
        "readiness": "not-run",
    }


def publish_phase_d_component(*, fake_root: Path, target_root: Path) -> None:
    """Materialize an already-restored fake-root's tree onto the real
    (or staging-mirrored) filesystem root, so a disaster-recovery
    receipt recording this component means what it means for every
    other runtime-recovery component: genuinely present at the restore
    target, not merely proven reconstructable in a throwaway directory.

    Reuses _copy_tree -- the SAME mode-preserving, symlink-rejecting,
    exclusive-create primitive restore_phase_d_component itself is
    built from -- so this call inherits its safety properties for free:
    it refuses (raises PhaseDRecoveryError, via the underlying
    FileExistsError from _copy_plain's os.O_EXCL) rather than silently
    overwriting ANY file that already exists at its destination under
    target_root. A partial prior restore attempt, or files installed
    for an unrelated reason, must be dealt with explicitly by the
    caller/operator -- this function never guesses which pre-existing
    content is safe to clobber.

    Deliberately does not chown anything -- ownership of newly-created
    files/directories falls out of whichever effective UID this process
    runs as (root, when the caller invokes this under sudo for a real
    target -- see deploy/restore/75-protected-updater.sh's own
    staging/real sudo distinction, matching deploy/restore/
    90-system-config.sh's established USE_SUDO idiom). Never starts,
    enables, or reloads anything -- activation of the restored generation
    remains a deliberate, separate, privileged step outside this
    function's (and this whole restore stage's) scope."""

    _copy_tree(Path(fake_root), Path(target_root))
