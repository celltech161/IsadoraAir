# IsadoraAir

Django-based radio automation system built for [Oak Grove Radio](https://oakgroveradio.com) (KOGR-LP, Minneapolis KS).

IsadoraAir manages the full music library, schedule programming, playlist generation, and live on-air playback for a broadcast radio station — from importing and analyzing a track to actually mixing it out through the studio monitor. It replaces a previous FastAPI/mpv prototype with a proper Django application backed by PostgreSQL, plus a standalone GStreamer playback engine.

## Features

**Library Management**
- Import audio files from disk with automatic tag reading (ID3, Vorbis, MP4/AAC) via mutagen
- Audio analysis: waveform generation, auto-detection of cue-in and next-start (mix) points via ffmpeg
- 29,000+ track library with searchable/filterable frontend and full track detail editing
- Bulk actions: mark ready-to-air, assign categories, set metadata
- Per-track cue points, rotation weight, energy level, vocal type, end type, RBDS overrides
- Category Kind (Music/Imaging/Spot/Talk, extensible) with an admin-manageable fill color per kind, shown on the live dashboard's queue

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
- Dashboard "Mic" button (click-to-toggle) gates a live studio mic input into the on-air mix via a two-mixer pipeline: the decks' summed output feeds a duck gain stage before joining the mic in a shared master mixer, so a track transition mid-talkover never causes a duck discontinuity
- Optional ducking (admin enable/disable + level in dB) smoothly ramps program audio down/up over ~500ms on PTT toggle — no clicks from an instantaneous level change
- Mic hardware controls (gain, preamp, etc.) are dynamically enumerated from the configured device's real ALSA mixer controls in admin — no hardcoded interface-specific fields, so it adapts to whatever's actually plugged in

**Live Dashboard**
- Dual-deck view (stacks on mobile) with waveform, live position, and transport controls for whichever deck is playing; the idle deck previews the next queued track
- "Coming Up" queue table for the full remaining hour — drag-to-reorder, insert a track by search, color-coded by Category Kind
- Manual "play a playlist now" override, a "Restart Engine" recovery button, and the mic PTT toggle

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
- Django admin, organized into Library / Traffic / Config sections so unrelated models don't all pile into one bucket
- Analysis Configuration (cue-in/next-start dBFS thresholds), Recency Configuration, Log Fill Configuration
- UI Theme: site-wide color palette and nav clock styling, editable with a native color+opacity picker, no page reload required
- django-axes login lockout on repeated failed sign-ins

## Architecture

```
IsadoraAir (Django 5.2 LTS)
├── library/                     # Main app
│   ├── models.py                # Track, Artist, Album, Category, CategoryKind, Rotation,
│   │                            # Playlist, ScheduleBlock, PlaylistLog, UITheme, etc.
│   ├── views.py                 # Page views + JSON API endpoints
│   ├── admin.py                 # Admin registration + Library/Traffic/Config sectioning
│   ├── context_processors.py    # Injects UITheme into every template
│   ├── services/
│   │   ├── log_builder.py       # Playlist generation algorithm
│   │   └── engine.py            # GStreamer playback engine (standalone process)
│   ├── management/commands/
│   │   ├── import_songs.py      # Library scanner (mutagen)
│   │   ├── analyze_tracks.py    # Audio analysis (ffmpeg + DSP)
│   │   ├── run_engine.py        # Entry point for the playback engine service
│   │   └── fix_unknown_artists.py
│   └── templates/library/       # dashboard, schedule, playlists, library, logs, track detail
├── hardware/                     # Audio device config: AudioOutput/AudioInput (incl. AGC,
│                                  # mic gain, dynamically-enumerated ALSA mixer controls),
│                                  # AudioPipeline (sample rate), DuckingConfig
├── encoders/                     # Icecast/Shoutcast streaming (Liquidsoap), own service
├── monitoring/                   # System/service/transmitter/audio health checks, own service
├── rbds/                         # RBDS/RDS client for StereoTool (UECP + ASCII), own service
├── templates/
│   └── base.html                 # Dark-themed base template, mobile nav, live clock
├── deploy/
│   ├── isadoraair.nginx
│   ├── isadoraair-gunicorn.service
│   ├── isadoraair-engine.service      # Playback engine systemd unit
│   ├── isadoraair-encoders.service    # Streaming encoders systemd unit
│   ├── isadoraair-monitoring.service  # Monitoring poller systemd unit
│   ├── isadoraair-rbds.service        # RBDS/RDS client systemd unit
│   ├── isadoraair-backup.service      # Nightly backup, oneshot
│   ├── isadoraair-backup.timer        # Triggers the above at 03:30 daily
│   ├── syndicated-kin.service         # KIN news ingestion, oneshot
│   ├── syndicated-kin.timer           # Hourly :57, 05:00-17:00
│   ├── syndicated-bsky-post.service   # Now-playing -> Bluesky, oneshot
│   └── syndicated-bsky-post.timer     # Every 2 minutes
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
process above (`isadoraair-gunicorn`, `isadoraair-engine`,
`isadoraair-encoders`, `isadoraair-rbds`, `isadoraair-monitoring`) runs as
its own systemd unit. `isadoraair-backup.timer` runs a nightly database
dump + app tree + live-config backup, pushed off-box via SFTP — the
script itself (`backup_isadoraair.sh`) lives outside the repo at
`~/bin/`, alongside its `~/.iasboxbu.cred` remote-target credentials.
`syndicated-kin.timer`/`syndicated-bsky-post.timer` run syndicated-show
ingestion and Bluesky now-playing posts — those scripts live outside the
repo at `~/syndicated-ingest/` (own venv, separate from this project's),
with credentials in `~/.syndicated_ingest.cred`.

## Project Status

| Phase | Status |
|-------|--------|
| 1. Django models | Complete |
| 2. Library import & analysis | Complete |
| 3. Log builder | Complete |
| 4. Playback engine | Complete — dual-deck GStreamer mixer, real crossfading, broadcast-clock hour handling, studio-monitor AGC |
| 5. Live dashboard | Complete — dual-deck view, full-hour queue, manual overrides |
| 6. Streaming, RDS & monitoring | Complete — Icecast/Shoutcast relay, StereoTool RBDS client, system/transmitter health checks with alerting |
| 7. Studio mic + ducking | Complete — dashboard PTT, dynamically-enumerated hardware mixer controls, graceful ducking of program audio |

Actively running end-to-end on a test box: schedule → log builder → playback engine → studio monitor output, plus live streaming, RDS, and monitoring.

## License

Private project. Not open source.
