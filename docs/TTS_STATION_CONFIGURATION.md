# Station logical TTS configuration (Runtime Foundation C)

Foundation C is implemented and tested in Git but is **not deployed and no
production caller has been switched**. It adds the station policy layer above
the Foundations A+B process-isolated runtime.

## Ownership boundaries

```text
feature wording/persona/schedule
        -> enabled StationTTSVoice logical name
        -> resolved engine + provider identity + defaults
        -> shared TTS process boundary
        -> canonical runtime/assets
        -> validated private WAV
```

`StationTTSVoice` owns a stable logical name, enabled state, engine, language,
speed, and either a Kokoro provider voice ID or a `PiperVoiceModel` reference.
It contains no executable, venv, checkout, model, or home-directory path.

`PiperVoiceModel` owns a stable model ID, the ONNX and paired ONNX-JSON
**basenames**, their SHA-256 values, model-bound language, and native sample
rate. The provider joins those basenames beneath the canonical
`/var/lib/isadoraair/tts/piper` root from `runtime_components.json`; callers
cannot submit a path. Both files and hashes, the `<model>.onnx.json` pairing,
and the JSON language/sample-rate declarations are checked before synthesis.
Models are external assets and never enter Git.

The registry has no seeded rows. A schema migration therefore leaves every
installation valid, disabled, and unconfigured. In particular, Oak Grove's
voice choices are not product defaults.

## Feature configuration remains above TTS

Weather's `voice_schedule` still maps hours to feature slot keys such as
`day` and `night`. `WeatherVoicePersona` maps each slot to a nullable shared
logical voice while retaining weather-only display name, full on-air name,
and signoff. The TTS resolver never reads those persona fields.

`WebRequestConfig.dedication_tts_voice` and
`RoadConditionsConfiguration.tts_voice` are nullable inactive cutover
references. Dedications retain a 30-second feature bound. Road reports retain
a 600-second per-segment bound because a multi-event report can be minutes
long; the generic TTS default remains 120 seconds.

Road conditions may instead deliberately enable
`tts_use_weather_schedule` at cutover, resolving the existing weather schedule
through `WeatherVoicePersona`. This preserves scheduled day/night behavior
without importing the companion's source. It is off by default and the current
caller does not read it yet.

## Public logical interfaces

Django callers use the high-level service:

```python
from isadoraair.tts.station import synthesize_station_voice

synthesize_station_voice(
    text,
    voice="logical-station-name",
    output_path=wav_path,
    timeout_seconds=30,
)
```

The stable CLI always resolves the same logical registry:

```bash
printf '%s' "$TEXT" | /usr/local/bin/isadoraair-tts \
  --voice logical-station-name \
  --output-file "$WAV" \
  --timeout 120
```

`--voice` has exactly one meaning here: `StationTTSVoice.name`. `--speed` and
`--language` are optional overrides; omission uses station voice defaults. The
public parser deliberately has no `--engine`, `--model`, runtime-path, or
asset-path option. The internal Python service/provider boundary still carries
the resolved engine plus native Kokoro voice or Piper model ID.

The installed command should be a symlink:

```text
/usr/local/bin/isadoraair-tts -> /opt/isadoraair/deploy/isadoraair-tts
```

The launcher resolves its Git-owned real location, clears caller `PYTHONPATH`,
enters the authoritative checkout, and invokes its main venv with `-E`. The
provider dispatcher then supplies the exact authoritative module root to the
dedicated engine interpreter. This preserves arbitrary-CWD behavior without a
second application-source copy in engine venvs.

## Piper contract and evidence

Piper remains **supported optional**. If no enabled logical Piper voice is
selected, an absent Piper runtime/assets are valid. Selecting a configured
Piper voice with a missing canonical executable raises
`TTSRuntimeUnavailable`; missing, invalid, unpaired, or checksum-mismatched
assets raise `TTSVoiceUnavailable`. There is no Kokoro-to-Piper or
Piper-to-Kokoro failover.

Read-only inspection on 2026-08-22 confirmed the standby runtime is
`piper-tts==1.4.2`. A disposable real synthesis using the female HFC model and
explicit JSON sidecar produced uncompressed `pcm_s16le`, one channel,
two-byte samples, 22050 Hz. The temp WAV was deleted. The inspected station
asset evidence was:

