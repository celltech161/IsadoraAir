# IsadoraAir

Django-based radio automation system built for [Oak Grove Radio](https://oakgroveradio.com) (KOGR-LP, Minneapolis KS).

IsadoraAir manages the full music library, schedule programming, playlist generation, and live on-air playback for a broadcast radio station — from importing and analyzing a track to actually mixing it out through the studio monitor. It replaces a previous FastAPI/mpv prototype with a proper Django application backed by PostgreSQL, plus a standalone GStreamer playback engine.

## Screenshots

**On-air console** — twin waveform decks with album art, VU meter, listener count, mic controls, and the coming-up queue.

![Dashboard](docs/screenshots/dashboard.png)

**Library** — 29,000+ tracks, searchable/filterable/sortable, with bulk actions and an import + CD-rip page under the same roof.

![Library](docs/screenshots/library.png)

## Features

**Library Management**
- Import audio files from disk or via drag-and-drop upload at `/library/import/`; automatic tag reading (ID3, Vorbis, MP4/AAC, and RIFF LIST INFO for WAV) via mutagen
- WAV / AIF / AIFF uploads are automatically transcoded to FLAC on their first analyze pass (same DB row, tags preserved, original file removed) so the library ends up single-format and tag-friendly
- CD ripping directly on the box (see the CD Ripping section below)
- Audio analysis: waveform generation, auto-detection of cue-in and next-start (mix) points via ffmpeg; per-category threshold overrides (dBFS for cue-in and next-start) let quiet material like classical music use later triggers than the global defaults
- Fast cue-point re-pick: analyze_tracks persists the mono envelope into the waveform JSON, so re-picking cue points after a threshold tweak (per-track "Reset Cue Points" button on `/track/<pk>/`, or per-category "Update Cue Points" on `/categories/`) runs in seconds instead of the minutes a full re-decode would take
- `fix_unknown_artists` management command: parses "Artist - Title" out of tracks whose artist is "Unknown Artist" and writes the split back to file metadata; WAV/AIF get transcoded to FLAC first (since they can't carry tags cleanly) with the original marked `ready2air=False` for manual weeding
- 29,000+ track library with searchable/filterable frontend and full track detail editing
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

**Streaming Encoders** (`encoders/` app, `isadoraair-encoders` service)
- Liquidsoap-backed relay to Icecast and Shoutcast (v1/v2) mounts, one process per shared ALSA capture device fanning out to every enabled stream (mp3/aac/vorbis) on that device
- Live now-playing metadata pushed to each stream from the playback engine
- Self-reported silence detection per feed (no second ALSA reader on the same device) surfaced to Monitoring

**RBDS/RDS Encoder Client** (`rbds/` app, `isadoraair-rbds` service)
- Sends station PS (with optional scrolling multi-frame rotation) and RadioText (RT/RT+) to StereoTool's RDS encoder, in either binary UECP or StereoTool's ASCII dialect
- Promo rotation with priority-interrupt scheduling that returns to now-playing once shown; each message can source its text from a static field, a local file, or a URL
- Read-only status dashboard; all configuration is admin-only

**System Monitoring** (`monitoring/` app, `isadoraair-monitoring` service)
- systemd/disk/CPU/memory/temperature/transmitter/audio-silence health checks, admin-configurable thresholds
- Email (and SMS-via-carrier-gateway) alerting with per-check debounce and cooldown
- Transmitter integration (Aquabroadcast COBALT) for forward/reverse power, VSWR, PA temperature, fan speed, and RF interlock

**Admin & Configuration**
- Django admin, organized into Library / Traffic / Config / Logs sections so unrelated models don't all pile into one bucket
- Analysis Configuration (cue-in/next-start dBFS thresholds), Recency Configuration, Log Fill Configuration, Remote DJ Configuration (STUN server, ICE UDP port range, gain)
- UI Theme: site-wide color palette and nav clock styling, editable with a native color+opacity picker, no page reload required
- Admin-editable navigation menu (Config → Nav Menu): labels, target URLs (Django URL name or arbitrary URL), one level of dropdown children, drag-sort, per-item active-highlighting hints — no template edits needed to reshape the top nav
- EmailLog (Logs section): every outgoing email sent through Django's mail API — password resets, admin invites, monitoring alerts, anything — leaves a read-only row in the admin. Bodies truncated at 10k chars with a visible marker; auto-pruned after 90 days by a systemd timer (`isadoraair-prune-emaillog.timer`)
- Password-reset flow with "Forgot password?" on the login page + an admin-side invite button for creating no-password accounts and mailing them a setup link
- django-axes login lockout on repeated failed sign-ins

**Content Ingestion & Integrations**
- Syndicated show ingestion: 20+ shows automatically fetched, tagged, artwork-attached where available, and delivered into their rotation categories on each source's real broadcast schedule (KIN news, BirdNote Daily, Academic Minute, Big Picture Science, Acoustic Cafe, Democracy Now, Anjunachill, Grateful Dead Hour, etc.). Each show is a `syndicated-<slug>.timer`/`.service` pair matching the source-box crontab that was the original authoritative schedule
- Weather integration: NWS-sourced current temperature, one-day and three-day forecasts feeding RadioText messages via the RBDS client, plus alert beeps for active watches/warnings played straight to a dedicated ALSA loopback into StereoTool — bypasses the playback engine so alerts still fire during a manual override or engine restart
- Bluesky auto-poster: now-playing metadata pushed to a Bluesky account every 2 minutes, with de-duplication so an unchanged track doesn't re-post
- Remote content polling (`ogremote`): pulls fresh content from a remote source and stages it into the library, with a separate urgent-replay path for time-sensitive drops

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

```bash
# Clone
git clone git@github.com:celltech161/isadoraair.git /opt/isadoraair
cd /opt/isadoraair

# System packages the playback engine needs (GStreamer + PyGObject bindings,
# ALSA output, plus a decoder set covering the library's actual formats)
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-alsa \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav

# Liquidsoap (streaming encoders) and ALSA utils (mixer control enumeration
# for the hardware admin, device listing)
sudo apt install liquidsoap alsa-utils

# CD ripping toolchain -- whipper drives cdparanoia + flac; libdiscid is
# the disc-ID library the Python `discid` package binds to. Skip these
# if the box has no optical drive.
sudo apt install whipper cdparanoia flac

# Virtual environment (--system-site-packages so PyGObject/gi, which is an
# OS package above, is visible inside the venv)
python3 -m venv venv --system-site-packages
source venv/bin/activate
pip install -r requirements.txt

# Environment
cp .env.example .env  # Edit with your database credentials

# Database
python manage.py migrate

# Create admin user
python manage.py createsuperuser

# Import music library
python manage.py import_songs /path/to/music

# Analyze tracks (waveforms, cue points)
python manage.py analyze_tracks

# Run the web app
python manage.py runserver

# Run the playback engine (separate process — this is what actually
# outputs audio; runserver alone won't play anything)
python manage.py run_engine

# Optional separate processes — streaming, RDS, and health monitoring
# each run independently and aren't required for basic playback
python manage.py run_encoders
python manage.py run_rbds
python manage.py run_monitoring
```

See `deploy/` for nginx and systemd service configs for production — each
long-running process above (`isadoraair-gunicorn`, `isadoraair-engine`,
`isadoraair-encoders`, `isadoraair-rbds`, `isadoraair-monitoring`) runs as
its own systemd unit; timer-driven jobs (backup, EmailLog prune,
syndicated ingestions, weather, Bluesky poster) each ship as a
`.service`/`.timer` pair. `isadoraair-backup.timer` runs a nightly
database dump + app tree + live-config backup, pushed off-box via SFTP —
the script itself (`backup_isadoraair.sh`) lives outside the repo at
`~/bin/`, alongside its `~/.iasboxbu.cred` remote-target credentials.
Syndicated ingestion and the Bluesky poster scripts also live outside
the repo at `~/syndicated-ingest/` (own venv, separate from this
project's), with credentials in `~/.syndicated_ingest.cred`.

**Note on the `deploy/` unit files:** every unit ships with the paths of
the live KOGR-LP install (`/home/jreed/isadoraair-django/venv/…`,
`/home/jreed/syndicated-ingest/…`, etc.) hardcoded — they're the exact
files running on the real box, published as reference rather than as a
paramaterized template. If you deploy under a different path (or a
different user), search-and-replace those literals before enabling any
unit. `/opt/isadoraair` on the live box is a symlink to
`/home/jreed/isadoraair-django`, which is why the `WorkingDirectory=`
lines look inconsistent with the `ExecStart=` paths — same target, two
names.

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
| 8. Content ingestion | Complete — 20+ syndicated shows on real schedules, weather data + alerts, Bluesky auto-poster |
| 9. Remote DJ over WebRTC | Complete — browser-based remote console, mix-minus monitor return, gated remote mic, full queue authority for the connected DJ |
| 10. Email + admin infrastructure | Complete — EmailLog transport-layer capture, invite/reset flows, admin-editable nav menu, django-axes lockout |

Actively running end-to-end on a live station: schedule → log builder → playback engine → StereoTool → transmitter, plus live streaming, RDS, monitoring, remote DJ, and content ingestion.

## Security

For security concerns, see [SECURITY.md](SECURITY.md). Please report privately rather than opening a public issue.

## License

Licensed under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE) for the full text.

The AGPL is a strong copyleft. In short: you can use, modify, and redistribute this software (including for commercial purposes), but any modified version you run as a network-accessible service must have its source publicly available to the users of that service. That fits the community-broadcast ethos of this project — a broadcast automation stack that stays open even when it's deployed as a station's operational tool.
