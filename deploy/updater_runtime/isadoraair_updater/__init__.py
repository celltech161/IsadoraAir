"""Standalone privileged IsadoraAir updater runtime.

This package is intentionally Python-stdlib-only.  The copy in Git is a
review/distribution artifact; production must execute only the protected copy
installed under ``/usr/local/libexec/isadoraair-updater``.
"""

# The client<->daemon SOCKET wire protocol (see protocol.py's decode_request/
# encode_response) -- must stay in exact lockstep with updatecenter/
# backend_client.py's and deploy/updater_runtime/updaterctl.py's own
# PROTOCOL_VERSION constants, all three of which are independently
# maintained copies of this same number (see test_phase_b_protocol.py's
# test_runtime_v4_keeps_wire_protocol_v3, which exists specifically to
# prove RUNTIME_VERSION/MANIFEST_PROTOCOL_VERSION can advance without
# forcing every existing client's request shape to change). Bump this
# ONLY for an actual wire-protocol shape change (new/changed request or
# response fields) -- never merely because release-manifest execution
# semantics changed; that is MANIFEST_PROTOCOL_VERSION's job below.
PROTOCOL_VERSION = 4
# Generation 8 deliberately bridges the already-deployed v3 Django client and
# CLI while introducing one v4-only action.  PING replies in the request's
# version so an r0088 client does not mistake a compatible bridge for a break.
SUPPORTED_WIRE_PROTOCOLS = (3, 4)
# The protected-runtime handoff bridge is negotiated on the oldest protocol
# this worker still promises to serve.  This lets generation 8 validate a
# history containing v3-only intermediate generations while selecting the
# newest effective candidate, and protects the still-installed r0088 client.
HANDOFF_WIRE_PROTOCOL = SUPPORTED_WIRE_PROTOCOLS[0]

# This package's own code version -- independent of both protocol
# numbers above and below.
#
# 4 -> 5 (Update Center Phase D, D3): this package gains real Phase-D
# runtime-first execution semantics -- a supervisor/candidate IPC
# client (supervisor_client.py), protected-runtime candidate
# materialization from root-trusted Git (protected_materialize.py),
# signed managed-unit policy consumption (release.py's
# resolve_unit_policy(), sourced from protected_bootstrap.policy when
# a generation supplies one, D0 generation 1's compiled
# MANAGED_UNIT_POLICIES otherwise), the runtime-handoff milestone
# vocabulary and central pre-mutation gate (runtime_handoff.py), and
# fingerprint contract v3 becoming authoritative for a direct protected-
# runtime target release (release.py's derive_plan()). This is a real
# change to what this package's own code DOES, not a redefinition of
# either protocol number below -- see D3's own workorder: "Do not bump
# merely for cosmetics."
#
# 5 -> 6 (r0084 correctness repair): planning and handoff now preserve
# the newest protected-runtime transition crossed by an aggregated plan,
# including its introducing release/predecessor/commit provenance. Direct
# protected targets retain their legacy v3 authorization fingerprint.
#
# 6 -> 7 (reviewed migration approval): executor.py's _validate_target_
# schema now checks a matching updatecenter.models.MigrationPlanApproval
# (looked up by the existing updatecenter_probe subprocess call, which now
# also accepts --release-id/--target-commit) before raising
# MIGRATION_OPERATION_MANUAL, and threads a job_id through so it can log
# which approval was used. jobs.py/daemon.py gained one new optional key
# (migration_plan_review) on the EXISTING GET_JOB_STATUS response and job-
# state schema -- no wire PROTOCOL_VERSION bump (no request/response field
# changed shape, only a new optional field was added) and no
# MANIFEST_PROTOCOL_VERSION bump (no release-manifest field's execution
# meaning changed). A real code change to this package regardless, per
# this constant's own rule -- see docs/UPDATE_CENTER.md's "Reviewed
# migration approval" section.
#
# 7 -> 8 (generation-6 probe-compatibility repair -- r0090 production
# incident): executor.py's _strict_probe() required the full 13-key
# reviewed-plan payload shape UNCONDITIONALLY, including for the
# current-schema probe, which always runs against whatever application
# source is CURRENTLY installed -- before any source advancement in the
# same job. Protected-runtime generation and ordinary application-source
# deployment are on independent cadences, so a currently-installed
# source may correctly predate this feature and emit only the legacy
# 7-key shape (see docs/UPDATE_CENTER.md's "Reviewed migration approval"
# section). _strict_probe now selects its required schema from the
# CALLER's own review-context flag (whether release_id/target_commit was
# actually requested), never from the payload's own claimed shape: the
# current-schema probe accepts either the exact legacy 7-key shape or
# the full 13-key shape held at its documented null defaults, while the
# target-schema probe is completely unchanged -- still exactly the full
# 13-key shape, strictly validated. No wire PROTOCOL_VERSION bump, no
# MANIFEST_PROTOCOL_VERSION bump, no BOOTSTRAP_PROTOCOL_VERSION bump --
# this is a correctness fix to this package's own probe-parsing code,
# not a change to any cross-process compatibility contract.
#
# 8 -> 9 (r0092 bootstrap recovery): approval authority moves out of the
# application migration graph into the root-owned ApprovalStore. The staged
# probe is discovery-only, the executor performs exact protected lookup, and
# protocol v4 adds the narrow approval action while this generation continues
# accepting v3 from the still-installed r0088 application.
RUNTIME_VERSION = 9