| Station asset | SHA-256 |
|---|---|
| `en_US-hfc_female-medium.onnx` | `914c473788fc1fa8b63ace1cdcdb44588f4ae523d3ab37df1536616835a140b7` |
| `en_US-hfc_female-medium.onnx.json` | `03f1fa0622b80463283592d97aca9f6e89aec345a5c56b7257723e0093c58b6c` |
| `en_US-hfc_male-medium.onnx` | `d11e403a02bdf5a670c877b3dc56e0e1c8cece6fb30289586314dffdc0a78cb0` |
| `en_US-hfc_male-medium.onnx.json` | `f66847424aed0bf99ecbb5d7cfde47c0a906f426a0daf7c46f305e7d21afd886` |

These hashes document the current station candidates; the migration does not
insert them.

Piper's `--length-scale` controls phoneme duration, so the provider maps the
public speed multiplier to `length_scale = 1 / speed`. Language is fixed by
the model JSON (`en_US` for the inspected HFC files), normalized only for
hyphen/underscore and case comparison; Piper has no per-request language
switch. Validation uses the selected model's exact declared sample rate and
does not force Kokoro's 24 kHz contract.

## Oak Grove controlled mapping (operator-created, never seeded)

| Feature/logical proposal | Engine/provider | Persona/business metadata |
|---|---|---|
| `weather-day` | Kokoro `af_jessica` | slot `day`: Claira / Claira Sky / `I'm Claira Sky.` |
| `weather-night` | Kokoro `am_liam` | slot `night`: Max / Max Weatherly / `I'm Max Weatherly.` |
| `dedications` | Kokoro `am_fenrir` | dedication wording remains in `webrequests` |
| `piper-hfc-female` | Piper model `en_US-hfc_female-medium` | optional station alternative only |
| `piper-hfc-male` | Piper model `en_US-hfc_male-medium` | optional station alternative only |

The current weather schedule remains unchanged. Road conditions can preserve
the same day/night selection by choosing the weather-schedule option after the
two persona rows exist, or can deliberately select one fixed logical voice.

## Output and scratch permissions

Foundation B's mode `0600` output remains intentional. All current repo-managed
dedication, weather, engine, and road-sync units run as the same rendered
`@@ISA_USER@@`; ffmpeg is a child of the synthesis job. A future road-audio
unit must use that same account. Under that service-user model every intended
consumer can read the file, so no broader mode or cross-user group is needed.

The inactive repo tmpfiles template defines:

```text
d /run/isadoraair/tts 0700 @@ISA_USER@@ @@ISA_USER@@ -
```

`systemd-tmpfiles` recreates it after reboot beneath volatile `/run`. Each
request owns cleanup of its bounded scratch work; a service must remove stale
request files in `finally` blocks, and reboot clears anything left after a
crash. Atomic publication temporaries remain beside the requested destination,
not in this scratch directory, so `os.replace()` stays on one filesystem.

## Exact later deployment and caller cutover

1. Deploy code and apply schema migrations while all new voice references
   remain null/disabled. Do not switch callers.
2. Provision the canonical Piper venv only if Piper will be selected, and copy
   approved models/configs to the canonical asset root. Verify version,
   filenames, and hashes.
3. Render/install the tmpfiles rule and create the runtime directory as the
   configured service account. Install only the stable symlink above.
4. In Django admin, create checksum-pinned Piper model rows as needed; create
   disabled logical voices; create weather persona rows; then review and enable
   only the selected logical voices.
5. Set dedication and road-condition references deliberately. For identical
   current road behavior, enable its weather-schedule option only after both
   scheduled persona mappings resolve to enabled logical voices.
6. As `@@ISA_USER@@` and from an unrelated working directory, manually run one
   logical Kokoro smoke and each selected Piper voice. Verify exit status, WAV
   contract, owner, mode `0600`, and same-user ffmpeg readability.
7. In a dedicated IsadoraAir caller migration, replace only dedication's direct
   Kokoro subprocess with `synthesize_station_voice`; keep its wording,
   ffmpeg/FLAC, metadata, scheduling, and 30-second bound unchanged. Observe.
8. Separately replace road_conditions' dynamic companion import with internal
   persona/logical resolution and replace each direct Kokoro segment call with
   the shared service at its 600-second bound. Keep segmentation, transitions,
   loudness, final FLAC, fingerprinting, and scheduling unchanged. Observe.
9. In a separate weather-ingest repository pass, consume `voice_personas` from
   `dump_weather_config`, retain persona/schedule/wording there, and replace
   `voices.synthesize()` with the installed CLI using logical voice, stdin text,
   output path, and timeout only. Remove its engine binaries/model paths only
   after acceptance.

At every stage, a configured provider failure is surfaced. Changing engines is
an explicit station configuration action, never an automatic retry.
