# Runtime Foundation E3: deterministic offline TTS provisioning

Foundation E3 consumes trusted local build material and constructs the
canonical Kokoro and Piper runtimes. It performs no acquisition and does not
activate any production caller. fdkaac publication, the stable system CLI,
tmpfiles installation, caller migration, backup v3, restore integration, and
fresh-installer orchestration remain later work.

## Authority boundaries

Three existing product/station contracts remain authoritative:

1. `isadoraair/runtime_components.json` owns canonical paths, product package
   versions, Kokoro asset identities, providers, and availability policy.
2. `isadoraair/runtime_requirements.py` owns the read-only mapping from active
   station features to selected logical voices, engines, and Piper models.
3. `isadoraair/runtime_validation.py` owns package, asset, model metadata,
   synthesis, and WAV acceptance.

E3 adds `runtime-bundle.json`. This is not another product manifest. It records
the immutable files in one local rebuild payload and the exact host ABI for
which its wheels were prepared. Its `product_contract_sha256` is the SHA-256
of the product manifest's canonical JSON representation; disagreement fails
before provisioning.

## Bundle schema version 1

The bundle root contains exactly one manifest plus every declared file. Extra
files, missing files, non-regular files, and symlinks are rejected. All paths
are confined POSIX-relative names.

```json
{
  "schema_version": 1,
  "bundle_id": "stable-operator-supplied-identity",
  "platform": {
    "os": "linux",
    "architecture": "x86_64",
    "python_implementation": "cpython",
    "python_version": "MAJOR.MINOR.PATCH",
    "python_abi": "cpython-MAJORMINOR"
  },
  "product_contract_sha256": "PRODUCT-CONTRACT-DIGEST",
  "components": {
    "kokoro": {
      "lock": {
        "filename": "kokoro/requirements.lock",
        "sha256": "LOCK-DIGEST"
      },
      "wheelhouse": "kokoro/wheelhouse",
      "wheels": [
        {
          "filename": "distribution-version-platform.whl",
          "package": "distribution",
          "version": "EXACT-VERSION",
          "sha256": "WHEEL-DIGEST"
        }
      ],
      "assets": {
        "model": {"filename": "kokoro/assets/PRODUCT-MODEL", "sha256": "PRODUCT-DIGEST"},
        "voices": {"filename": "kokoro/assets/PRODUCT-VOICES", "sha256": "PRODUCT-DIGEST"}
      },
      "provenance": [
        {"filename": "kokoro/NOTICE.txt", "sha256": "NOTICE-DIGEST"}
      ]
    },
    "piper": {
      "lock": {
        "filename": "piper/requirements.lock",
        "sha256": "LOCK-DIGEST"
      },
      "wheelhouse": "piper/wheelhouse",
      "wheels": [
        {
          "filename": "piper_tts-version-platform.whl",
          "package": "piper-tts",
          "version": "EXACT-PRODUCT-VERSION",
          "sha256": "WHEEL-DIGEST"
        }
      ],
      "models": [
        {
          "model_id": "STATION-MODEL-ID",
          "model": {"filename": "piper/models/MODEL.onnx", "sha256": "MODEL-DIGEST"},
          "config": {"filename": "piper/models/MODEL.onnx.json", "sha256": "CONFIG-DIGEST"},
          "language": "en-us",
          "sample_rate_hz": 22050
        }
      ],
      "provenance": [
        {"filename": "piper/NOTICE.txt", "sha256": "NOTICE-DIGEST"}
      ]
    }
  }
}
```

Each included component must have a nonempty complete wheel closure and
license/provenance material. Piper may have no models when it is only build
material, but station-aware apply requires every E1-selected model/config pair
to be present and to match its DB-owned basenames, hashes, language, native
sample rate, and pairing.

The lock file allows comments/blank lines and otherwise requires exactly one
form per package:

```text
distribution==EXACT-VERSION --hash=sha256:WHEEL-DIGEST
```

The package/version/hash set in the lock must exactly equal the manifest's
wheel set. Product-owned package pins must also match
`runtime_components.json`. Only wheels are permitted; VCS, editable, index,
and sdist inputs have no bundle representation. An IsadoraAir application
wheel is explicitly rejected: provider source remains in the one authoritative
Git checkout and is supplied to the isolated Kokoro interpreter by the
existing dispatcher boundary.

Kokoro model and voices names/hashes must match the product contract. They are
not stored in Git, and E3 does not decide their public redistribution status.
Piper assets are station-owned, so their identities are checked against the
selected E1 model requirements during planning.

## Plan and apply

The station-aware operator interface is:

```bash
python manage.py provision_runtime_components \
    --bundle /path/to/offline-bundle \
    --plan

python manage.py provision_runtime_components \
    --bundle /path/to/offline-bundle \
    --apply
```

`--json` produces deterministic machine-readable output. Exactly one of
`--plan` and `--apply` is mandatory; omission never implies apply.

