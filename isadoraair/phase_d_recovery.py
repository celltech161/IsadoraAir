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
import re
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

# r0034: protected-runtime generation 1 predates the protected_runtime
# release-manifest field entirely -- r0026 ("FINAL MANUAL UPDATER
# BOOTSTRAP", manual_bootstrap_required=true, see
# docs/UPDATE_CENTER_PHASE_D.md) has no protected_runtime block for
# resolve_protected_runtime_binding() to derive this from. Proven, not
# guessed: build_attestation_statement(release_id="r0026",
# previous_release_id="r0025", generation=1,
# descriptor_sha256=<this constant>) verifies against this host's own
# real, already-existing 00-primary-release.json signature under
# runtime-slots/.staging/attestations-A, checked against the real
# installed primary-release.pem -- a cryptographic round-trip against
# genuine evidence, not an assumption (r0034 discovery record). Fixed,
# never operator-configurable, for the same reason every other
# compiled constant in this module is: a config field naming this
# would let a compromised config file alone redirect what capture
# treats as generation 1's proven binding.
GENERATION_ONE_MANUAL_BOOTSTRAP_DESCRIPTOR_SHA256 = (
    "196c93c43eba5a07dfe1fcb023c3dfb18c4a908a259a6b8c99d4333fb140f1a1"
)
GENERATION_ONE_MANUAL_BOOTSTRAP_BINDING = {
    "release_id": "r0026", "previous_release_id": "r0025", "previous_generation": None,
}

# The application's own committed release-manifest directory -- capture's
# one, capture-time-only dependency on the git checkout (never read by
# validate_phase_d_component itself, which stays offline/self-contained;
# see resolve_protected_runtime_binding's own docstring).
DEFAULT_RELEASES_DIR = Path(__file__).resolve().parents[1] / "deploy" / "releases"

# r0035: the closed set of modes _copy_plain will ever accept for a
# SOURCE file it is capturing -- unchanged from before r0035, just
# promoted to a named constant so it can be reused (restore_modes
# validation) instead of re-typed as a literal.
TRUSTED_PHASE_D_SOURCE_MODES = frozenset({0o600, 0o640, 0o644, 0o700, 0o750, 0o755})

# r0035: the uniform mode every file captured into a Phase-D recovery
# component is now WRITTEN at inside the payload -- matching the exact
# mode every OTHER Foundation-E component already uses, and safely
# readable by the unprivileged backup process (isadoraair-backup.service
# runs as jreed, never root; see docs/RUNTIME_BACKUP_PAYLOAD.md's
# "Backup-readable storage vs. restored modes" note). None of the files
# this applies to contain a secret, credential, or private key --
# station.json/updater-bootstrap.json/runtime-state.json are root/config
# paths and bookkeeping, trust-policy.json/signer keys/descriptors/
# attestations are deliberately public trust evidence. A file's TRUE,
# deliberately-restrictive installed mode (when it has one -- currently
# only the three files above) is instead recorded in the restore
# manifest's own restore_modes field and re-applied explicitly at
# restore time (restore_phase_d_component), never inferred from
# whatever mode the payload copy itself happens to have.
PHASE_D_STORAGE_MODE = 0o644


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


def _copy_plain(source: Path, destination: Path, *, mode: int | None = None) -> str:
    """Copies one plain, single-link, non-symlink regular file.

    The SOURCE's own mode is always validated against
    TRUSTED_PHASE_D_SOURCE_MODES -- r0035: previously this check was
    silently skipped whenever a caller passed an explicit `mode=`
    override (the override's own value was checked instead, the
    source's real mode was never looked at) -- a latent gap no caller
    happened to trigger before r0035 introduced the first ones.
    Returns the source's own validated mode as a 4-digit octal string,
    regardless of whether `mode` overrides what gets WRITTEN -- the
    caller's one way to learn/record a file's true original mode when
    storing it at a different, backup-readable one (see
    PHASE_D_STORAGE_MODE and restore_modes)."""
    try:
        info = source.lstat()
    except OSError as exc:
        raise PhaseDRecoveryError(f"required recovery input is missing: {source}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PhaseDRecoveryError(f"recovery input is not a plain single-link file: {source}")
    source_mode = stat.S_IMODE(info.st_mode)
    if source_mode not in TRUSTED_PHASE_D_SOURCE_MODES:
        raise PhaseDRecoveryError(f"recovery input has an unsupported or writable mode: {source}")
    publication_mode = source_mode if mode is None else mode
    if publication_mode not in TRUSTED_PHASE_D_SOURCE_MODES:
        raise PhaseDRecoveryError(f"recovery input has an unsupported or writable destination mode: {destination}")
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
    return f"{source_mode:04o}"


def _copy_tree(source: Path, destination: Path, *, mode: int | None = None) -> None:
    """mode, when given, overrides every copied file's WRITTEN mode
    uniformly (r0035) -- used to normalize a source tree whose files
    carry no deliberate, documented mode contract of their own (e.g.
    descriptors/attestations staged by the real worker/supervisor
    under whatever process umask happened to apply -- see
    resolve_protected_runtime_binding's own docstring for why that
    staging is never mode-deliberate) into the same uniform,
    backup-readable storage convention every other Foundation-E
    component already uses. Leaves _copy_plain's own source-mode
    validation fully in effect either way."""
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
            _copy_plain(file_path, destination / relative, mode=mode)


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
    releases_dir: Path = DEFAULT_RELEASES_DIR,
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
        "releases_dir": Path(releases_dir),
    }


