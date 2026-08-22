# Shared IsadoraAir TTS runtime interface

Runtime Foundation B defines one engine-neutral interface for IsadoraAir and
its companion projects. It is implemented and tested in the repository but is
**not deployed or used by current production callers yet**.

## Architecture

```text
Django / management command / companion process
        |
        v
SynthesisRequest / stable isadoraair-tts CLI
        |
        v
dependency-free dispatcher and atomic output service
        |
        +-- Kokoro dedicated Python runtime
        |     -> isadoraair.tts.provider_cli
        |     -> Foundation A KokoroSynthesizer
        |
        +-- Piper provider boundary
              -> optional and unconfigured until Foundation C
```

There is no daemon. Each request launches one bounded provider subprocess.
The Django environment imports only standard-library IsadoraAir modules; it
does not import `kokoro-onnx`, ONNX Runtime, NumPy, or Piper.

The dispatcher obtains runtime paths from
`isadoraair/runtime_components.json`. Callers never supply an engine venv,
model path, voice-database path, source module, CPU affinity, or service-user
home directory.

Module discovery is also dispatcher-owned. The Git-owned
`deploy/isadoraair-tts` launcher resolves its own real path, enters that single
authoritative checkout, and execs the checkout's main venv interpreter with
Python environment variables ignored. The provider dispatcher separately
passes its resolved application root as a fresh, exact `PYTHONPATH` to the
dedicated engine interpreter. It never inherits a caller's `PYTHONPATH`.
Consequently neither the public CLI nor
`<kokoro-runtime-python> -m isadoraair.tts.provider_cli` depends on caller CWD,
shell activation, or a second source copy installed into every engine venv.

## Public Python contract

Repository callers can use:

```python
from isadoraair.tts import synthesize

synthesize(
    text,
    engine="kokoro",
    voice="logical_voice_id",
    output_path="/path/to/output.wav",
    speed=1.0,
    language="en-us",
    timeout_seconds=120,
)
```

The equivalent immutable request type is `SynthesisRequest`. Supported engine
IDs are `kokoro` and `piper`. Voice is a logical station-configured identifier,
not an ONNX/model pathname.

Input text and logical voice are required. Speed and timeout must be positive,
finite numbers. The default timeout is 120 seconds; callers synthesizing long
programs may choose a larger bound, such as the existing road-report 600-second
limit.

## Stable external CLI contract

The future installed interface is:

```bash
printf '%s' "$TEXT" | /usr/local/bin/isadoraair-tts \
    --engine kokoro \
    --voice logical_voice_id \
    --output-file /path/to/output.wav \
    --speed 1.0 \
    --language en-us \
    --timeout 120
```

The identical dispatcher can currently be exercised from any working
directory through the Git-owned repo-local launcher:

```bash
/path/to/isadoraair/deploy/isadoraair-tts ...
```

Foundation C may expose that same file at `/usr/local/bin/isadoraair-tts` with
a symlink to the canonical `/opt/isadoraair` checkout. Foundations A+B do not
install that symlink.

`--model` is retained as an alias for `--voice`, `--output_file` for
`--output-file`, and `--lang` for `--language`. These aliases reduce migration
risk for existing Piper-shaped subprocess callers. The documented public names
remain `--voice`, `--output-file`, and `--language`.

Text is read only from stdin. The CLI does not accept runtime/model paths and
does not print input text, environment variables, or configuration.

### Exit status

| Status | Meaning |
|---:|---|
| 0 | Success; validated output atomically published |
| 2 | Command-line usage error |
| 10 | Request/configuration error |
| 11 | Selected engine runtime unavailable |
| 12 | Selected logical voice unavailable |
| 13 | Synthesis or output validation failed |
| 14 | Provider exceeded the request timeout |

Errors use a short category and safe reason on stderr. Arbitrary provider
stderr is drained to prevent deadlock but is never copied into the public
error. At most 4 KiB is retained for recognizing IsadoraAir's structured
provider errors; unstructured output is reported only by byte count.

## Runtime process and timeout behavior

The dispatcher launches provider commands with fixed argument lists and no
shell. Text is supplied through a temporary stdin file, avoiding a blocked
pipe writer. Provider stdout is discarded. The provider gets an allowlisted
non-secret environment rather than Django's complete environment.

Each provider starts in its own process group. A timeout sends `SIGTERM` to the
whole group, waits one second, then sends `SIGKILL` if needed and reaps the
provider. This also terminates grandchildren an engine may have launched.

CPU affinity is not TTS implementation behavior. No fixed cores, thread count,
or niceness are restored here. Later production resource policy may be owned by
systemd or explicit generic runtime settings. The request timeout remains a
caller/service concern.