`--plan` validates the complete bundle, resolves E1 requirements, obtains
current E2 evidence, and reports selected components, reasons, payload files,
current status, deterministic generation identities, target pointers, and
blocking errors. It creates no runtime, virtualenv, lock, or database state.

The reusable `RuntimeProvisioner` accepts already-resolved
`RuntimeRequirements`. The management command is only the Django/station
adapter, allowing future backup, restore, and installer code to call the same
Python API without reproducing selection or provisioning logic.

## Strict offline installation

Every replacement venv is created fresh. E3 then invokes its Python with the
equivalent of:

```bash
python -I -m pip install \
    --isolated \
    --disable-pip-version-check \
    --no-input \
    --no-index \
    --only-binary=:all: \
    --find-links BUNDLE-WHEELHOUSE \
    --require-hashes \
    -r BUNDLE-LOCK
```

The subprocess environment is a minimal PATH/locale/TZ/TMPDIR allowlist plus
settings that disable indexes, pip configuration, input, version checks, and
user-site packages. stdin/stdout/stderr are isolated, processes have bounded
timeouts and process-group cleanup, and no code path implements download,
index fallback, VCS acquisition, or sudo.

## Immutable generations and relocation safety

Venvs are built directly at their permanent generation paths:

```text
/opt/isadoraair-runtime/kokoro/generations/<evidence-id>/
/opt/isadoraair-runtime/piper/generations/<evidence-id>/
```

Only after staged E2 validation does E3 atomically switch each canonical
`venv` symlink to its generation. Entry-point shebangs therefore contain the
same permanent absolute generation path used after publication; no venv is
renamed from `/tmp` or otherwise relocated.

Assets use whole-directory generations:

```text
/var/lib/isadoraair/tts/generations/kokoro/<evidence-id>/
/var/lib/isadoraair/tts/generations/piper/<evidence-id>/
```

The canonical `kokoro` or `piper` directory is an atomic symlink pointer. A
Kokoro model/voices pair and all selected Piper model/config pairs therefore
become visible together rather than as mixed individual file updates.
Generation IDs derive from the bundle identity, product contract, component
wheel/lock/assets, and selected Piper requirements. Repair suffixes are used
only when a corrupted current generation occupies the deterministic identity.

Build locks, exact lock material, bundle identity, and provenance records are
retained inside the immutable runtime generation. Prior generations are not
deleted. A pre-E3 real directory encountered at a canonical pointer is moved
to a retained legacy generation during the reversible switch.

## Validation, idempotence, and rollback

Apply performs:

```text
reverify bundle
-> create permanent replacement generation
-> install only local hash-locked wheels
-> stage complete asset generation
-> validate staged paths with Foundation E2
-> switch asset/runtime pointers
-> run final Foundation E2 acceptance
```

Path mapping changes only physical locations supplied to the existing E2
validator. Package versions, asset hashes, provider behavior, synthesis, and
WAV criteria are not copied or weakened.

An already-valid selected component plans `no_op`. Apply still performs its
preflight, acquires the persistent provision lock, and re-plans under that lock
before authoritatively returning `no_op`; it creates no venv, asset, generation,
or pointer churn. Repeated apply against the same accepted runtime is serialized
with a concurrent provisioner while creating no additional generation.

Post-publication acceptance requires clean product-contract and station-
requirement evidence plus an E2 pass for every component staged by that apply.
Evidence for unrelated components remains in the result, but a pre-existing
unrelated failure does not roll back a correctly repaired TTS component or make
the aggregate station result appear healthy.

Any staging failure removes only the unpublished generation. Publication
failure restores all prior pointers in reverse order, reruns E2, checks that
contract/requirement errors are unchanged and the staged components returned to
their prior statuses, and removes the rejected generation. Unrelated component
evidence does not decide scoped rollback success. Pointer-restoration and
rollback-validation errors are reported explicitly while preserving the
original provisioning failure as their cause. E3 never switches engines or
invokes an automatic fallback.

## Privilege and filesystem boundary

Plan is unprivileged. Apply beneath an existing caller-owned `--target-root`
is unprivileged and maps every canonical absolute path below that root. The
target root and publication parents may not be symlinks, generation paths may
not escape the root, and bundle symlinks/path traversal are rejected.

Canonical `/opt` and `/var/lib` apply requires the process itself to have root
privileges. E3 never calls `sudo`. Canonical directories and generations are
created with traversable private-administration defaults; immutable asset and
provenance files are published mode 0644 so the service-user model can read
them. Final ownership policy and production activation remain a later pass.

## Future DR/installer consumption

Backup v3 should preserve this bundle shape rather than a copied virtualenv:
complete wheel closures and locks, Kokoro assets, selected Piper assets and
DB-owned hashes, and applicable notices/licenses. Strict restore and a fresh
installer should both feed that payload to E3 and then rely on E2 acceptance.
They must not copy this logic into `restore/70-tts.sh`.

No production caller should be migrated until the canonical runtime and later
system surfaces have separately passed their activation gates. Historical
weather-ingest TTS remains an explicit companion/caller-migration concern.
