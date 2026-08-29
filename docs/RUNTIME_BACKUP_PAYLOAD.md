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

- Not E7C — no real station backup-v3 archive has been taken and staged
  through this path yet; that whole-archive acceptance proof is a
  separate, later checkpoint.
- Not canonical production activation — nothing here has installed
  Kokoro, Piper, or fdkaac on the production host, enabled the
  `RECOVERY_PAYLOAD_ROOT` persistent location on production, or touched
  `/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`, or `/usr/local`
  on the live station.
- Not a TTS caller migration — `webrequests/services.py` and
  `road_conditions/synthesis.py` still call their own hardcoded
  `KOKORO_BINARY` directly; nothing here changes that.
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

### Kokoro — operator-declared, never gated on E1's `required` flag

**Historical-caller gap (load-bearing for this decision):** Runtime
Foundation E1's station-requirement resolver only sees TTS demand that
flows through `StationTTSVoice` / `WebRequestConfig.
dedication_tts_voice_id` / `RoadConditionsConfiguration.tts_voice_id`.
On the current production station, `WebRequestConfig.enabled` and
`RoadConditionsConfiguration.enabled` are both `True` — both features
are live — but both `dedication_tts_voice_id` and `tts_voice_id` are
`None`, and there are zero `StationTTSVoice` rows at all. Both features'
actual synthesis code (`webrequests/services.py`'s and
`road_conditions/synthesis.py`'s own hardcoded `KOKORO_BINARY =
"/home/jreed/kokoro/bin/kokoro_synth"` constants) calls the historical
Kokoro binary directly and unconditionally, never consulting
`StationTTSVoice` at all. **E1 therefore currently resolves
`kokoro.required = False` even though this station operationally
depends on Kokoro right now.**

Consequence: Kokoro/native inclusion in a recovery payload is
**operator-declared** — present in the payload because the operator
supplied real `--tts-bundle`/`--native-source-dir` material for it, full
stop. The builder never consults `resolve_current_runtime_requirements()`
to decide whether to include or omit Kokoro, and never treats
`required=False` as license to silently produce a payload that can't
actually restore this station's current capability.

This exact same historical-caller gap is why **restore-time**
provisioning (see "Restore integration" below) also never re-derives
"is Kokoro required" from the freshly-restored database — it would
reintroduce the identical blind spot, just one step later.

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

## Recovery-component policy (Runtime Foundation E7B)

`isadoraair.runtime_recovery.evaluate_recovery_policy(evidence,
required_components)` — a small, explicit, operator-declared overlay on
top of `RuntimeRecoveryEvidence`, answering "does the *currently
selected* payload still positively contain what this station's DR
policy requires" without ever consulting E1:

- `RECOVERY_POLICY_COMPONENT_NAMES = {"kokoro", "piper", "native_fdkaac"}`
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

`deploy/backup_isadoraair.sh`'s `BACKUP_REQUIRED_RECOVERY_COMPONENTS`
env var (comma-separated, empty by default) is the one place this
policy gets configured for the nightly backup — see "Backup v3
integration."

## Persistent payload location (Runtime Foundation E7B — established, not yet activated on production)

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
  file must be owner-matching, single-link, regular, non-symlink mode 0644;
  `current` must be an owner-matching confined one-hop symlink. Runtime
  service identities can traverse/read but cannot modify the source.
- **Not activated on any host this session** — `RECOVERY_PAYLOAD_ROOT`
  in `deploy/backup_isadoraair.sh` defaults to
  `/var/lib/isadoraair/runtime-recovery`, but nothing in E7B creates
  that directory or a `current` pointer on production. Until an operator
  explicitly runs `prepare_runtime_recovery_payload --apply` +
  `--activate` there, the nightly backup finds nothing configured (exit
  code 2) and — with no `BACKUP_REQUIRED_RECOVERY_COMPONENTS` policy set
  — continues without a `runtime-recovery/` payload and labels the result
  format 2.1.0 / `legacy_non_self_contained`, exactly like every backup
  taken before E7B for runtime-DR purposes.

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
   --json --require-components "$BACKUP_REQUIRED_RECOVERY_COMPONENTS"`;
   the strict parser rejects empty entries, whitespace, duplicates, and
   unknown names rather than silently weakening policy. The variable is
   `$BACKUP_REQUIRED_RECOVERY_COMPONENTS` (comma-separated, empty by
   default).
2. Exit code 2 **and** no policy configured → warn and continue without
   a payload (backward-compatible, no new failure mode for a host that
   hasn't adopted E7B yet). Any other nonzero exit (broken payload, or
   exit 2 *with* a policy configured — "not configured" is never an
   acceptable answer to an explicit requirement) → abort before upload.
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
   present, Piper station-selection freshness, and the configured
   recovery-component policy plus whether it was satisfied — never the
   nested wheel/hash tables themselves (the embedded manifests remain
   the integrity authority).

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
   - uses payload/policy requiredness for Kokoro and native fdkaac, avoiding
     the dormant historical-Kokoro caller blind spot; Piper is deliberately
     different and must match the freshly restored DB's E1 model/config
     identity before its station-derived requirements are handed to E3. See
     `monitoring/management/commands/provision_runtime_components.py`'s
     `_requirements_for_recovery_tts` / `_requirements_for_recovery_native`.
3. Native fdkaac preserves the full authority chain: E4's real
   `--prepare-fdkaac` (unprivileged) then `--publish-fdkaac` (protected
   — still requires `--trusted-preparer-uid` for a real canonical `/`
   target, exactly as before; a `--staging-root` restore never needs
   it). TTS uses E3's single real `--apply` (no separate prepare/publish
   phase in E3, unlike E4).
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

## What's still open after E7B

- **E7C** — staging a real station backup-v3 archive through this exact
  restore path has not been done; E7B only proves the implementation
  safely supports it.
- **Canonical production activation** — `RECOVERY_PAYLOAD_ROOT` is not
  populated or activated on the production host.
- **E8** — fully offline whole-machine acceptance remains a separate,
  later checkpoint.
