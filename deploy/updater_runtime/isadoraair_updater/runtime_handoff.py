"""Update Center Phase D, D3: runtime-first worker handoff.

This module is the worker-side heart of D3 -- the milestone vocabulary
a durable job passes through while handing execution off to a
candidate protected-runtime generation (D3-D), the one central gate
that refuses every production-mutating executor call until that
handoff is durably accepted (D3-K), and the trusted-Git materialization
of a candidate bundle into the supervisor's own inactive-slot staging
area (D3-B).

Nothing here trusts a worker-created descriptor, and nothing here
performs signature/attestation verification -- see D3-A's own split:
the supervisor (deploy/updater_bootstrap/isadoraair_updater_bootstrap/
verification.py) is the one party whose verify_candidate_bundle() call
actually gates activation. This module only proves the MATERIALIZED
bytes match the descriptor the release manifest itself pinned
(descriptor_sha256, then every file's own sha256/size), and only
tracks/enforces the milestone sequence -- it never accepts a worker's
own "verified=true" as authoritative for anything beyond "safe to ask
the supervisor."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from protected_bootstrap.descriptor import (
    DescriptorError, RuntimeDescriptor, parse_descriptor_dict,
)
from protected_bootstrap.manifest_field import ProtectedRuntimeField

from .release import ReleaseError, TrustedRepository


class HandoffError(RuntimeError):
    pass


class MutationGateError(RuntimeError):
    """D3-K: raised ONLY by require_mutation_allowed() below --
    deliberately a distinct exception type from executor.
    ExecutionError so a caller cannot silently reclassify a refused
    mutation as an ordinary execution failure without a conscious
    except clause. executor.py's own call sites catch this explicitly
    and re-raise as ExecutionError("RUNTIME_ACTIVATION_NOT_ACCEPTED",
    ..., manual=True) -- manual, never auto-retried, since it means
    either a genuine bug reached a mutation call too early, or a
    resumed job's own milestones are unexpectedly incomplete."""


# D3-D's exact milestone vocabulary, added to the SAME job-store
# milestone list jobs.py's own Phase-B milestones already use (jobs.py
# imposes no vocabulary of its own beyond a shape regex -- see jobs.
# JobStore.milestone()) -- in the order a durable job passes through
# them for a protected_runtime release, entirely BEFORE any Phase-B
# production-mutation milestone (trusted_plan_validated, target_
# staged, ..., services_restarted) may occur. Never duplicated wholly:
# the supervisor's own ActivationPhase state machine (deploy/
# updater_bootstrap/isadoraair_updater_bootstrap/activation.py) is a
# SEPARATE, independently durable record of the SAME real-world
# handoff -- this list is the worker JOB's own immutable identity
# evidence (job, target release, candidate generation, descriptor
# digest, activation boundary), never a copy of supervisor slot-
# activation state (see this module's own MILESTONE constants'
# docstrings and D3-D's explicit "do not duplicate supervisor state
# wholesale" instruction).
MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED = "runtime_descriptor_validated"
MILESTONE_RUNTIME_CANDIDATE_STAGED = "runtime_candidate_staged"
MILESTONE_RUNTIME_CANDIDATE_VERIFIED = "runtime_candidate_verified"
MILESTONE_RUNTIME_ACTIVATION_REQUESTED = "runtime_activation_requested"
MILESTONE_RUNTIME_ACTIVATION_ACCEPTED = "runtime_activation_accepted"
MILESTONE_RUNTIME_GENERATION_COMMITTED = "runtime_generation_committed"

HANDOFF_MILESTONES = (
    MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED,
    MILESTONE_RUNTIME_CANDIDATE_STAGED,
    MILESTONE_RUNTIME_CANDIDATE_VERIFIED,
    MILESTONE_RUNTIME_ACTIVATION_REQUESTED,
    MILESTONE_RUNTIME_ACTIVATION_ACCEPTED,
    MILESTONE_RUNTIME_GENERATION_COMMITTED,
)

