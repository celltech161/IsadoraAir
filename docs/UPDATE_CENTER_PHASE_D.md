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
