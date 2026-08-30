"""D2-E: strict supervisor-owned runtime state, and D2-F: its durable
atomic writer. Only the fields actually needed to recover slot
activation safely -- no arbitrary job/release metadata copied in here
(that lives in the Django-side UpdateJob record, an entirely different,
D3-owned concern)."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .activation import ActivationPhase
from .descriptor import generation_advances
from .slots import Slot

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StateError(ValueError):
    """Raised for any structurally- or logically-invalid state document
    -- ambiguous/inconsistent state fails closed, it is never
    best-effort-interpreted (see this module's own docstring intent and
    D2-E's explicit "invalid or ambiguous state must fail closed")."""


@dataclasses.dataclass(frozen=True)
class ActivationTransaction:
    transaction_id: str
    candidate_slot: Slot
    candidate_generation: int
    candidate_descriptor_sha256: str
    phase: ActivationPhase

    def to_dict(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "candidate_slot": self.candidate_slot.value,
            "candidate_generation": self.candidate_generation,
            "candidate_descriptor_sha256": self.candidate_descriptor_sha256,
            "phase": self.phase.value,
        }


@dataclasses.dataclass(frozen=True)
class RuntimeState:
    schema_version: int
    active_slot: Slot
    active_generation: int
    active_descriptor_sha256: str
    previous_slot: Slot | None
    previous_generation: int | None
    previous_descriptor_sha256: str | None
    activation: ActivationTransaction | None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "active_slot": self.active_slot.value,
            "active_generation": self.active_generation,
            "active_descriptor_sha256": self.active_descriptor_sha256,
            "previous_slot": self.previous_slot.value if self.previous_slot else None,
            "previous_generation": self.previous_generation,
            "previous_descriptor_sha256": self.previous_descriptor_sha256,
            "activation": self.activation.to_dict() if self.activation else None,
        }