# D3-F: the exact boundary from which the OLD worker may safely yield
# a durable job -- legal only once activation has been durably
# REQUESTED (the milestone is appended to the job record via the SAME
# atomic JobStore.milestone() write every other milestone uses, so
# "the record says requested" and "the record is durable" are the
# same fact -- see jobs.py's own fsync'd _atomic_write()). Not legal
# any earlier (nothing has been asked of the supervisor yet -- see
# HandoffLifecycle.require_can_yield below); legal EVEN IF the
# supervisor has not yet itself accepted the request (D3-F's own
# documented case: "failure after safe-yield publication but before
# candidate activation -- supervisor may restart previous worker and
# job may resume before production mutation").
SAFE_YIELD_MILESTONE = MILESTONE_RUNTIME_ACTIVATION_REQUESTED

# D3-K: the ONE milestone that must already be durable before ANY
# production-mutation executor call may run, for a job whose target
# release declares protected_runtime. See require_mutation_allowed().
MUTATION_GATE_MILESTONE = MILESTONE_RUNTIME_ACTIVATION_ACCEPTED


def handoff_required(protected_runtime_field: ProtectedRuntimeField | None) -> bool:
    """The one predicate everything else in this module is keyed off
    of. True iff the ALREADY-INDEPENDENTLY-RESOLVED target release
    (release.TrustedPlan.protected_runtime, or release.
    manifest_for_release(chain, target_release_id).protected_runtime)
    declares a protected_runtime field at all -- never inferred from
    file content, a request field, or a worker's own opinion."""
    return protected_runtime_field is not None


def require_mutation_allowed(protected_runtime_field: ProtectedRuntimeField | None, milestones) -> None:
    """D3-K's central pre-mutation gate. A complete no-op for an
    ordinary release (protected_runtime_field is None) -- every
    existing Phase-B mutation call site's behavior is BYTE-FOR-BYTE
    unchanged for every release that does not declare protected_
    runtime (parity, D3-C/D3-K's own explicit requirement). For a
    protected_runtime release, raises MutationGateError unless
    MUTATION_GATE_MILESTONE is already present in `milestones` --
    fail closed, never a default-permissive gate a future refactor
    could quietly stop calling and not notice (see executor.py's own
    call sites, one per mutating operation, never a single check at
    the top of execute())."""
    if protected_runtime_field is None:
        return
    if MUTATION_GATE_MILESTONE not in set(milestones):
        raise MutationGateError(
            "production mutation refused: this job's target release declares "
            f"protected_runtime, and {MUTATION_GATE_MILESTONE!r} is not yet a durable "
            "milestone -- runtime activation has not been accepted"
        )


@dataclasses.dataclass(frozen=True)
class MaterializedCandidate:
    descriptor_bytes: bytes
    descriptor: RuntimeDescriptor
    descriptor_sha256: str


