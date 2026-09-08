# Runtime backup payload contract — Runtime Foundation E7A + E7B

Runtime Foundation E7A defined the durable, machine-readable disaster-
recovery runtime payload contract ("backup v3 runtime payload") that
Foundation E3 (offline TTS) and E4 (native fdkaac) provisioners need to
run completely offline: the artifact shape and its plan/apply
builder/validator API. Runtime Foundation E7B (this checkpoint) wires
that artifact into the real disaster-recovery path: `deploy/
backup_isadoraair.sh` now produces/validates it as part of the nightly
backup (format version 3.0.0), and `deploy/restore/50-native-deps.sh` /
`deploy/restore/70-tts.sh` now consume it, delegating to Foundation
E3/E4's own canonical provisioners instead of their old ad hoc
mechanisms for backup-based recovery. See "Backup v3 integration" and
"Restore integration" below for exactly what changed.

## What this still is not

- Not canonical production activation — nothing here has installed
  Kokoro, Piper, or fdkaac on the production host, enabled the
  `RECOVERY_PAYLOAD_ROOT` persistent location on production, or touched
  `/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`, or `/usr/local`
  on the live station.
- Historical note (CLOSED as of r0029): at the time this checkpoint was
  written, `webrequests/services.py` and `road_conditions/synthesis.py`
  still called their own hardcoded `KOKORO_BINARY` directly, bypassing
  StationTTSVoice. r0029 removed both hardcoded fallbacks outright —
  every current caller resolves a voice through StationTTSVoice or
  fails closed. See "Historical Kokoro-caller blind spot (CLOSED r0029)"
  below for what this changed for the backup policy specifically.
- Not Phase 5's clean-machine restore drill, and not E8's fully offline
  whole-machine acceptance.

## Architecture: reuse, not reinvention

Every piece of *identity* this payload needs already has a Foundation E
authority — E7A/E7B add only the container format and the backup/restore
orchestration around them, never a second copy of any of it:

| Concern | Authority (reused, not duplicated) |
|---|---|
| Product-contract identity | `isadoraair.runtime_bundle.product_contract_digest` |
| Platform/Python ABI identity | `isadoraair.runtime_bundle.current_platform_contract` (via the nested E3 bundle's own check) |
| TTS wheel closure / Kokoro assets / Piper models | `isadoraair.runtime_bundle.load_runtime_bundle` — an ordinary, unmodified E3 `runtime-bundle.json` |
| fdkaac/libfdk-aac source archive identity | `isadoraair.runtime_native.verify_native_sources`, itself reading `runtime_components.json`'s `components.fdkaac.source_archives` |
| Which Piper models the station currently needs (payload build time) | `isadoraair.runtime_requirements.resolve_current_runtime_requirements` (E1) |
| Native fdkaac prepare/publish (restore time) | `isadoraair.runtime_native.NativeRuntimeProvisioner` (E4), via `manage.py provision_runtime_components --fdkaac --recovery-payload` |
| Kokoro/Piper provisioning (restore time) | `isadoraair.runtime_provisioning.RuntimeProvisioner` (E3), via `manage.py provision_runtime_components --recovery-payload` |

A future product-contract or Piper-model hash change breaks an already-
built E7 payload's validation automatically — nothing here forked its
own copy of any of these identities (proven by
`isadoraair/tests/test_runtime_recovery.py::ContractReuseTests`).

## Payload shape

```
<payload-root>/
    runtime-recovery.json      # small, orchestration-only manifest
    tts/                       # an ordinary, unmodified E3 bundle
        runtime-bundle.json
        kokoro/...
        piper/...
    native/
        fdkaac/                # exactly the files
            fdk-aac-2.0.3.tar.gz          declared by
            fdkaac-1.0.7.tar.gz           components.fdkaac.source_archives
```

Both `tts/` and `native/fdkaac/` are independently optional — a payload
may carry either or both, never neither. `tts/` is a completely ordinary
E3 bundle directory; `isadoraair.runtime_recovery` never re-derives or
restates anything about its contents, it only points at it and
re-validates it in place via `load_runtime_bundle`. `native/fdkaac/`
carries *only* the two source archives `runtime_components.json`
already names — no filename/byte/hash information is restated in
`runtime-recovery.json` either; `verify_native_sources` (E4) remains the
sole authority for that identity.

### `runtime-recovery.json`

```json
{
  "schema_version": 1,
  "payload_id": "runtime-recovery-20260901T120000Z",
  "product_contract_sha256": "<sha256 of the full validated product manifest>",
  "built_at": "2026-09-01T12:00:00Z",
  "components": {
    "tts": {
      "path": "tts",
      "bundle_id": "<the nested bundle's own bundle_id>",
      "manifest_sha256": "<the nested bundle's own runtime-bundle.json hash, captured at build time>"
    },
    "native_fdkaac": { "path": "native/fdkaac" }
  },
  "piper_selection_sha256": "<sha256 of the station's currently-required Piper models, or the empty-selection digest>"
}
```

`components.tts.manifest_sha256` is not self-referential (it hashes a
*different* file, the nested bundle's own manifest, never
`runtime-recovery.json`'s own bytes) — it is a cheap tamper-evidence
cross-check: if the embedded `runtime-bundle.json` was edited after the
recovery payload was built, this catches it immediately, and
`load_runtime_bundle` catches everything else (wheel/asset/model
content) regardless.

Platform/ABI identity is deliberately **not** duplicated at the
top level — the embedded E3 bundle already carries and validates its
own `platform` block against the current host on every load. Restating
it here would be a second, potentially-disagreeing source of truth for
no benefit; evidence output surfaces it (derived from the nested
bundle) instead of storing it twice.

## Two distinct policies — do not conflate them

E7A/E7B has **two** separate "is this required" decisions, made at
different times, by different mechanisms, on purpose:

1. **Inclusion policy (payload-build time)** — did the operator supply
   real `--tts-bundle`/`--native-source-dir` material when running
   `prepare_runtime_recovery_payload --apply`? Entirely a human
   decision; the builder never consults E1 to decide what to include.
   See "Inclusion policy" below.
2. **Recovery-component policy (nightly-backup time)** — does the
   *currently selected* payload (whatever's activated at
   `RECOVERY_PAYLOAD_ROOT/current`) still contain what an operator has
   declared the station requires for disaster recovery? This is
   `isadoraair.runtime_recovery.evaluate_recovery_policy`, driven by
   `deploy/backup_isadoraair.sh`'s `BACKUP_REQUIRED_RECOVERY_COMPONENTS`
   env var (empty/unset by default — no policy configured, no new
   failure mode). See "Backup v3 integration" below.

Both deliberately never consult E1's `required` flag for Kokoro, for
the same underlying reason (see next section).

## Inclusion policy

### Kokoro — operator-declared payload preparation, never gated on E1's `required` flag

This section is about *payload preparation* (`RuntimeRecoveryBuilder` —
what material gets physically embedded), a separate concern from the
*backup policy* question of what a payload must already contain (see
"Historical Kokoro-caller blind spot (CLOSED r0029)" above).

**Historically (CLOSED as of r0029):** before then, Runtime Foundation
E1's station-requirement resolver could resolve `kokoro.required =
False` even while a station operationally depended on Kokoro, because
`webrequests/services.py`'s and `road_conditions/synthesis.py`'s own
hardcoded `KOKORO_BINARY` constants bypassed `StationTTSVoice` entirely.
r0029 removed that gap — E1 now sees a station's real Kokoro demand.

Payload preparation nonetheless remains **operator-declared** — Kokoro/
native material is present in a payload because the operator supplied
real `--tts-bundle`/`--native-source-dir` material for it, full stop.
The builder never consults `resolve_current_runtime_requirements()` to
decide whether to include or omit Kokoro. This is now a deliberate,
independent design choice (an operator explicitly chooses what to
embed) rather than a historical-gap workaround — the separate backup
POLICY question (r0046, above) is what actually enforces that whatever
gets embedded matches the station's current, automatically-resolved
need.

For the same reason, **restore-time** provisioning (see "Restore
integration" below) still never re-derives "is Kokoro required" from
the freshly-restored database — the embedded bundle reflects whatever
the backup's policy already justified, which can legitimately have
diverged from the database by restore time; re-deriving it here would
publish material that doesn't match what was actually validated and
embedded.

### Piper — station-aware, safely reuses E1

Piper has no equivalent historical-caller bypass anywhere in this
codebase (confirmed by inspection: no hardcoded Piper invocation exists
outside the new `isadoraair.tts.*` dispatcher). E1's live resolution is
therefore a safe, authoritative signal for Piper specifically. The
builder computes `piper_selection_sha256` from
`isadoraair.runtime_requirements.resolve_current_runtime_requirements()`
by default whenever the embedded TTS bundle actually carries a `piper`
component (never as an unconditional side effect of every `apply()`
call — a native-only or Kokoro-only payload needs no station database
at all to build); an explicit `piper_selection` argument overrides live
resolution for tests or deliberate reproducibility. `docs/RUNTIME_
DEPLOY_BASELINE.md`'s own package-prerequisite section already
documents this exact class of gap for Kokoro at the *baseline* layer —
this is the same finding, applied here to *payload inclusion*.

Only the models a `StationTTSVoice` row actually references are ever
required for station-aware freshness — arbitrary `.onnx` files are never
scooped up. At restore time, the bundle-derived model/config digest must
equal the payload's recorded selection digest, and that digest must equal
the freshly restored DB's E1 selection. E3 receives those DB-owned Piper
requirements only after all three identities match (see "Restore integration").

### fdkaac — source material only, never a shortcut authority

The payload carries *only* the two immutable source archives E4 already
names. It never carries a copy of `/usr/local/bin/fdkaac`, a built
shared library, or an arbitrary build tree as a "reconstruction
shortcut" — the recovery authority remains exactly:

```
source archives -> Foundation D build authority -> E4 prepare
                 -> E4 protected publication -> E2 validation
```

E7 only makes the first step of that chain available offline; restore
now drives the rest of that exact chain (see "Restore integration").

## Preparation interface

`isadoraair.runtime_recovery.RuntimeRecoveryBuilder` — plan/apply,
mirroring E3/E4/E5's own established shape:

```python
from isadoraair.runtime_recovery import RuntimeRecoveryBuilder

builder = RuntimeRecoveryBuilder()
plan = builder.plan(
    tts_bundle="/path/to/an/existing/e3/bundle",
    native_source_dir="/path/to/fdkaac/sources",
    output="/path/to/new/payload",
)
if plan.ready:
    result = builder.apply(
        tts_bundle="/path/to/an/existing/e3/bundle",
        native_source_dir="/path/to/fdkaac/sources",
        output="/path/to/new/payload",
    )
```

CLI equivalent (adds a third `--activate` mode, Runtime Foundation
E7B — see "Persistent payload location"):

```bash
python manage.py prepare_runtime_recovery_payload --plan \
    --tts-bundle /path/to/bundle --native-source-dir /path/to/sources \
    --output /path/to/new/payload

python manage.py prepare_runtime_recovery_payload --apply \
    --tts-bundle /path/to/bundle --native-source-dir /path/to/sources \
    --output /path/to/new/payload

python manage.py prepare_runtime_recovery_payload --activate \
    --base-root /var/lib/isadoraair/runtime-recovery --payload-id runtime-recovery-20260901T120000Z
```

Guarantees:

- `plan()` is entirely read-only (proven:
  `PlanApplyTests.test_plan_has_zero_filesystem_mutation`);
- `apply()` requires a caller-owned destination that does not already
  exist — no silent overwrite of an existing payload;
- `apply()` builds under a same-parent temporary sibling and only
  `os.rename()`s it into place after the freshly-copied payload
  re-validates cleanly end-to-end — a failed apply leaves nothing at
  `--output` (proven:
  `FilesystemSafetyTests.test_failed_apply_leaves_no_partial_payload_at_output`);
- `activate_recovery_payload()` validates `<base-root>/payloads/<id>`
  cleanly *before* touching anything, then atomically repoints
  `<base-root>/current` at it (symlink written to a same-directory temp
  name, then `os.replace()`d — the same atomic-pointer pattern E3's own
  `_atomic_pointer` uses) — it never mutates the payload directory
  itself, and never overwrites a payload already at
  `<base-root>/payloads/<id>` (proven:
  `PersistentLocationTests.test_activate_never_overwrites_a_payload_directory`);
- never mutates `/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`, or
  `/usr/local`; never restarts a service; never migrates a caller;
  never invokes `sudo` internally;
- every input is a caller-supplied local path — no network fetch, no
  acquisition mode of any kind exists anywhere in this contract.

## Validation interface

`isadoraair.runtime_recovery.validate_recovery_payload` — read-only,
structured, never raises for a bad/missing/tampered payload (only for a
genuinely unexpected internal error), callable directly from Python so
backup/restore orchestration never needs to parse human CLI text:

```python
from isadoraair.runtime_recovery import validate_recovery_payload, RESULT_PASS

evidence = validate_recovery_payload("/path/to/payload")
if evidence.result != RESULT_PASS:
    ...  # evidence.to_dict() carries full structured detail
```

`validate_current_recovery_payload(root)` is the DB-aware convenience
wrapper (resolves live Piper selection, falling back to `not_checked`
rather than a guess if the database can't be inspected — mirroring
Runtime Foundation E6's own bootstrap-safe design).

CLI equivalent, extended for Runtime Foundation E7B with `--base-root`/
`--current` (resolve the persistent-location pointer instead of taking
a direct path) and `--require` (repeatable; an explicit, operator-
declared recovery-component policy check — see "Recovery-component
policy" below):

```bash
python manage.py validate_runtime_recovery_payload <path>
python manage.py validate_runtime_recovery_payload \
    --base-root /var/lib/isadoraair/runtime-recovery --current \
    --require kokoro --require native_fdkaac --json
```

Exit codes with `--current` (a direct path use only ever uses 0/1,
matching every other Foundation E validator): `0` valid and satisfies
any `--require` policy; `1` a genuine failure (invalid/tampered/stale,
or fails an explicit `--require`) — always fatal; `2` **not
configured** — `--base-root/current` was never set up on this host at
all, distinct on purpose so a caller (the nightly backup) can choose to
warn-and-continue only when *no* policy requires this payload to exist
yet, and always hard-fail on a genuinely broken one.

Fails closed for: missing/malformed top-level manifest, wrong schema
version, wrong product-contract digest, a component path escaping the
payload root (absolute, `..`, backslash), a missing declared file, an
extra undeclared file where strict closure applies, a symlink anywhere
in the tree, a hardlinked file, a non-regular file, a changed byte/hash
anywhere in the embedded bundle or native archives, an incompatible
platform/ABI, a malformed nested E3 bundle, an invalid native source
archive, a stale Piper station-model requirement, an unknown or
duplicate component, an unsatisfied `--require` policy, and any
unsupported manifest field.

## Recovery-component policy (Runtime Foundation E7B, automatic policy r0046)

`isadoraair.runtime_recovery.evaluate_recovery_policy(evidence,
required_components)` — a small, evidence-only overlay on top of
`RuntimeRecoveryEvidence`, answering "does the *currently selected*
payload still positively contain every one of these component names":

- `RECOVERY_POLICY_COMPONENT_NAMES = {"kokoro", "piper", "native_fdkaac", "protected_updater"}`
  — generic component names, never a station name, never hardcoded to
  Oak Grove or any other specific station.
- A component named in the policy but **absent, invalid, or (for
  Piper specifically) not confirmed current** counts as unsatisfied —
  `not_checked` is never silently promoted to satisfied. Piper's own
  station-model freshness check already fails closed to `not_checked`
  when the database can't be inspected (`validate_current_recovery_payload`);
  this policy layer treats `not_checked` the same as absent for
  anything the policy actually requires.
- Not-required components may be absent with no penalty — the policy is
  strictly opt-in per component, never "everything must be present."

**Where the `required_components` set itself comes from** is a separate
question, answered one of two ways:

1. **Automatic (r0046, the normal path)** —
   `isadoraair.runtime_recovery.resolve_automatic_recovery_policy()`
   derives it directly from `isadoraair.runtime_requirements.
   resolve_current_runtime_requirements()` (Runtime Foundation E1):
   `kokoro`/`piper` map straight across, `fdkaac` maps to
   `native_fdkaac`. `protected_updater` comes from an *independent*
   product/deployment rule (`protected_updater_is_required()`) that
   never reads the payload/component itself — see "Historical
   Kokoro-caller blind spot (CLOSED r0029)" below for why this became
   safe, and "protected_updater: an independent product/deployment
   rule" for that component specifically. Raises `RuntimeRecoveryError`
   (fail closed) if the station's current configuration can't be
   resolved at all (e.g. an invalid weather voice schedule) — it never
   guesses a policy from indeterminate configuration.
2. **Explicit (advanced/test override)** — `--require`/
   `--require-components`, or `deploy/backup_isadoraair.sh`'s
   `BACKUP_REQUIRED_RECOVERY_COMPONENTS` env var (comma-separated). When
   set, this REPLACES the automatic policy entirely for that run — the
   two are mutually exclusive at the CLI layer
   (`validate_runtime_recovery_payload --require-current-station-policy`
   vs `--require`/`--require-components`).

`deploy/backup_isadoraair.sh` uses the automatic policy by default (no
special environment override needed) and only falls back to the
explicit `BACKUP_REQUIRED_RECOVERY_COMPONENTS` path when an operator
sets it — see "Backup v3 integration."

### Historical Kokoro-caller blind spot (CLOSED r0029)

Before r0029, `webrequests/services.py`'s and
`road_conditions/synthesis.py`'s own hardcoded `KOKORO_BINARY =
"/home/jreed/kokoro/bin/kokoro_synth"` constants let a station
operationally depend on Kokoro (both features live, actually
synthesizing through that binary) while bypassing `StationTTSVoice`
entirely — so Runtime Foundation E1 could resolve `kokoro.required =
False` even though Kokoro was genuinely required. r0029 removed both
hardcoded fallbacks outright (`road_conditions/voice.py`'s
`resolve_voice()` now raises `VoiceResolutionError` immediately instead
of falling back; `webrequests/services.py`'s dedication synthesis raises
`TTSConfigurationError` when no voice is configured) — every current
caller resolves a voice through `StationTTSVoice` or fails closed. E1's
`resolve_current_runtime_requirements()` therefore now sees a station's
complete, current Kokoro/Piper/fdkaac demand with no blind spot, which
is exactly what makes `resolve_automatic_recovery_policy()` (above)
safe to introduce for kokoro/piper/fdkaac. This historical gap is why
Kokoro/native *payload preparation* (`RuntimeRecoveryBuilder`, see
"Kokoro — operator-declared" below) remained a deliberately separate,
operator-driven step rather than something r0046 also automates — an
operator still explicitly chooses which physical material to embed in a
payload; r0046 only changes whether the BACKUP considers a given
payload adequate.

### protected_updater: an independent product/deployment rule

`protected_updater` has no station-configuration analogue at all — it
isn't "selected" the way a TTS voice or an AAC encoder is. Its
automatic requiredness rule (`protected_updater_is_required()`) instead
answers "does this installation run the Phase-D Update Center
architecture" by checking whether `updatecenter` is installed in
Django's app registry — unconditionally true for every current
IsadoraAir 1.2+ deployment (`isadoraair/settings.py`'s
`INSTALLED_APPS`, not a per-station choice). This is deliberately NEVER
derived from the recovery payload's own state (whether a
`protected_updater` component happens to be present, valid, or
readable) — a corrupted or missing payload/component must never be able
to remove its own backup requirement.

## Persistent payload location (Runtime Foundation E7B — established and activated on production)

`isadoraair.runtime_recovery`:

```
<base-root>/
    payloads/
        <payload-id-one>/        # an ordinary, immutable, already-
        <payload-id-two>/        # validated payload directory each --
        ...                      # never overwritten in place once written
    current -> payloads/<payload-id>     # a symlink, atomically repointed
```

- `resolve_current_recovery_payload_root(base_root)` reads exactly one
  thing — the `current` symlink — never scans `payloads/` for "the
  newest one." It follows exactly **one** symlink hop (via
  `os.readlink()`, not `Path.resolve()`, which follows an unbounded
  chain — a real bug caught and fixed during E7B by
  `PersistentLocationTests.test_symlinked_payload_id_directory_is_rejected`),
  confines the target strictly inside `payloads/`, and rejects the
  target itself being a further symlink.
- Raises `RecoveryPayloadNotConfiguredError` (a distinct subclass) when
  `base_root` or `current` simply doesn't exist yet — never generically
  "the same kind of failure" as a broken/tampered pointer. This is what
  lets the CLI's exit code 2 exist (see "Validation interface").
- `activate_recovery_payload(base_root, payload_id)` is the only way
  `current` ever moves — validates first, atomic pointer swap second,
  never touches the payload directory.
- The trust boundary is enforced, not merely documented: base root,
  `payloads/`, selected payload and every nested directory must have the
  expected administrative owner (UID 0 by default) and mode 0755; every
  file must be owner-matching, single-link, regular, non-symlink mode
  0644 -- **except** (r0031, narrowed by r0035) a file under an
  activated payload's own `protected-updater/` subtree, which may
  instead be 0644 or 0755 (an executable entrypoint --
  `updater_bootstrapd.py`/`updaterd.py`), never anything more permissive
  than that (`isadoraair.runtime_recovery.
  _PROTECTED_UPDATER_TRUSTED_FILE_MODES`); `current` must be an
  owner-matching confined one-hop symlink. Runtime service identities
  can traverse/read but cannot modify the source.

  **Backup-readable storage vs. restored modes (r0035).** Before r0035,
  `capture_phase_d_component` preserved each protected config/state
  file's real installed mode straight into the payload --
  `station.json`/`updater-bootstrap.json`/`runtime-state.json` at 0600
  root:root, exactly matching the live system. That preservation is
  what made the very first production `--apply --phase-d` backup fail:
  `isadoraair-backup.service` runs unprivileged, as `jreed`, and
  `validate_runtime_recovery_payload`'s inventory pass reads full file
  content to compute hashes, which a 0600 root-owned file structurally
  refuses to any non-owner. Those three files are now stored at the
  same uniform, backup-readable `isadoraair.phase_d_recovery.
  PHASE_D_STORAGE_MODE` (0644) every other Foundation-E component
  already uses -- none of them contain a secret, credential, or private
  key (paths, socket locations, bookkeeping; private signing keys
  remain categorically absent, unaffected). Each file's TRUE,
  deliberately-restrictive installed mode is recorded separately in the
  restore manifest's own `restore_modes` field and re-applied
  explicitly by `restore_phase_d_component` at actual restore time --
  never inferred from whatever mode the payload copy happens to carry.
  Restore-mode fidelity (0600 landing on the real/staging restore
  target) is therefore unaffected; only the payload's own at-rest
  storage representation changed. `runtime-descriptors/` and
  `runtime-attestations/<role>/` are also now normalized to 0644
  unconditionally at capture time (their real installed modes were
  never a deliberate contract, only whatever the real worker/
  supervisor's own process umask happened to produce when staging
  them) -- and `runtime-descriptors/` no longer duplicates the
  installed `.staging/attestations-<slot>/` subtree, which was
  previously copied in wholesale but never read by anything.
- **Activated on production** (r0031) — `RECOVERY_PAYLOAD_ROOT` in
  `deploy/backup_isadoraair.sh` defaults to
  `/var/lib/isadoraair/runtime-recovery`; production's `current` there
  selects a real, validated payload, kept current via
  `prepare_runtime_recovery_payload --phase-d --apply` +
  `--activate` -- see "Publishing a schema-2 Phase-D recovery payload"
  below for the full operator workflow and when to re-run it. A host
  that has genuinely never run this at all still gets the honest,
  original E7B behavior described in the rest of this bullet's history:
  the nightly backup finds nothing configured (exit code 2) and, with
  no `BACKUP_REQUIRED_RECOVERY_COMPONENTS` policy set, continues without
  a `runtime-recovery/` payload, labeling the result format 2.1.0 /
  `legacy_non_self_contained`.

## Publishing a schema-2 Phase-D recovery payload (r0031)

`manage.py prepare_runtime_recovery_payload`'s `--phase-d` flag (a
modifier on the existing `--plan`/`--apply`, alongside the unchanged,
fully schema-agnostic `--activate`) derives a fresh
`protected_updater` component from THIS host's own installed,
currently-active Phase-D state and attaches it to the current
Foundation-E payload as a new schema-2 payload -- no protected
filesystem path is ever operator-typed; every capture input (slots
root, runtime state, trust policy, signer root, descriptors,
attestations) is derived from the installed `/etc/isadoraair/
station.json` + `/etc/isadoraair/updater-bootstrap.json`, plus the two
fixed, trusted, never-configurable constants
(`isadoraair.phase_d_recovery.INSTALLED_BOOTSTRAP_SOURCE_ROOT`/
`INSTALLED_SUPERVISOR_SERVICE`). See
`isadoraair.runtime_recovery.build_and_attach_installed_phase_d_payload`'s
own docstring for the exact mechanics, including how a current payload
that is ALREADY schema 2 (a prior refresh) is handled: a fresh
schema-1 base is re-derived from its own embedded tts/native_fdkaac
material first (never mutating its tree), then the newly-captured
Phase-D component is attached to that fresh base as yet another new
payload -- the prior schema-2 payload is never overwritten in place.

**Attestation binding (r0034).** Each captured generation's
`runtime-attestations/<role>/binding.json` -- the `release_id`/
`previous_release_id`/`previous_generation` needed to reconstruct the
exact statement its real signatures were made against -- is derived
from the application's own committed `deploy/releases/` chain
(`isadoraair.phase_d_recovery.resolve_protected_runtime_binding`), the
one capture-time-only input that comes from the git checkout rather
than installed system state. The real installed system durably
persists none of this (`RuntimeState` carries no `release_id` field;
`REQUEST_ACTIVATION`'s `release_id`/`previous_release_id` are
transient IPC-only values). Generation 1 is a fixed exception:
`deploy/releases/r0026.json` (the manual bootstrap) predates the
`protected_runtime` manifest field entirely, so its binding
(`release_id="r0026"`, `previous_release_id="r0025"`) is a hardcoded,
cryptographically-proven constant
(`isadoraair.phase_d_recovery.GENERATION_ONE_MANUAL_BOOTSTRAP_BINDING`)
rather than a manifest lookup -- see that constant's own docstring for
the verification record. A future protected-runtime generation bump
needs no new code here: as long as its release manifest correctly
declares `protected_runtime`, the derivation picks it up automatically.

```bash
# Real production use requires root -- both reading the installed
# 0600 root:root config files and writing beneath the root-owned
# persistent recovery root need it; see build_and_attach_installed_
# phase_d_payload's own docstring for why this is inherent to the
# security model, not a gap.
sudo venv/bin/python manage.py prepare_runtime_recovery_payload \
  --plan --phase-d --base-root /var/lib/isadoraair/runtime-recovery
sudo venv/bin/python manage.py prepare_runtime_recovery_payload \
  --apply --phase-d --base-root /var/lib/isadoraair/runtime-recovery
# --activate is unchanged and already schema-agnostic -- no --phase-d needed:
sudo venv/bin/python manage.py prepare_runtime_recovery_payload \
  --activate --base-root /var/lib/isadoraair/runtime-recovery --payload-id <id from --apply>
```

**When to re-run this.** Whenever the currently-activated payload's
`protected_updater` component should represent a NEW active/previous
generation pair -- i.e., after any legitimate protected-runtime
generation change (an ordinary signed candidate promotion), if the
disaster-recovery payload should reflect it. Nothing does this
automatically; the recovery payload is a deliberate, operator-refreshed
DR artifact, not a live mirror of the protected runtime's own state.
Publication alone never activates anything -- `--activate` remains a
separate, deliberate step, and both together never touch the live
protected runtime itself (no new generation, no state change, no
arming/starting/reloading).

**Backup-v3 consumes whatever is activated, automatically.**
`deploy/backup_isadoraair.sh` already resolves `current` via
`validate_runtime_recovery_payload --base-root ... --current --json`
(unchanged by r0031) and reports whatever schema/components/policy that
payload's own evidence carries -- once a schema-2 payload is `current`,
the very next nightly backup embeds `runtime-recovery/protected-updater/`
and reports `payload_schema_version: 2` with no code change needed.
E8 should select/build a backup archive taken *after* this activation
-- an archive whose embedded payload is still schema 1 (taken before
activation) will not exercise `protected_updater` at all, same as any
other stale-payload archive.

## No network fallback

Nothing in this contract fetches anything over the network, ever, at
any stage. Acquisition/preparation (an operator explicitly running
`prepare_runtime_recovery_payload --apply`/`--activate` against local
material they already obtained some other way) stays structurally
separate from backup validation/copying (`deploy/backup_isadoraair.sh`
consumes an already-prepared, already-validated local payload, and
fails closed before upload if the selected payload is missing when a
policy requires it, or malformed/tampered/stale) and from restore
consumption (`deploy/restore/50-native-deps.sh` / `70-tts.sh` extract
and validate the embedded payload, then delegate to E3/E4 — never
`--download-sources`, never `pip install`, for the backup-based path).
None of these three roles ever becomes `pip download`/model-download/
fdkaac-source-download on anyone's behalf.

## Backup v3 integration (Runtime Foundation E7B)

`deploy/backup_isadoraair.sh` is now implementation version 3.0.0. The
archive format is intentionally separate and machine-readable in
`runtime-recovery-archive.json`: only a validated payload satisfying a
non-empty explicit policy is format `3.0.0` / `self_contained_v3`.
Otherwise the script emits format `2.1.0` / `legacy_non_self_contained`,
see `docs/DISASTER_RECOVERY_RESTORE.md`'s
"Backward compatibility" section. New behavior, inserted as one step
between the existing reports/royalty step and recovery-credential
encryption:

1. Resolve the current recovery payload at `$RECOVERY_PAYLOAD_ROOT`
   (default `/var/lib/isadoraair/runtime-recovery`, overridable) via
   `manage.py validate_runtime_recovery_payload --base-root ... --current
   --json`, plus exactly one policy mode: **`$BACKUP_REQUIRED_RECOVERY_COMPONENTS`
   unset/empty (the normal case, r0046)** → `--require-current-station-policy`,
   deriving the required set automatically from
   `isadoraair.runtime_recovery.resolve_automatic_recovery_policy()` (see
   "Recovery-component policy" above); **`$BACKUP_REQUIRED_RECOVERY_COMPONENTS`
   set** → `--require-components "$BACKUP_REQUIRED_RECOVERY_COMPONENTS"`
   instead, a deliberate advanced/test override that replaces the
   automatic policy entirely (the strict parser rejects empty entries,
   whitespace, duplicates, and unknown names rather than silently
   weakening policy).
2. Exit code 2 **and** the active policy (automatic or explicit)
   required nothing at all → warn and continue without a payload
   (backward-compatible, no new failure mode for a host that hasn't
   adopted E7B yet, or — on some future non-Phase-D deployment — one
   whose automatic policy genuinely needs nothing). Any other nonzero
   exit (broken payload, or exit 2 *with* a non-empty policy — "not
   configured" is never an acceptable answer to a real requirement,
   automatic or explicit) → abort before upload. Under the r0046
   automatic policy this soft path is unreachable for a normal current
   production station, since `protected_updater` is always required.
3. On success: recursively copy the payload without attempting to preserve
   administrative ownership (the backup service is unprivileged), never regenerate it,
   into the archive's `runtime-recovery/` directory, then **re-validate
   the copy** (belt-and-suspenders, mirroring the existing app.tar.gz
   manage.py/.env presence checks) before proceeding.
4. `runtime-recovery-archive.json` records archive format/class, inclusion,
   payload/product identity, included components, required policy and its
   satisfaction, and Piper freshness. `MANIFEST.txt` records the same class
   for humans plus inclusion status, payload ID, schema
   version, product-contract digest, tts/native_fdkaac component
   states, which tts components (`kokoro`/`piper`) are actually
   present, Piper station-selection freshness, the required-component
   policy's SOURCE (`automatic`/`explicit`) and its component list plus
   whether it was satisfied, and — for diagnostics, never secrets — the
   automatically-resolved reasons per required component (e.g. which
   enabled encoder selected HE-AAC, or the protected_updater
   product/deployment rule) — never the nested wheel/hash tables
   themselves (the embedded manifests remain the integrity authority).

This step runs identically under `DRY_RUN=1` (no network involved, same
as every other local archive-building step) and never calls
`--download-sources` or `pip install` — see
`isadoraair/tests/test_deploy_backup_script.py::RuntimeRecoveryPayloadBackupTests`.

## Restore integration (Runtime Foundation E7B)

`deploy/restore/50-native-deps.sh` and `deploy/restore/70-tts.sh` each
gained a **backup-based DR mode**, selected automatically whenever
`--archive` is given (unless the operator explicitly asks for the old
mechanism — `--source-dir`/`--download-sources` for 50,
`--legacy-connected-install` for 70). In that mode:

1. `deploy/restore/lib.sh`'s `restore_locate_recovery_payload` (the one
   shared contract both stages use — neither guesses the archive layout
   independently) invokes a stdlib-only extractor that pre-scans member
   names/types, rejects traversal, absolute paths, links, duplicates and
   non-regular entries, then atomically publishes a private extracted tree.
2. The extracted payload is validated (`manage.py
   validate_runtime_recovery_payload <path>`), then handed to
   `manage.py provision_runtime_components` via its new
   `--recovery-payload` option, which:
   - supplies `--bundle` (TTS) or the native fdkaac source directory
     automatically — never guessed or re-derived by the restore stage;
   - uses payload/policy requiredness for Kokoro and native fdkaac (what
     the backup's recovery-component policy already justified embedding,
     not whatever the freshly-restored database says right now — see
     "Historical Kokoro-caller blind spot (CLOSED r0029)" above for why
     that reasoning outlives the gap it was originally written for);
     Piper is deliberately different and must match the freshly restored
     DB's E1 model/config identity before its station-derived
     requirements are handed to E3. See
     `monitoring/management/commands/provision_runtime_components.py`'s
     `_requirements_for_recovery_tts` / `_requirements_for_recovery_native`.
3. Native fdkaac preserves the full authority chain: E4's real
   `--prepare-fdkaac` (unprivileged) then `--publish-fdkaac` (protected
   — still requires `--trusted-preparer-uid` for a real canonical `/`
   target, exactly as before). r0041: `50-native-deps.sh` itself now
   determines and supplies that UID internally (whatever its own
   process's UID was when it ran `--prepare-fdkaac`, captured via
   `id -u` immediately afterward) and runs only the `--publish-fdkaac`
   half under `sudo`, via `restore_manage_command` + the same
   `USE_SUDO` idiom `75-protected-updater.sh` established —
   `--trusted-preparer-uid` is no longer a public `50-native-deps.sh`
   flag an operator can (or needs to) supply; a `--staging-root` restore
   still runs entirely unprivileged and passes no UID at all (E4's own
   `_validated_preparer_uid` treats that as "use the caller's own EUID",
   correct there since prepare and publish are the same unprivileged
   identity). TTS uses E3's single real `--apply` (no separate prepare/
   publish phase in E3, unlike E4) — `RuntimeProvisioner._preflight_apply`
   requires root for a canonical `/` target the same way E4 does, so
   r0041 also escalates `70-tts.sh`'s one `--apply` call under `sudo`
   for a real (non-staging) restore, the same `USE_SUDO` idiom, nothing
   else about its invocation changed.
4. **60-python.sh now runs before 50-native-deps.sh** in `restore.sh`'s
   order (reversing the numeric order the filenames imply) — E4
   delegation runs as a `manage.py` command and needs the restored app's
   Python environment to exist first. See `deploy/restore/README.md`'s
   dependency map for the full rationale; this has no effect on the
   legacy connected-install path (a plain C build, no venv dependency).
5. Missing/legacy/non-self-contained archive metadata fails the default
   backup-based stages closed. Successful E3/E4 publication records a receipt
   bound to the archive/payload identity; stage 95 requires that receipt to
   cover every required component before overall PASS. The explicit legacy
   connected/manual modes remain available but are never selected as fallback. See
   `docs/DISASTER_RECOVERY_RESTORE.md`'s "Backward compatibility"
   section for the full picture, including what this means for
   automated self-contained-DR reporting.

The pre-E7B mechanisms — `deploy/build_fdkaac.sh --source-dir`/
`--download-sources` for native, ad hoc per-engine
`python3 -m venv`+`pip install kokoro-onnx`/`piper-tts` for TTS — remain
available as an explicit, clearly separate connected/fresh-install mode
(task requirement: never remove a legitimate explicit path, but never
let a backup-based restore reach for it on its own).

## E7C real acceptance (2026-08-29)

E7C staged a real station backup-v3 archive through this exact restore
path, end to end, using real material — not fixtures, not mocked
seams. It is proof, not new architecture: no product-code changes were
needed except the two defects below, both exposed by using genuine
material (real PyPI wheels, a real host filesystem) that no synthetic
test fixture had ever exercised.

**Real station recovery policy determined, not assumed.** Read-only
inspection of the live station database confirmed E1's own resolver:
`kokoro.required=False`, `piper.required=False`,
`fdkaac.required=True` (this station's HE-AACv2 encoder). Confirmed
`webrequests/services.py` and `road_conditions/synthesis.py` still call
their own hardcoded `KOKORO_BINARY` directly today — the documented
historical-caller gap this whole design exists to route around remains
live. This station's actual recovery policy is therefore `kokoro` +
`native_fdkaac`; Piper is genuinely not applicable (zero
`StationTTSVoice` rows). **Historical snapshot, superseded by r0029:**
this was a real, dated (2026-08-29) observation of the station AT THAT
TIME, kept as an evidence trail (do not edit it away) — r0029 (Sept 1)
subsequently removed both hardcoded `KOKORO_BINARY` fallbacks, so a
fresh run of this same inspection today would resolve `kokoro.required`
from `StationTTSVoice` like any other engine. See "Historical
Kokoro-caller blind spot (CLOSED r0029)" above.

**The full chain proved real, twice over.** A real E3 Kokoro bundle
(pip-downloaded, hash-locked wheel closure at the exact versions the
live legacy Kokoro install already uses; this station's real
`kokoro-v1.0.onnx`/`voices-v1.0.bin`, hash-matching the product
contract) and real E4 native sources (the two fdk-aac/fdkaac archives,
fetched via `deploy/build_fdkaac.sh --download-sources`, matching their
pinned hashes) were assembled into a real recovery payload, activated,
backed up into a real `self_contained_v3` archive (real `pg_dump`, real
app tree, real live nginx/systemd configs), and restored into a
disposable, isolated target: real E4 build (`gcc`) producing a working
`fdkaac`/`libfdk-aac.so` proven via real AAC-LC/HE-AAC/HE-AACv2
encode+decode, and real E3 provisioning producing a working Kokoro venv
proven via a real synthesized WAV (PCM16 mono 24kHz, matching the
product output contract). Stage 95's receipt-gated acceptance passed
for real (`{"accepted":true,"payload_id":"e7c-real-acceptance-1",
"recovered_components":["kokoro","native_fdkaac"]}`), and a real
negative proof (a tampered receipt, a receipt for a different
payload_id, a missing receipt, and a legacy archive) all failed closed
exactly as designed.

**Two real defects found and fixed** (`isadoraair/runtime_validation.py`,
pre-existing E3/E6-era code, exposed — not introduced — by E7B's new
recovery-payload requirement shape, `required=True` with no
station-selected voice list):

1. `_kokoro_smoke` silently skipped its own synthesis smoke test
   whenever `requirement.voices` was empty, while still recording
   `provider_synthesis_pcm16_mono_24000: verified=True` — a
   false-positive Foundation E2 PASS for exactly the recovery-payload
   requirement shape E7B introduced. Fixed: falls back to a fixed,
   station-independent capability-probe voice
   (`KOKORO_CAPABILITY_PROBE_VOICE`) instead of skipping.
2. `_piper_smoke`'s `voices_by_model[model.model_id]` lookup would have
   raised `KeyError` — crashing E2 acceptance outright — for any
   station whose recovery payload legitimately contains Piper (found by
   code inspection; this station has none, so it was never hit here).
   Fixed the same way: falls back to the `PiperModelRequirement`'s own
   `language` and a neutral default speed.

Both are covered by real regression tests
(`isadoraair/tests/test_runtime_validation.py::KokoroCapabilityProbeFallbackTests`,
`::PiperCapabilityProbeFallbackTests`) and by an unrelated third defect
found the same way: `isadoraair.runtime_bundle._wheel_is_compatible`
rejected a real PyPI wheel (`protobuf`, a real `kokoro-onnx`
transitive dependency) using the CPython stable ABI
(`cp310-abi3`) on a newer interpreter — real pip installs this
wheel without complaint; the bundle's own pre-flight check was
stricter than pip itself. Fixed to treat an `abi3` wheel's `cpXY`
python tag as a forward-compatible minimum version, matching real
`packaging`/pip semantics (`isadoraair/tests/test_runtime_bundle.py::WheelAbi3CompatibilityTests`).

**One real, non-code, operational finding**: a fresh Kokoro venv's
bundled `espeakng-loader`/`libespeak-ng.so` silently truncates its own
data-path override once the venv's absolute path exceeds roughly 160
characters, falling back to a nonexistent build-time CI path with a
misleading "No such file or directory" error instead of a clear
diagnostic. Empirically bisected to a threshold between 157 (works) and
162 (fails) characters. **Confirmed safe for real canonical production
paths** (`/opt/isadoraair-runtime/kokoro/venv/...` = 95 chars via the
symlink, 130 chars via the real generation directory — both well under
the threshold) — this is not a production-activation blocker. It *is* a
real trap for an operator staging a restore drill under a long
`--staging-root` path (e.g. deeply nested `/home/.../something-long`):
packaging/asset validation still passes, but Kokoro synthesis silently
fails with an error that never mentions path length. **Keep
`--staging-root` short** (e.g. `/tmp/restore-test`) until this is
tracked further upstream or worked around locally; not a Runtime
Foundation E7 code defect, so nothing here was patched for it.

**One real, pre-existing (not E7-scoped) restore-safety issue, found
and fixed in the E7C closure pass**: under `--staging-root`,
`deploy/restore/30-postgresql.sh` `pg_restore`s into an isolated
`$RESTORE_DB_NAME` (e.g. `isadoraair_restore_test`) but
`$RESTORE_TARGET_ROOT/.env` is a byte-faithful copy of the real
station's `.env` — so any later `manage.py` command would connect to
whatever `.env` literally says (the real production database name on a
shared host) unless the caller separately exported `DB_NAME=...`.
Fixed narrowly at the one place every stage already resolves the
correct name: `deploy/restore/lib.sh`'s `restore_parse_common_args` now
`export`s `DB_NAME="$RESTORE_DB_NAME"`, which python-decouple's
`config()` reads ahead of `.env` — every later `manage.py` invocation
in that stage's process automatically targets the same database
`pg_restore` just used, with no per-stage code and nothing for an
operator to remember. `.env` on disk is never rewritten (it must stay
byte-faithful for eventual real restoration). Production restores are
unaffected — the exported value is exactly `isadoraair` (or an explicit
`--db-name`), the same value `.env` already carries there. Covered by
`isadoraair/tests/test_restore_tooling.py::RestoreDbNameExportFunctionalTests`
(including a direct check that python-decouple actually honors the
exported value over the file).

**Closure pass (2026-08-30)**: the literal `deploy/backup_isadoraair.sh`
self-contained-v3 proof remains **not achievable in this sandbox** —
re-confirmed fresh (not merely restated): no non-interactive
root-capable execution path exists here (`sudo -n`/`sudo -n -l` both
require interactive auth, no `doas`/`pkexec`, `/etc/sudoers.d/`
unreadable, and unprivileged user namespaces are blocked at the sandbox
policy layer despite `kernel.unprivileged_userns_clone=1`). The
retained payload (`payload_id: e7c-real-acceptance-1`) was re-validated
from scratch and still passes cleanly with the identical identity
(product digest `c341...30a10e`, components `{tts: kokoro,
native_fdkaac}`) already exercised through the real E3/E4 staged
restore — the chain the literal script would have fed remains
independently proven, just not through that one specific script
invocation.

## What's still open (updated through r0031)

- ~~Publication~~ — **done.** E7A/E7B/E7C shipped through r0028/r0029;
  r0030 added the numbered restore-stage integration for
  `protected_updater`; r0031 added the operator-facing schema-2
  publication/activation workflow itself (see "Publishing a schema-2
  Phase-D recovery payload" below).
- ~~Canonical production activation~~ — **done.** `RECOVERY_PAYLOAD_ROOT`
  (`/var/lib/isadoraair/runtime-recovery`) is populated and activated on
  the production host; `current` selects a real, validated payload.
- ~~Caller migration~~ — **done** (r0029). `webrequests/services.py` and
  `road_conditions/synthesis.py` no longer have a `KOKORO_BINARY`
  fallback of any kind — both fail clearly instead of attempting a
  deleted binary when left unconfigured; canonical shared-TTS is the
  only path either ever takes.
- **E8** — fully offline whole-machine acceptance remains a separate,
  later checkpoint (r0030 removed the restore-orchestration blocker
  that would have prevented `protected_updater` from ever being
  exercised; r0031 removed the publication blocker that would have
  left `current` schema-1 forever). See this doc's own "Phase-D
  protected updater recovery extension" section below for exactly what
  E8 should now use.
- **Phase 5** — an actual bare/clean-machine restore drill (original
  host and GitHub unavailable) remains later work.
- ~~Operator-memory required-component policy~~ — **done** (r0046).
  `BACKUP_REQUIRED_RECOVERY_COMPONENTS` being empty/unset by default
  meant a normal scheduled backup on a fully configured production
  station could still legally produce `legacy_non_self_contained`
  without an operator remembering to set it. `resolve_automatic_recovery_policy()`
  now derives the required set automatically and fail-closed (kokoro/
  piper/fdkaac from E1, `protected_updater` from an independent
  product/deployment rule); `deploy/backup_isadoraair.sh` uses it by
  default, with the explicit env var kept only as a deliberate
  advanced/test override. See "Recovery-component policy" above.

## Phase-D protected updater recovery extension

Historical Runtime Foundation E payloads remain valid schema 1. A payload that
claims Phase-D updater recovery uses schema 2 and adds the
`protected_updater` component. It carries the immutable bootstrap source and
service template, active and previous A/B generations, exact runtime state,
station/bootstrap configuration, public trust policy and public signer keys,
runtime descriptors, public attestation wrappers and a strict file/hash/mode
restore manifest. Private signing keys are categorically excluded; existing
station credentials retain the encrypted-credential policy.

`isadoraair.phase_d_recovery.validate_phase_d_component` is the reusable
validator used by payload/backup-v3 handling and fake-root restore. A backup
claiming Phase-D capability fails closed for missing supervisor material,
incomplete active or previous slots, inconsistent state, absent trust/key or
attestation evidence, descriptor mismatch, unsafe filesystem objects, or an
unsatisfied signature threshold. Schema-1 archives do not claim this capability
and remain historical rather than being mislabeled corrupt.

The offline restore path never contacts GitHub. It first revalidates all
provenance, then materializes the bootstrap, configuration, public trust,
runtime slots and state under canonical A/B paths. The unprivileged fake-root
harness still reports `worker_started=false` and `readiness=not-run`: the
production entry point correctly requires root and root-owned protected
ancestry, neither of which a user-owned fake root can represent. D5.1B then
completed the complementary privileged proof on the disposable host: restored
B/gen2 started DISARMED under the production ownership and network sandbox,
PING/readiness passed, `START_UPDATE` was refused without creating a job, and
both A/gen1 and B/gen2 verified independently. A harmless signed gen3 update
also proved restored continuity while leaving Weather, migrations, services
and application behavior untouched. Repeating the final bootstrap on KOGR
production remains D6; it was not performed by this acceptance.
