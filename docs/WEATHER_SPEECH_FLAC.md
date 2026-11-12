# Generated speech: FLAC standardization and loudness processing (r0134 candidate)

## What changed

| Area | Before | Now |
|---|---|---|
| Weather speech (Temp, Obs, Forecast, Alert) | MP3 (libmp3lame 320 kbps), no level processing, about -22 LUFS | 44.1 kHz stereo 16-bit **FLAC**, calibrated gain + true-peak limiter, 1 s lead-in unchanged |
| Weather publication | `.mp3` only | `.flac` (standard) or `.mp3`; a FLAC is an ordinary new library asset |
| Dedication intros | native 24 kHz mono FLAC, no peak control (9 of 28 above -1 dBTP) | same file format, plus a no-gain 0.84 peak limiter |
| KanDrive (road conditions) | two-pass loudnorm to -16 LUFS / -1 dBTP | **unchanged** |
| Shared TTS runtime, engine, schema, packages, protected runtime | | **unchanged** |

No migration, package, systemd unit, protected-runtime or engine change.

## Weather chain (`weather_ingest/lib/speech_audio.py`)

```
aresample=44100, aformat=fltp/stereo        ffmpeg's power-preserving mono->stereo upmix (same as the old -ac 2)
volume=<gain> dB                            fixed, per product and per announcer voice
alimiter=limit=0.84:level=false:latency=1   about -1.5 dBFS; no auto make-up; no 5 ms time shift
adelay=1000:all=1                           the 1 s lead-in, after the limiter
-c:a flac -sample_fmt s16 -compression_level 5
```

The gain is a single deterministic pass: no per-clip measurement and no loudnorm. A voice with no calibrated entry
uses the default row and logs a warning rather than failing the broadcast. Keep KanDrive's two-pass loudnorm: its
text is long and its files are consumed once, so measuring is worth it there; Weather clips are seconds long.

### Calibration (2026-11-11; measured on the station's existing audio, no synthesis on air)

Target: -16 LUFS integrated (the station reference, the same as KanDrive), true peak at or below -1 dBTP.

| Voice | Product | Gain | Result |
|---|---|---|---|
| Max_Weatherly | forecast, obs, alert | +6.0 dB | -16.1 to -16.3 LUFS, TP -1.5 dBTP |
| Max_Weatherly | temp | +4.75 dB | about -16.0 LUFS (text dependent, +/- 0.6) |
| Claira_Sky | forecast, obs | +6.5 dB | -16.1 LUFS, TP -1.4 dBTP |
| Claira_Sky | temp | +4.75 dB | -16.0 LUFS (two clips) |
| Claira_Sky | alert | +6.5 dB | **provisional: no Claira alert sample exists** |

Gains differ per product because the products differ in native level; one universal gain would leave three of the
four 1.1 to 1.3 LU off target.

## Dedication intros (`webrequests/services.py`, `isadoraair/announcements/renderer.py`)

A dedication intro is native mono. The pipeline duplicates mono into both channels at unity (GStreamer
`audioconvert`), which plays about 3 LU louder than the file's own mono measurement, so a native mono intro already
sits at about -16 LUFS on air. The calibrated gain is therefore **0 dB** and the work is a peak safeguard only
(`peak_ceiling=0.84`, no resample, no remix, `latency=1` so the cue markers, which come from the final file's
duration, still line up with the speech). Applying the stereo -16 target to a mono file directly would be about
3 LU too hot.

`peak_ceiling` is an optional field on the Speech Splice and Rotation Asset specs, default `None`: every other
caller of the renderer produces exactly the command it always did.

## Publication and Track registration

This is an encoder and filename transition, not a media migration. The publisher (`weather/publication.py`) accepts a
`.flac` (or `.mp3`) candidate whose extension matches the destination filename, and otherwise behaves exactly as
before: per-destination lock, atomic replacement, last-known-good rollback, normal `sync_track_file` registration,
waveform promotion, provenance sidecar, failure notification. A FLAC is registered by its own file path:

