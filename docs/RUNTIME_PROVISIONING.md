# Runtime Foundations E3/E4: deterministic offline runtime provisioning

Foundation E3 consumes trusted local build material and constructs the
canonical Kokoro and Piper runtimes. It performs no acquisition and does not
activate any production caller. Foundation E4 adds the native fdkaac
publication adapter described below. The stable installed system CLI and
tmpfiles ownership of the containing directories are Foundation E5's
concern -- see `docs/RUNTIME_SYSTEM_SURFACES.md` -- not this document's.
Caller migration, backup v3, restore integration, and fresh-installer
orchestration remain later work beyond E5.

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

## E4 native fdkaac adapter

Foundation D remains the sole fdkaac build authority:
`runtime_components.json` owns versions, source archive filenames/byte counts/
SHA-256 identities, canonical paths, and the build/validator paths;
`deploy/build_fdkaac.sh` owns extraction and compilation; and
`deploy/check_he_aac.sh` owns linkage and LC/HE/HEv2 capability acceptance.
E4 does not duplicate those constants or recipes. It supplies deterministic
orchestration and a protected publication transaction.

The native plan is read-only and explicit:

```bash
python manage.py provision_runtime_components --fdkaac --plan
python manage.py provision_runtime_components --fdkaac --plan \
  --native-source-dir /path/to/native/fdkaac
```

`--bootstrap-fdkaac` deliberately selects the component for a future fresh
installer even when current station features do not require it. Otherwise E1
station requirements decide selection. A required healthy component is an
authoritative no-op under the common E3/E4 provision lock: source material is
not inspected, no build occurs, `ldconfig` is not called, and `/usr/local` is
not changed. A required broken/missing component is blocked until an explicit
source or prepared directory is supplied. Unrelated TTS failures remain visible
in evidence but do not invalidate a successful component-scoped fdkaac repair.

### Explicit two-identity lifecycle

Compilation and protected publication are deliberately separate operations:

```bash
# Trusted, unprivileged administrative/provisioning identity
python manage.py provision_runtime_components \
  --prepare-fdkaac \
  --native-source-dir /path/to/native/fdkaac \
  --prepared-native-root /path/to/new-prepared-root

# Explicit privileged identity for canonical /usr/local publication
python manage.py provision_runtime_components \
  --publish-fdkaac \
  --prepared-native-root /path/to/new-prepared-root \
  --trusted-preparer-uid "$(id -u TRUSTED_PREPARER_ACCOUNT)"
```

The first phase requires a new caller-owned output root. It verifies both
manifest-named archives as non-symlink, single-link regular files with exact
byte counts and hashes, copies them into a private preparation directory, and
invokes exactly the existing local-only build shape:

```text
deploy/build_fdkaac.sh --source-dir VERIFIED_PRIVATE_COPY --prefix STAGED_PREFIX
```

E4 never supplies `--download-sources` or `--allow-production-prefix`, never
infers a source directory, and never runs `sudo`. The build script retains the
authoritative `BUILD_HEAAC` missing-tool diagnostic. After the script's own
validation, E4 validates the prefix again and writes a hash/size/mode receipt.
The preparer is a trusted administrator or provisioning identity, not an
IsadoraAir runtime/service account and not an account whose prepared tree is
writable by a runtime service. Preparation remains unprivileged.

The second phase revalidates that mutable caller-owned receipt and material
under the common lock, then copies only fdkaac, the exact versioned libfdk-aac,
and temporary checker metadata into a mode-0700 private sibling beneath the
mapped `/usr/local`. That protected copy is rehashed and receives the full
authoritative staged check before canonical mutation. On real `/`, this phase
requires the process itself to be root and requires an explicit
`--trusted-preparer-uid`. The publisher checks the kernel `st_uid` and rejects
symlinks, hard links, or group/world-writable modes for the prepared root,
receipt, relied-upon prefix directories, artifacts, and SONAME. The UID written
in the receipt is diagnostic only: it cannot select or infer the trusted
identity. Caller-owned `--target-root` tests and future staging can publish
unprivileged and default to the current caller's UID. There is no username
lookup, `SUDO_USER` behavior, internal privilege broker, or root
`safe.directory` configuration.

This handoff protects against path escape, link substitution, receipt/material
disagreement, and mutation by identities other than the explicitly trusted
preparer before protected restaging. It assumes that the selected preparer
identity and the privileged publisher are trusted administrative actors; it is
not a cryptographic defense against a malicious preparer using its own UID.

### Trust boundary

E4 proves the source contract during honest preparation, receipt/file
consistency at handoff, kernel ownership and restrictive modes after the
privileged publisher accepts that handoff, a root-owned protected re-copy,
content hashes, Foundation D functionality, and canonical component-scoped E2
acceptance. Prepared material remains owned by the explicitly selected
unprivileged preparer UID; it is not made root-owned merely to cross the
boundary.

E4 assumes that the preparation identity is a trusted administrative or
provisioning actor. It deliberately does not provide cryptographic attestation
that a malicious trusted preparer actually ran the audited build script. The
IsadoraAir runtime/service identity must not be the preparer: compromising the
running application or service must not grant a path to construct native
runtime material that root will later trust.

### Minimal transaction and rollback

Canonical publication owns only:

```text
/usr/local/lib/libfdk-aac.so.2.0.3
/usr/local/bin/fdkaac
```

No header tree, static archive, libtool archive, pkg-config tree, or other
unrelated `/usr/local` content is recursively installed. E4 snapshots the exact
pre-state, prepares fsynced same-directory temporary files, atomically replaces
the versioned library first and fdkaac second, then invokes the host `ldconfig`
with a minimal environment and bounded process group. `ldconfig` owns the
runtime SONAME cache/link update. Caller-owned mapped test roots use ldconfig's
directory-only mode, so no host cache is touched.

Final acceptance is the existing Foundation E2 validator against the canonical
binary and library root. Its explicit `--runtime-only` checker mode omits only
the build-time pkg-config metadata requirement; exact expected versioned
library identity, ELF dependency/resolution, AAC-LC, HE-AAC, HE-AACv2, and
ffmpeg decode checks are unchanged. Protected build staging still uses the full
pkg-config-aware check.

Any failure after mutation restores the prior regular files (including an
absent pre-state), reruns `ldconfig`, and compares fdkaac's E2 evidence with its
pre-publication evidence. Rollback failure is reported while retaining the
original failure as its cause. Private protected staging is removed after both
success and failure; unrelated `/usr/local` files are never cleanup targets.

E4 does not acquire the two private source archives, install `BUILD_HEAAC`
packages, capture backup payloads, integrate restore/fresh-install flows, or
activate production. The future DR source shape remains
`native/fdkaac/{fdk-aac-2.0.3.tar.gz,fdkaac-1.0.7.tar.gz,SHA256SUMS,provenance}`.
