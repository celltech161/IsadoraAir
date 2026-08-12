# Companion-project git-readiness audit

Roadmap item 1.2 Phase 2, item 19. Read-only inspection of the three
companion ingest projects (`/home/jreed/syndicated-ingest/`,
`/home/jreed/weather-ingest/`, `/home/jreed/ogremote-ingest/`) — none of
which are currently under version control at all. **No `git init` was
run and no files inside those three directories were created, moved, or
modified as part of this audit** — this is preparation for a separate,
controlled future pass, not that pass itself.

Each subdirectory here (`syndicated-ingest.gitignore`,
`weather-ingest.gitignore`, `ogremote-ingest.gitignore`) is a *candidate*
`.gitignore`, ready to be reviewed and dropped into the corresponding
project root when that controlled pass happens. Findings below explain
the reasoning.

## Summary: safe to version now?

**Yes, with the recommended `.gitignore`s applied first.** No embedded
credentials, API keys, or tokens were found in any of the three
projects' Python source (a broad regex scan for literal
`password=`/`api_key=`/`token=`-style assignments found zero hits across
all three). All three that need external credentials read them from an
external, home-directory, mode-0600 cred file — the same established
pattern as the main IsadoraAir repo's own `~/.iasboxbu.cred`:

| Project | Cred file | Loader |
|---|---|---|
| `syndicated-ingest` | `~/.syndicated_ingest.cred` | `lib/creds.py` |
| `ogremote-ingest` | `~/.ogremote_ingest.cred` | `lib/creds.py` |
| `weather-ingest` | none found — uses the system `mail`/`sendmail` command for alerting (`lib/notify.py`), no smtplib/credential setup at all | n/a |

All three cred files confirmed to exist on the host at mode `0600`,
`jreed`-owned — consistent, correctly restrictive, and (correctly) not
recommended for inclusion in any `.gitignore`'s *tracked* set (they live
one directory up, at `$HOME`, outside any of these three project roots
anyway, so there's no risk of accidentally committing them from inside
the project).

## What must be excluded, and why

All three projects mix **source code** (`.py` scripts, small fixed audio
assets like stingers/beds/intro sounds) with **generated/downloaded
content** (fetched show audio, cached API responses, per-run state
tracking, logs) in the *same* directories — there's no clean
source-vs-output directory split to rely on, so the `.gitignore`s below
are deliberately specific rather than a single blanket rule.

- **`venv/`** — present in all three, 350 MB / 172 MB / 17 MB
  respectively. Regenerable from each project's own dependency list
  (assuming one exists or gets added — not verified this pass).
- **`__pycache__/`, `*.pyc`** — standard.
- **Downloaded/cached show audio** — the single biggest volume by far:
  `syndicated-ingest/kns/audio_cache/` and
  `syndicated-ingest/birdnote/episodes/`+`zip_cache/` alone account for
  ~780 MB of the project's ~800 MB total. This is fetched content,
  re-fetchable from each show's own upstream source, not source code.
- **Per-run state/tracking JSON** (`downloaded.json`, `state.json`,
  `manifest.json`, `scrape_debug.json`, `last_batch.json`,
  `*_state.json`, `nws_obs_cache.json`, and similar under `data/`
  directories) — runtime bookkeeping, not source, and actively
  misleading to version (a committed "last downloaded" marker would go
  stale immediately).
- **Logs** (`*.log`, plus the numbered rotated forms already observed,
  e.g. `get_available_uploads.log.1`/`.2`) — same reasoning.
- **`incoming/`** (ogremote-ingest) — empty at inspection time, but is a
  transient staging directory for incoming content by design.

## What's safe (and probably worth) keeping as real source

- All `.py` scripts.
- Small, fixed, intentional audio assets that are *inputs* the scripts
  use, not outputs they produce — e.g. `syndicated-ingest/kns/woosh.wav`
  and `satp_intro.wav` (transition/intro stingers), each project's small
  `media/` bed-music/stinger files
  (`weather-ingest/media/weather_beeps.flac`,
  `ogremote-ingest/media/stinger.mp3` and friends), and small fixed
  show-art PNGs (e.g. `amos/a-moment-of-science-default.png`). These are
  small (individually well under 1 MB) and don't change on their own —
  genuinely more like source than output. **Flagged for a human decision
  in the actual `git init` pass, not force-included by this audit** —
  the `.gitignore` candidates below don't exclude them, but do call out
  each one explicitly so nobody excludes them by accident with an overly
  broad `*.wav`/`*.mp3` rule either.
- The one `.bak` file found (`kns/get_kns.py.pre-ffmpeg-concat.bak`) is
  almost certainly safe to just delete rather than version — flagged,
  not decided, since deleting host files is out of this audit's
  read-only scope.

## Per-project notes

### syndicated-ingest

24 per-show subdirectories (`105live/`, `academic_minute/`, `acafe/`,
`amos/`, `anjunachill/`, `askakansan/`, `birdnote/`, `bps/`, `bsky/`,
`dnow/`, `enhanced/`, `floyd/`, `fsn/`, `gdead/`, `gll/`,
`innovation_now/`, `kin/`, `kns/`, `lib/`, `rch/`, `warrior/`) +
`venv/`. Total ~950 MB, of which `venv/` (350 MB), `kns/audio_cache/`
and `birdnote/episodes/`+`zip_cache/` (~780 MB combined) account for the
overwhelming majority. See `syndicated-ingest.gitignore`.

### weather-ingest

Flatter structure — a handful of top-level `.py` scripts
(`amber_alert.py`, `amber_poll.py`, `current_temp.py`,
`update_local_wx_data.py`, `wx_alert.py`, `wx_alert_beep.py`,
`wx_forecast.py`), `lib/`, `data/` (cached weather JSON, 92 KB total —
small), `media/` (one FLAC), `venv/` (172 MB). Smallest and simplest of
the three. See `weather-ingest.gitignore`.

### ogremote-ingest

`get_available_uploads.py`, `urgent_pa.py`, `lib/`, `processors/`,
`data/` (2 small JSON state files), `media/` (several small
bed-music/stinger files, 65 MB total — largest media set of the three,
still small relative to `incoming/`'s potential growth), `incoming/`
(empty now, transient by design), `venv/` (17 MB, smallest). Three
rotated log files present at inspection time (760 KB combined) — the
project appears to do its own log rotation already (numbered `.1`/`.2`
suffixes), so the `.gitignore` covers the pattern generally rather than
the specific current filenames. See `ogremote-ingest.gitignore`.

## Recommended next step (not this pass)

When the controlled `git init` pass happens: apply the relevant
`.gitignore` from this directory to each project root, review the
"safe to keep" audio/image assets listed above with a human eye (add
them explicitly if wanted), confirm each project's `venv/` can actually
be regenerated (a `requirements.txt` may need to be written first if
one doesn't already exist — not verified this pass), then `git init` +
initial commit + push to a private remote. Out of scope for this pass
per its own explicit boundary.
