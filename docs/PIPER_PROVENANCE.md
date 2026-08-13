# PiperTTS provenance/reproducibility — IsadoraAir 1.2 Phase 3

Inspection of the production install (2026-08-12). Piper is
**intentionally retained** as part of the future generated-speech
architecture (interchangeable with Kokoro, and a lower-resource
fallback) -- not classified as obsolete just because the current
production path favors Kokoro. No changes made to any live install; no
large voice/model assets added to Git.

## Two separate things share the name "piper" on this host

This matters for reproducibility, so it's stated explicitly:

1. **`piper-tts==1.4.2`, installed inside weather-ingest's own venv**
   (`/home/jreed/weather-ingest/venv/bin/piper`) -- this is the binary
   `lib/voices.py`'s `PIPER_BINARY` actually points at and the one
   `wx_alert.py`/`current_temp.py`/`wx_forecast.py` invoke. **This is
   the live, in-use Piper install.**
2. **A separate, older, standalone install at `/home/jreed/piper/`**
   (its own Python 3.10 venv, its own `bin/piper` executable) --
   confirmed via `grep` across both weather-ingest and IsadoraAir
   source: **its own `bin/piper` is never referenced anywhere.** What
   *is* still actively used from this directory is its collection of
   8 `.onnx`/`.onnx.json` voice-model pairs (`amy`, `hfc_female`,
   `hfc_male`, `joe`, `lessac-high`, `lessac-medium`, `libritts_r`,
   `norman`) -- `lib/voices.py`'s `piper_fallback` paths point directly
   at two of them (`en_US-hfc_{female,male}-medium.onnx`). So this
   directory now serves as a **shared voice-model repository**, not an
   active Piper installation in its own right -- its own venv/binary
   are vestigial and could be removed without affecting anything (not
   done in this pass; read-only inspection only).

## What it is (the live install)

[`piper-tts`](https://github.com/OHF-voice/piper1-gpl) (`pip` package
`piper-tts==1.4.2`) -- the Home Assistant-maintained successor to the
original `rhasspy/piper` project (GPL-3.0-or-later license, worth
knowing if license compliance ever matters for a distribution
decision -- not evaluated further here, out of scope). Confirmed via
`pip show`:

```
piper-tts==1.4.2
  requires: onnxruntime, pathvalidate
```

Installed: `onnxruntime==1.27.0` (note: a **different** onnxruntime
version than Kokoro's own venv, `1.28.0` -- the two TTS installs are
fully independent environments, no shared state, so this is expected
and harmless, not a conflict).

**`piper-phonemize` is NOT installed and NOT needed** -- this newer
fork bundles its own phonemization: `piper/espeakbridge.so` (a compiled
extension, `ldd` shows it links only against `libc.so.6` -- fully
self-contained, no system `espeak-ng` dependency) plus its own
`piper/espeak-ng-data/` directory shipped inside the pip package
itself. **This is a meaningfully different native-dependency story
than Kokoro** (which does depend on the system `espeak-ng` package) --
worth knowing so a restore doesn't assume both engines share one
phonemization dependency.

## Install location, mechanism, native dependencies

- **Mechanism**: ordinary `pip install piper-tts` into weather-ingest's
  own venv (`/home/jreed/weather-ingest/venv`) -- not apt, not a GitHub
  release download, not a source build.
- **Native/shared-library dependencies**: none beyond `libc` --
  self-contained (see `espeakbridge.so` above).
- **Executable/entry point**: `venv/bin/piper`, a standard pip
  console-script entry point (confirmed `file` reports "Python script,
  ASCII text executable").

## Command-line interface

```
piper --model <path-to-.onnx> --output_file <path.wav>
```
(`-m`/`--model` and `-f`/`--output-file`/`--output_file` are Piper's
own native long-form flags -- `lib/voices.py`'s Kokoro wrapper was
built to match *this* shape, not the other way around; see
`docs/KOKORO_PROVENANCE.md`.) `--model` takes a **file path** to an
`.onnx` model (unlike Kokoro, where `--model` is a voice *name*) --
Piper auto-discovers the matching `<model>.onnx.json` config file
alongside it (not passed explicitly by `lib/voices.py`'s call, and
confirmed every `.onnx` under `/home/jreed/piper/` has its `.json`
sibling present). Reads text from stdin (matching `lib/voices.py`'s
`subprocess.run([...], input=text.encode("utf-8"), ...)` call), writes
mono WAV.

## Voice/model assets currently present

8 model/config pairs under `/home/jreed/piper/` (see table above), all
`en_US`, `medium` quality except `lessac-high`. Only two
(`hfc_female-medium`, `hfc_male-medium`) are currently wired into
`lib/voices.py`'s `piper_fallback` slots -- the other six are present
on disk but not referenced by any current voice-slot config
(station-specific selection, not a generic dependency question -- see
`docs/TTS_SUPPORT_MATRIX.md`).

## Reproducibility verdict

**Runtime: yes, fully.** `pip install piper-tts` into any venv is a
complete, ordinary recipe -- no native build step, no system
`espeak-ng` dependency (self-contained), no separate `piper-phonemize`
package to chase down.

**Models: yes.** Piper voice models are a well-known, standard,
publicly-downloadable catalog (the `piper-tts` package itself ships a
`download_voices.py` helper, confirmed present at
`piper/download_voices.py`, for fetching official voices by name) --
reproducing this station's exact current selection means downloading
the same 8 named voices, which is fully deterministic by name
(`en_US-hfc_female-medium`, etc.), not something requiring provenance
archaeology the way Kokoro's un-logged download did.

## Smoke test performed (2026-08-12)

```
echo "This is a Phase 3 Piper reproducibility smoke test." | \
  venv/bin/piper --model /home/jreed/piper/en_US-hfc_female-medium.onnx --output_file <tmp>.wav
```
Result: exit 0, produced a valid `pcm_s16le`, 22050 Hz, mono, 3.1s WAV
(verified via `ffprobe`), using a real production voice file. Temp
output deleted immediately after verification.

## Dependency-manifest gap found and fixed

`weather-ingest/requirements.txt` (added earlier in this same Phase 3
pass) initially listed only `requests` as a direct dependency --
correct for weather-ingest's own **imported** Python modules, but
incomplete: `venv/bin/piper` is invoked as an **external CLI binary**
via `subprocess`, never `import`ed, so a plain grep for `import`
statements missed it entirely. `piper-tts==1.4.2` has been added to
`weather-ingest/requirements.txt` to close this gap -- see that
project's own completion-report entry for the corrected manifest and
re-validation.
