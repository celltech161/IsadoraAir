# Media playback incident validation

`MediaPlaybackIncident` is the durable evidence and work queue for two exact
deck-local failures:

- `watchdog_stall`, after decoded/resampled media buffers stop making progress;
- `deck_pipeline_error`, when the current deck generation posts a GStreamer
  error.

Output, microphone, Remote DJ, and unrelated shared-pipeline errors are not
media incidents. The original engine event remains the immediate alarm.

The live path snapshots the exact Track, file path, deck generation, durations,
media-buffer liveness, and EOS milestones in memory. It then detaches the deck,
releases the mixer request pad, advances playout when possible, and only after
that records the incident. Filesystem inspection, database validation work,
email, and all subprocesses remain off the GLib/audio path.

One daemon worker consumes pending database rows serially. Startup returns a
row left in `validating` by a prior process to `pending`. Shutdown signals an
active validator, terminates and reaps its process group, and returns its row to
`pending`; engine shutdown never waits for the daemon.

## Independent validation

The validator records file size and nanosecond mtime at incident creation and
checks them before and after validation. It runs, at low scheduling priority
when `nice` is available:

```text
ffprobe -v error -select_streams a:0 -show_entries ... -of json FILE
ffmpeg -hide_banner -nostdin -v error -xerror -threads 1 -i FILE -map 0:a:0 -f null -
```

It also launches a separate Python process containing an isolated GStreamer
pipeline:

```text
filesrc -> decodebin -> audioconvert -> audioresample -> audio/x-raw -> fakesink sync=false
```

Every process has bounded stdout/stderr, a hard deadline, its own process
group, TERM/grace/KILL handling, and an explicit reap. No command uses a shell.

## Result meanings

- `CONFIRMED_MEDIA_FAILURE`: no usable ffprobe audio stream or full ffmpeg
  decode failure.
- `GSTREAMER_MEDIA_COMPATIBILITY_FAILURE`: ffprobe and ffmpeg pass, but the
  isolated IsadoraAir-like GStreamer decode errors or stalls without EOS.
- `ENGINE_COMPLETION_PATH_FAILURE`: live milestones prove both decoder EOS
  (`A`) and real-media-leg EOS (`B`) reached the engine, but completion was
  lost later. This does not blame the file.
- `MEDIA_VALIDATION_CLEAN`: all three independent checks pass; retain the
  incident for runtime investigation.
- `FILE_MISSING_OR_CHANGED`: the bytes present at incident time cannot be
  validated reliably.
- `INCONCLUSIVE`: unavailable/timed-out tooling, infrastructure failure, or
  contradictory evidence.

Each completed result produces a `media_health` SystemEvent and, when enabled,
an email through the existing Monitoring Notification Config. Email cooldown
uses path + size + mtime + classification, so repeat incidents are suppressed
across restarts while a replaced file remains eligible for a new alert.

Incidents are read-only in Django admin under Logs. Validation never changes
`Track.ready2air`, moves/deletes media, or alters category, scheduling, cue, or
rotation data. Any quarantine or replacement is an operator decision.
