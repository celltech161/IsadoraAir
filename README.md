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

**Live Dashboard**
- Dual-deck view (stacks on mobile) with waveform, live position, and transport controls for whichever deck is playing; the idle deck previews the next queued track
- "Coming Up" queue table for the full remaining hour — drag-to-reorder, insert a track by search, color-coded by Category Kind
- Manual "play a playlist now" override and a "Restart Engine" recovery button

**Admin & Configuration**
- Django admin, organized into Library / Traffic / Config sections so unrelated models don't all pile into one bucket
- Analysis Configuration (cue-in/next-start dBFS thresholds), Recency Configuration, Log Fill Configuration
- UI Theme: site-wide color palette and nav clock styling, editable with a native color+opacity picker, no page reload required

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
├── hardware/                     # Audio device config (AudioOutput/AudioInput, incl. AGC settings)
├── templates/
│   └── base.html                 # Dark-themed base template, mobile nav, live clock
├── deploy/
│   ├── isadoraair.nginx
│   ├── isadoraair-gunicorn.service
│   └── isadoraair-engine.service # Playback engine systemd unit
└── legacy/                       # Original FastAPI prototype (reference only)
```

## Stack

- **Backend:** Django 5.2 LTS, PostgreSQL, Gunicorn
- **Playback:** GStreamer 1.0 (PyGObject) — standalone engine process, IPC with Django via JSON files
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

# Virtual environment (--system-site-packages so PyGObject/gi, which is an
# OS package above, is visible inside the venv)
python3 -m venv venv --system-site-packages
source venv/bin/activate
pip install django psycopg2-binary python-decouple mutagen gunicorn django-admin-sortable2

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
```

See `deploy/` for nginx and systemd service configs for production —
`isadoraair-gunicorn.service` (web) and `isadoraair-engine.service`
(playback) run as separate systemd units.

## Project Status

| Phase | Status |
|-------|--------|
| 1. Django models | Complete |
| 2. Library import & analysis | Complete |
| 3. Log builder | Complete |
| 4. Playback engine | Complete — dual-deck GStreamer mixer, real crossfading, broadcast-clock hour handling, studio-monitor AGC |
| 5. Live dashboard | Complete — dual-deck view, full-hour queue, manual overrides |

Actively running end-to-end on a test box: schedule → log builder → playback engine → studio monitor output.

## License

Private project. Not open source.
