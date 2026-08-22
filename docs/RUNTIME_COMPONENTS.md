# Runtime component contract

Runtime Foundation A established a Git-owned, machine-readable product
contract for the separately provisioned Kokoro, Piper, and fdkaac runtimes.
Runtime Foundation B adds the shared TTS request, dispatcher, and stable CLI
contract documented in `docs/TTS_RUNTIME.md`:

```text
isadoraair/runtime_components.json
```

`isadoraair.runtime_components` is the validating loader. Future
provisioners, validators, disaster-recovery restore, and the interactive
installer must consume this contract instead of copying versions, hashes, or
paths into independent scripts.

## Foundation A/B deployment status

These phases own source and define the future contract; they do **not** deploy
or migrate production:

- The active Kokoro wrapper and assets remain under `/home/jreed/kokoro`.
- Existing weather, road-condition, and dedication callers remain unchanged.
- `/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`,
  `/run/isadoraair/tts`, and `/usr/local/bin/isadoraair-tts` have not been
  created or activated by this work.
- No model, voice database, Piper model, native source archive, or virtualenv
  is stored in Git.

## Canonical planned paths

| Purpose | Contract path |
|---|---|
| Application/provider source | `/opt/isadoraair` |
| Separate runtime environments | `/opt/isadoraair-runtime` |
| Kokoro model and voice data | `/var/lib/isadoraair/tts/kokoro` |
| Station-selected Piper models | `/var/lib/isadoraair/tts/piper` |
| Stable shared TTS CLI | `/usr/local/bin/isadoraair-tts` |
| TTS scratch files | `/run/isadoraair/tts` |
| fdkaac executable | `/usr/local/bin/fdkaac` |
| libfdk-aac installation | `/usr/local/lib` |

These defaults contain no station username or `$HOME` assumption. The Kokoro
execution contract deliberately sets no fixed CPU affinity, thread count, or
niceness policy. A later deployment phase may expose bounded resource tuning
without making one station's CPU topology the product default.

## Component semantics

### Kokoro

Kokoro is conditionally required. A station that selects it for an enabled
feature must have a valid runtime, model, voices database, and provider. A
station that does not select it may omit it.

The checked-in implementation is now owned by:

```text
isadoraair/tts/normalization.py
isadoraair/tts/kokoro.py
deploy/isadoraair-tts
```

It preserves the production normalizer and mono signed-16-bit WAV behavior.
Foundation B's shared process-isolated interface now owns the public Python and
CLI contract. The Git-owned `deploy/isadoraair-tts` launcher exercises that
dispatcher from any working directory without venv activation or a caller
`PYTHONPATH`; it resolves the authoritative checkout from its own path. The
provider dispatcher supplies that same checkout explicitly to the dedicated
runtime interpreter. The future `/usr/local/bin/isadoraair-tts` entry point has
not been installed. Existing callers remain unmigrated.

The runtime package is `kokoro-onnx==0.4.7`. The manifest also records the
proven runtime dependency versions and exact SHA-256 identities for
`kokoro-v1.0.onnx` and `voices-v1.0.bin`.

### Piper

Piper is a supported optional component at `piper-tts==1.4.2`:

```text
not selected and absent -> OPTIONAL PASS
selected and valid      -> PASS
selected and incomplete -> FAIL
```

There are no product-default Piper models. The shared service accepts Piper as
an engine identity without requiring it when unselected; selecting it reports
that no logical voice is configured. Model filenames and hashes will be
station-selected configuration consumed by Foundation C's provider and the
future shared validator.

### fdkaac/libfdk-aac

fdkaac is conditionally required only for features selecting HE-AAC or
HE-AACv2. The contract records fdkaac 1.0.7, libfdk-aac 2.0.3, and the exact
local-source archive identities needed for a future GitHub-free DR build:

| Archive | SHA-256 |
|---|---|
| `fdk-aac-2.0.3.tar.gz` | `e25671cd96b10bad896aa42ab91a695a9e573395262baed4e4a2ff178d6a3a78` |
| `fdkaac-1.0.7.tar.gz` | `145d4684c9325a2bd650e46a04b03327abe780a7b59cce47e6de8af2064fb2c7` |

Foundation A does not alter `deploy/build_fdkaac.sh` or DR payloads.

## Product and station ownership

The product contract owns runtime versions, artifact identities, canonical
paths, availability semantics, and provider behavior. Voice selection and
speech personas remain station configuration. Oak Grove's current Kokoro and
Piper voice choices are deliberately absent from the generic manifest.

## Provenance and redistribution boundary

- `kokoro-onnx` software is identified upstream as MIT licensed.
- The Kokoro model is identified by its audited model documentation as Apache
  2.0 licensed.
- Coverage of the exact aggregated `voices-v1.0.bin` for distribution in a
  public IsadoraAir release remains:

```text
LICENSE CONFIRMATION REQUIRED FOR PUBLIC REDISTRIBUTION
```

That open public-distribution question does not prevent IsadoraAir from owning
its wrapper/provider source. Exact license references are retained in the
machine-readable contract; private DR still needs to preserve applicable
license and notice material with the artifacts.

## Next-phase companion boundary

Runtime Foundation C should add station TTS configuration, complete the Piper
logical-voice provider, and prepare controlled caller migrations. Road
conditions must then stop dynamically importing `weather-ingest/lib/voices.py`,
and weather-ingest must consume the stable CLI instead of owning Kokoro/Piper
runtime internals. None of those caller changes belongs to Foundation B.