- first publication creates a new Track (whatever id Django assigns); later publications of the same file update it;
- an existing MP3 Track or file is **never** read, re-pointed, rewritten or deleted by a FLAC publication;
- there is no cross-format conversion, fallback or migration, and no change to the engine, schema or `delivery.py`.

Permanent destinations (unchanged on every generation; NWS and AMBER alerts share the alert file):

| Category | File |
|---|---|
| WxTemp | `current_temp.flac` |
| WxObs | `current_obs.flac` |
| WxForecast | `forecast.flac` |
| WxAlert | `wx_alert.flac` |

Metadata: the FLAC carries the same title and artist values the MP3 carried (as Vorbis comments); the library's tag
reader populates the Track from them (covered by a real-ffmpeg test).

Diagnostics check the `.flac` files only. A legacy MP3 left in a category is not evidence that the FLAC workflow is
healthy. WxAlert remains event-driven: with no active alert the FLAC legitimately does not exist yet.

## Operator-managed MP3 retirement (required)

The old MP3 Tracks and files are the operator's to retire. Nothing here deletes them, rewrites playlists, logs or
rotations, or changes their readiness. After the first FLAC publication of an asset, **the old MP3 Track and the new
FLAC Track are both in the same category** until the operator retires the MP3. That is a temporary, operator-managed
condition with real consequences; do not leave both eligible:

- **Scheduling and rotations** may pool both Tracks, and approved logs or playlists keep referencing the old MP3 Track,
  which nothing regenerates any more (it will air stale). Review playlists, rotations and scheduled references and
  switch them to the FLAC Track before retiring the MP3.
- **WxAlert urgent insert** takes the first ready Track of the `WxAlert` category with no ordering (engine behavior,
  deliberately unchanged). While the old `wx_alert.mp3` Track is eligible it can be chosen instead of the new FLAC.

### Cutover checklist

1. Confirm no alert transition or urgent insertion is underway; if one is, defer the cutover.
2. Install the release normally (no migration, package or unit change; see the release manifest for restarts).
3. Before the new workflow is in operational service, retire the old **`WxAlert/wx_alert.mp3`** Track from urgent
   selection (clear Ready to Air or remove it from rotation, as the operator prefers).
4. Let the scheduled producers run. Each product's FLAC appears at its first publication; confirm in Weather Setup
   Status that it reports the `.flac` as `ready`, and in the Library that a FLAC Track exists at the permanent path.
5. For WxTemp, WxObs and WxForecast, move playlist and rotation references from the old MP3 Track to the FLAC Track,
   then retire the MP3 Track. Listen once on air and compare the level to a KanDrive segment.
6. WxAlert: the FLAC does not exist until a qualifying alert occurs. Until then the category has no ready alert Track
   (the old MP3 is retired by step 3); there is no automatic fallback. When the first real alert is generated, verify
   the FLAC Track is registered and is the one selected. Do not generate a test alert on the live station.
7. Delete the old MP3 files only as a separate decision after acceptance. Do not re-import the directory while the
   retired MP3 Tracks are still wanted gone: a library scan would register any MP3 left on disk.

### Rolling back the encoder

Roll back the source to r0133 and re-enable the old MP3 Tracks; r0133 publishes `*.mp3`. The FLAC Tracks and files are
ordinary library assets that the operator can retire the same way.

## Open items

1. Claira's alert gain (+6.5 dB) is provisional: no Claira alert sample exists. Claira's output was measured, not
   auditioned.
2. Fixed gains: a new voice or a very different phrasing style needs a calibration row; until then the default row
   (Max's values) applies with a logged warning.
3. The `help_text` of `WeatherConfig.alert_sound_trigger_events` (`weather/models.py`) still names `wx_alert.mp3`; changing
   it needs a migration, so it is a separate cosmetic follow-up.