def materialize_candidate(
    repository: TrustedRepository, protected_runtime_field: ProtectedRuntimeField,
    target_commit: str, staging_directory: Path,
) -> MaterializedCandidate:
    """D3-B: stages a candidate bundle from THIS worker's own root-
    owned, independently trusted Git repository -- never a live
    application checkout, never application-owned Git, never a
    Django-uploaded file, never an HTTP request body, never database
    content (every one of those is unreachable from this function's
    own parameters: `repository` is a TrustedRepository, `target_
    commit` was independently resolved by release.derive_plan(), and
    `protected_runtime_field` came from a manifest this same trusted
    repository's own release chain already parsed and cross-checked --
    see release.py's _cross_check()).

    Sequence (D3-B's own numbered list, 1-7 -- #8, "repeat independent
    supervisor verification before activation," is deliberately NOT
    this function's job; see this module's own top docstring):
      1/2. `target_commit` and `protected_runtime_field` are already
           the caller's own independently-derived facts (release.py).
      3. load the exact descriptor bytes from root-owned Git objects
         at protected_runtime_field.descriptor_path, and verify them
         against the MANIFEST's own signed descriptor_sha256 pin --
         never trust a descriptor whose bytes don't match what the
         release manifest itself committed to.
      4. load every descriptor-listed file, from the SAME trusted
         commit, at a path resolved relative to the descriptor file's
         OWN containing directory (this module's one explicit
         convention: a descriptor at deploy/updater_runtime/updater-
         descriptor.json whose files list an entry path of
         "updaterd.py" names deploy/updater_runtime/updaterd.py in the
         trusted tree -- exactly mirroring how the descriptor's own
         `entrypoint` field is later resolved AGAIN, independently,
         relative to the SLOT directory by deploy/updater_bootstrap/
         isadoraair_updater_bootstrap/launch.py's resolve_entrypoint()).
      5. (attestations) -- loaded and verified by whichever party
         calls verify_candidate_bundle, not here.
      6. (n/a here -- see #3 above, this function's own inline check).
      7. materialize ONLY into `staging_directory`, which the caller
         must have obtained from the supervisor's own inactive-slot
         staging area (see supervisor_client.py / the D3-A activation
         request flow) -- this function never decides where that is.

    Refuses (HandoffError) on any missing/oversize/mismatched file,
    exactly like protected_bootstrap.descriptor.verify_descriptor_
    against_directory would after the fact -- checked WHILE writing
    here so a bad trusted-Git read can never leave a partially-
    materialized, silently-accepted staging directory behind."""
    descriptor_bytes = repository.read_file(
        target_commit, protected_runtime_field.descriptor_path, maximum=1024 * 1024,
    )
    if descriptor_bytes is None:
        raise HandoffError(
            f"descriptor {protected_runtime_field.descriptor_path!r} is unreadable at {target_commit}"
        )
    actual_descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
    if actual_descriptor_sha256 != protected_runtime_field.descriptor_sha256:
        raise HandoffError(
            f"descriptor at {protected_runtime_field.descriptor_path!r} does not match the release "
            f"manifest's own pinned descriptor_sha256 ({actual_descriptor_sha256} != "
            f"{protected_runtime_field.descriptor_sha256})"
        )
    try:
        descriptor = parse_descriptor_dict(
            json.loads(descriptor_bytes.decode("utf-8")), label="candidate descriptor",
        )
    except (DescriptorError, UnicodeDecodeError, ValueError) as exc:
        raise HandoffError(f"descriptor is invalid: {exc}") from exc

    base = PurePosixPath(protected_runtime_field.descriptor_path).parent
    staging_directory = Path(staging_directory)
    for entry in descriptor.files:
        repo_path = str(base / entry.path)
        content = repository.read_file(target_commit, repo_path, maximum=entry.size_bytes + 1)
        if content is None:
            raise HandoffError(f"candidate file {repo_path!r} is unreadable at {target_commit}")
        if len(content) != entry.size_bytes:
            raise HandoffError(
                f"candidate file {repo_path!r} size {len(content)} != descriptor size {entry.size_bytes}"
            )
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise HandoffError(f"candidate file {repo_path!r} sha256 does not match the descriptor")
        destination = staging_directory / entry.path
        try:
            destination.relative_to(staging_directory)
        except ValueError as exc:
            raise HandoffError(f"descriptor path {entry.path!r} escapes the staging directory") from exc
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, int(entry.mode, 8))
        try:
            os.write(fd, content)
        finally:
            os.close(fd)

    return MaterializedCandidate(
        descriptor_bytes=descriptor_bytes, descriptor=descriptor,
        descriptor_sha256=actual_descriptor_sha256,
    )


def new_supervisor_staging_directory(slots_root: Path) -> Path:
    """D3-A/D3-B's cross-package interop contract, stated precisely:
    this worker package must NEVER import deploy/updater_bootstrap/
    isadoraair_updater_bootstrap/slots.py (the immutable supervisor's
    own tree), yet the bytes this worker materializes (materialize_
    candidate above) must land somewhere that supervisor's OWN
    slots.publish_slot() will later accept as `staged` -- that
    function requires `staged.parent == layout.staging_root` EXACTLY,
    where `layout.staging_root == slots_root / ".staging"`. This
    function independently reproduces that one path fact (not the
    code) so the two sides agree on WHERE without either importing
    the other. See test_phase_d3_supervisor_ipc.py's own cross-package
    parity test, which imports BOTH copies and asserts the paths agree
    for the same slots_root."""
    staging_root = Path(slots_root) / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(dir=staging_root, prefix="candidate-"))


