# Kokoro TTS provenance/reproducibility — IsadoraAir 1.2 Phase 3

Inspection of the production install (`/home/jreed/kokoro`, 2026-08-12).
No large model files added to Git; no changes made to the live install.

## Runtime Foundation A status

The product-relevant wrapper implementation is now Git-owned in
`isadoraair/tts/normalization.py` and `isadoraair/tts/kokoro.py`. Its canonical
runtime and asset contract is `isadoraair/runtime_components.json`; see
`docs/RUNTIME_COMPONENTS.md`.

This is a source-ownership milestone, **not a production migration**. The live
installation and all current callers still use the paths documented below.
The future canonical paths have not been created, callers have not been
switched, and the production wrapper files have not been modified.

## What it is

[`kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx) (`pip`
package `kokoro-onnx==0.4.7`) — an ONNX Runtime port of the
Kokoro-82M text-to-speech model, run entirely locally/offline (no API
calls, no network dependency at synthesis time).

## Current production layout (legacy path, still active)

```
/home/jreed/kokoro/
├── kokoro-v1.0.onnx     325,532,387 bytes -- the model weights
├── voices-v1.0.bin       28,214,398 bytes -- voice embedding vectors
├── bin/
│   ├── kokoro_synth        shell wrapper (CPU throttling, see below)
│   └── _kokoro_synth.py    actual synthesis CLI (Python)
├── samples/               20 reference WAV clips, one per voice
└── venv/                  standalone venv (Python 3.14.4, no --system-site-packages)
```

## Artifact identity (recorded here since no download record exists)

| File | Size (bytes) | SHA-256 |
|---|---|---|
| `kokoro-v1.0.onnx` | 325,532,387 | `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5` |
| `voices-v1.0.bin` | 28,214,398 | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

**Honest gap, matching how the fdk-aac build's own history was handled
in Phase 2**: no download log, shell history entry, or README on disk
records exactly which upstream release these two files came from.
`kokoro-v1.0.onnx` / `voices-v1.0.bin` are the standard filenames the
`kokoro-onnx` project's own GitHub releases publish -- reconstructing
today would mean downloading the matching pair from that project's
releases page and verifying against the checksums above, not
re-deriving them from source (the model itself is a pretrained neural
network; there is no "build" step, only "obtain the exact weights").
If the checksums above don't match a fresh download, that's the signal
this box is running a different release than whatever's current
upstream -- worth pinning down explicitly before relying on it,
not silently assumed identical.

## Python / native dependencies

`venv/` is a standalone `python3 -m venv` (Python 3.14.4, no
`--system-site-packages`). Direct dependencies (`pip show kokoro-onnx`):

```
kokoro-onnx==0.4.7
  requires: colorlog, espeakng-loader, numpy, onnxruntime, phonemizer-fork
```

Installed versions on production: `numpy==2.5.1`, `onnxruntime==1.28.0`,
`phonemizer-fork==3.3.1`, `espeakng-loader==0.2.4`.

**Native prerequisite**: `espeak-ng` (apt: `espeak-ng`,
`espeak-ng-data`, `libespeak-ng1` -- confirmed installed,
`1.52.0+dfsg-5build1`) -- `phonemizer-fork`/`espeakng-loader` shell out
to it for grapheme-to-phoneme conversion before synthesis.

The historical manual recreation shape is:
```bash
sudo apt install espeak-ng
python3 -m venv venv
venv/bin/pip install kokoro-onnx==0.4.7
```

This is not yet the supported provisioner. Runtime Foundation A records the
proven package versions in the component manifest without adding TTS-only
packages to the main Django `requirements.txt`. A later provisioner will
create the separate runtime deterministically.

## Command-line interface IsadoraAir/weather-ingest use

`bin/kokoro_synth` is a **Piper-compatible entrypoint** (see
`docs/PIPER_PROVENANCE.md`) -- weather-ingest's `lib/voices.py` can
point a voice slot at either engine with the same argument shape:

```
echo "text to speak" | kokoro_synth --model <voice_name> --output_file out.wav [--speed 1.0] [--lang en-us]
```

Reads UTF-8 text from stdin, writes 24 kHz mono 16-bit PCM WAV.
`--model` is a **voice name** (e.g. `af_jessica`, `am_fenrir`), not a
file path -- resolved against `voices-v1.0.bin`'s embedded voice table.

The active shell wrapper applies station-host CPU throttling (`taskset -c
0-3`, `nice -n 19`, capped ONNX Runtime thread pools). Those machine-specific
mechanics are not product defaults. The text preprocessing (decimal-point,
phone-number, and `911` pronunciation fixes plus hashtag stripping) is
product-relevant behavior and is now preserved with regression tests in the
repository implementation. The repository version has no username, home
directory, fixed CPU affinity, or fixed four-thread assumption.

## Voice assets currently present

20 voices (`af_*`/`am_*` prefix = American female/male), all bundled
within `voices-v1.0.bin` itself (not separate per-voice files) --
`samples/*.wav` are just 20 short reference clips, one per voice, for
human A/B listening, not something synthesis depends on. Which voices
are actually *selected* for use (`lib/voices.py`'s `VOICES` dict, e.g.
`af_jessica` for day, station-configured) is station-specific config,
not a generic dependency question -- see
`docs/TTS_SUPPORT_MATRIX.md`'s "generic vs station-specific" boundary.

## Reproducibility verdict

**Runtime: yes.** `pip install kokoro-onnx` + `apt install espeak-ng`
is a complete, ordinary recipe -- no custom build step, unlike fdk-aac.

**Model: yes, with the caveat above.** The two artifacts are publicly
downloadable from the `kokoro-onnx` project's releases; this document
now records their exact identity (checksums) so a restore can verify
it obtained the *same* files, even though it can't prove which
specific upstream release tag originally produced them.

## Smoke test performed (2026-08-12)

```
echo "This is a Phase 3 reproducibility smoke test." | bin/kokoro_synth --model af_jessica --output_file <tmp>.wav
```
Result: exit 0, produced a valid `pcm_s16le`, 24000 Hz, mono, 3.0s WAV
(verified via `ffprobe`). Temp output deleted immediately after
verification -- no production-generated speech files touched.