# The release-MANIFEST execution-semantics protocol (see release.py's
# manual_blockers(), compared against each release's declared
# minimum_updater_protocol_version) -- mirrors updatecenter/manifest.py's
# own UPDATER_PROTOCOL_VERSION, which must be bumped in lockstep with
# this one (see docs/UPDATE_CENTER.md). Deliberately a DIFFERENT number
# from the wire PROTOCOL_VERSION above -- the two protocols cover
# genuinely different boundaries (an operator's CLI/Django client
# talking to this daemon, versus this daemon's own interpretation of a
# release author's manifest) and must be free to change independently.
#
# 3 -> 4: systemd_units_new_required's execution semantics changed -- a
# required unit is no longer unconditionally `enable --now`d; see
# MANAGED_UNIT_POLICIES below.
#
# 4 -> 5 (Update Center Phase D, D3): a release manifest's
# protected_runtime field (D1-A) now has real EXECUTION semantics for
# the first time -- when present, this worker's own execute() no
# longer runs its ordinary Phase-B pipeline directly on the currently
# active process. It instead validates just enough to know a handoff
# is required, stages+independently-verifies the signed candidate
# generation into the supervisor's inactive slot, requests activation,
# and yields the durable job (still open, still owned by this SAME
# job_id) to whichever worker the supervisor next starts -- see
# runtime_handoff.py. A release declaring protected_runtime therefore
# means something this worker could not even attempt to execute before
# this version; a release requiring it must declare
# minimum_updater_protocol_version=5 so a pre-Phase-D updater refuses
# it (UPDATER_UPGRADE_REQUIRED) rather than attempting its ordinary
# pipeline against a runtime it cannot actually replace.
#
# r0084 deliberately retains 5: selecting an intermediate protected
# transition repairs the existing Phase-D execution meaning. Raising this
# value would make the generation-3 WRJE worker reject the bridge before it
# could activate the corrected generation-5 runtime.
MANIFEST_PROTOCOL_VERSION = 5

# The BOOTSTRAP SUPERVISOR protocol (Update Center Phase D, [P1] 1.16
# D1) -- how a future stable, rarely-changing supervisor process and a
# candidate protected-runtime WORKER generation negotiate/verify a
# handoff (see deploy/updater_runtime/protected_bootstrap/ and
# docs/UPDATE_CENTER_PHASE_D.md). Does not exist before Phase D at all;
# 1 is its first-ever value, not a continuation of any other counter
# here. Bump this ONLY for an actual change to what the supervisor and
# a candidate worker generation must agree on to safely hand off
# (protected_bootstrap.manifest_field.ProtectedRuntimeField's
# minimum_bootstrap_protocol_version is compared against this) -- never
# merely because worker source changed (RUNTIME_VERSION's job) or a
# release manifest's own execution semantics changed
# (MANIFEST_PROTOCOL_VERSION's job).
#
# This project deliberately tracks FOUR independent protocol/version
# concepts, never conflated:
#   1. PROTOCOL_VERSION      -- client<->daemon socket wire shape.
#   2. MANIFEST_PROTOCOL_VERSION -- release-manifest execution semantics.
#   3. RUNTIME_VERSION       -- this package's own code version (no
#                                compatibility contract by itself; two
#                                different RUNTIME_VERSION values can
#                                still agree on every protocol above).
#   4. BOOTSTRAP_PROTOCOL_VERSION -- supervisor<->candidate-worker
#                                generation handoff compatibility
#                                (Phase D only).
# A single "the code changed" event never automatically bumps any of
# these -- each one is bumped only when ITS OWN specific compatibility
# boundary's meaning actually changes, following the exact bridge
# pattern already proven for PROTOCOL_VERSION/MANIFEST_PROTOCOL_VERSION
# (see test_phase_b_protocol.py's test_runtime_v4_keeps_wire_protocol_v3):
# a new runtime version supports the OLD protocol value(s) alongside any
# new one until every caller has moved, at which point (and only then,
# as a later, separate, reviewed change) the old value may be retired.
BOOTSTRAP_PROTOCOL_VERSION = 1
