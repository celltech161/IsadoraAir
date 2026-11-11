# Generated speech: FLAC standardization and loudness processing (r0134 candidate)

## What changed

| Area | Before | Now |
|---|---|---|
| Weather speech (Temp, Obs, Forecast, Alert) | MP3 (libmp3lame 320 kbps), no level processing, about -22 LUFS | 44.1 kHz stereo 16-bit **FLAC**, calibrated gain + true-peak limiter, 1 s lead-in unchanged |
| Weather publication | `.mp3` only | `.flac` (standard) or `.mp3`; the asset's existing Track is **adopted in place** |
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

## Track identity and cutover

Why a Weather FLAC must **adopt** the MP3's Track instead of registering a new one:

- The engine's urgent insert takes the first ready Track of `WxAlert` with no ordering. With two Tracks the lowest id
  (the stale MP3) wins forever and a new alert never airs.
- Scheduling would pool two Tracks per category; approved logs would keep airing a stale file nothing regenerates.
- `PlaylistItem.track` and `RotationSlot.track` are PROTECT, and `LogItem`/`PlayEvent` hold history; deleting the old
  Track is not possible without detaching about 8,500 log items and 7,158 play events.

`publish_weather_asset` therefore, inside its existing atomic transaction and under the asset lock, re-points the
asset's existing Track (same row, same id) at the new file, then runs the normal sync and analysis. Locks, in the
library's documented order: the asset flock (category + stem), the path locks of both files (sorted), then the Track
row (`select_for_update(of=("self",))`). The previous file is never touched or deleted.

### States a station can be in

| State | Behavior |
|---|---|
| Before install | MP3 Tracks. Nothing changes. |
| Installed, no producer has run | MP3 Tracks, still valid and airing; diagnostics report the `.mp3`. |
| First FLAC publication of an asset | The Track is adopted; the asset now plays the FLAC. Other assets stay MP3 until their own first run. |
| FLAC publication fails (encode, sync, analysis, waveform) | Rolled back: the Track stays at the MP3, the new FLAC is removed, the MP3 is untouched. Same as any current failed publication. |
| Fresh install (no previous Track) | The Track is created at the `.flac` path. |
| Alert asset with no active alert | Stays MP3 until the first FLAC alert converts it. |

Tracks at both extensions (a hand-made duplicate): nothing is adopted, publication proceeds, a warning is returned
and logged by the producer; resolve the duplicate asset manually.

### Operator cutover

Installation is a normal Update Center install (no restart beyond what the release manifest declares). There is no
manual migration step. Afterwards:

1. Run no TTS or alert by hand. The next scheduled producer runs perform each asset's adoption.
2. After each product's first FLAC publication, confirm in Weather Setup Status that the artifact reports the `.flac`
   and `ready`, and that the Library shows the same Track id with format `flac`.
3. Listen to each product once on air; compare level to a KanDrive segment.
4. Leave the old `.mp3` files in place. They are no longer referenced; remove them only as a separate, deliberate
   cleanup after the cutover has been accepted. (Do not re-import the directory first: a library scan would register
   the orphaned MP3 as a second Track and recreate the duplicate-asset problem.)

### Rollback

Rolling the source back to r0133 after any asset has been adopted is **not** self-contained: r0133's publisher would
create a new Track at the `.mp3` path next to the adopted FLAC Track. Before rolling back, for each adopted asset
publish the retained old MP3 back through this release's publisher (adoption works in both directions), e.g.
`manage.py publish_weather_asset /srv/isadoraair/music/WxTemp/current_temp.mp3 WxTemp current_temp.mp3`, which
re-points the same Track at the MP3. Do not roll back source with FLAC Tracks in service without doing this
(or a reviewed manual re-point). This is a reason not to delete the old MP3 files until the cutover is accepted.

## Open decisions for review

1. **No cross-format fallback for urgent alerts.** If the alert FLAC publication fails, the previous MP3 keeps airing
   (as today). There is no attempt to publish an MP3 instead.
2. **Fixed gains.** A new voice, or a significantly different phrasing style, needs a calibration row; until then the
   default row (Max's values) applies with a warning.
3. **Old MP3 cleanup** is deliberately not automated.
