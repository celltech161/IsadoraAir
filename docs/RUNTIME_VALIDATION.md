# Runtime Foundation E: read-only validation

Runtime Foundation E passes E1 and E2 establish one reusable answer to two
separate questions:

1. What does the IsadoraAir product contract expect?
2. Which components does this station's active configuration require, and does
   the canonical installation prove that they work?

E1/E2 themselves **do not provision anything**. They do not install packages,
download assets, create virtual environments, change database rows, modify
system paths, or start services. Current dedication, road-condition, and
weather-ingest callers remain on their historical production paths.

Runtime Foundation E3 adds the separate deterministic offline provisioner
documented in `docs/RUNTIME_PROVISIONING.md`. It consumes E1 requirements and
must pass these E2 checks before accepting a generation; it does not change
the definitions in this document.

## Canonical evidence before caller cutover

Foundation E reports only the canonical product runtime described by
`runtime_components.json`. It does not report whether historical
`/home/...` production TTS paths are operational and does not use those paths
to infer canonical requirements.

Before caller cutover, legacy production Kokoro can therefore be actively
working while no canonical feature reference selects Kokoro and the canonical
Kokoro component correctly reports `optional_absent`. That result is not
evidence that current on-air TTS is broken; it says only that this station does
not yet require the separately provisioned canonical Kokoro runtime. E3 and
the later production-activation pass will provision and validate canonical
runtimes before each historical caller is deliberately migrated.

## Ownership and API

`isadoraair/runtime_components.json` remains the only product authority for
canonical paths, versions, artifact hashes, availability policy, and the
fdkaac build/validation contract. `isadoraair/runtime_components.py` remains
its structural loader.

`isadoraair/runtime_requirements.py` reads station configuration without using
the singleton models' mutating `load()`/`get_or_create()` methods. It produces
path-free `RuntimeRequirements`; it never inspects runtime files or starts a
provider. `isadoraair/runtime_validation.py` consumes those requirements and
returns `RuntimeEvidence`.

The Python entry point is:

```python
from isadoraair.runtime_validation import validate_current_runtime

evidence = validate_current_runtime()
```

Operators can use:

```bash
python manage.py validate_runtime_components
python manage.py validate_runtime_components --json
```

The human form summarizes each component. JSON mode writes only the compact,
deterministically ordered evidence document to stdout. The command exits zero
only when station configuration is inspectable and every required component
passes. An unselected component with no canonical footprint is
`optional_absent` and does not fail the command.

## Station requirement semantics

Logical TTS voices become selected only through active feature configuration;
merely creating an enabled or disabled, unreferenced `StationTTSVoice` does not
require its engine.

- Scheduled weather personas with a non-null logical voice select that voice.
- Enabled web-request dedications with a non-null logical voice select it.
- Enabled road conditions select their fixed non-null logical voice, or all
  configured voices in the weather schedule when the schedule option is on.
- A selected enabled Kokoro voice requires Kokoro.
- A selected enabled Piper voice requires Piper and carries its exact
  station-owned `PiperVoiceModel` basenames, hashes, language, and native sample
  rate into the requirement.
- Any enabled streaming `Encoder` using AAC requires fdkaac. Its bitrate maps
  to HE-AACv2 (up to 64 kbps), HE-AAC (up to 96 kbps), or AAC-LC (above 96
  kbps), matching the live script renderer.
- An `AircheckConfig` using HE-AAC requires fdkaac only when an enabled encoder
  group actually hosts the always-on aircheck output on the canonical input
  device.

Selected disabled voices, malformed selected voice/model records, malformed
weather schedules, unsupported engines, and an enabled road schedule with no
configured logical voice are explicit station-configuration failures. There
is no engine fallback and no guessed product or station default.

## Evidence schema version 1

The top level records:

- `schema_version`;
- overall `result` (`pass` or `fail`);
- the runtime manifest schema version and SHA-256 identity;
- structured product-contract and station-requirement errors;
- one sorted component record for Kokoro, Piper, and fdkaac.

Each component record states whether it is required and why, its product
expectation, safe observed identity, artifact and capability evidence,
selected Piper model evidence, status (`pass`, `fail`, or `optional_absent`),
and bounded safe diagnostics. Optional installed-but-broken components may
report their own `fail`, but only required failures affect the overall result.
No environment, credentials, connector settings, provider stderr, or arbitrary
station paths are included.

## Read-only capability proofs

Kokoro validation checks the canonical executable, isolated package versions,
model and voice-database hashes, then uses the existing IsadoraAir subprocess
provider for a real mono PCM16 24 kHz synthesis. The smoke process runs from an
unrelated temporary working directory while the dispatcher explicitly owns the
authoritative Git module root.

Piper validation checks its canonical executable and isolated package version.
For every selected station model it delegates basename/root confinement,
model/config pairing, hashes, language, native sample rate, synthesis, and
native-rate WAV proof to the existing `PiperTTSProvider` and `TTSService`.

fdkaac validation invokes `deploy/check_he_aac.sh` with explicit canonical
binary/library paths and a bounded process group, then translates its result.
Canonical validation uses `--runtime-only`, omitting only build-time pkg-config
metadata that E4 deliberately does not publish. Expected versioned-library
identity, ELF linkage/resolution, AAC-LC, HE-AAC/SBR, HE-AACv2/SBR+PS, and
ffmpeg-decode proofs remain unchanged. Full E4 staging validation continues to
require pkg-config evidence. Foundation E does not duplicate those checks.

All synthesis output lives in automatically cleaned temporary directories.
There is no network operation and no sudo invocation.

## Future consumers and remaining legacy checks

The API is intentionally independent of a particular caller so a later
provisioner, backup v3, bare-machine restore, fresh installer,
`check_deploy_baseline`, production activation gate, or diagnostics view can
consume the same evidence.

That integration is not part of E1/E2. In particular,
`deploy/restore/70-tts.sh` still provisions historical home-directory runtimes
for its explicit connected/fresh-install mode (Runtime Foundation E7B,
2026-08-29: its default backup-based-DR mode now delegates to Foundation
E3 instead — see `docs/RUNTIME_BACKUP_PAYLOAD.md`'s "Restore
integration"), `deploy/restore/95-validate.sh` still calls `check_deploy_baseline`, and that
management command still carries older `/home/jreed/...` presence-oriented TTS
checks. `deploy/restore/50-native-deps.sh` already delegates native work to the
authoritative fdkaac tooling and should remain the provisioning layer rather
than being folded into this validator.