def _require_sha(value, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.match(value):
        raise StateError(f"{field}: must be exactly 64 lowercase hex characters")
    return value


def _require_slot(value, field: str) -> Slot:
    if value not in (Slot.A.value, Slot.B.value):
        raise StateError(f"{field}: must be exactly 'A' or 'B'")
    return Slot(value)


def _require_positive_int(value, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StateError(f"{field}: must be a positive integer")
    return value


def _require_uuid(value, field: str) -> str:
    if not isinstance(value, str):
        raise StateError(f"{field}: must be a string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise StateError(f"{field}: must be a valid UUID") from exc
    if str(parsed) != value:
        raise StateError(f"{field}: must be a canonical lowercase UUID string")
    return value


def _parse_activation(value) -> ActivationTransaction | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StateError("activation: must be an object or null")
    known = {"transaction_id", "candidate_slot", "candidate_generation", "candidate_descriptor_sha256", "phase"}
    if set(value) != known:
        raise StateError(f"activation: must have exactly {sorted(known)!r}")
    transaction_id = _require_uuid(value["transaction_id"], "activation.transaction_id")
    candidate_slot = _require_slot(value["candidate_slot"], "activation.candidate_slot")
    candidate_generation = _require_positive_int(value["candidate_generation"], "activation.candidate_generation")
    candidate_descriptor_sha256 = _require_sha(value["candidate_descriptor_sha256"], "activation.candidate_descriptor_sha256")
    raw_phase = value["phase"]
    try:
        phase = ActivationPhase(raw_phase)
    except ValueError as exc:
        raise StateError(f"activation.phase: {raw_phase!r} is not a known phase") from exc
    return ActivationTransaction(
        transaction_id=transaction_id, candidate_slot=candidate_slot,
        candidate_generation=candidate_generation,
        candidate_descriptor_sha256=candidate_descriptor_sha256, phase=phase,
    )


def parse_runtime_state_dict(data: dict) -> RuntimeState:
    if not isinstance(data, dict):
        raise StateError("runtime state must be a JSON object")
    known = {
        "schema_version", "active_slot", "active_generation", "active_descriptor_sha256",
        "previous_slot", "previous_generation", "previous_descriptor_sha256", "activation",
    }
    if set(data) != known:
        raise StateError(f"runtime state must have exactly {sorted(known)!r}, got {sorted(data)!r}")

    if data["schema_version"] != SCHEMA_VERSION:
        raise StateError(f"unsupported schema_version {data['schema_version']!r}")

    active_slot = _require_slot(data["active_slot"], "active_slot")
    active_generation = _require_positive_int(data["active_generation"], "active_generation")
    active_descriptor_sha256 = _require_sha(data["active_descriptor_sha256"], "active_descriptor_sha256")

    previous_slot_raw = data["previous_slot"]
    previous_generation_raw = data["previous_generation"]
    previous_descriptor_sha_raw = data["previous_descriptor_sha256"]
    previous_fields = (previous_slot_raw, previous_generation_raw, previous_descriptor_sha_raw)
    if previous_fields == (None, None, None):
        previous_slot = previous_generation = previous_descriptor_sha256 = None
    elif None in previous_fields:
        raise StateError("previous_slot/previous_generation/previous_descriptor_sha256 must be all-null or all-present")
    else:
        previous_slot = _require_slot(previous_slot_raw, "previous_slot")
        previous_generation = _require_positive_int(previous_generation_raw, "previous_generation")
        previous_descriptor_sha256 = _require_sha(previous_descriptor_sha_raw, "previous_descriptor_sha256")
        if previous_slot is active_slot:
            raise StateError("previous_slot must not equal active_slot -- they identify different physical slots")
        if previous_generation >= active_generation:
            raise StateError("previous_generation must be strictly older than active_generation")

    activation = _parse_activation(data["activation"])
    if activation is not None:
        if activation.candidate_slot is active_slot:
            raise StateError("activation.candidate_slot must never equal active_slot -- the active slot is never a candidate")
        if not generation_advances(activation.candidate_generation, active_generation):
            raise StateError("activation.candidate_generation must strictly exceed active_generation")

    return RuntimeState(
        schema_version=data["schema_version"], active_slot=active_slot, active_generation=active_generation,
        active_descriptor_sha256=active_descriptor_sha256, previous_slot=previous_slot,
        previous_generation=previous_generation, previous_descriptor_sha256=previous_descriptor_sha256,
        activation=activation,
    )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class IndeterminateStateWriteError(RuntimeError):
    """D2 corrective review, Correction 2. Raised ONLY when
    os.replace() has already succeeded but the following parent-
    directory fsync() then fails. This is NOT an ordinary write
    failure: the destination path has already been atomically renamed
    to the NEW content and is visible to this process (and to any
    other process on this host that reads it right now) -- but whether
    that rename itself survives a concurrent crash/power-loss before
    the directory entry is flushed to durable storage is genuinely
    unknown on some filesystems/mount options.

    A caller must NEVER treat this as "the old state is still
    authoritative" (it may not be -- the rename already happened from
    this process's own point of view) and must NEVER treat it as "the
    new state is durably committed" (that is exactly what could not be
    confirmed). The only correct response is to stop, decline to
    proceed with whatever this write was for (e.g. an activation
    transaction), and surface the situation for operator attention --
    never silently retry-and-hope, never silently pick a side."""

    def __init__(self, path: Path, original_error: OSError):
        super().__init__(
            f"{path}: os.replace() succeeded but the parent-directory fsync failed afterward -- "
            f"rename durability across a crash is indeterminate: {original_error}"
        )
        self.path = path
        self.original_error = original_error


def write_runtime_state_atomically(path: Path, state: RuntimeState) -> None:
    """D2-F's exact durable-publication sequence:
      1. same-directory temporary file (guarantees the final rename
         cannot cross filesystems);
      2. restrictive mode (0600 -- state.json is root-only, no
         group/world access, from the moment it exists);
      3. write the complete serialized bytes;
      4. flush the Python-level buffer;
      5. fsync(file) -- the bytes are durable on the temp inode before
         anything else happens;
      6. os.replace() -- atomic rename, the ONLY moment readers can
         ever observe a state transition, never a partial write;
      7. fsync(parent directory) -- the RENAME ITSELF is durable, not
         just the file's bytes; without this, a crash right after
         os.replace() could still lose the rename on some filesystems/
         mount options.

    Returns normally (None) only once ALL SEVEN steps have succeeded --
    that is the ONLY case in which the caller may treat the new state
    as durably authoritative.

    A failure at steps 1-5, or os.replace() itself (step 6) failing,
    raises an ordinary OSError -- in every one of those cases the
    destination path is PROVABLY UNTOUCHED (os.replace() never ran, or
    never completed), so "the old state, if any, remains authoritative"
    is simply true; the temporary file is cleaned up and the caller may
    safely retry.

    A failure at step 7 ALONE (directory fsync, after a successful
    replace) raises IndeterminateStateWriteError instead -- see that
    exception's own docstring for why this case is handled distinctly
    rather than folded into the same generic OSError path. There is
    nothing to "clean up" in this case (the temporary name no longer
    exists; it IS the destination now), and this function does not
    attempt a second rename to "undo" the replace -- see this module's
    own corrective-review notes (docs/UPDATE_CENTER_PHASE_D.md): a
    second rename cannot erase the original durability ambiguity, it
    can only ADD a second one, so this function does not pretend to
    offer a rollback here at all -- that decision belongs to the
    caller (supervisor.py), which always fails the transaction closed
    rather than attempting to reconstruct anything from this state."""
    path = Path(path)
    raw = json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    directory = path.parent
    fd, temp_name = _mkstemp_in(directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise

    # From this point on, os.replace() has ALREADY succeeded -- `path`
    # is already the new content. temp_name no longer exists at its
    # original location, so there is nothing left to clean up on a
    # failure below; only the directory-fsync durability question
    # remains, handled as its own distinct outcome.
    try:
        _fsync_directory(directory)
    except OSError as exc:
        raise IndeterminateStateWriteError(path, exc) from exc


def _mkstemp_in(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=directory, prefix=".runtime-state.", suffix=".tmp")
    os.chmod(name, 0o600)
    return fd, name


def read_runtime_state(path: Path) -> RuntimeState:
    raw = Path(path).read_text(encoding="utf-8")
    return parse_runtime_state_dict(json.loads(raw))