def resolve_protected_runtime_binding(
    *, generation: int, descriptor_sha256: str, releases_dir: Path,
) -> dict[str, Any]:
    """Derives {"release_id", "previous_release_id", "previous_generation"}
    -- the binding that was ORIGINALLY signed for one protected-runtime
    generation -- deterministically, from the committed release-manifest
    chain. Never fabricated, never read from installed runtime state
    (which durably records none of this: RuntimeState carries no
    release_id field, and REQUEST_ACTIVATION's release_id/
    previous_release_id are transient IPC-only values, never persisted
    -- see r0034's own discovery record).

    release_id/previous_release_id are cryptographically load-bearing:
    they are two of the four fields build_attestation_statement signs
    (with generation and descriptor_sha256). previous_generation is NOT
    part of the signed statement at all -- it exists only for
    generation_advances' separate, non-cryptographic monotonicity
    check, so an imprecise-but-chain-derived value here can never
    weaken signature verification itself, only that supplementary
    anti-rollback check.

    Reuses updatecenter.release_chain's own validated chain assembly
    (the same ordering manage.py validate_release_manifests itself
    depends on) rather than re-walking previous_release_id links here.

    Raises PhaseDRecoveryError if no committed release's protected_runtime
    field matches (generation, descriptor_sha256) -- for every
    generation except 1 (see GENERATION_ONE_MANUAL_BOOTSTRAP_BINDING's
    own docstring), this is a genuine, honestly-reported recovery-
    evidence gap, never silently guessed at."""
    from updatecenter import release_chain

    try:
        manifests = release_chain.load_manifest_files(Path(releases_dir))
        chain = release_chain.build_chain(manifests)
    except release_chain.ChainError as exc:
        raise PhaseDRecoveryError(f"release-manifest chain is invalid: {exc}") from exc

    protected_chain = [entry for entry in chain if entry.manifest.protected_runtime is not None]
    match_index = next(
        (index for index, entry in enumerate(protected_chain)
         if entry.manifest.protected_runtime.generation == generation),
        None,
    )
    if match_index is not None:
        match = protected_chain[match_index]
        if match.manifest.protected_runtime.descriptor_sha256 != descriptor_sha256:
            raise PhaseDRecoveryError(
                f"release {match.manifest.release_id} declares protected-runtime generation "
                f"{generation} with descriptor {match.manifest.protected_runtime.descriptor_sha256}, "
                f"but the installed descriptor is {descriptor_sha256} -- refusing to derive a binding "
                "for material that does not match its own claimed release"
            )
        predecessor = protected_chain[match_index - 1] if match_index > 0 else None
        return {
            "release_id": match.manifest.release_id,
            "previous_release_id": match.manifest.previous_release_id,
            "previous_generation": (
                predecessor.manifest.protected_runtime.generation if predecessor is not None else None
            ),
        }

    if generation == 1 and descriptor_sha256 == GENERATION_ONE_MANUAL_BOOTSTRAP_DESCRIPTOR_SHA256:
        return dict(GENERATION_ONE_MANUAL_BOOTSTRAP_BINDING)

    raise PhaseDRecoveryError(
        f"no committed release declares protected-runtime generation {generation} with descriptor "
        f"{descriptor_sha256} -- this generation's original attestation binding cannot be recovered "
        "from authoritative data"
    )


