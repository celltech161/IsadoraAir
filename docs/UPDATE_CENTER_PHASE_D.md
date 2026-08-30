# Update Center Phase D — self-updating protected runtime

Phase D exists to close the one remaining gap in the Update Center
architecture: **the protected updater worker's own code and protected
managed-unit policy cannot currently update themselves.** Every other
kind of release (Django app code, migrations, templates, most systemd
units) installs entirely through the protected Update Center job
pipeline (`docs/UPDATE_CENTER.md`). A change to
`deploy/updater_runtime/**` — the worker's own source, including
`isadoraair_updater.release.MANAGED_UNIT_POLICIES` — instead requires
`manual_bootstrap_required: true` and a station-side SSH/sudo bridge,
today's only way to get new, trusted bytes onto the root-owned
installation path.

**Product acceptance criterion.** After one final manual bootstrap,
ordinary releases that change protected updater worker code or
protected managed-unit policy install entirely through Update Center —
no SSH, no sudo, no manual file copy, no manual protected-service
restart, no root config edit. Changing the immutable supervisor/trust
anchor itself may remain an exceptional manual operation; that is a
deliberately different, much rarer event than an ordinary worker/policy
release.

This document describes the target architecture and, explicitly, which
parts of it are actually implemented as of D1 (this phase) versus
designed-but-not-yet-active.

## D1 status

D1 ("Stable Contracts") implements the DATA and VERIFICATION contracts
D2+ will build the actual A/B activation machinery on top of:
`deploy/updater_runtime/protected_bootstrap/` (descriptor, policy,
attestation, trust, verification, cross-check, manifest field) plus a
new optional `protected_runtime` release-manifest field and a
fingerprint contract v3. **None of this is wired into live activation
behavior yet.** No supervisor exists. No A/B slot exists. The existing
`isadoraair_updater.release._cross_check()`'s `manual_bootstrap_required`
gate for `deploy/updater_runtime/**` changes is completely unchanged.

## Target architecture (D2 and beyond)

### Stable supervisor

