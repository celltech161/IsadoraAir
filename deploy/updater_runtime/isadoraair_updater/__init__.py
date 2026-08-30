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
