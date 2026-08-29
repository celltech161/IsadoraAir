# Runtime backup payload contract — Runtime Foundation E7A

Runtime Foundation E7A defines the durable, machine-readable disaster-
recovery runtime payload contract ("backup v3 runtime payload") that
Foundation E3 (offline TTS) and E4 (native fdkaac) provisioners need to
run completely offline. **This checkpoint is the artifact and its
builder/validator only** — it is not yet wired into the nightly backup
script or the restore stages. See "E7B handoff" below for exactly what
comes next.

## What this is not

- Not backup v3 itself — `deploy/backup_isadoraair.sh` (still v2.1.0) is
  unmodified and does not yet produce or consume this payload.
- Not a change to `deploy/restore/50-native-deps.sh` or
  `deploy/restore/70-tts.sh` — both are unmodified and still use their
  own pre-Foundation-E mechanisms.
- Not canonical runtime activation — nothing here provisions Kokoro,
  Piper, or fdkaac, installs any Foundation E5 system surface, or
  touches `/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`, or
  `/usr/local`.

## Architecture: reuse, not reinvention

Every piece of *identity* this payload needs already has a Foundation E
authority — E7A adds only the small container format around them, never
a second copy of any of it:

| Concern | Authority (reused, not duplicated) |
|---|---|
| Product-contract identity | `isadoraair.runtime_bundle.product_contract_digest` |
| Platform/Python ABI identity | `isadoraair.runtime_bundle.current_platform_contract` (via the nested E3 bundle's own check) |
| TTS wheel closure / Kokoro assets / Piper models | `isadoraair.runtime_bundle.load_runtime_bundle` — an ordinary, unmodified E3 `runtime-bundle.json` |
| fdkaac/libfdk-aac source archive identity | `isadoraair.runtime_native.verify_native_sources`, itself reading `runtime_components.json`'s `components.fdkaac.source_archives` |
| Which Piper models the station currently needs | `isadoraair.runtime_requirements.resolve_current_runtime_requirements` (E1) |

A future product-contract or Piper-model hash change breaks an already-
built E7 payload's validation automatically — E7A never forked its own
copy of any of these identities (proven by
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
scooped up (that policy lives entirely inside the embedded E3 bundle's
own `piper_models` schema, unchanged).

### fdkaac — source material only, never a shortcut authority

The payload carries *only* the two immutable source archives E4 already
names. It never carries a copy of `/usr/local/bin/fdkaac`, a built
shared library, or an arbitrary build tree as a "reconstruction
shortcut" — the recovery authority remains exactly:

```
source archives -> Foundation D build authority -> E4 prepare
                 -> E4 protected publication -> E2 validation
```

E7 only makes the first step of that chain available offline.

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

CLI equivalent:

```bash
python manage.py prepare_runtime_recovery_payload --plan \
    --tts-bundle /path/to/bundle --native-source-dir /path/to/sources \
    --output /path/to/new/payload

python manage.py prepare_runtime_recovery_payload --apply \
    --tts-bundle /path/to/bundle --native-source-dir /path/to/sources \
    --output /path/to/new/payload
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
- never mutates `/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`, or
  `/usr/local`; never restarts a service; never migrates a caller;
  never invokes `sudo` internally;
- every input is a caller-supplied local path — no network fetch, no
  acquisition mode of any kind exists in E7A.

## Validation interface

`isadoraair.runtime_recovery.validate_recovery_payload` — read-only,
structured, never raises for a bad/missing/tampered payload (only for a
genuinely unexpected internal error), callable directly from Python so
a future E7B backup/restore orchestrator never needs to parse human CLI
text:

```python
from isadoraair.runtime_recovery import validate_recovery_payload, RESULT_PASS

evidence = validate_recovery_payload("/path/to/payload")
if evidence.result != RESULT_PASS:
    ...  # evidence.to_dict() carries full structured detail
```

`validate_current_recovery_payload(root)` is the DB-aware convenience
wrapper (resolves live Piper selection, falling back to `not_checked`
rather than a guess if the database can't be inspected — mirroring
Runtime Foundation E6's own bootstrap-safe design). CLI equivalent:
`python manage.py validate_runtime_recovery_payload <path>`.

Fails closed for: missing/malformed top-level manifest, wrong schema
version, wrong product-contract digest, a component path escaping the
payload root (absolute, `..`, backslash), a missing declared file, an
extra undeclared file where strict closure applies, a symlink anywhere
in the tree, a hardlinked file, a non-regular file, a changed byte/hash
anywhere in the embedded bundle or native archives, an incompatible
platform/ABI, a malformed nested E3 bundle, an invalid native source
archive, a stale Piper station-model requirement, an unknown or
duplicate component, and any unsupported manifest field.

## Persistent payload location — proposed, not established

No production canonical directory is created or written to in E7A.
E7A's builder operates only against caller-supplied paths. For E7B's
eventual nightly-backup consumption, the natural location would be
something like `/var/lib/isadoraair/runtime-recovery/<payload_id>`,
root-owned (matching Foundation E5's own `/var/lib/isadoraair/tts`
convention) with the *acquisition* step (an operator explicitly running
`prepare_runtime_recovery_payload --apply`) kept structurally separate
from nightly backup's own read-only *validate-then-copy* step — see "No
network fallback" below. This is a proposal for E7B review, not
something this checkpoint establishes on any host.

## No network fallback

Nothing in E7A fetches anything over the network, ever. E7A
deliberately separates **acquisition/preparation** (an operator
explicitly running `prepare_runtime_recovery_payload --apply` against
local material they already obtained some other way) from **backup
validation/copying** (E7B's future job: consume an already-prepared,
already-validated local payload, and fail closed if it is missing or
stale for a required capability, rather than trying to fetch anything
on the nightly job's behalf). The nightly backup job must never become
`pip download`/model-download/fdkaac-source-download on a schedule.

## E7B handoff (not performed in this checkpoint)

- `deploy/backup_isadoraair.sh` (v2.1.0) would gain a read-only
  preflight step calling `validate_current_recovery_payload()` against
  the (E7B-established) persistent payload location, failing the
  backup closed if a required capability's payload is absent or stale,
  and would then include the validated payload directory verbatim in
  its tar.gz — no wheel/model/archive logic of its own.
- `deploy/restore/70-tts.sh` would, instead of its current ad-hoc
  `python3 -m venv` + `pip install kokoro-onnx`/`piper-tts`, extract the
  backup's embedded `tts/` directory and hand it to Foundation E3's
  `RuntimeProvisioner` as an ordinary bundle — the exact mechanism E3
  already expects, offline.
- `deploy/restore/50-native-deps.sh` would, instead of its current
  `deploy/build_fdkaac.sh --download-sources` connected-install
  fallback, extract the backup's embedded `native/fdkaac/` directory and
  pass it as `--source-dir` to Foundation E4's
  `NativeRuntimeProvisioner` — again offline, no network path needed.

None of this is implemented yet; E7A ends with the artifact and its
Python API those three scripts can consume in that next checkpoint.