A small, rarely-changing, root-owned process that itself becomes the
new "immutable trust anchor" (replacing today's "the installed worker
copy is trusted because it's root-owned" model). The supervisor's own
job is narrow: hold the socket/process lifecycle steady across worker
generations, and verify a candidate worker bundle BEFORE ever executing
a line of it — see `protected_bootstrap/verification.py`'s
`verify_candidate_bundle()`, already built to be importable by such a
process without Django or the current worker's own executor/daemon
code.

### A/B worker slots

Two installed generations on disk at once (A and B); the supervisor
activates whichever slot last passed full independent verification, and
can revert to the other slot without any git/network operation if a
new generation misbehaves. Not implemented in D1 — `protected_bootstrap`
provides the verification the switch will eventually depend on, not the
switch itself.

### Signed runtime bundles

A "generation" is: a `runtime-descriptor.json` (D1-B,
`protected_bootstrap/descriptor.py`) declaring an exact, sorted file
inventory (path/sha256/mode/size) plus an aggregate `bundle_sha256`,
and one or more detached Ed25519 signatures over a small, exact,
deterministic attestation statement (D1-D,
`protected_bootstrap/attestation.py`) binding the release id, its
predecessor, the generation number, and the descriptor's own digest.
Verification is layered like a small Merkle chain: signature ->
descriptor bytes exact -> descriptor's own declared file hashes exact
-> real files on disk exact. See `verification.py`'s own docstring for
the full chain.

### Protected policy

`isadoraair_updater.release.MANAGED_UNIT_POLICIES` — today a Python
dict literal, itself part of the protected source tree — has a signed
DATA equivalent (D1-C, `protected_bootstrap/policy.py`) that can travel
inside a signed bundle exactly like any other file, letting a future
release teach the station a new managed unit (e.g. the Weather forecast
services from `feature/weather-schedule-authority`, kept unmerged as
reference material for this exact use case) without a worker source
edit. See "Is moving policy to signed data safe?" below.

### D0 final bootstrap bridge

The one remaining manual step. A future release (`r0026` in current
planning, not yet cut) ships Phase-D-capable planner/worker source
(including this D1 work) but is installed through today's EXISTING
manual-bootstrap path — it declares `manual_bootstrap_required: true`
and, critically, **does not populate the new `protected_runtime`
manifest field at all**, because the manifest parser THAT bridge
release is installed BY (the currently-deployed r0025 parser) has never
heard of that field and would reject an unknown key. Only the NEXT
release after that (`r0027` in current planning) may populate
`protected_runtime`, because by then the manually-bootstrapped r0026
planner/worker already understands it. This is a two-release
compatibility bridge, not a new manual step per worker evolution — see
D1-J's own tests
(`updatecenter/tests/test_phase_d1_manifest_bridge.py::
FinalBootstrapCompatibilityBridgeTests`) for this proven directly
against synthetic fixtures, and against the real r0025.json manifest
for the "still parses unaffected" half.

### Runtime-first execution

Not yet designed in detail; the working assumption is that, once a
candidate generation is verified and activated, the supervisor execs
into (or otherwise hands control to) that generation's own entrypoint,
rather than the supervisor itself growing worker responsibilities. D2's
job.

### Safe rollback boundary

Because a generation is a fully self-contained, independently verified
bundle (not an incremental patch), reverting to a previously-installed,
still-on-disk generation requires no network access and no git
operation — the supervisor already has both A and B slots' bytes
locally. `generation_advances()` (D1-B/shared) refuses a bare replay or
rollback of the ACTIVE generation number for a NEW candidate, but that
is a distinct concern from the supervisor choosing, as an operational
decision, to reactivate an already-installed OLDER slot it still holds
— D2's job to design, not this document's to resolve.

### Four separate protocol/version concepts

See `isadoraair_updater/__init__.py`'s own extensive docstring, restated
here for one place that names all four together:

| Concept | Constant | Bump when |
|---|---|---|
| Client<->daemon socket wire shape | `PROTOCOL_VERSION` | A request/response field actually changes shape |
| Release-manifest execution semantics | `MANIFEST_PROTOCOL_VERSION` | A manifest field's MEANING changes in a way old planning code could misinterpret |
| This package's own code version | `RUNTIME_VERSION` | Any real code change — no compatibility contract by itself |
| Supervisor<->candidate-worker handoff | `BOOTSTRAP_PROTOCOL_VERSION` | What a supervisor and a candidate generation must agree on to safely hand off changes |

None of these bump merely because "the code changed." The existing
`PROTOCOL_VERSION`/`MANIFEST_PROTOCOL_VERSION` bridge precedent
(`test_phase_b_protocol.py::test_runtime_v4_keeps_wire_protocol_v3`)
extends the same way to future wire-breaking or manifest-semantic
changes: the new runtime supports the OLD protocol value(s) alongside
any new one until every caller/predecessor generation has moved, and
only THEN, as a later, separate, reviewed change, may the old value be
retired. `protected_bootstrap/cross_check.py`'s
`cross_check_protected_runtime()` already enforces the wire half of
this rule structurally (`current_wire_protocol_version` must remain in
a candidate's `supported_wire_protocols`) and the bootstrap half
(`minimum_bootstrap_protocol_version` must not exceed what the current
supervisor understands) — both currently a no-op for every real release
via the `phase_d_active` gate, until D2 wires it in.

## Is moving policy to signed data safe?

Evaluated directly, not assumed. The concern: does a JSON policy FILE
carry weaker integrity/authenticity than the SAME facts compiled
directly into `release.py`'s own Python source?

**No meaningful regression, provided (and only provided) the policy
file is treated as part of the SAME atomically-signed bundle as
everything else** — which D1-C's own requirement already forces
("the policy file must be part of the runtime descriptor inventory,"
`protected_bootstrap/policy.py`'s module docstring) and D1-G's
recommendation makes explicit (a policy-only change increments
generation and walks the identical verified path as a code change,
never a shortcut). Reasoning:

1. The policy file's bytes are covered by the descriptor's own
   `bundle_sha256` and by each individual file's `sha256` entry,
   exactly like `release.py`'s own source file. An attacker who could
   tamper with the policy file without a valid M-of-N signature is
   equally unable to tamper with the Python source without one — both
   require producing a valid signature over the descriptor, which
   requires the private keys.
2. There is no possibility of "new parser + old policy file" or "old
   parser + new policy file" mismatch, because both are verified as
   ONE atomic bundle per generation (this is exactly why a policy-only
   change must still bump `generation` — see the "YES" recommendation
   this task's own D1-G section adopted).
3. Worker code remains the sole authority for what `ENABLE_NOW`/
   `INSTALL_ONLY` actually DO (see `protected_bootstrap/policy.py`'s
   module docstring and `PolicyEnumMirrorTests` in
   `test_phase_d1_policy_trust_attestation.py`, which cross-checks the
   two independently-maintained enum value sets never silently
   diverge) — the policy file can only SELECT from a small, closed,
   already-existing enum; it can never introduce a new activation
   behavior, a wildcard/glob/regex unit match, or an arbitrary systemctl
   verb. `parse_policy_dict()` enforces exact unit basenames only, a
   closed policy-value enum, no duplicates, bounded count, and a
   canonical sorted representation.
4. `json.loads()` on already-signature-verified bytes carries strictly
   less execution risk than a Python source file ever importing (no
   `eval`, no arbitrary module-level side effects) — moving TOWARD data
   is, if anything, a narrowing of what a compromised file could do,
   not a widening.

This workorder does not change live production behavior to prove
this — `MANAGED_UNIT_POLICIES` remains the sole live authority; the
signed-policy-file contract is validated in isolation
(`test_phase_d1_policy_trust_attestation.py`), never yet consulted by
any activation path.

## DR implications

Not designed in D1. Two relevant properties this phase's contracts
already establish that D2/DR planning can build on: (a) a fully-
verified generation is self-contained and content-addressed
(`bundle_sha256`), so a disaster-recovery restore that reproduces the
same bytes reproduces the same digest and the same signature validity
with no re-signing; (b) `generation_advances()`'s strict-increase rule
means a restored station can safely fast-forward past intermediate
generations it never itself installed (a "skip" is legitimate) but can
never be tricked into accepting a replay of a superseded generation.

## Trust threshold is an operational decision, not this phase's

D1-E (`protected_bootstrap/trust.py`) builds a genuinely configurable
M-of-N schema and evaluator — proven directly by
`test_configurable_m_of_n_not_hardcoded_2_of_2` exercising 1-of-3,
2-of-3, and 3-of-3 against the identical signer set. The actual
production threshold and signer count are deliberately left to a later,
separate operational decision; this phase's job was the capability, not
the policy.
