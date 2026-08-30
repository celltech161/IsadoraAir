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
PROTOCOL_VERSION = 3

# This package's own code version -- independent of both protocol
# numbers above and below.
RUNTIME_VERSION = 4

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
MANIFEST_PROTOCOL_VERSION = 4

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
