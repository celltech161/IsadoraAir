# weather-ingest

Local weather/alerting for IsadoraAir: current-temperature and
multi-day forecast announcements (TTS), watch/warning alert beeps
(fires an IsadoraAir FX Cart), and IPAWS AMBER/BLU/MEP alert polling.
Companion production project to IsadoraAir, not part of that repo --
runs as its own set of systemd timers/services on the same host.

**Shared-TTS migration status (implementation branch)**: all speech
synthesis in this project has been migrated, on this development
branch, to route through IsadoraAir's canonical shared TTS surface
(`/usr/local/bin/isadoraair-tts`, logical-voice based -- see "Runtime
integration prerequisites" below) instead of invoking a TTS provider
binary directly. This code has NOT yet been accepted onto production
weather-ingest, and `/home/jreed/kokoro` must NOT be deleted until it
is -- do not treat this README as describing the currently-running
production script set until that cutover has actually happened.

## What it does

- `current_temp.py` -- current temperature announcer (shared logical TTS).
- `wx_forecast.py` -- 1-day/3-day, day/night forecast announcers (4
  separate scheduled variants).
- `update_local_wx_data.py` -- polls/refreshes the shared weather cache
  at IsadoraAir's admin-editable `WEATHER_DATA_DIR`.
- `wx_alert.py` / `wx_alert_beep.py` -- watch/warning detection and the
  on-air alert beep (fires an IsadoraAir FX Cart through the playback
  engine).
- `amber_alert.py` / `amber_poll.py` -- IPAWS OPEN feed polling and CAP
  1.2 parsing for the AMBER/BLU/MEP alert pipeline (`lib/ipaws.py`;
  FEMA's feed is public/unauthenticated, no API key involved).

## Entry points

One `systemd` service per script (`Type=oneshot`,
`WorkingDirectory=/home/jreed/weather-ingest`,
`ExecStart=/home/jreed/weather-ingest/venv/bin/python
/home/jreed/weather-ingest/<script>.py`), each with a matching
`.timer`: `wx-current-temp`, `wx-forecast-{1day,3day}-{day,night}`,
`wx-update-local-data`, `wx-alert-beep`, `amber-alert-poll`. Units live
in `/etc/systemd/system/` -- not part of this repo.

## Python / venv

`venv/` (not versioned) is a standalone `python3 -m venv` (Python
3.14.4, no `--system-site-packages`).

Recreate it:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

`requirements.txt` (added 2026-08-12, IsadoraAir 1.2 Phase 3) covers
this project's own Python environment -- `requests` is genuinely the
only direct PyPI dependency (verified against every tracked `.py`
file's actual `import` statements). It is **not**, by itself, enough to
make weather-ingest functional -- see "Runtime integration
prerequisites" below for what else has to exist.

## Runtime integration prerequisites (not pip-installable)

Unlike syndicated-ingest, this project has almost no ordinary
dependencies of its own -- what it actually needs is a set of *other
running systems*, cross-venv/cross-process:

- **IsadoraAir itself, checked out and migrated**, reachable at
  `ISADORAAIR_DIR` (see below) with its own venv and `.env` in place.
  `lib/wxconfig.py` and `lib/notify.py` both shell out to
  `$ISADORAAIR_DIR/venv/bin/python manage.py <command>` -- if
  IsadoraAir's database isn't migrated and its own venv isn't built,
  every script here fails at the first config read.
- **`WeatherConfig` / `AmberAlertConfig` rows** in that database,
  populated via the Django admin -- there is no local fallback config;
  an empty/default config, not a missing file, is what a fresh install
  actually presents here.
- **The canonical shared TTS CLI**, `/usr/local/bin/isadoraair-tts`, at
  the fixed path `lib/voices.py` hardcodes (see
  `ISADORAAIR_TTS_BINARY`) -- a root-installed, standalone executable
  published by IsadoraAir's own Runtime Foundation E5
  (`isadoraair.runtime_surfaces`), NOT part of this project's venv and
  not something `pip install -r requirements.txt` provides. Takes the
  script text on stdin and a **logical** voice name (`--voice
  <StationTTSVoice.name>`, e.g. `Claira_Sky`) -- this project never
  knows or passes a provider-native voice id, model path, or engine
  flag; which underlying provider/model actually speaks is resolved
  entirely inside that CLI, from IsadoraAir's own database. See the
  IsadoraAir repo's `docs/TTS_RUNTIME.md` for what it does internally.
  (Historically this project invoked a TTS provider binary --
  `/home/jreed/kokoro/bin/kokoro_synth` or Piper -- directly, with a
  hardcoded day/night model-id table; that coupling is retired as of
  the shared-TTS migration. `/home/jreed/kokoro` itself is a separate,
  still-live IsadoraAir runtime concern, not touched by this project
  either way.)
- **`ffmpeg`** -- multi-clip alert/forecast concatenation
  (`wx_alert.py`, `amber_alert.py`, `wx_forecast.py`, `current_temp.py`
  all shell out to it directly). Already a documented IsadoraAir
  dependency; would need `apt install ffmpeg` on a box that doesn't
  already run IsadoraAir.
- **`/srv/isadoraair/music`** (`lib/delivery.py`'s `LIBRARY_ROOT`) --
  where synthesized announcements actually land for the playback engine
  to pick up.

None of these are things `pip install -r requirements.txt` can satisfy
-- they're a restore-ordering dependency (IsadoraAir must exist and be
migrated first), not a packaging gap.

## Configuration and credentials -- different pattern than syndicated-ingest

This project has **no standalone credential file of its own**. Instead:

- Station/alert configuration (`WeatherConfig`, `AmberAlertConfig`) is
  admin-editable inside IsadoraAir's own Django database, read here via
  a cross-venv `subprocess` call into IsadoraAir's `manage.py
  dump_weather_config` / `dump_amber_alert_config` (`lib/wxconfig.py`).
- Runtime weather storage also comes from IsadoraAir: `dump_weather_config`
  returns its resolved `WEATHER_DATA_DIR`, and every entry point uses
  `lib/wxconfig.py`'s one shared resolver. The canonical/default directory is
  `/var/lib/isadoraair/weather`; it never falls back to this source checkout.
  Only this one path is relayed, not IsadoraAir's complete `.env`.
- Failure-notification email goes through IsadoraAir's own
  `send_weather_notification` management command (`lib/notify.py`),
  using IsadoraAir's `EMAIL_*` settings -- no separate SMTP credential
  here either.
- The IPAWS OPEN feed itself (`lib/ipaws.py`) is public/unauthenticated.

This means several files hardcode
`ISADORAAIR_DIR = Path("/home/jreed/isadoraair-django")` (and
`lib/delivery.py` hardcodes `LIBRARY_ROOT = Path("/srv/isadoraair/music")`,
`lib/voices.py` hardcodes `ISADORAAIR_TTS_BINARY =
"/usr/local/bin/isadoraair-tts"`) -- a real reconstructability
dependency on IsadoraAir's own absolute install path/canonical runtime
surfaces and its database, not a self-contained config. See the
IsadoraAir repo's `docs/HARDCODED_PATH_AUDIT.md` (IsadoraAir 1.2 Phase
3) for the full classification of every such path across all three
companion projects and which ones were judged worth a low-risk
env-override.

## External directories (outside this repo, not versioned)

- `WEATHER_DATA_DIR` (canonical/default `/var/lib/isadoraair/weather`) --
  polled weather/alert cache, history, and dedupe-fingerprint files. This is
  operational state outside the Git checkout and is shared with Django.
- `venv/`, `__pycache__/`, `lib/__pycache__/` -- see above.

See `.gitignore` for the exact exclusion list.

## What IS versioned

All `*.py` source (top level and `lib/`), plus `media/weather_beeps.flac`
-- a small, fixed audio asset `wx_alert_beep.py` plays, not generated
output.

## Voice resolution (shared-TTS migration)

`day`/`night`/`auto` resolve identically across every speech-producing
script (`current_temp.py`, `wx_forecast.py`, `wx_alert.py`,
`amber_alert.py`), via `lib/voices.py`'s `resolve_voice()`:

```
WeatherConfig.voice_schedule (exported "voice_schedule")
    -> voice_for_hour()          (pure schedule logic; "auto" only)
    -> WeatherConfig.voice_personas[slot]  (exported persona: logical_voice/
                                             display_name/full_name/signoff)
    -> /usr/local/bin/isadoraair-tts --voice <logical_voice>
```

Both the schedule and the persona come from `lib/wxconfig.py`'s
`load_weather_config()` (the same cross-venv `dump_weather_config`
export this project has always used) -- there is no local, independent
copy of either the schedule or a provider voice-id table anywhere in
this project.

The 4 deployed `wx_forecast.py` systemd units each still pass an
explicit `--voice day`/`--voice night` matched to their own schedule
window -- unchanged by this migration. `--voice auto` is fully
supported by every speech script (see above) for manual/test use; a
future consolidation of forecast voice-schedule authority into
`WeatherConfig.voice_schedule` alone (switching those units to
`--voice auto`) is a deliberately separate, not-yet-scheduled task.

## Tests

`tests/` -- Python stdlib `unittest` + `unittest.mock`, no live network
or TTS-provider calls anywhere. Run with:

```bash
python3 -m unittest discover -s tests -v
```
