# IsadoraAir

Django-based radio automation system built for [Oak Grove Radio](https://oakgroveradio.com) (KOGR-LP, Minneapolis KS).

IsadoraAir manages the full music library, schedule programming, and playlist generation for a broadcast radio station. It replaces a previous FastAPI/mpv prototype with a proper Django application backed by PostgreSQL.

## Features

**Library Management**
- Import audio files from disk with automatic tag reading (ID3, Vorbis, MP4/AAC) via mutagen
- Audio analysis: waveform generation, auto-detection of cue-in and next-start (mix) points via ffmpeg
- 29,000+ track library with searchable/filterable frontend and full track detail editing
- Bulk actions: mark ready-to-air, assign categories, set metadata
- Per-track cue points, rotation weight, energy level, vocal type, end type, RBDS overrides

**Schedule Programming**
- Clock/Rotation model: Clocks define hour templates, Rotations are weighted category pools
- Visual 7x24 schedule grid (responsive — day-view on mobile) for assigning Clocks to time blocks
- ScheduleBlock supports recurring (day-of-week) and one-off (specific date) patterns
- Holiday-themed rotation weighting with configurable ramp-in/ramp-out periods

**Log Builder**
- Generate playlists per hour from the schedule: ScheduleBlock → Clock → ClockSlot → Rotation → Category → Track
- Recency avoidance with configurable artist and title separation windows (global defaults + per-category overrides)
- Time-based or proportional-to-category-size recency modes
- Duration-aware track selection near end of hour to hit 60-minute targets
- Progressive constraint loosening when eligible tracks are exhausted
- Draft/approve workflow with rebuild and delete

**Admin & Configuration**
- Django admin for all models with inline editing, bulk actions, and search
- Analysis Configuration: adjustable dBFS thresholds for cue-in and next-start detection
- Recency Configuration: global artist/title separation defaults, per-category overrides

## Architecture

```
IsadoraAir (Django 5.2 LTS)
├── library/                  # Main app
│   ├── models.py             # 15 models (Track, Artist, Album, Category, Clock, Rotation, etc.)
│   ├── views.py              # Page views + JSON API endpoints
│   ├── admin.py              # Full admin registration with bulk actions
│   ├── services/
│   │   └── log_builder.py    # Core playlist generation algorithm
│   ├── management/commands/
│   │   ├── import_songs.py   # Library scanner (mutagen)
│   │   ├── analyze_tracks.py # Audio analysis (ffmpeg + DSP)
│   │   └── fix_unknown_artists.py
│   └── templates/library/    # Frontend pages
├── templates/
│   └── base.html             # Dark-themed base template with nav
├── deploy/
│   ├── isadoraair.nginx      # nginx reverse proxy config
│   └── isadoraair-gunicorn.service
└── legacy/                   # Original FastAPI prototype (reference only)
```

## Stack

- **Backend:** Django 5.2 LTS, PostgreSQL, Gunicorn
- **Frontend:** Django templates, vanilla JavaScript (no framework)
- **Web Server:** nginx with HTTPS (self-signed cert for LAN)
- **Audio Analysis:** ffmpeg, mutagen
- **OS:** Ubuntu 26.04 LTS

## Setup

```bash
# Clone
git clone git@github.com:celltech161/isadoraair.git /opt/isadoraair
cd /opt/isadoraair

# Virtual environment
python3 -m venv venv
source venv/bin/activate
pip install django psycopg2-binary python-decouple mutagen gunicorn

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

# Run
python manage.py runserver
```

See `deploy/` for nginx and systemd service configs for production.

## Project Status

| Phase | Status |
|-------|--------|
| 1. Django models | Complete |
| 2. Library import & analysis | Complete |
| 3. Log builder | Complete |
| 4. Playback engine | Not started |
| 5. Live dashboard | Not started |

## License

Private project. Not open source.