def attestations_staging_directory(slots_root: Path, candidate_slot: str) -> Path:
    """A FIXED, deterministic path -- keyed only by `candidate_slot`
    ('A' or 'B'), never a worker-chosen random name -- so the
    supervisor's own ipc_server.py can find a candidate's staged
    attestation files using ONLY facts the wire protocol already
    carries (candidate_slot) plus its own already-configured
    slots_root, with no new path field needed on the wire at all (see
    protocol.py's own REQUEST_ACTIVATION fields, and D3-A's explicit
    "no path" rule). Deliberately a SIBLING of the randomly-named
    bundle staging directory, never nested inside it -- verification.
    verify_descriptor_against_directory() walks the ENTIRE bundle
    root and rejects any file not in the descriptor's own inventory,
    so attestation files must never live inside the bundle root that
    gets promoted into slots_root/<candidate_slot>."""
    if candidate_slot not in ("A", "B"):
        raise SlotPublishError("candidate_slot must be exactly 'A' or 'B'")
    return Path(slots_root) / ".staging" / f"attestations-{candidate_slot}"


def descriptor_staging_path(slots_root: Path, candidate_slot: str) -> Path:
    """Same fixed-convention reasoning as attestations_staging_
    directory() above, for the one other thing the supervisor's
    verify_candidate_bundle() needs that is not itself part of the
    promoted bundle: the raw descriptor BYTES (that function takes
    descriptor_bytes as a plain parameter -- it never reads a
    descriptor off disk itself, by design, since "where the
    descriptor lives" is a staging-layout concern, not a verification
    concern). A sibling FILE (not a directory), deliberately named
    distinctly from attestations_staging_directory()'s own path so
    the two can never collide."""
    if candidate_slot not in ("A", "B"):
        raise SlotPublishError("candidate_slot must be exactly 'A' or 'B'")
    return Path(slots_root) / ".staging" / f"descriptor-{candidate_slot}.json"


def stage_descriptor(descriptor_bytes: bytes, slots_root: Path, candidate_slot: str) -> Path:
    destination = descriptor_staging_path(slots_root, candidate_slot)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = destination.with_suffix(".json.tmp")
    temp.write_bytes(descriptor_bytes)
    os.replace(temp, destination)
    return destination


def stage_attestations(
    repository: TrustedRepository, protected_runtime_field: ProtectedRuntimeField,
    target_commit: str, slots_root: Path, candidate_slot: str,
) -> Path:
    """Copies every attestation file protected_runtime_field.
    attestations names, VERBATIM (this worker never parses or
    interprets their content -- see this module's own top docstring:
    signature verification is the supervisor's job, never this one's),
    from root-trusted Git into attestations_staging_directory()'s
    fixed path. Clears any stale prior content for this candidate_slot
    first, so a supervisor reading this path never sees attestations
    left over from an earlier, unrelated candidate that used the same
    slot letter."""
    destination = attestations_staging_directory(slots_root, candidate_slot)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, mode=0o700)
    for index, path in enumerate(protected_runtime_field.attestations):
        content = repository.read_file(target_commit, path, maximum=65536)
        if content is None:
            raise HandoffError(f"attestation {path!r} is unreadable at {target_commit}")
        (destination / f"{index:02d}-{PurePosixPath(path).name}").write_bytes(content)
    return destination


class SlotPublishError(HandoffError):
    pass