def capture_phase_d_component(
    *, output: Path, bootstrap_root: Path, supervisor_service: Path,
    slots_root: Path, runtime_state: Path, station_config: Path,
    bootstrap_config: Path, trust_policy: Path, signer_root: Path,
    descriptors_root: Path, attestations_root: Path, releases_dir: Path,
) -> dict:
    """Build one new component atomically from explicit local inputs.

    releases_dir is capture's one, capture-time-only dependency on the
    application's own git checkout (deploy/releases/) -- used solely
    to synthesize each captured generation's runtime-attestations/
    <role>/binding.json (r0034) via resolve_protected_runtime_binding.
    The real installed system persists no release_id/previous_release_id
    anywhere durable (see that function's own docstring), so capture is
    the ONE place that can recover and embed it; validate_phase_d_component
    itself never touches releases_dir and stays fully offline/self-
    contained, exactly as this module's own docstring requires."""

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

        # r0035: station.json/updater-bootstrap.json/runtime-state.json
        # are the only files with a deliberate, documented, restrictive
        # installed mode (0600 root:root) -- everything else in this
        # component is already public trust evidence or has its own
        # mode contract enforced elsewhere (descriptor.py's entrypoint
        # vs. plain-file split). Store them at the same uniform,
        # backup-readable PHASE_D_STORAGE_MODE every other file uses,
        # and record each one's TRUE source mode (_copy_plain's own
        # return value) as an explicit restore instruction -- never
        # inferred later from whatever mode the payload copy has.
        restore_modes: dict[str, str] = {}
        restore_modes["runtime-state.json"] = _copy_plain(
            Path(runtime_state), staging / "runtime-state.json", mode=PHASE_D_STORAGE_MODE,
        )
        restore_modes["station.json"] = _copy_plain(
            Path(station_config), staging / "station.json", mode=PHASE_D_STORAGE_MODE,
        )
        restore_modes["updater-bootstrap.json"] = _copy_plain(
            Path(bootstrap_config), staging / "updater-bootstrap.json", mode=PHASE_D_STORAGE_MODE,
        )
        _copy_plain(Path(trust_policy), staging / "trust-policy.json")
        _copy_tree(Path(signer_root), staging / "signer-public-keys")

        # r0035: descriptors_root is the real installed .staging/
        # directory, which ALSO holds attestations-<slot>/ subdirectories
        # (a separate, already-role-remapped copy of the same signature
        # evidence lives under attestations_root/runtime-attestations --
        # see resolve_installed_phase_d_capture_kwargs). Copying the
        # whole tree here (the pre-r0035 behavior) silently duplicated
        # that subtree into the payload, unused by anything that reads
        # runtime-descriptors/ (capture/validate only ever glob flat
        # *.json files here) and carrying whatever incidental mode the
        # real worker's own process umask happened to produce when it
        # staged them -- never a deliberate mode contract. Copy only
        # the flat descriptor-*.json files this component actually
        # uses, normalized to the uniform storage mode.
        for descriptor_path in sorted(Path(descriptors_root).glob("*.json")):
            _copy_plain(
                descriptor_path, staging / "runtime-descriptors" / descriptor_path.name,
                mode=PHASE_D_STORAGE_MODE,
            )
        # r0035: same incidental-umask reasoning for the per-signer
        # attestation files -- normalize to the uniform storage mode.
        _copy_tree(Path(attestations_root), staging / "runtime-attestations", mode=PHASE_D_STORAGE_MODE)

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

        # r0034: synthesize each present generation's own binding.json --
        # the real installed .staging/attestations-<slot>/ directory
        # never contains one (confirmed: the real worker/supervisor only
        # ever write/read per-signer signature files there). Derived
        # deterministically from the committed release chain, never
        # copied from anywhere that doesn't actually have it.
        active_binding = resolve_protected_runtime_binding(
            generation=state.active_generation,
            descriptor_sha256=state.active_descriptor_sha256,
            releases_dir=releases_dir,
        )
        active_binding_path = staging / "runtime-attestations" / "active" / "binding.json"
        active_binding_path.write_bytes(_canonical(active_binding))
        active_binding_path.chmod(0o644)
        if state.previous_slot is not None:
            previous_binding = resolve_protected_runtime_binding(
                generation=state.previous_generation,
                descriptor_sha256=state.previous_descriptor_sha256,
                releases_dir=releases_dir,
            )
            previous_binding_path = staging / "runtime-attestations" / "previous" / "binding.json"
            previous_binding_path.write_bytes(_canonical(previous_binding))
            previous_binding_path.chmod(0o644)

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
            "restore_modes": restore_modes,
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
    """Reusable backup-v3 and restore validator for one component.

    Always validates a portable, captured/staged recovery artifact --
    never live installed state -- so its embedded trust policy is
    parsed via parse_trust_policy_dict_for_recovery_artifact (r0033),
    not parse_trust_policy_dict. The component this function is handed
    legitimately still sits under ordinary, non-root-owned scratch
    space at every one of its call sites (capture_phase_d_component's
    post-capture self-check, attach_phase_d_recovery_component's
    pre-copy check, restore_phase_d_component's pre-materialization
    check, and backup-v3's own load-time inspection)."""

    _runtime_paths()
    from isadoraair_updater_bootstrap.descriptor import parse_descriptor_dict, verify_descriptor_against_directory
    from isadoraair_updater_bootstrap.state import parse_runtime_state_dict
    from isadoraair_updater_bootstrap.trust import parse_trust_policy_dict_for_recovery_artifact
    from isadoraair_updater_bootstrap.verification import verify_candidate_bundle

    component = Path(root)
    if component.is_symlink() or not component.is_dir():
        raise PhaseDRecoveryError("protected-updater recovery component is not a plain directory")
    manifest = _read_json(component / MANIFEST_NAME, label="Phase-D restore manifest")
    required = {
        "schema_version", "active", "previous", "bootstrap_protocol", "supervisor_service",
        "runtime_state_sha256", "trust_threshold", "public_signer_ids", "runtime_identities", "files",
        "restore_modes",
    }
    if set(manifest) != required or manifest["schema_version"] != SCHEMA_VERSION:
        raise PhaseDRecoveryError("Phase-D restore manifest has an unsupported schema")
    if manifest["supervisor_service"] != "updater-bootstrapd.service":
        raise PhaseDRecoveryError("Phase-D supervisor service identity is not recognized")
    # r0035: restore_modes records each protected config/state file's
    # TRUE, deliberately-restrictive installed mode -- what
    # restore_phase_d_component re-applies explicitly at
    # materialization time -- decoupled from PHASE_D_STORAGE_MODE, the
    # uniform, backup-readable mode the payload copy itself is stored
    # at. Validated here, at the one place every caller (capture's
    # self-check, attach, restore, backup-v3 inspection) always goes
    # through, so a malformed entry is caught before it could ever
    # reach restore's own mode-application.
    restore_modes = manifest["restore_modes"]
    if not isinstance(restore_modes, dict):
        raise PhaseDRecoveryError("Phase-D restore manifest restore_modes must be an object")
    known_paths = {entry["path"] for entry in manifest["files"]}
    for relative_path, raw_mode in restore_modes.items():
        if relative_path not in known_paths:
            raise PhaseDRecoveryError(f"restore_modes references a file absent from this component: {relative_path!r}")
        if not isinstance(raw_mode, str) or not re.fullmatch(r"0[0-7]{3}", raw_mode):
            raise PhaseDRecoveryError(f"restore_modes entry for {relative_path!r} is not a 4-digit octal string")
        if int(raw_mode, 8) not in TRUSTED_PHASE_D_SOURCE_MODES:
            raise PhaseDRecoveryError(f"restore_modes entry for {relative_path!r} is not a trusted mode: {raw_mode!r}")
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
    trust = parse_trust_policy_dict_for_recovery_artifact(
        rewritten, signer_directory=signer_directory.resolve(), label="recovered trust policy",
    )
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

        def restore_mode(relative_path: str) -> int | None:
            # r0035: restore_modes' shape/trusted-membership is already
            # validated (validate_phase_d_component, called above as
            # this function's own first statement) -- this only converts
            # the recorded octal string to an int. None (no entry) means
            # "no override" -- _copy_plain then preserves whatever mode
            # the payload copy itself has, its pre-r0035 default.
            raw = restore_manifest["restore_modes"].get(relative_path)
            return int(raw, 8) if raw is not None else None

        # Materialize the exact protected surfaces named by the validated
        # root-owned configuration. The payload stores slots by logical role
        # (active/previous); restore maps those roles back to their recorded
        # A/B identities and republishes each descriptor at the supervisor's
        # fixed staging convention so the real worker can verify itself.
        _copy_tree(payload / "bootstrap" / "source", staging / "usr/local/libexec/isadoraair-updater-bootstrap")
        service_name = _read_json(payload / MANIFEST_NAME, label="restore manifest")["supervisor_service"]
        _copy_plain(payload / "bootstrap" / service_name, staging / "etc/systemd/system" / service_name)
        _copy_plain(
            payload / "station.json", staging / "etc/isadoraair/station.json",
            mode=restore_mode("station.json"),
        )
        _copy_plain(
            payload / "updater-bootstrap.json", staging / "etc/isadoraair/updater-bootstrap.json",
            mode=restore_mode("updater-bootstrap.json"),
        )
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
        _copy_plain(
            payload / "runtime-state.json", restored(bootstrap.runtime_state_path),
            mode=restore_mode("runtime-state.json"),
        )
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
