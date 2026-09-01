# Update Center Phase D — self-updating protected runtime

**Status through D5.1C (2026-09-01).** Phase D5 and its D5.1A live-update
and D5.1B offline-recovery acceptance were completed on the disposable
`isadoraair2` host. Final implementation source is
`f45877b26f2065b59649459e3d79d51e6b104f83`; the two commits after the
accepted D5.1A source only correct schema-2 recovery assembly and restore
layout. Sections below that describe D1–D4 as not yet deployed, or D5 as
pre-bootstrap, are retained as the chronological design record. They are not
the current acceptance status. KOGR production was not touched and D6 has not
begun.

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

## D2 status

D2 ("Stable Supervisor + A/B Runtime Storage") implements the immutable
supervisor's own source tree, `deploy/updater_bootstrap/
isadoraair_updater_bootstrap/` -- config, security, process, descriptor,
attestation, trust, verification, slots, state (+ its durable atomic
writer), activation (the phase state machine), protocol (the private
root-only control IPC), launch (fixed worker process exec), readiness,
and the supervisor orchestrator tying them together. A draft (not
installed) `deploy/updater-bootstrapd.service` and a development-only
signing helper (`deploy/updater_bootstrap/tools/sign_release_bundle.py`)
round it out.

**Independence from the replaceable worker tree is real, not asserted**:
this package imports nothing from `deploy/updater_runtime/
isadoraair_updater/**` or `protected_bootstrap/**`, nothing from Django,
nothing from an application checkout/venv -- Python stdlib and fixed
absolute OS-utility invocations only. Where the supervisor needs logic
equivalent to the worker's own D1 contracts (descriptor/trust/
attestation/verification), it has its own SEPARATE, independently
written implementation, proven to agree with the worker-side one only
by a large shared fixture corpus
(`updatecenter/tests/test_phase_d2_parity.py`) that imports both
packages and compares outcomes -- never by one importing the other.

**Still none of this is wired into live activation.** No real
Update Center job creates a transaction, launches a real candidate
worker, or requests activation from this supervisor -- every D2 test
uses synthetic fixtures/temp directories/a throwaway test worker
script, exactly as scoped. `START_UPDATE` is untouched;
`updatecenter/views.py`, `job_service.py`, and the worker's own
`daemon.py`/`executor.py`/`jobs.py` have zero changes in this phase.
That handoff -- a real UpdateJob driving real candidate staging through
this real supervisor, and the `runtime_activation_accepted` milestone
gating real database/source/systemd mutation -- is D3's job.

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

## D2 corrective review (2026-08-30)

Four corrections applied to D2 before D3 begins. See each module's own
docstring/tests for the full detail; summarized here.

### Correction 1 — supervisor capability inheritance

**Finding**: `deploy/updater-bootstrapd.service` already contained
`AmbientCapabilities=CAP_SETUID CAP_SETGID` (byte-identical to the
r0006-hardened worker unit's own line) — the D2 report's claim that it
was "deliberately omitted" was a stale description of an earlier draft
that had already been superseded before the file was committed. The
prose was wrong; the shipped unit was already correct. Fixed the
false claim here and in the corrective commit message; added
`updatecenter/tests/test_phase_d2_supervisor_capability.py` (9 tests)
so a *future* edit dropping the line fails a fast, unprivileged test
rather than only being caught by an expensive manual acceptance run.

**Why the supervisor unit needs it, not just the worker**: under Phase
D the worker is a root CHILD PROCESS the supervisor forks
(`launch.py: launch_worker()` → `process.py: launch_tracked()`, a
plain `subprocess.Popen`, never an `execve()` that replaces the
supervisor). Per `capabilities(7)`, the ambient capability set survives
`execve()` of a non-privileged (non-setuid, no file-capability) program
— true for both hops here: the supervisor's own `/usr/bin/python3`
target, and the worker's own `/usr/bin/python3 -I <slot>/<entrypoint>`
target. Neither is setuid or has file capabilities (confirmed:
`test_worker_process_launch_uses_plain_non_shell_exec_that_cannot_
strip_ambient_capabilities`). So a capability set on the *supervisor's*
own systemd unit is what actually reaches the worker child process's
own later `runuser --user ISA_USER` call
(`isadoraair_updater.process.CommandRunner.run_as_user()`, unchanged,
still `runuser`-based — confirmed still present by
`test_worker_still_actually_uses_runuser_for_privilege_drop`).

**Manual acceptance (do not automate in CI, matches r0006's own
convention)** — NOT executed in this development environment (no
non-interactive root/sudo available in this session; see the prior
weather-schedule-authority corrective-review report for the same
limitation). The r0006 single-hop transient-unit proof
(`docs/UPDATE_CENTER.md`'s own "Manual systemd capability acceptance"
section) already establishes the underlying kernel rule for one
non-privileged exec hop; this is its two-hop extension, to be run by
an operator during an approved maintenance review once D0 is staged:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=isadoraair-bootstrap-capability-proof \
  --property=User=root --property=Group=jreed \
  --property=NoNewPrivileges=yes \
  --property='AmbientCapabilities=CAP_SETUID CAP_SETGID' \
  /usr/bin/python3 -c "
import subprocess
# Hop 1: simulates the supervisor forking the worker as a plain child
# process (exactly launch.py's launch_tracked() -- no shell, no setuid
# wrapper).
subprocess.run([
    '/usr/bin/python3', '-c',
    'import subprocess; '
    'subprocess.run([\"/usr/sbin/runuser\", \"--user\", \"jreed\", '
    '\"--\", \"/usr/bin/cat\", \"/proc/self/status\"])',
])
"
```

Require the resulting `jreed` child's reported `CapPrm`, `CapEff`, and
`CapAmb` to all be zero — the exact acceptance target:

```text
Supervisor:  root-owned, root-executed (User=root, ambient CAP_SETUID/CAP_SETGID)
Worker:      root child of supervisor (inherits ambient set across a non-privileged execve)
Application: ISA_USER child of runuser -- CapPrm=0, CapEff=0, CapAmb=0
```

The final D0 production acceptance must explicitly re-prove, on the
real staged supervisor/worker (not only this simulated transient
unit): (1) supervisor/worker startup succeeds; (2) the worker's own
privilege-drop self-check succeeds (mirroring the existing
`protected_runtime_valid` self-check); (3) the resulting application-
user child has zero permitted/effective/ambient capabilities.

### Correction 2 — post-rename fsync durability semantics

**Finding, confirmed real**: the D2 report's fault-injection tests
proved no CLOBBERING of the existing destination on failure, but never
distinguished failure *before* `os.replace()` (destination provably
unchanged) from failure *after* a successful `os.replace()` but before
the parent-directory `fsync()` (destination already changed at the
filesystem level; only the RENAME's own crash-durability is uncertain,
not its current-process visibility). The original single test for this
boundary (`test_failure_after_replace_before_directory_fsync_still_
leaves_file_durable_on_disk`) only asserted the new file is visible to
the *same still-running process* -- true, but not the relevant safety
question, which is what a crash right at that boundary means for the
*next* process to read this file after a restart.

**Fix**: `write_runtime_state_atomically()` keeps its ordinary contract
(returns `None` on full success, raises `OSError` for any failure
*before* `os.replace()` succeeds -- these are the genuinely
recoverable cases where the destination is provably untouched and "old
state remains authoritative" is simply true). A parent-directory-fsync
failure occurring *after* a successful `os.replace()` is no longer
allowed to surface as an indistinguishable bare `OSError` -- it is
caught and re-raised as a new, distinct `IndeterminateStateWriteError`
(a small dataclass-carrying exception, not a silently-ignorable return
value: a caller cannot accidentally fail to notice it the way a
forgotten return-value check could happen). A caller that CATCHES
`IndeterminateStateWriteError` receives enough to log the exact
situation but is never handed anything resembling "safe to treat X as
current" -- the only correct response is to stop, decline to proceed
with activation, and surface it for operator attention.

`supervisor.py`'s own transition functions remain deliberately I/O-free
(pure `RuntimeState -> RuntimeState`, unchanged by this correction --
see D2-S's own scope boundary: nothing in this phase persists a
transition to disk itself, that wiring is D3's job). This correction's
job was narrower and prerequisite to that wiring: making the ATOMIC
WRITER's own contract unambiguous, so whichever D3 code eventually
calls `write_runtime_state_atomically()` after a `supervisor.py`
transition has no way to accidentally treat an indeterminate rename as
either "old" or "new" -- it must catch `IndeterminateStateWriteError`
specifically and fail the transaction closed. This is proven now by
`test_phase_d2_state.py`'s own fault-injection tests distinguishing all
seven boundaries (see Tests below), not deferred to D3 to discover.

On restart, `read_runtime_state()` is unchanged (it already either
successfully parses one complete, valid JSON document or raises --
`os.replace()`'s atomicity guarantees there is never a half-written
file to misparse) — recovery logic does not need to change to handle
"old vs. new state after an indeterminate write," because by the time
recovery runs, the file that exists IS one complete state or the
other; `recovery_action_for()`'s already-conservative "discard any
non-idle transaction" rule already covers both possibilities without
needing to know which one actually happened.

### Correction 3 — active-slot invariant, made mechanical

`commit_transaction()` was already, by construction, the only function
in `supervisor.py` that assigns to `RuntimeState.active_slot` — now
proven by a dedicated AST-based test
(`test_only_commit_transaction_assigns_active_slot`) that greps every
function in the module for an assignment to that field, rather than
relying on the module docstring's own claim. Also added an explicit
test proving candidate launch/readiness classification
(`readiness.classify_readiness`) never receives or returns anything
resembling `RuntimeState` at all — structurally incapable of
influencing `active_slot`, not merely "doesn't currently."

### Correction 4 — worker process lifecycle ownership

Added `isadoraair_updater_bootstrap.worker_lifecycle`, a small pure
policy module the supervisor consults (not yet wired to a real event
loop, matching D2-S's own scope) answering exactly one question per
observed transition: is starting a NEW worker legal given the
CURRENTLY tracked one's state (`NONE` / `RUNNING` /
`EXITED_UNACKNOWLEDGED`)? Refuses to launch a second worker while one
is already tracked as running (no duplicate simultaneous workers);
requires an explicit `acknowledge_exit()` after `record_exit()` before
a new launch is legal (a normal exit and a crash reach the identical
state here -- this layer only tracks "may I launch," not "what
happened," deliberately); bounds consecutive restart attempts within a
rolling time window (`max_consecutive_restart_attempts`/
`restart_attempt_window_seconds`, both configurable, neither wired to
any automatic reset); and a freshly-constructed instance (what a
restarted supervisor process necessarily has -- it cannot remember a
prior instance's PID) always starts able to launch, matching
`supervisor.py`'s own conservative recovery stance.

Orphan prevention: `process.py`'s `TrackedChild.terminate()` already
used process-group signaling (`os.killpg`) with a SIGTERM grace period
before SIGKILL — unchanged, now proven directly against a real
subprocess tree (a worker that itself spawns a grandchild) via `pgrep`
on the process group, not merely asserted. Independently, the
supervisor's own draft unit relies on systemd's DEFAULT `KillMode`
(`control-group`, when the directive is simply absent, as it is here)
so a `systemctl stop`/`restart` of the supervisor itself sends
SIGTERM/SIGKILL to its entire cgroup — including the worker child
process — as a second, independent safety net on top of (not instead
of) whatever explicit termination the supervisor's own code performs;
`test_no_kill_mode_weakening_the_cgroup_wide_stop_restart_safety_net`
refuses a future `KillMode=process` edit that would narrow this to
only the main PID.

## D3 status — runtime-first worker handoff (harness-proven, not yet deployed)

D3 makes the runtime-first handoff sequence real: an already-running
worker can hand a durable update job to a brand-new protected-runtime
generation the D2 supervisor starts, without ever letting a production
mutation (migration, source advancement, systemd reconciliation,
service restart) happen before the new runtime is proven and accepted.
Everything below is implemented and exercised by real, non-mocked
tests (real Unix sockets, real Ed25519 signing via `openssl`, a real
local Git repository, real `fcntl.flock` lock acquisition across two
`JobStore` instances) — **nothing has been deployed, installed, or
run as root**; see this section's own "what remains" list at the end.

### Version identities

- `PROTOCOL_VERSION = 3` (Django↔worker wire shape) — **unchanged**.
- `RUNTIME_VERSION = 4 → 5`, `MANIFEST_PROTOCOL_VERSION = 4 → 5` — a
  real change to worker code and to `protected_runtime`'s own
  execution semantics (see below), not a cosmetic bump. Mirrored in
  `updatecenter/manifest.py`'s `UPDATER_PROTOCOL_VERSION`.
- `BOOTSTRAP_PROTOCOL_VERSION = 1` — unchanged.

### D3-A — real worker↔supervisor IPC

- `deploy/updater_bootstrap/isadoraair_updater_bootstrap/ipc_server.py`
  (new): the real Unix-socket server for the D2 `protocol.py` private
  activation protocol. PING / GET_RUNTIME_STATE / REQUEST_ACTIVATION /
  GET_ACTIVATION_STATUS. SO_PEERCRED-authorized (root-only by default,
  overridable only for tests, matching `isadoraair_updater.daemon.
  UpdaterDaemon`'s own established pattern). REQUEST_ACTIVATION never
  authorizes anything by itself — the server always independently
  re-derives the descriptor/attestation/inventory proof
  (`verification.verify_candidate_bundle`) against files the worker
  staged at fixed, convention-based paths (never a path carried on the
  wire).
- `deploy/updater_runtime/isadoraair_updater/supervisor_client.py`
  (new): the worker's own strict client, an INDEPENDENT reimplementation
  of the same wire shape (never imports the supervisor's own tree —
  Correction 1's independence boundary applies in both directions).
  Kept honest by `test_phase_d3_supervisor_ipc.py`'s own byte-for-byte
  wire-encoding parity test.
- **Real bug found and fixed during this work**: the server's original
  `handle_connection()` checked SO_PEERCRED *before* draining the
  client's request bytes. For an unauthorized peer this left unread
  bytes in the socket's own receive buffer at close time, and Linux
  answered with an abrupt RST instead of a clean FIN — the CLIENT's
  own second `recv()` call (still inside its read loop, since the
  short rejection response was well under the response size cap) then
  raised `ConnectionResetError` instead of seeing a clean EOF. Fixed to
  read-then-authorize, exactly matching `daemon.py`'s own already-
  proven order. `test_phase_d3_supervisor_ipc.py` locks this in.

### D3-B — candidate materialization from root-trusted Git

`deploy/updater_runtime/isadoraair_updater/runtime_handoff.py` (new,
worker-side):

- `materialize_candidate()` — loads the exact descriptor bytes at
  `protected_runtime.descriptor_path` from root-trusted Git, verifies
  them against the release manifest's own signed `descriptor_sha256`
  pin, parses the descriptor, then loads every descriptor-listed file
  from the SAME trusted commit (path resolved relative to the
  descriptor's own containing directory) and writes it into a staging
  directory, verifying size/sha256 per file as it writes. Never trusts
  a worker-created descriptor; never touches a live checkout,
  application-owned Git, a Django upload, or an HTTP request body.
- `new_supervisor_staging_directory()` / `publish_to_candidate_slot()`
  — independently reproduce (never import) `slots.py`'s own
  `staging_root`/`publish_slot()` path conventions and atomic-rename
  semantics, so the worker's staged bytes land exactly where the
  supervisor's own slot layout expects them, refusing to ever publish
  into the currently active slot.
- `stage_descriptor()` / `stage_attestations()` — copy the raw
  descriptor bytes and every attestation file (verbatim, unparsed) to
  fixed, convention-based sibling paths
  (`slots_root/.staging/descriptor-<slot>.json`,
  `slots_root/.staging/attestations-<slot>/`) the supervisor can find
  using only facts the wire protocol already carries (`candidate_slot`)
  plus its own configured `slots_root` — no new path field was added
  to the wire protocol.
- Attestation file wire shape (new, since none existed before this
  work): `{"schema_version": 1, "signer_id": "...", "signature_base64":
  "..."}`, one file per `protected_runtime.attestations` entry.

### D3-C — signed protected policy actually consulted

- `release.py`: `resolve_unit_policy(unit, *, signed_policy)` — a
  signed `ProtectedPolicyDocument` (D1's `protected_bootstrap.policy`)
  is consulted first when supplied; a unit it doesn't mention, or a
  fully absent signed policy (`None`, every existing caller today),
  falls back to `MANAGED_UNIT_POLICIES` unchanged. `SystemdManager`
  gained an optional `signed_policy=None` constructor parameter and
  now calls `resolve_unit_policy()` instead of reading
  `MANAGED_UNIT_POLICIES` directly.
- `GENERATION_1_POLICY_DOCUMENT` — an exact, parity-tested data
  representation of today's compiled `MANAGED_UNIT_POLICIES`, in D1's
  signed-policy shape. This is what D0's bootstrap generation is meant
  to carry as its own initial signed policy.
- **Known, deliberate scope limit**: this makes a *signed policy*
  authoritative for a unit's *activation behavior* (`ENABLE_NOW` vs
  `INSTALL_ONLY`). It does **not** yet let a signed policy introduce a
  genuinely *new* unit name outside `KNOWN_MANAGED_UNITS` — that
  constant is still a module-level compile-time value, consumed by
  `release.py`'s manifest cross-checking *before* any candidate bundle
  has been materialized (a real chicken-and-egg ordering question: the
  release chain must validate before a runtime handoff can even begin,
  but a signed policy naming a new unit would live inside the very
  candidate bundle that handoff produces). Left for D4 — see below.

### D3-D — durable job milestones (worker job store)

`jobs.py`'s job-state shape gained one new field,
`protected_runtime_candidate` (`{generation, descriptor_sha256,
candidate_slot}` or `null`), set once by the old worker when it stages
a candidate, read (never duplicated further) by a resuming candidate.
`runtime_handoff.py` defines the exact six milestones, in order:

```
runtime_descriptor_validated
runtime_candidate_staged
runtime_candidate_verified
runtime_activation_requested   <- SAFE_YIELD_MILESTONE (D3-E/F)
runtime_activation_accepted    <- MUTATION_GATE_MILESTONE (D3-K)
runtime_generation_committed
```

The supervisor's own `ActivationPhase` state machine (D2) is a
SEPARATE, independently durable record of the same real-world handoff
— this list is the worker job's own minimal identity evidence, never a
wholesale copy of supervisor slot-activation state.

### D3-E/D3-F — durable job ownership transfer, proven not asserted

`Executor._execute_runtime_handoff()`: for a job whose target release
declares `protected_runtime`, the old worker validates just enough to
know a handoff is required, stages+publishes the candidate, requests
activation, and — once `runtime_activation_requested` is durable —
calls `self.store.close()` (releasing the real `fcntl.flock` exclusive
`.daemon.lock`) and **returns without ever calling
`store.succeed()`/`store.fail()`**, leaving the job durably `"running"`
and open for whichever worker the supervisor starts next.

`test_phase_d3_executor_handoff.py`'s
`test_lock_ownership_transfers_to_a_second_jobstore_after_yield` proves
this directly: a **second, independent `JobStore` instance** cannot
acquire the lock before the old worker yields (`JobError`), and *can*
acquire it immediately afterward — the real OS primitive, not a
narrative claim. A re-entrant call against the same job_id from the
same old process (e.g. a Django submission retry racing the first
worker thread) is idempotent, absorbed by the supervisor's own
transaction-id correlation (D3-A) rather than starting a second,
conflicting transaction.

Bounded-timeout termination of the old worker PROCESS itself (as
opposed to its job-store lock, proven above) reuses D2's own
already-tested `worker_lifecycle.py`/`process.TrackedChild.terminate()`
primitives — real supervisor event-loop wiring that actually calls
`terminate()` when a worker overstays a bound is D4 work, matching D2's
own established "not yet wired to a real event loop" scope pattern for
that module.

### D3-G/D3-H — candidate readiness and job recovery

- `runtime_handoff.classify_handoff_recovery()` — pure function:
  distinguishes ordinary clean startup from a Phase-D handoff resume of
  exactly one durable job, requiring the job's own recorded
  `protected_runtime_candidate` generation/descriptor to match what
  this candidate process was actually activated as, and requiring
  `SAFE_YIELD_MILESTONE` to already be present (a job whose old worker
  never reached a legal yield point is never treated as resumable).
  Raises (never guesses) on more than one candidate or a mismatch.
- `daemon.UpdaterDaemon` gained optional
  `expected_handoff_generation`/`expected_handoff_descriptor_sha256`
  constructor parameters (both `None` by default — every existing/
  non-Phase-D daemon startup is unaffected). When both are supplied,
  `recover_jobs()` consults `classify_handoff_recovery()` first and
  resumes the matched job specifically; otherwise it falls back to its
  original, unchanged "at most one active job" rule.
- D2's own `readiness.py` contract (facts a candidate must prove:
  slot/generation/descriptor SHA, bootstrap protocol, wire protocol,
  config parsed, privilege-drop self-check, job-store lock acquired,
  worker socket bound) remains the target shape for a real candidate
  startup script to report — wiring an actual `updaterd.py` entrypoint
  to emit `ReadinessFacts` over the bootstrap protocol is D4 work (D2's
  own `readiness.py` docstring already flagged this as D3's job; the
  facts contract and its classification are complete and tested, only
  the real startup script's own emission is not yet written).

### D3-I — independent trusted-plan re-derivation

Achieved by construction, not a new module: `Executor.execute()`
**always** re-fetches trusted Git and calls `derive_plan()` from
scratch on every single invocation, then refuses
(`PLAN_FINGERPRINT_MISMATCH`) unless the freshly-derived fingerprint
equals the job's own durably-stored `expected_plan_fingerprint`. A
candidate worker resuming a handoff-yielded job therefore performs
EXACTLY the same independent re-derivation an ordinary fresh job
already required — no new code path, no new trust decision — proven by
`test_phase_d3_fingerprint_v3.py`'s cross-boundary parity and by
`test_phase_d3_executor_handoff.py`'s own idempotent-reentry test.

### D3-J — fingerprint contract v3

`release.py`'s `protected_runtime_fingerprint_payload()` (already
written in D1) is now what `derive_plan()` actually uses whenever the
TARGET release declares `protected_runtime` — `TrustedPlan` gained a
`protected_runtime: ProtectedRuntimeField | None = None` field
(default preserves every existing `TrustedPlan(**data)` test fixture).
`updatecenter/execution_contract.py` gained an independently-maintained
mirror, `protected_runtime_fingerprint_payload()`/
`protected_runtime_execution_fingerprint()`, and `planner.py`'s
`build_plan()` now selects v2/v3 the same way. `test_phase_d3_
fingerprint_v3.py` proves Django's and the worker's independent v3
computations are byte-identical for the same facts, that v3 preserves
every v2 fact unchanged except `contract_version`, and that v2/v3
never accidentally collide.

### D3-K — mutation gate

`runtime_handoff.require_mutation_allowed()`: a complete no-op for an
ordinary release; for a `protected_runtime` release, refuses
(`MutationGateError`) unless `runtime_activation_accepted` is already a
durable milestone. Called individually at **every** production-
mutating call site in `Executor.execute()` — checkpoint/migration,
source advancement, collectstatic, systemd reconciliation, service
restarts — never once at the top of the function, so a future new
mutating step must add its own gate call to compile into the intended
behavior; a missing call is a missing call at that one site, not a
globally bypassed check. `Executor._require_mutation_allowed()`
converts the refusal into `ExecutionError("RUNTIME_ACTIVATION_NOT_
ACCEPTED", ..., manual=True)`.

### D3-L — candidate-ready vs. activation-accepted

Unchanged from D2: `ActivationPhase.CANDIDATE_READY` and `COMMITTED`
remain genuinely distinct phases (`activation.runtime_activation_
accepted()` is true only for `COMMITTED`), and D3's own worker-side
`MUTATION_GATE_MILESTONE` is `runtime_activation_accepted` — a fact the
WORKER records only once the supervisor's own activation response
confirms the transaction reached that phase, never merely because a
candidate process bound a socket.

### D3-M/D3-N — failure handling

Covered directly by tests in this pass: bad candidate signature (fails
closed, transaction returns to idle, in-memory transaction-id
correlation dropped), wire-claimed digest/generation mismatch against
independently-derived facts (refused), a second concurrent activation
request while one is in flight (refused, first request unaffected), a
missing/unbootstrapped supervisor configuration
(`UNBOOTSTRAPPED_SUPERVISOR`, `manual=True`), and a transport-absent
supervisor socket (`SupervisorTransportError`, classified distinctly
from an explicit rejection). Every one of these is provably PRE-
`runtime_activation_accepted`, so D3-K's own gate structurally
guarantees no production mutation occurred. Post-acceptance failure
handling is intentionally unchanged from existing Phase-B semantics
(fail/manual-intervention as appropriate; no automatic runtime
rollback) — no new code was added there, matching D3-N's own explicit
instruction.

### D3-P — Django-facing outage semantics

No production code changes were required: `updatecenter/job_service.
py`'s existing retry-once/`SUBMISSION_UNCERTAIN`/reconcile-via-
`GET_JOB_STATUS` design (built for ordinary transient socket
unavailability) already satisfies D3-P's requirements exactly, proven
directly against a real Unix socket that is absent, then present, in
`test_phase_d3_django_outage_semantics.py`: a submission during the
outage window becomes `SUBMISSION_UNCERTAIN` (never `FAILED`), the
active lock is retained, and reconciliation once the socket returns
updates the SAME `UpdateJob` row — no new job is ever created.

### What remains before D4/production (explicit)

- A real candidate `updaterd.py` startup path that reports
  `ReadinessFacts` over the bootstrap protocol and calls
  `UpdaterDaemon` with `expected_handoff_generation`/
  `expected_handoff_descriptor_sha256` populated from its own launch
  arguments.
- A real supervisor event loop that actually calls `launch_worker()`,
  polls readiness, and calls `TrackedChild.terminate()` on a bounded
  timeout — `worker_lifecycle.py`'s policy and `ipc_server.py`'s
  protocol handling are both ready to be driven by it.
- Letting a signed policy introduce a genuinely NEW unit name (not
  merely override an existing one's activation behavior) — requires
  resolving the `KNOWN_MANAGED_UNITS`-is-a-module-constant ordering
  question noted under D3-C above.
- The D3-R Weather-unit policy-change proof fixture specifically (the
  underlying mechanism — `resolve_unit_policy()` overriding an
  existing unit's policy via signed data alone — is implemented and
  tested in `test_phase_d3_signed_policy.py`; the *end-to-end* fixture
  with a real second-generation signed release was not built this
  pass).
- No r0026/r0027 manifests, no root install, no systemd activation —
  all explicitly out of scope for D3 and untouched.

## D4 status — functional runtime completion (harness-proven, not deployed)

D4 closes the five gaps D3 explicitly left open: a real supervisor
event loop, a real candidate startup/readiness handshake, actual same-
job resumption through to downstream mutation, signed-policy authority
over genuinely new managed-unit names, and the Weather-unit end-to-end
mechanism. Everything below is exercised by real, non-mocked tests
(real subprocesses launched by a real event loop, real Unix sockets,
real openssl Ed25519 signing) — **nothing has been deployed, installed,
or run as root**.

### D4-A — real supervisor daemon

New `isadoraair_updater_bootstrap/supervisor_daemon.py`:
`SupervisorDaemon` recovers any interrupted activation conservatively
at startup (D2's own `recovery_action_for`/`apply_recovery`), launches
the active slot's worker, runs the IPC server in a background thread,
and polls the activation transaction's own phase to react: stop the
old worker at the approved boundary (waiting for its voluntary exit,
force-terminating past a bound), launch the candidate, and roll back
pre-acceptance on candidate crash or either of two now-real timeouts
(readiness, and acceptance). Never gains release-chain planning,
migrations, application Git manipulation, systemd unit policy,
collectstatic, pg_dump, or Django — none of that code exists in this
module. `updater_bootstrapd.py` now actually drives it.

### D4-B/D4-C — real candidate startup and readiness

`updaterd.py` gained four fixed-shape candidate-identity arguments
(`--expected-slot/--expected-generation/--expected-descriptor-sha256/
--expected-job-uuid`) and a two-phase installation-safety check (its
own directory's basename must match the expected slot immediately;
`_ENTRY_ROOT.parent` must equal the supervisor's own configured
`slots_root` once config is trusted). `launch.launch_worker()` gained
a strict `CandidateIdentity` parameter -- the only way these four
values ever reach a candidate's argv. `UpdaterDaemon.report_candidate_
readiness()` reports every D4-C fact (slot, generation, descriptor
SHA, bootstrap/wire protocol, config-parsed, privilege-drop self-
check, job-store lock actually held, worker socket actually bound,
trusted repository usable, resumable job UUID) -- each one reflecting
something already independently verified by the time it's reported,
never an optimistic hardcode. `ipc_server._handle_report_readiness()`
independently re-checks every fact against the supervisor's own
activation transaction before ever marking a candidate ready.

### D4-D — same-job resumption, proven concurrency-safe

`Executor.execute()`'s handoff branch became a real three-way split
(not the D3 binary one): mutation-gate-satisfied falls through to the
ordinary pipeline; safe-yield-present-but-not-yet-accepted is handled
ONLY by a process whose own `expected_handoff_generation/descriptor_
sha256/resumable_job_uuid` (bound at `Executor.__init__`, populated
only by a real candidate launch) match this exact job -- any other
process (including the old worker re-entering `execute()` after it
already yielded) takes no action at all; neither milestone present is
the old worker's own first pass. Mutation authority is therefore bound
to PROCESS IDENTITY the supervisor itself assigned at launch, never to
job state alone. `test_phase_d4_supervisor_daemon.py`'s real end-to-end
test proves the full chain reaches a committed generation with the
correct previous-slot/generation retained as LKG.

### D4-E/D4-F — signed policy becomes the runtime-authoritative unit allowlist

**Real bug found and fixed**: `SystemdManager._install_one()`'s own
allowlist check still compared directly against the bare compiled
`KNOWN_MANAGED_UNITS` constant, completely bypassing D3-C's own
signed-policy resolution -- a genuinely new, signed-policy-authorized
unit would still have been refused at the INSTALL step even though its
activation policy resolved correctly. Fixed: `release.
resolve_known_managed_units(active_policy=...)` is now the ONE
authoritative known-unit resolver (signed policy first, compiled map
only as the D0/legacy fallback), and `SystemdManager._known_units()`
routes every one of its four allowlist checks through it.
`derive_plan()`/`manual_blockers()`/`_cross_check()` all gained an
explicit `known_units` parameter (defaulted to preserve today's
behavior byte-for-byte when omitted). Regression-tested directly:
`test_phase_d4_new_unit_authority.SystemdManagerNewUnitInstallTests`
proves the SAME new unit is refused under the compiled fallback and
successfully installed+activated under a signed policy that authorizes
it.

### D4-G/D4-H — two-stage authorization

`manual_blockers()` no longer decides unit-name-known-ness at all for
a `protected_runtime` release (deferred, D4-H's own "CURRENT-RUNTIME
EXECUTABLE ACTION vs. ACTION DEFERRED UNTIL VERIFIED TARGET RUNTIME
ACTIVATION" distinction) -- the real gate moved into the handoff
pipeline's own `MILESTONE_RUNTIME_CANDIDATE_VERIFIED` step, which now:
(1) independently re-verifies the just-staged/published candidate
bundle using D1's own worker-side `protected_bootstrap.verification.
verify_candidate_bundle` (built in D1, never actually called from a
real code path until now) -- defense in depth alongside the
supervisor's own independent re-verification, never a replacement for
it; (2) if that fails, refuses outright; (3) resolves the candidate's
own `protected-policy.json` (read only from the already-hash-verified
staged bundle, `runtime_handoff.resolve_candidate_policy_from_bundle`
-- D4-P's own "never from application checkout/database/env" rule);
(4) `verify_new_units_authorized_by_candidate_policy()` requires every
unit outside this worker's OWN active policy to be named by the
candidate's policy AND to agree with the manifest's own predecessor-
diff-checked declared intent. Any failure raises before
`REQUEST_ACTIVATION` is ever sent -- the old worker never executes
anything either way. Point 8 ("new worker independently re-derives the
same fact") is satisfied by construction: the candidate's own
`SystemdManager.reconcile()` call, wired to ITS OWN active policy (D4-
P), already fails closed via its existing "unit has no managed-unit
activation policy" check if that policy doesn't actually contain the
unit.

### D4-I — central mutation-phase barrier

`Executor._enter_mutation_phase()` -- one explicit, unconditional call
sitting exactly at the transition point in `execute()`'s own body,
after the three-way branch resolves and before the first mutating
line. Every existing per-mutator `require_mutation_allowed()` call
remains, unchanged, as defense-in-depth -- both share the identical
underlying rule, so a bug in one is never silently compensated for by
the other.

### D4-J — runtime acceptance / supervisor commit

Two new bootstrap-protocol actions, `REPORT_READINESS` and
`CONFIRM_RUNTIME_ACCEPTANCE` (protocol.py, mirrored in
`supervisor_client.py`) -- both still only bounded identity facts,
still zero path/command/argv fields. `ipc_server._handle_confirm_
runtime_acceptance()` requires `CANDIDATE_READY` (readiness
independently confirmed) and re-checks identity one more time before
ever calling `commit_transaction()` -- never merely because a socket
was bound. `Executor._accept_runtime_as_candidate()` writes
`runtime_activation_accepted` durably FIRST, then informs the
supervisor (a transport failure here is logged, non-fatal -- the
worker's own already-durable acceptance is authoritative for ITS OWN
progression regardless of whether the supervisor's generation-commit
bookkeeping succeeds yet).

### D4-K — recovery

Real-process tests prove: supervisor restart... (recovery_action_for,
unchanged from D2); old worker that never exits voluntarily is force-
terminated past a bound; candidate that crashes before or after
reporting readiness rolls back and restarts the previous worker;
candidate that reports ready but never confirms acceptance now times
out and rolls back too -- **a real gap this pass found and fixed**
(`SupervisorDaemon` previously had no bound on that phase at all and
would have waited forever). `test_phase_d4_supervisor_daemon.py`
exercises all of these against real subprocesses and a real socket.

### D4-L — Django continuity

No new code needed beyond D3-P's own proof -- `job_service.py`'s
existing design already covers this window correctly.

### D4-M/D4-N — integration harness / Weather fixture

Scoped deliberately: `test_phase_d4_supervisor_daemon.py` proves the
REAL process/IPC orchestration (old worker → supervisor → candidate →
committed generation) using lightweight synthetic fixture workers
(matching D2's own established pattern for this exact reason -- the
real `updaterd.py` cannot run without root in any environment, test or
production); `test_phase_d4_new_unit_authority.py`/
`test_phase_d4_adversarial_new_unit.py` prove the new-unit
authorization mechanism itself, end to end, with real signing, against
the real `SystemdManager.reconcile()` call. A SINGLE further harness
combining a full real Django "application" fixture (migrations,
`manage.py`, `updatecenter_probe`) with the real trusted-Git chain, the
real supervisor daemon, AND a real subprocess candidate all in one test
-- proving an *observable synthetic systemd mutation specifically after
runtime acceptance*, and the full four-forecast-unit Weather fixture
-- was not built this pass; the pieces it would combine are each
independently, genuinely proven above and in D3's own harness. Left
for D4 follow-up / D5.

### D4-P — worker's own signed-policy source

`StationConfig` gained `phase_d_trust_policy_path`/`phase_d_signer_root`
(both-or-neither, matching D3's `phase_d_supervisor_*` pattern) -- the
SAME root-owned trust material the supervisor uses, read-only for the
worker. `Executor._load_phase_d_trust_policy()` loads it; `Executor.
active_policy` (constructor parameter, `None` by default) is what
`SystemdManager`/`resolve_known_managed_units` actually consult. Never
loaded from application checkout, live Git working tree, database, an
environment variable, or an arbitrary station config path.

### D4-Q — process security preserved

`git diff --stat` against `deploy/updater-bootstrapd.service` and
`deploy/isadoraair-updater.service` is empty -- neither file was
touched. D2's own capability regression suite
(`test_phase_d2_supervisor_capability.py`) and the broader security
suite (`test_phase_b_security.py`) both pass unchanged.

### What remains before D5/production (explicit)

- The combined full-Django-app + full-trusted-Git + real-candidate-
  subprocess harness proving an observable mutation strictly after
  runtime acceptance in ONE end-to-end run (D4-M's own strongest form),
  and the full four-unit Weather fixture (D4-N) -- the underlying
  mechanisms are each independently proven; only the single combined
  demonstration was not built.
- Making the pg_dump/canonical-runtime-path/TTS-launcher environmental
  test failures hermetic (pre-existing, unrelated to Phase D, already
  tracked).
- Everything D3's own "what remains" list already named that D4 did
  not touch: production signer keys, `/etc/isadoraair/` install,
  `/usr/local/libexec/` install, the Phase-D systemd unit's actual
  activation, retiring the old updater, r0026/r0027.

## D5 pre-bootstrap integration contract

D5 implements the release-authoring and recovery substrate before any station
is bootstrapped. It does not install or activate Phase D. The current composed
fixtures use the real mutation gate, active/candidate signed-policy rules,
`SystemdManager`, Django audit mirror and Unix backend transport. They do not
yet constitute the mandatory single-process-chain acceptance run: the real
production `updaterd.py` correctly refuses non-root execution and verifies
UID-0-owned protected ancestry, while this host provides neither a privileged
disposable harness nor unprivileged user namespaces. No test bypass is added to
that security boundary. A privileged disposable run of the real supervisor,
old worker and real candidate remains a D5 acceptance blocker. In the Weather
authority case generation N does not recognize any of the four
`wx-forecast-{1day,3day}-{day,night}.service` names; generation N+1 adds all
four as `INSTALL_ONLY`. The reviewed templates differ only by
`--voice day/night` becoming `--voice auto`. No timer changes, service starts,
enables, restarts, migration, or checkout advancement occur before the durable
`runtime_activation_accepted` milestone.

The D5 release tool is
`deploy/updater_bootstrap/tools/sign_release_bundle.py`. The safe workflow is:

1. Generate canonical generation-1 policy from the compiled D0 authority.
2. Build the descriptor from the closed protected-runtime source inventory.
3. Review the source diff, policy, descriptor and exact attestation statement.
4. Sign the statement with an explicitly named private key. The tool invokes
   only `/usr/bin/openssl` with fixed argv, never a shell or caller command.
5. Verify each signature against its explicitly named public key.
6. Run `manage.py validate_protected_runtime_release` with a public trust
   fixture and predecessor facts before sealing the release.

The private key is never inferred, logged, placed in the repository or copied
to a station. Symlink, hard-linked, group/world-accessible, or non-regular
private-key paths are refused. Rebuilding unchanged input yields byte-identical
policy, descriptor and statement bytes.

### Initial signer recommendation and rotation

For KOGR/WRJE's current single-operator reality, begin with **1-of-2**: one
offline primary release key plus a separately stored recovery/rotation key.
Signing twice with two keys controlled and stored by the same person in the
same place does not materially add independent authorization. **2-of-2** is
appropriate only with genuinely separate custodians and has the availability
risk that loss of either key blocks every protected update. **2-of-3** is the
preferred future multi-custodian posture when three real, separately controlled
keys/custodians exist.

A trust-root transition must be authorized by the currently trusted threshold,
install the new public root, and only after an accepted transition retire the
old root. Worker/policy evolution and rotations representable inside the
established signed trust model require no station SSH. A change to the immutable
supervisor's root-config/trust parser or recovery from loss of all threshold
keys still requires a privileged bootstrap/recovery operation; D5 does not
pretend otherwise.

### Final manual bootstrap inventory (do not execute in D5)

| Artifact | Class | Owner/mode target | Secret | Recovery payload |
|---|---|---|---|---|
| `deploy/updater_bootstrap/updater_bootstrapd.py` and `isadoraair_updater_bootstrap/*.py` | repo artifact | root:root, dirs 0755, Python 0644, entrypoint 0755 | no | yes |
| `deploy/updater-bootstrapd.service` | repo artifact | root:root 0644 | no | yes |
| `/etc/isadoraair/updater-bootstrap.json` | operator/root config | root:root 0600 | path policy only | yes |
| `/etc/isadoraair/updater-trust.json` | operator/root config | root:root 0644 | public only | yes |
| signer public PEM files | generated release artifacts | root:root 0644 | no | yes |
| generation-1 worker tree and `protected-policy.json` | repo/generated bundle | root:root; dirs 0755, entrypoint 0755, other files 0644 | no | active slot |
| generation-1 descriptor and public attestation wrappers | generated release artifacts | root:root 0644 | no | yes |
| initial `runtime-state.json` | generated bootstrap state | root:root 0600 | no | yes |
| A/B root and staging root | operator/root filesystem | root:root 0750; staging 0700 | no | reconstructed |
| Phase-D station-path additions | operator/root config | root:root 0600 | existing credential policy | yes, encrypted where required |
| legacy `/usr/local/libexec/isadoraair-updater` | rollback-only LKG | retain current protected ownership | no new secret | yes until retirement |

Generation 1 policy is generated programmatically from
`MANAGED_UNIT_POLICIES` and regression-tested for exact unit membership and
identical `ENABLE_NOW`/`INSTALL_ONLY` semantics: no extra and no missing unit.
It is not production-signed in D5.

### Draft release sequence (not immutable manifests)

**Future r0026 — FINAL MANUAL UPDATER BOOTSTRAP.** Remains readable by r0025,
uses the old manifest semantics, sets `manual_bootstrap_required: true`, and
carries the Phase-D application/planner source, immutable supervisor source and
unit, generation-1 worker/bundle tooling and operator ceremony. It does not use
`protected_runtime`, because r0025 cannot safely interpret that field. This is
the last routine manual updater bridge.

**Future r0027 — FIRST AUTOMATIC PROTECTED-RUNTIME UPDATE + WEATHER
AUTHORITY.** Declares `protected_runtime`, advances the signed generation and
policy, authorizes exactly the four Weather services as `INSTALL_ONLY`, and
changes their reviewed templates to `--voice auto`. The entire runtime handoff,
same-job continuation and post-acceptance service-template reconciliation runs
through Update Center with no SSH, sudo, manual copy, updater restart, or root
configuration change.

No final `r0026.json` or `r0027.json` is created by D5.

### Product promise after the final bootstrap

After successful installation of the Phase-D bootstrap supervisor, ordinary
releases that modify protected updater worker code or signed managed-unit
policy do **not** require station-side SSH, sudo, manual file copy, manual
updater restart, or manual root configuration changes. Exceptional privileged
work remains limited to immutable-supervisor implementation/sandbox changes,
trust-root loss with no authorized rotation path, catastrophic protected root
state corruption, and OS-level recovery.

### Capability live-proof runbook (D6, not D5)

1. Record the supervisor unit and process identity and confirm
   `NoNewPrivileges=yes`, `AmbientCapabilities=cap_setuid,cap_setgid`.
2. Submit a synthetic signed no-production-mutation handoff through the normal
   supervisor socket; never invoke a caller-supplied command.
3. Record supervisor, protected worker and final `ISA_USER` PIDs from durable
   readiness/job evidence.
4. Prove the supervisor-spawned worker successfully traversed the fixed
   `runuser` privilege-drop path.
5. Read `/proc/<ISA_USER-pid>/status` and require `CapPrm`, `CapEff` and
   `CapAmb` all equal `0000000000000000`.
6. Fail acceptance if PID identity, ancestry, UID/GID, readiness, or any zero
   capability assertion cannot be established. Never weaken the unit sandbox
   to make the proof pass.

### Observation and rollback after D0

After bootstrap, require the supervisor active, generation 1 worker ready,
ordinary Update Center planning/status healthy, no job ambiguity, and no
production feature change caused merely by D0. Retain the legacy protected
updater as rollback-only material. Retire it only after r0027 succeeds through
the automated path, all four Weather units are installed-only with no timer or
restart side effects, A/B rollback evidence is intact, backup-v3 contains and
validates Phase D, an offline fake-root restore succeeds, and the observation
window has no supervisor restart/readiness/job-continuity faults.

## D5.1C consolidation status

D5.1A completed the privileged single-process-chain acceptance on the
disposable host with the real supervisor, generation-1 worker, generation-2
candidate, signed policy, Django audit mirror and systemd reconciliation.
One durable UpdateJob UUID survived generation-1 yield, supervisor handoff,
generation-2 recovery and mutation. The supervisor committed B/gen2 before
the durable `runtime_activation_accepted` gate opened; A/gen1 remained the
previous known-good slot and the activation record cleared.

The disposable release bridge retained the intended compatibility contract:

- r0026 (`e44696d973f9d7a228c2e4158a29df247767e045`) is parseable by r0025,
  sets `manual_bootstrap_required: true`, omits `protected_runtime`, and
  establishes the immutable supervisor, trust/config/state and signed gen1.
- r0027 (`4a7f27dd76d6bb21c1755aed88acb90ae4684702`) is the first ordinary
  protected-runtime update. Its signed gen2 policy authorizes exactly the four
  Weather service names as `INSTALL_ONLY`; reconciliation performed one
  daemon reload and no enable, start, restart, timer or core-service change.
- The generation-1 bridge exception is limited to the immediate manual
  predecessor carrying Phase-D policy. Ordinary candidates still require
  generation advancement, exact slot/generation/descriptor identity, root
  ancestry, signature threshold and protected-diff authorization.

Schema-2 recovery preserves 0600 protected configuration/state, 0755
entrypoints, canonical A/B slots, `.staging/descriptor-A.json` and
`.staging/descriptor-B.json`, active/previous semantics, public trust/signers,
attestations, policy, exact inventory and restore provenance. It excludes
private signing keys, database payloads, Git credentials and TLS private keys.
D5.1B proved privileged offline DISARMED startup, PING/readiness, refusal of
`START_UPDATE` without job creation, independent gen1/gen2 verification and a
harmless signed gen3 continuity update with no Weather, migration, service or
application-feature mutation.

The one-final-manual-bootstrap product promise is unchanged: after the
production bootstrap, ordinary worker and signed-policy evolution requires no
station SSH, sudo, manual copy, protected-service restart or root config edit.
Manual intervention remains exceptional for immutable-supervisor or sandbox
changes, loss of every authorized trust key, catastrophic protected-state
corruption, and OS-level recovery.

The D5.1 manifests, signing material and host changes are disposable
acceptance fixtures, not KOGR production artifacts. The final production
bootstrap, production signing ceremony and production observation window
remain D6 work.
