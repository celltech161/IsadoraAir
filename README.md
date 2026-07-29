# IsadoraAir

Django-based radio automation system built for [Oak Grove Radio](https://oakgroveradio.com) (KOGR-LP, Minneapolis KS).

IsadoraAir manages the full music library, schedule programming, playlist generation, and live on-air playback for a broadcast radio station — from importing and analyzing a track to actually mixing it out through the studio monitor. It replaces a previous FastAPI/mpv prototype with a proper Django application backed by PostgreSQL, plus a standalone GStreamer playback engine.

## Screenshots

**On-air console** — twin waveform decks with album art, VU meter, listener count, mic controls, and the coming-up queue.

![Dashboard](docs/screenshots/dashboard.png)

**Library** — searchable/filterable/sortable, with bulk actions and an import + CD-rip page under the same roof. Runs comfortably at tens of thousands of tracks on modest hardware — the KOGR-LP production install pictured here holds ~30k rows, and the underlying Postgres + indexed queries scale well beyond that for anyone with a bigger collection.

![Library](docs/screenshots/library.png)

## Features

**Library Management**
- Import audio files from disk or via drag-and-drop upload at `/library/import/`; automatic tag reading (ID3, Vorbis, MP4/AAC, and RIFF LIST INFO for WAV) via mutagen
- WAV / AIF / AIFF uploads are automatically transcoded to FLAC on their first analyze pass (same DB row, tags preserved, original file removed) so the library ends up single-format and tag-friendly
- CD ripping directly on the box (see the CD Ripping section below)
- Audio analysis: waveform generation, auto-detection of cue-in and next-start (mix) points via ffmpeg; per-category threshold overrides (dBFS for cue-in and next-start) let quiet material like classical music use later triggers than the global defaults
- Fast cue-point re-pick: analyze_tracks persists the mono envelope into the waveform JSON, so re-picking cue points after a threshold tweak (per-track "Reset Cue Points" button on `/track/<pk>/`, or per-category "Update Cue Points" on `/categories/`) runs in seconds instead of the minutes a full re-decode would take
- `fix_unknown_artists` management command: parses "Artist - Title" out of tracks whose artist is "Unknown Artist" and writes the split back to file metadata; WAV/AIF get transcoded to FLAC first (since they can't carry tags cleanly) with the original marked `ready2air=False` for manual weeding
- Searchable/filterable frontend with full track detail editing; comfortably handles libraries in the tens of thousands of tracks on modest hardware (Postgres + indexed queries scale further for anyone with a bigger collection)
- Bulk actions: mark ready-to-air, assign categories, set metadata
- Per-track cue points, rotation weight, energy level, vocal type, end type, RBDS overrides
- Category Kind (Music/Imaging/Spot/Talk, extensible) with an admin-manageable fill color per kind, shown on the live dashboard's queue

**CD Ripping** (built into `/library/import/`)
- Insert a CD, click "Detect CD" — libdiscid reads the disc, MusicBrainz supplies the album + track metadata, all fields land in an editable form so the operator can adjust before ripping
- Falls back to a blank editable table when MusicBrainz has no matching release (unknown discs / rare pressings still rippable, just with manual tag entry)
- Ripping itself is whipper (secure cdparanoia-based ripper) as a detached subprocess so a gunicorn worker restart doesn't kill an in-flight rip; per-job staging dir keeps concurrent probing safe
- AccurateRip verification is captured per track (match / nomatch / notfound badges on the Done screen); `require_accurate_rip` config gate lets non-verifying tracks land with a warning instead of being rejected outright — appropriate since many CD-Rs and older pressings aren't in the community reference database
- Live progress UI: subprocess stdout streams into `status_message` every second, terminal-state dispatcher rehydrates on browser refresh so a mid-rip page reload picks up where it left off
- Drive characteristics (device path, read offset, staging root, AR strictness) live in a `CDRipConfig` singleton editable at `/admin/library/cdripconfig/` — replacing the drive doesn't need a redeploy, just an offset lookup at accuraterip.com/driveoffsets.htm
- Each ripped track lands as `<Artist> - <Title> [Trk NN].flac` under `<library_root>/<category>/`, ready2air=False so it hits the same human-review gate as any other new import

**Schedule Programming**
- 7x24 schedule grid (responsive — day-view on mobile) assigning either a weighted Rotation or a fixed Playlist to each hour, recurring (day-of-week) or one-off (specific date)
- Rotations are ordered slots of Categories; Playlists are hand-built ordered track lists (drag-to-reorder builder UI)
- Holiday-themed rotation weighting with configurable ramp-in/ramp-out periods

**Log Builder**
- Generates each hour's playlist automatically from its ScheduleBlock (Rotation → Category → Track, or Playlist → PlaylistItem directly), auto-approved with no manual gate
- Recency avoidance with configurable artist/title separation windows (global defaults + per-category overrides), time-based or proportional-to-category-size
- Duration-aware track selection near end of hour, progressive constraint loosening when eligible tracks are exhausted
- Log Fill Configuration: tops a short hour up from a fallback category (repeat-last or fixed) so a short playlist/rotation never leaves the engine short

**Playback Engine** (`library/services/engine.py`, runs as its own `isadoraair-engine` service)
- Single GStreamer pipeline mixing two independent decks through a live `audiomixer`, with real crossfading timed off each track's analyzed cue-in/next-start points — not a scripted fade curve, the "fade" is each track's own mastered outro/intro
- Broadcast-clock hour handling: swaps to the next hour's log ~30s before top-of-hour rather than waiting for the current hour's queue to drain, so playback stays locked to wall-clock hours
- Pause/resume/eject/seek per deck, all live-controllable from the dashboard
- Interim AGC (compressor + makeup gain + limiter) for the studio monitor output specifically, configured from its `AudioOutput` admin page

**Live Studio Mic + PTT + Graceful Ducking**
- Dashboard "Studio Mic" button (click-to-toggle) gates a live studio mic input into the on-air mix via a two-mixer pipeline: the decks' summed output feeds a duck gain stage before joining the mic in a shared master mixer, so a track transition mid-talkover never causes a duck discontinuity
- Optional ducking (admin enable/disable + level in dB) smoothly ramps program audio down/up over ~500ms on PTT toggle — no clicks from an instantaneous level change
- Mic hardware controls (gain, preamp, etc.) are dynamically enumerated from the configured device's real ALSA mixer controls in admin — no hardcoded interface-specific fields, so it adapts to whatever's actually plugged in

**Remote DJ over WebRTC**
- Browser-based remote DJ console at `/remote-dj/` — a remote DJ connects from any phone or laptop, hears program audio via a WebRTC monitor-return (mix-minus so they don't hear themselves), and can talk over via a gated remote mic that mixes into the on-air chain
- Server-side WebRTC via GStreamer's `webrtcbin` (media stays direct UDP; nginx only terminates the signaling websocket); STUN-only ICE with a pinned UDP port range for router forwarding, STUN DNS pre-warmed at engine start to shorten cellular first-connect
- Talk gate (open/close remote mic into the on-air mix) is operator-controlled from the main dashboard; the same ducking config applies uniformly to whichever mic is live (studio local, remote, or both)
- Full queue authority for the connected DJ — search-to-add, Play Now, drag-to-reorder, per-row force-next — so they can run their own show start to finish; track-detail links are the only console feature intentionally kept out of remote_dj mode
- Login-gated on a dedicated `remote_dj` Django group with time-signed short-lived signaling tokens; anyone not in the group gets a minimal 'not authorized' page instead of the console

**Live Dashboard**
- Dual-deck view (stacks on mobile) with click-to-seek waveform, live position, and transport controls for whichever deck is playing; the idle deck previews the next queued track
- "Coming Up" queue table for the full remaining hour — drag-to-reorder (mouse and touch, same Pointer Events code path), insert a track by search, per-row "force next" button, color-coded by Category Kind
- Manual "play a playlist now" override, a "Restart Engine" recovery button, Studio Mic PTT, and a Remote Mic gate button that lights up when a remote DJ is connected

**FX Carts / Hotkeys**
- Grid of one-shot audio buttons (drops, stingers, jingles, ID sweepers, sound effects) always visible on the main dashboard + remote-DJ console; first 8 in a compact row, "More…" expands the full grid. Mobile-portrait: collapsed behind a single "FX Carts (N) ▼" toggle with ~50% button size so the panel doesn't eat the deck view
- Each cart is a `FXCart` admin row (Config → FX Carts) with configurable name, audio file, per-cart gain trim, retrigger mode (Restart / Ignore / Stop for click-to-play/stop long beds), keyboard shortcut (single key; unique across all carts; focus-aware so typing into search doesn't fire them), and RGBA idle + playing colors via the same color+opacity picker UI Theme uses
- **Button IS the progress bar** — as the audio plays, the button's playing color sweeps left-to-right over the file's actual duration, then snaps back to idle
- File upload: drag-drop an audio file straight onto the cart admin change form; server places it under `/srv/isadoraair/carts/` and fills in the filepath field. Hard-coded paths still work — the two entry methods coexist. File-existence badge (green ● / red ⚠) in the admin list view catches "file was deleted but the row is still here" mistakes at a glance
- Audio path: persistent FX sub-mixer + single permanent pad on the master mixer — no pad churn on the main audio path, so a fire (or a spam of them) can never destabilize the on-air chain. A permanent silent source keeps the mixer / alsasink continuously hot so the first fire after idle doesn't lose its leading edge to a cold-start
- `FXBusConfig` singleton (Config → FX Bus): global bus volume (live-adjustable) + polyphony cap (max simultaneous fires; over-cap fires are dropped)

**Voice Tracking**
- Two DJ voice-overs per song — one for the outro (fires at the outgoing track's `outro_starts_seconds`) and one for the intro (fires before the incoming track's `intro_until_seconds`). Track-bound, so "That was Dolly Parton's 1978 hit…" is recorded once and plays every rotation of that song
- Browser recording via `MediaRecorder`, WebM/Opus captured client-side, transcoded to 16-bit PCM WAV in the browser (`audioBufferToWav`) and uploaded — no server-side ffmpeg dependency, works on any device with a mic. All three DSP passes (AGC, noise suppression, echo cancellation) are forced OFF via `{exact: false}` constraints so the raw take reaches the on-air chain untouched by Chrome's speech-optimized guessing
- In-browser audio editor with waveform display (mono / stereo aware L/R lanes), keep/delete trim modes with in-session undo stack, peak normalize (0.95 target), zoom on mousewheel + Alt-drag pan + click-to-seek. Destructive Save-and-close writes a fresh WAV via atomic tmpfile-then-rename, so a crash mid-write can't corrupt the airable file
- Playback engine sequencing: at outgoing outro_starts, the state machine enters VT mode, fires the outro-VT with a configurable duck ramp on the deck bus, plays outgoing to natural end, holds for outro-VT to finish, waits `min_gap_ms`, fires intro-VT, and computes an incoming-deck start delay so the intro-VT ends exactly at the incoming track's `intro_until_seconds` — with music underlap when needed. Dedicated `vt_duck_gain` element upstream of the mic duck so VT ducking and mic ducking can be tuned independently
- Gated on `intro_until_seconds` and `outro_starts_seconds` markers being set (per SoundExchange-adjacent design: no VT should ever land in a track's vocal window). Record buttons enable in real time as the operator types marker values and auto-save the track before opening the recording modal, so no "click Save first, wait, then record" ceremony
- `/voicetracks/` index page (staff + remote_dj) lists every VT with track / artist / position / duration / edit / delete; Track detail page (`/track/<id>/`) has an inline recording modal for intro + outro slots. `VoiceTrackConfig` singleton (Config → Voice Track Config) tunes the global duck depth, ramp, and inter-VT gap

**Aircheck Recording** (`aircheck/` app)
- Captures what actually went out over the air, on demand, from any dashboard — start/stop button, session list with duration + file size + status
- Backed by a persistent liquidsoap `output.file` block driven over telnet (`aircheck.reopen`), so no per-session ffmpeg subprocess is spawned and no ALSA device is contended with the encoders — the same in-process source that feeds Icecast/Shoutcast is what gets written to disk
- HE-AAC encodes are remuxed async on stop for compact archival (~1/10th the size of FLAC) without blocking the session state, so a "did I really air that ad?" question a week later is a browser click away

**Streaming Encoders** (`encoders/` app, `isadoraair-encoders` service)
- Liquidsoap-backed relay to Icecast and Shoutcast (v1/v2) mounts, one process per shared ALSA capture device fanning out to every enabled stream (mp3/aac/vorbis) on that device
- Live now-playing metadata pushed to each stream from the playback engine
- Self-reported silence detection per feed (no second ALSA reader on the same device) surfaced to Monitoring

**RBDS/RDS Encoder Client** (`rbds/` app, `isadoraair-rbds` service)
- Sends station PS (with optional scrolling multi-frame rotation) and RadioText (RT/RT+) to StereoTool's RDS encoder, in either binary UECP or StereoTool's ASCII dialect
- Extended Country Code (ECC) transmitted via UECP's Slow Labelling Codes command (RDS group 1A, variant 0) so receivers can fully qualify the PI code's country instead of inferring it from the PI's leading nibble alone — UECP only, StereoTool's ASCII dialect has no ECC command
- Per-Category RBDS PTY (Program Type) override with an optional 8-character PTYN (Program Type Name) — lets a specific rotation category broadcast a different PTY than the station-wide default while its tracks are airing (e.g. a Sunday jazz block sending PTY=Jazz on top of an otherwise Rock station), configurable per category in Django admin or the `/categories/` frontend page; PTY override applies on both protocols, PTYN is UECP-only (no ASCII equivalent)
- Promo rotation with priority-interrupt scheduling that returns to now-playing once shown; each message can source its text from a static field, a local file, or a URL
- Read-only status dashboard; all configuration is admin-only

**System Monitoring** (`monitoring/` app, `isadoraair-monitoring` service)
- systemd/disk/CPU/memory/temperature/transmitter/audio-silence health checks, admin-configurable thresholds
- Email (and SMS-via-carrier-gateway) alerting with per-check debounce and cooldown
- Transmitter integration (Aquabroadcast COBALT) for forward/reverse power, VSWR, PA temperature, fan speed, and RF interlock

**Royalty Reporting** (Reports frontend + `royalty_report` command)
- SoundExchange NCE Report of Use generator — per-unique-track spin counts with ISRC, album, marketing label, aggregate tuning hours, and a service-identifier header block. Music-category-kind plays only, 30-second SoundExchange threshold applied at query time. Also produces a human-readable summary format for eyeballing before submission, and a raw-CSV audit dump of every PlayEvent.
- Append-only `PlayEvent` evidence ledger — written by the playback engine at deck creation and closed out at deck removal. Snapshotted fields (title, artist, album, label, ISRC, category kind) are immutable once written so a downstream track rename or delete can't corrupt historical rows. Retention: 3 years by default (SoundExchange's typical audit lookback), auto-pruned by `isadoraair-prune-royalty-ledger.timer`.
- `Track.isrc` field auto-populated from ID3 TSRC / Vorbis ISRC tags at import; the `backfill_isrc` command re-reads existing library files; the `backfill_isrc_musicbrainz` command queries MusicBrainz for tracks with no tag ISRC using artist + title + album + duration matching (single-candidate confidence required unless `--allow-ambiguous` is passed).
- Aggregate Tuning Hours computed automatically from per-minute Icecast / Shoutcast listener samples (`isadoraair-sample-icecast.timer`) integrated across the reporting period with irregular-sample handling and outage caps. Manual override supported for reconciliation against a stream host's own admin panel.
- `/reports/` web page (staff-only) with a month picker, format selector, ATH-override input, and a table of past reports showing ATH, ISRC coverage, and one-click download. Persisted `RoyaltyReport` archive lives on disk forever with a metadata row for audit trail.
- Station identity for the NCE header row lives in a `StationInfo` admin singleton (Config → Station Info) — legal name, call letters, stream/program name. TuneIn credentials likewise in a `TuneInConfig` singleton (Config → TuneIn AIR). No `.env` editing required to change either.

**Admin & Configuration**
- Django admin, organized into Library / Traffic / Config / Logs sections so unrelated models don't all pile into one bucket
- Analysis Configuration (cue-in/next-start dBFS thresholds), Recency Configuration, Log Fill Configuration, Remote DJ Configuration (STUN server, ICE UDP port range, gain)
- UI Theme: site-wide color palette and nav clock styling, editable with a native color+opacity picker, no page reload required
- Admin-editable navigation menu (Config → Nav Menu): labels, target URLs (Django URL name or arbitrary URL), one level of dropdown children, drag-sort, per-item active-highlighting hints — no template edits needed to reshape the top nav
- Group-based access control: non-staff/superuser users are gated to the union of allowed paths for their Django groups, admin-editable via a `GroupAccess` model (Groups → access inline) — no code edit to grant a group a new URL prefix. Ships seeded with a `remote_dj` group (Remote DJ console + read-only library + track detail) and a `Contributor` group (library browse + upload their own tracks, no editing others'). Cached in-process and invalidated on save so an admin change takes effect on the very next request
- Station timezone (Config → Station Time): every displayed time on the site (dashboard clocks, schedule current-hour highlight, log timestamps, Coming Up ETAs) is pinned to the admin-selected IANA zone regardless of the viewer's device timezone. Applies to server-side render + client-side JS clocks alike, no restart
- EmailLog (Logs section): every outgoing email sent through Django's mail API — password resets, admin invites, monitoring alerts, anything — leaves a read-only row in the admin. Bodies truncated at 10k chars with a visible marker; auto-pruned after 90 days by a systemd timer (`isadoraair-prune-emaillog.timer`)
- Password-reset flow with "Forgot password?" on the login page + an admin-side invite button for creating no-password accounts and mailing them a setup link
- django-axes login lockout on repeated failed sign-ins

**Content Ingestion & Integrations**
- Syndicated program ingestion framework: any external audio program can be pulled from its source, tagged (with artwork where available), and delivered into a rotation category on its own broadcast schedule. Each show is a `syndicated-<slug>.timer`/`.service` pair paired with a small per-source fetcher script; the framework handles metadata, categorization, file placement, and the ready2air gate. Per-source fetchers live outside this repo since they typically carry feed URLs, credentials, or scraping logic specific to each provider. The KOGR-LP install runs 20+ syndicated shows through this framework
- Weather integration: NWS-sourced current temperature, one-day and three-day forecasts feeding RadioText messages via the RBDS client, plus alert beeps for active watches/warnings played straight to a dedicated ALSA loopback into StereoTool — bypasses the playback engine so alerts still fire during a manual override or engine restart
- Bluesky auto-poster: now-playing metadata pushed to a configured Bluesky account every 2 minutes, with de-duplication so an unchanged track doesn't re-post
- TuneIn AIR now-playing pusher: hits TuneIn's broadcaster metadata API on every track change with one HTTP call per song start (respects their explicit "do not use a timer to submit a song" rule by deduping on PlayEvent id — the timer fires every 30s but only makes an outbound call when the current PlayEvent id differs from the last successful push). `commercial=true` set automatically for Spot-category plays. Credentials in Config → TuneIn AIR
- `ogremote` receiver: **ogremote is a separate newsgathering / voiceover tool that is not part of this project** — it runs on its own box and produces content to be aired. IsadoraAir ships only the receiving-side integration: polls for available uploads and dispatches urgent-replay drops into the library. Optional; disable the two `ogremote-*.timer` units if you're not running ogremote upstream

## Architecture

```
IsadoraAir (Django 5.2 LTS)
├── library/                     # Main app
│   ├── models.py                # Track, Artist, Album, Category, CategoryKind, Rotation,
│   │                            # Playlist, ScheduleBlock, PlaylistLog, UITheme,
│   │                            # NavMenuItem, RemoteDJConfig, EmailLog,
│   │                            # CDRipJob, CDRipConfig, etc.
│   ├── views.py                 # Page views + JSON API endpoints (incl. /api/cd/*)
│   ├── admin.py                 # Admin registration + Library/Traffic/Config/Logs sectioning
│   ├── cd_ripping.py            # libdiscid + MusicBrainz disc detection + eject/rip helpers
│   ├── auth_forms.py            # Invite-capable password-reset form (subclasses Django's stock)
│   ├── email_backend.py         # LoggingSMTPBackend — every send leaves a row in EmailLog
│   ├── context_processors.py    # Injects UITheme + nav_menu into every template
│   ├── services/
│   │   ├── log_builder.py       # Playlist generation algorithm
│   │   ├── engine.py            # GStreamer playback engine (standalone process)
│   │   └── remote_dj_signaling.py  # WebRTC signaling websocket server (in-engine)
│   ├── management/commands/
│   │   ├── import_songs.py      # Library scanner (mutagen + RIFF-INFO for WAV)
│   │   ├── analyze_tracks.py    # Audio analysis + auto-transcode WAV/AIF -> FLAC
│   │   ├── cd_rip_run.py        # Detached whipper subprocess + post-processing (tags,
│   │   │                        # rename, DB rows). Spawned by /api/cd/rip-start/
│   │   ├── fix_unknown_artists.py  # Parse "Artist - Title" from title -> tags + DB
│   │   ├── run_engine.py        # Entry point for the playback engine service
│   │   ├── prune_emaillog.py    # EmailLog retention prune (systemd timer)
│   │   └── ...                  # duplicate finders, orphan cleanup, category checks
│   └── templates/library/       # dashboard (shared with /remote-dj/), schedule,
│                                # playlists, library, logs, track detail, login
├── hardware/                     # Audio device config: AudioOutput/AudioInput (incl. AGC,
│                                  # mic gain, dynamically-enumerated ALSA mixer controls),
│                                  # AudioPipeline (sample rate), DuckingConfig,
│                                  # RemoteDJAudioInput
├── encoders/                     # Icecast/Shoutcast streaming (Liquidsoap), own service
├── monitoring/                   # System/service/transmitter/audio health checks, own service
├── rbds/                         # RBDS/RDS client for StereoTool (UECP + ASCII), own service
├── weather/                      # NWS ingestion + alert-beep config (feeds RBDS, own timers)
├── templates/
│   └── base.html                 # Dark-themed base template, mobile nav, live clock
├── deploy/                       # systemd units (one .service + .timer per timer-driven job)
│   ├── isadoraair.nginx                 # nginx site config
│   ├── isadoraair-gunicorn.service      # Web/API
│   ├── isadoraair-engine.service        # Playback engine + remote-DJ signaling
│   ├── isadoraair-encoders.service      # Streaming encoders (Liquidsoap)
│   ├── isadoraair-monitoring.service    # Monitoring poller
│   ├── isadoraair-rbds.service          # RBDS/RDS client
│   ├── isadoraair-analyze.*             # Periodic re-analysis of newly added tracks
│   ├── isadoraair-backup.*              # Nightly full backup (03:30)
│   ├── isadoraair-prune-emaillog.*      # Daily EmailLog retention prune (04:15, 90d default)
│   ├── syndicated-*.*                   # 20+ syndicated-show ingestions on their real air times
│   ├── syndicated-bsky-post.*           # Now-playing -> Bluesky, every 2 minutes
│   └── wx-*.*                           # Weather data + alert beep (~30s cadence check)
└── legacy/                       # Original FastAPI prototype (reference only)
```

## Stack

- **Backend:** Django 5.2 LTS, PostgreSQL, Gunicorn
- **Playback:** GStreamer 1.0 (PyGObject) — standalone engine process, IPC with Django via JSON files
- **Streaming:** Liquidsoap — standalone encoders process relaying to Icecast/Shoutcast
- **Hardware control:** ALSA (`amixer`/`arecord`/`aplay`) for device enumeration and mixer control
- **Frontend:** Django templates, vanilla JavaScript (no framework)
- **Web Server:** nginx with HTTPS (self-signed cert for LAN)
- **Audio Analysis:** ffmpeg, mutagen
- **OS:** Ubuntu 26.04 LTS

## Setup

Ubuntu 26.04 LTS is the tested target. Any modern Debian/Ubuntu should work
with equivalent package names. This walkthrough goes from a fresh box to
audible playback.

### 1. System packages

```bash
# Postgres (server + Python client build headers) and web server
sudo apt install postgresql postgresql-contrib libpq-dev nginx

# Python + build essentials for pip wheels that need to compile
sudo apt install python3 python3-venv python3-dev build-essential

# GStreamer (playback engine core) with a decoder set covering the
# formats a real library actually contains
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-alsa \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav

# Liquidsoap (streaming encoders), ALSA utils (mixer control enumeration
# for the hardware admin, device listing), ffmpeg (waveform + cue-point
# analysis, syndicated-show processing)
sudo apt install liquidsoap alsa-utils ffmpeg

# CD ripping toolchain -- whipper drives cdparanoia + flac; libdiscid is
# the disc-ID library the Python `discid` package binds to. Skip these
# if the box has no optical drive.
sudo apt install whipper cdparanoia flac libdiscid0
```

### 2. ALSA loopback module (only if using StereoTool or a similar
external processor)

IsadoraAir's engine can feed a virtual ALSA loopback device that
StereoTool reads from -- the "canonical digital mix out to processor,
processed audio back in" path. If you're not running an external
processor, skip this step and send the engine straight to your real
sound card.

```bash
# Enable snd-aloop at boot
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf
sudo modprobe snd-aloop
```

### 3. PostgreSQL — create the database and user

```bash
sudo -u postgres psql <<'SQL'
CREATE USER isadoraair WITH PASSWORD 'change-me-in-.env';
CREATE DATABASE isadoraair OWNER isadoraair;
GRANT ALL PRIVILEGES ON DATABASE isadoraair TO isadoraair;
SQL
```

Whatever password you set here goes into `.env` in step 6.

### 4. Runtime directories

IsadoraAir writes to a few paths outside the repo. Create them with the
right ownership before the engine tries to use them:

```bash
sudo mkdir -p /srv/isadoraair/music /srv/isadoraair/waveforms
sudo mkdir -p /var/lib/isadoraair/weather /var/lib/isadoraair/reports
# Replace 'youruser' with whichever account will run the services
sudo chown -R youruser:youruser /srv/isadoraair /var/lib/isadoraair
```

Purposes:
- `/srv/isadoraair/music` — audio library root (matches `LIBRARY_ROOT`
  in `.env`).
- `/srv/isadoraair/waveforms` — pre-analyzed waveform PNGs (generated
  by `analyze_tracks` in step 9).
- `/var/lib/isadoraair/weather` — cached forecasts + alerts polled by
  the weather-ingest timer.
- `/var/lib/isadoraair/reports` — generated royalty / SoundExchange
  filings (persisted from the `/reports/` web page; overridable via
  the `REPORTS_ROOT` env var).

`/run/isadoraair/` (used for the engine's live state JSON) is created
automatically by the tmpfiles config in `deploy/` — no manual step
needed if you install the systemd units.

### 5. Clone + Python environment

```bash
git clone https://github.com/celltech161/IsadoraAir.git /opt/isadoraair
cd /opt/isadoraair

# --system-site-packages so PyGObject/gi (installed above as an OS
# package) is visible inside the venv
python3 -m venv venv --system-site-packages
source venv/bin/activate
pip install -r requirements.txt
```

### 6. Environment file

```bash
cp .env.example .env
```

Edit `.env` and set at least:

- `SECRET_KEY` — required in production (DEBUG=False will refuse to
  start without one). Generate a fresh key:
  ```bash
  python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
  ```
- `DB_PASSWORD` — the password you set in step 3.
- `MUSICBRAINZ_CONTACT` — an email address MusicBrainz can reach you
  at. Their API requires it in every request's User-Agent; leaving the
  default `unset@example.invalid` will get your ISRC backfills
  rate-limited or blocked.
- `LIBRARY_ROOT` — the path where the music library lives (probably
  `/srv/isadoraair/music` from step 4).

### 7. Database migrations + admin user

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 8. Import your music library

Point `import_songs` at a directory tree of audio files. It walks
recursively, reads tags via mutagen, and populates the Track / Artist
/ Album / Category tables. Category is taken from the top-level folder
name under `LIBRARY_ROOT`:

```
/srv/isadoraair/music/
    Rock/
        Artist Name/
            Album Title/
                01 Track.mp3
    Country/
        ...
    Enhanced/        <- syndicated show category
        enhanced.flac
```

```bash
python manage.py import_songs /srv/isadoraair/music
python manage.py check_categories   # flags folders whose Category is not created yet
```

Categories need to exist in the DB before import — create them at
`/admin/library/category/` first, or run `check_categories` and it
will tell you which folders are missing.

### 9. Analyze tracks (waveforms, cue points)

```bash
python manage.py analyze_tracks
```

Generates waveform PNGs and preliminary cue-in / cue-out / intro-until
/ outro-starts markers for every track. Runs in the background if
you install the systemd timer; the initial pass on a large library
can take hours.

### 10. Run the web app and playback engine

For a first-run smoke test, run them in the foreground in two shells:

```bash
# Shell 1 — web UI on http://localhost:8000
python manage.py runserver 0.0.0.0:8000

# Shell 2 — the playback engine (this is what actually produces audio;
# runserver alone plays nothing)
python manage.py run_engine
```

Open `http://<your-box>:8000/`, log in as the superuser you created,
and follow the dashboard from there. The engine will start
auto-playing whatever is scheduled the moment you build a log.

### 11. Optional: streaming, RDS, monitoring

Each of these is a separate long-running process. None is required
for basic playback:

```bash
python manage.py run_encoders    # Icecast/Shoutcast relay
python manage.py run_rbds        # RDS text to the transmitter
python manage.py run_monitoring  # System + transmitter health checks
```

### 12. Production: systemd units + nginx

See `deploy/` for the full production setup — every long-running
process above (`isadoraair-gunicorn`, `isadoraair-engine`,
`isadoraair-encoders`, `isadoraair-rbds`, `isadoraair-monitoring`)
runs as its own systemd unit. Timer-driven jobs (backup, EmailLog
prune, syndicated ingestions, weather, Bluesky poster) each ship as
a `.service`/`.timer` pair. `isadoraair-backup.timer` runs a nightly
database dump + app tree + live-config backup, pushed off-box via
SFTP — the script itself (`backup_isadoraair.sh`) lives outside the
repo at `~/bin/`, alongside its `~/.iasboxbu.cred` remote-target
credentials. Syndicated ingestion and the Bluesky poster scripts
also live outside the repo at `~/syndicated-ingest/` (own venv,
separate from this project's), with credentials in
`~/.syndicated_ingest.cred`.

## Migrating from NextKast, Rivendell, or another automation system

Some things worth knowing if you're coming from an existing station
running something else. This isn't an import tool — it's a
lay-of-the-land so you know what maps to what.

**Your existing music library moves with you.** IsadoraAir doesn't care
what created the audio files. Point `import_songs` at the directory
tree you already have; the mutagen-based tag reader handles MP3,
FLAC, WAV (both ID3 and RIFF LIST-INFO), M4A, Ogg, MP2, AIFF, and
ALAC. Category comes from the top-level folder name, so a Rivendell
`Group`-style organization already carries over if your folders are
grouped that way. If they're not, restructuring the folder tree is a
one-time `mv` operation before import.

**ISRC codes auto-populate from tags.** If your existing library
already has ISRC written into the ID3v2 TSRC or Vorbis ISRC frame,
`import_songs` picks them up and stores them for royalty reporting.
Whatever's missing can be filled in overnight by the MusicBrainz
backfill command (`backfill_isrc_musicbrainz`) — expect ~10% hit
rate on typical station music, more on well-catalogued genres.

**Rotations and Playlists are separate concepts** — this parallels
Rivendell's Clocks vs Log approach.
- A `Rotation` is an ordered list of category slots (or specific
  tracks) that the log builder walks to fill an hour. Category
  slots pick randomly from that category respecting recency
  separation, exactly like Rivendell's scheduler.
- A `Playlist` is a curated ordered list of specific tracks that
  the log builder copies verbatim, in order, no random picking.
- A `ScheduleBlock` maps either a Rotation or Playlist onto real
  time — recurring weekly patterns (day-of-week / hour) OR one-off
  overrides for a specific date.

**There's no separate workstation install per operator.** Everything
is web-based. Any operator with a browser and login credentials can
do anything they have permission for from anywhere — including live
DJ shifts via the WebRTC Remote DJ console. A NextKast admin
juggling three separate machine installs will find this a large
quality-of-life improvement.

**Audio quality is codec-discipline, not framework.** IsadoraAir uses
GStreamer where NextKast uses BASS and Rivendell uses libsndfile;
the audible result is the same when you avoid unnecessary format
conversions between decode and encode. Everything decodes into a
canonical 44100/2/s16 (or float, depending on the pipeline stage)
program bus, gets mixed once, and encoded once per destination — no
cascaded re-encoding.

**Commercial-style traffic (underwriting, affidavits, spot rotation)
is not in the current release.** If your station runs a heavy
underwriting schedule and needs affidavit-quality proof-of-play
reports for sponsors today, that piece is on the near-term roadmap
but not shipped yet. Community stations that only track
music/programming rotations are fully covered.

**No commercial license fee, no per-workstation seat.** IsadoraAir is
AGPLv3 — you can run it commercially, modify it, redistribute it.
The AGPL's network-service clause means if you host a modified
version as a public web service, that modified source has to be
available to the service's users. For an on-air station running an
unmodified copy internally, this changes nothing.

**Rough feature parity check** — most of what NextKast or Rivendell
does day-to-day is present:

| Feature | IsadoraAir |
|---|---|
| Multi-deck crossfade | Yes — dual-deck GStreamer mixer |
| Category/rotation scheduler | Yes — see Rotations above |
| Voice tracks | Yes — browser recorder, in-browser editor, engine state-machine handoff |
| Live assist / manual mode | Yes — dashboard override |
| Web request handling | Not yet |
| Remote DJ | Yes — WebRTC, full queue authority, mix-minus monitor return |
| Icecast/Shoutcast streaming | Yes — Liquidsoap-based relay |
| RDS/RBDS to transmitter | Yes — StereoTool RDS client |
| CD ripping | Yes — whipper + MusicBrainz metadata lookup |
| Waveform display + cue points | Yes — auto-analyzed on import |
| Multiple simultaneous encoders | Via Liquidsoap; not first-class in the admin yet |
| Traffic (underwriting/affidavits) | Not yet — planned |
| EAS integration | Permanently external (hardware ENDEC upstream) |

**Note on the `deploy/` unit files:** paths and the run-as user are
`@@PLACEHOLDER@@` tokens (`@@ISA_USER@@`, `@@ISA_ROOT@@`, etc). See
[`deploy/README.md`](deploy/README.md) for the full placeholder table
and a copy-pastable install snippet that renders + drops each unit
into `/etc/systemd/system/`. Six variables cover the whole set;
`ISA_USER` and `ISA_ROOT` are the only two that matter for a
minimal install without the syndicated-ingest / weather-ingest /
ogremote-ingest companion projects.

## Project Status

| Phase | Status |
|-------|--------|
| 1. Django models | Complete |
| 2. Library import & analysis | Complete |
| 3. Log builder | Complete |
| 4. Playback engine | Complete — dual-deck GStreamer mixer, real crossfading, broadcast-clock hour handling, studio-monitor AGC |
| 5. Live dashboard | Complete — dual-deck view, full-hour queue with drag-reorder + force-next, click-to-seek waveform, manual overrides |
| 6. Streaming, RDS & monitoring | Complete — Icecast/Shoutcast relay, StereoTool RBDS client, system/transmitter health checks with alerting |
| 7. Studio mic + ducking | Complete — dashboard PTT, dynamically-enumerated hardware mixer controls, graceful ducking of program audio |
| 8. Content ingestion | Complete — 20+ syndicated shows on real schedules, weather data + alerts, Bluesky auto-poster, TuneIn AIR now-playing push |
| 9. Remote DJ over WebRTC | Complete — browser-based remote console, mix-minus monitor return, gated remote mic, full queue authority for the connected DJ |
| 10. Email + admin infrastructure | Complete — EmailLog transport-layer capture, invite/reset flows, admin-editable nav menu, django-axes lockout |
| 11. Royalty reporting | Complete — PlayEvent evidence ledger, SoundExchange NCE / summary / raw-CSV generators, /reports/ frontend, ISRC auto-populate from tags + MusicBrainz backfill, ATH computed from Icecast/Shoutcast listener samples with manual override, 3-year retention prune |
| 12. FX carts / hotkeys | Complete — one-shot buttons with drag-drop file upload, RGBA colors, per-cart retrigger modes (restart/ignore/stop), keyboard shortcuts, mobile-collapsible panel, persistent FX sub-mixer with one permanent pad on the master mixer and always-on silence to keep the audio path hot |
| 13. Voice tracking | Complete — per-track intro + outro VT slots, browser recording with DSP forced off, in-browser waveform editor with keep/delete trim + peak normalize + undo, engine state machine sequences outro-VT → gap → intro-VT with computed underlap-delay so intro-VT ends exactly at incoming intro_until, dedicated vt_duck_gain in the pipeline, auto-resume on engine restart preserves position |

Actively running end-to-end on a live station: schedule → log builder → playback engine → StereoTool → transmitter, plus live streaming, RDS, monitoring, remote DJ, and content ingestion.

## Roadmap and current scope

Things a station operator might expect that IsadoraAir doesn't ship today. Some are on the near-term roadmap, some are permanent non-goals; both are called out so you know what you're getting:

- **EAS (Emergency Alert System)** — permanently external. Compliant EAS is always a hardware ENDEC (Sage/DASDEC/Trilithic) that inserts into the audio chain physically upstream of automation, so the alert reaches air even if the automation box is down. Vendor-side software EAS exists in development form but has no FCC approval as of this writing; when a software path becomes a compliant option, IsadoraAir intends to be an early adopter (SAGE-- If you're reading this hit us up!).
- **Commercial-style traffic** — underwriting spot scheduling, affidavit reports, PSA rotation tracking. The current "Traffic" admin section is programming-side (Rotations, Playlists, ScheduleBlocks), not spot-side. Planned.

## Security

For security concerns, see [SECURITY.md](SECURITY.md). Please report privately rather than opening a public issue.

## Support & Community

Ground rules for the GitHub side of this project — please read before opening an issue.

**Issues are for bona fide code defects only.** Reproducible bugs, crashes, factual errors in documentation, and security concerns (report those privately — see above) are on-topic. A minimal reproduction, the exact command that failed, and the full traceback / log line are what turn a report into something actionable.

**Support requests, "how do I set this up," "what's the right config for my station," and general help-me questions will be closed.** IsadoraAir is a working piece of software published for others to learn from, adapt, and run themselves — it is not a supported product. The code is open, the README is thorough, the Django / GStreamer / Liquidsoap / nginx upstream docs are excellent, and reading them is the expected first step.

**There is no official Discord, Slack, IRC channel, subreddit, mailing list, or forum for IsadoraAir.** If one appears, it is unaffiliated with this project; nothing said there is guidance from the maintainer, and no advice found there should be treated as authoritative.

**Response times are best-effort, as time permits.** This project is maintained around actually running a radio station; issues and pull requests are looked at when there's time between transmitter maintenance, on-air work, and everything else a small station requires. Silence on an issue is not disinterest, but it is also not a promise of eventual reply. If you rely on IsadoraAir in production, plan to be able to read and patch the code yourself.

**Pull requests are welcome and are the fastest path to seeing a change land.** A PR with a working patch (and, where relevant, a note on how you tested it) will get looked at before a feature-request issue with no code. Small, focused PRs land faster than large sweeping ones.

## License

Licensed under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE) for the full text.

The AGPL is a strong copyleft. In short: you can use, modify, and redistribute this software (including for commercial purposes), but any modified version you run as a network-accessible service must have its source publicly available to the users of that service. That fits the community-broadcast ethos of this project — a broadcast automation stack that stays open even when it's deployed as a station's operational tool.
