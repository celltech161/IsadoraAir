# TTS runtime baseline — Kokoro + Piper — IsadoraAir 1.2 Phase 3

Both local TTS engines are treated as first-class, independently
reproducible/preflightable dependencies. **Piper is not obsolete** just
because Kokoro is the current production-preferred voice -- both are
part of the generated-speech architecture as an explicitly selected alternate
voice engine. There is no automatic engine selection or failover.

## Support matrix

| Engine | Runtime reproducible? | Models reproducible? | Current use | Future role |
|---|---|---|---|---|
| Kokoro | **Yes** — `pip install kokoro-onnx` + `apt install espeak-ng`, no build step | **Yes, with a caveat** — public artifacts, exact upstream release tag unrecorded; checksums now on file (`docs/KOKORO_PROVENANCE.md`) so a restore can verify it got the *same* files | Active — current production voice for weather/dedication-intro speech | Preferred/high-quality voice |
| Piper | **Yes, fully** — `pip install piper-tts`, no build step, no system `espeak-ng` dependency (self-contained) | **Yes, fully** — official named voice catalog, package ships its own `download_voices.py` fetcher | Standby — installed, wired into `lib/voices.py`'s per-slot config, not the currently-selected engine for any active slot | Explicit station-configured alternate voice choice |

Both verdicts are based on real inspection + a real synthesis smoke
test performed during this phase (2026-08-12), not documentation
review alone -- see "Smoke tests performed" below.

## Where each engine actually lives

| | Kokoro | Piper |
|---|---|---|
| Install mechanism | Standalone venv + manually-placed model files | `pip install piper-tts` inside weather-ingest's own venv |
| Location | `/home/jreed/kokoro/` | `venv/bin/piper` (weather-ingest's venv); voice models at `/home/jreed/piper/` (a separate, shared repository — its own old standalone venv/binary there is unreferenced/vestigial, see `docs/PIPER_PROVENANCE.md`) |
| CLI shape | `kokoro_synth --model <voice_name> --output_file <wav>` (custom wrapper built to match Piper's own shape) | `piper --model <path.onnx> --output_file <wav>` (native) |
| Phonemization | System `espeak-ng` (apt) via `phonemizer-fork`/`espeakng-loader` | Bundled `espeakbridge.so` + bundled `espeak-ng-data`, zero system dependency |
| Dispatched by | `weather-ingest/lib/voices.py`'s `synthesize()`, `voice["engine"] == "kokoro"` | Same function, `voice["engine"] == "piper"` |

## TTS dependency boundaries

**Generic IsadoraAir TTS runtime requirements** (repo-managed/documented,
apply to any install):
- Supported Kokoro installation recipe — `docs/KOKORO_PROVENANCE.md`.
- Supported Piper installation recipe — `docs/PIPER_PROVENANCE.md`.
- `weather-ingest/lib/voices.py`'s dispatch mechanism (`engine`/`model`
  keys per voice slot) and the two binary-path constants
  (`KOKORO_BINARY`, `PIPER_BINARY`).
- The preflight checks covering both engines (`docs/RUNTIME_BASELINE.md`).

**Station-specific state** (Oak Grove's own configuration, not baked
into any generic tooling):
- Which voices are actually installed/selected in
  `lib/voices.py`'s `VOICES` dict (`af_jessica` for day, `am_liam`
  for night, `piper_fallback` paths, etc.). Dedications separately use
  `am_fenrir` in the current IsadoraAir caller.
- The 6 of 8 Piper voice files present under `/home/jreed/piper/` that
  aren't currently wired into any slot.
- Any station-configured default voice or per-feature voice choice
  (e.g. `road_conditions/voice.py`'s KanDrive feature, which
  deliberately does not use Piper at all today — a feature-level
  decision, not a dependency-tooling one).

No Oak Grove voice preference has been baked into `docs/KOKORO_PROVENANCE.md`,
`docs/PIPER_PROVENANCE.md`, or the preflight check beyond confirming
*an* engine/model is reachable — which specific voice a feature uses is
entirely `lib/voices.py`'s `VOICES` dict (weather-ingest) or each
feature's own config (IsadoraAir side), never hardcoded generic logic.

## Smoke tests performed (2026-08-12)

Both engines: tiny real synthesis to a throwaway temp path, verified
via `ffprobe` (valid codec/sample-rate/duration, non-empty), deleted
immediately after. No production-generated speech files touched.

| Engine | Command | Result |
|---|---|---|
| Kokoro | `bin/kokoro_synth --model af_jessica --output_file <tmp>.wav` | Exit 0. `pcm_s16le`, 24000 Hz, mono, 3.0s. |
| Piper | `venv/bin/piper --model /home/jreed/piper/en_US-hfc_female-medium.onnx --output_file <tmp>.wav` | Exit 0. `pcm_s16le`, 22050 Hz, mono, 3.1s. Re-verified against a **freshly rebuilt** venv (from `weather-ingest/requirements.txt` alone) with the same result, 1.9s clip — confirming the dependency manifest is actually sufficient to reproduce a working Piper install, not just that the existing one happens to work. |

## Engine-selection boundary

The shared generated-speech layer uses the engine named by the selected logical
station voice. A failure is surfaced to the caller; it does not retry through
another engine. Switching between Kokoro and Piper is a deliberate station
configuration change. CPU monitoring, automatic load thresholds, and fallback
decision logic are not part of the contract.