## Atomic output and validation

The shared service:

1. creates the requested parent directory if needed;
2. creates a mode-0600 temporary file in that same directory;
3. asks the selected provider to write that temporary path;
4. validates the WAV;
5. replaces the requested destination with `os.replace()`.

The same-directory temporary file guarantees the atomic rename does not cross
filesystems. An existing destination remains unchanged until validation has
passed. Timeout, provider failure, and validation failure remove the temporary
file and leave the existing destination intact. Parent directories created for
a failed request remain; directory creation is an explicit part of the public
contract. The resulting file is owned by the invoking user and retains the
temporary file's private mode. Mode `0600` is the intentional Foundations A+B
service contract, not a temporary implementation accident. Foundation C must
verify that each intended consumer can read these private outputs under the
selected service-user model. If another mode is genuinely required, that must
be a deliberate contract change during caller migration rather than an
incidental broadening to `0644`.

Common WAV validation requires:

- a non-empty, parseable WAV containing frames;
- uncompressed PCM;
- one channel;
- two-byte signed samples;
- sample rate between 8 and 192 kHz.

The Kokoro adapter additionally requires exactly 24 kHz. Piper's eventual
configured model contract may establish its own exact sample rate in Foundation
C.

## Provider responsibilities

### Kokoro

The dispatcher uses the component contract's dedicated Kokoro interpreter to
run `isadoraair.tts.provider_cli`. That worker imports and calls Foundation A's
`KokoroSynthesizer`; it does not duplicate normalization or WAV-writing logic.
Voice, speed, and language are forwarded unchanged.

### Piper

Piper remains `SUPPORTED OPTIONAL`. Building the default service does not test
or require Piper. Selecting Piper currently returns a voice-unavailable error
because there is intentionally no generic model map. Foundation C must add a
station-owned logical voice-to-model/hash mapping and provider adapter without
adding HFC or other station models as product defaults.

## Voice, persona, and scheduling boundaries

The TTS layer owns only:

```text
text + engine + logical voice + synthesis options -> WAV
```

`engine` selects an implementation. `voice` identifies the configured voice
within that implementation. A presentation name/persona, spoken signoff,
day/night selection, and weather schedule are business/station metadata above
the TTS service. This layer is not a weather scheduler and does not know persona
names.

## Companion boundary

A companion may decide when to synthesize and what text to speak. It may invoke
the installed CLI and interpret its documented exit status. It must not import
IsadoraAir source dynamically, discover engine venvs, know model paths, or
assume a station username.

## Planned caller migrations

No caller changes are included in Foundation B.

### Dedication intros

Current:

```text
webrequests.services
  -> /home/jreed/kokoro/bin/kokoro_synth
  -> temporary WAV
  -> existing ffmpeg FLAC workflow
```

Foundation C migration:

```text
webrequests.services
  -> isadoraair.tts.synthesize(... engine, logical voice, WAV path ...)
  -> existing ffmpeg FLAC workflow unchanged
```

The dedication voice moves from a Python constant to station configuration.
The common service supplies atomic WAV generation; dedication's existing final
FLAC/metadata behavior remains its responsibility.

### Road conditions

Current:

```text
road_conditions.voice
  -> dynamic import of weather-ingest/lib/voices.py
road_conditions.synthesis
  -> production Kokoro wrapper, once per segment
  -> existing transition/loudness/FLAC workflow
```

Foundation C migration:

```text
road_conditions
  -> resolve engine/logical voice from IsadoraAir station configuration
  -> shared synthesize() for each current segment, timeout_seconds=600
  -> existing transition/loudness/FLAC workflow unchanged
```

This removes the dynamic companion import while preserving road-specific
segmentation, transition audio, scheduling, and final FLAC publication.

### weather-ingest

Current weather-ingest owns engine executable/model paths and provider
dispatch. Its future product boundary is:

```text
weather-ingest decides when and what to speak
  -> /usr/local/bin/isadoraair-tts with engine + logical voice
  -> receives validated WAV or a documented nonzero exit
  -> performs its existing weather delivery/conversion behavior
```

Weather persona names, schedule, wording, and day/night policy remain outside
the TTS runtime. weather-ingest must no longer know Kokoro/Piper venv or model
paths. Foundation B does not modify that companion.

## Scratch provisioning

The component contract reserves `/run/isadoraair/tts` for future bounded
scratch use but Foundation B does not create it. Production migration should
provide it at boot, likely through a tmpfiles rule, owned by the configured
service account with no access for unrelated users. Per-request atomic output
temporaries remain beside their requested destinations so `os.replace()` stays
same-filesystem.