def publish_to_candidate_slot(slots_root: Path, candidate_slot: str, staged: Path, *, active_slot: str) -> Path:
    """D3-A: promotes a fully-materialized staging directory into the
    supervisor's own `slots_root/<candidate_slot>` path -- an
    independent reproduction of slots.publish_slot()'s exact atomic-
    rename semantics (same reasoning as new_supervisor_staging_
    directory above), NOT a privileged act of trust: the supervisor
    NEVER launches or activates whatever sits at this path merely
    because it exists there -- see verification.verify_candidate_
    bundle, which the supervisor always re-runs against these exact
    bytes before ever reaching CANDIDATE_VERIFIED. This function's own
    contribution is refusing to ever let a worker overwrite the
    CURRENTLY ACTIVE slot in place -- the same hard rule slots.
    publish_slot() itself enforces, checked here independently rather
    than assumed, since a compromised or buggy worker is exactly the
    actor this refusal must hold against."""
    if candidate_slot not in ("A", "B"):
        raise SlotPublishError("candidate_slot must be exactly 'A' or 'B'")
    if candidate_slot == active_slot:
        raise SlotPublishError(f"refusing to publish into the currently active slot {active_slot}")
    slots_root = Path(slots_root)
    staging_root = slots_root / ".staging"
    staged = Path(staged)
    if staged.parent != staging_root:
        raise SlotPublishError("staged content must come from this worker's own staging directory")
    destination = slots_root / candidate_slot
    if destination.exists():
        discard_target = staging_root / f"discarded-{candidate_slot}-{os.getpid()}-{id(staged)}"
        os.replace(destination, discard_target)
        shutil.rmtree(discard_target, ignore_errors=True)
    os.replace(staged, destination)
    directory_fd = os.open(slots_root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return destination


@dataclasses.dataclass(frozen=True)
class HandoffRecoveryFacts:
    """D3-H: what a resuming (candidate) worker needs to decide
    whether ITS OWN startup is an ordinary clean start or a Phase-D
    handoff recovery of one specific already-durable job."""
    job_id: str
    target_release_id: str
    protected_runtime_generation: int
    protected_runtime_descriptor_sha256: str


class RecoveryAmbiguous(HandoffError):
    """D3-H: raised whenever recovery classification cannot prove a
    single, unambiguous resumable job -- the caller must fail closed,
    never guess."""


def classify_handoff_recovery(
    job_states: list[dict], *, expected_generation: int, expected_descriptor_sha256: str,
) -> HandoffRecoveryFacts | None:
    """D3-H's own "distinguish ordinary clean startup from Phase-D
    handoff recovery" decision, as a pure function of the job records
    a freshly-started worker's OWN JobStore.list_states() already
    returned (no privileged I/O of its own).

    Returns None (ordinary clean startup -- nothing to resume) when no
    job in ACTIVE_STATES ('accepted'/'running') carries a
    'protected_runtime_candidate' record at all -- an ordinary Phase-B
    job in flight is not this module's concern.

    Returns HandoffRecoveryFacts only when EXACTLY ONE active job
    carries a protected_runtime_candidate record, its own
    SAFE_YIELD_MILESTONE (or later) is present (never resume a job
    whose OLD worker had not yet reached a legal yield point -- see
    D3-H's own "no production-mutation milestone has already begun
    under ambiguous ownership" requirement, which milestone ordering
    already guarantees: every Phase-B mutation milestone can only be
    reached AFTER MUTATION_GATE_MILESTONE, which is strictly after
    SAFE_YIELD_MILESTONE), and its recorded generation/descriptor
    match what THIS candidate's own supervisor activation transaction
    expects.

    Raises RecoveryAmbiguous (never silently picks one) for more than
    one candidate, or for a sole candidate whose recorded facts do not
    match what this candidate process was told to expect -- a mismatch
    here means either a stale/wrong job or a wrong candidate process,
    and continuing either would violate D3-H's own "never reinterpret
    a different target" rule."""
    candidates = []
    for state in job_states:
        if state.get("state") not in {"accepted", "running"}:
            continue
        record = state.get("protected_runtime_candidate")
        if not isinstance(record, dict):
            continue
        milestones = set(state.get("milestones", []))
        if SAFE_YIELD_MILESTONE not in milestones:
            continue
        candidates.append((state, record))

    if not candidates:
        return None
    if len(candidates) > 1:
        raise RecoveryAmbiguous(
            f"{len(candidates)} active jobs carry a protected_runtime_candidate handoff record -- "
            "recovery must be unambiguous"
        )

    state, record = candidates[0]
    required = {"generation", "descriptor_sha256"}
    if set(record) != required:
        raise RecoveryAmbiguous("protected_runtime_candidate record has an invalid shape")
    if record["generation"] != expected_generation or record["descriptor_sha256"] != expected_descriptor_sha256:
        raise RecoveryAmbiguous(
            "the one resumable job's recorded candidate generation/descriptor does not match "
            "what this candidate process was activated as"
        )
    return HandoffRecoveryFacts(
        job_id=state["job_id"],
        target_release_id=state["requested_target_release_id"],
        protected_runtime_generation=record["generation"],
        protected_runtime_descriptor_sha256=record["descriptor_sha256"],
    )
