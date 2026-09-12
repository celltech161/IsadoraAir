# IsadoraAir

**Open-source, browser-operated radio automation for terrestrial, community/NCE, and internet stations.**

IsadoraAir combines music-library management, schedule programming and automatic log generation, dual-deck playout with real crossfading, live assist, studio mic + PTT, WebRTC Remote DJ, browser voice tracking, FX carts, Aircheck recording, Icecast/Shoutcast streaming, RBDS/RDS, system/transmitter monitoring, royalty reporting, web song requests, and weather/road-condition integrations — all on one Linux host, run from a web browser.

It was built at [Oak Grove Radio](https://oakgroveradio.com) 98.5 (KOGR-LP, Minneapolis, KS) to replace a Windows-based NextKast OnAir + MagicRDS workflow. The design target is the space between lightweight desktop automation and a large traditional broadcast-automation plant: enough station-wide automation to run a real LPFM/community facility day to day, while keeping normal operation available from any browser and the source fully open.

## Screenshots

**On-air console** — twin waveform decks with album art, VU meter, listener statistics, mic controls, FX Carts, and the coming-up queue.

![Dashboard](docs/screenshots/dashboard.png)

**Library** — searchable/filterable/sortable, with bulk actions and an import + CD-rip page under the same roof. The KOGR-LP production install pictured here holds ~30k tracks; Postgres + indexed queries scale comfortably well beyond that.

![Library](docs/screenshots/library.png)

## Who it's for

IsadoraAir is aimed at LPFM, NCE/community, small commercial, and internet-radio operations that want real station-wide automation — scheduling, playout, streaming, RDS, monitoring, reporting — without a full traditional broadcast-automation plant or a per-workstation licensing model. If your station is currently running lightweight desktop software on one machine, or evaluating a much larger commercial system than you actually need, this is the gap it targets.

It is **not** aimed at large-market/multi-studio traditional broadcast groups needing deep hardware-plant integration, or at stations whose primary requirement is a full commercial traffic/billing system — see [Where IsadoraAir fits](#where-isadoraair-fits) below.

## Where IsadoraAir fits

A factual comparison against systems stations evaluating IsadoraAir are likely also looking at. This is positioning, not a "better than" claim — each of these is a real, actively maintained system with its own strengths.

| System | Model / emphasis | Relative to IsadoraAir |
|---|---|---|
| **IsadoraAir** | Open-source Linux, browser-first station automation for terrestrial/community + streaming. | Strong integrated Remote DJ, RDS, monitoring, royalty reporting, requests, and local-information (weather/road) workflows. Commercial traffic, generalized live-source/control workflows, and some advanced programming functions remain roadmap work. |
| **[Rivendell](https://www.rivendellaudio.org/)** | Open-source Linux, mature traditional broadcast automation — acquisition, scheduling, playout, full voice tracking, log customization, broad third-party plant integration. | More established in large/traditional broadcast-plant workflows and hardware integration history; IsadoraAir emphasizes web operation and a smaller/community-station operating model. |
| **[NextKast OnAir](https://www.nextkast.com/onAir/)** | Commercial all-in-one — music scheduling, remote StudioLink/GoLive, streaming/RDS, traffic/billing, advanced scheduling, triggers, sports/game scheduling, stream/on-air substitution, synchronization, AI-host tooling. | IsadoraAir trades that breadth (traffic, generalized triggers, sports workflows, synchronization, AI-host features) for an open Linux/web stack with no proprietary seat or module licensing. |
| **[PlayIt Live](https://www.playitsoftware.com/Products/Live)** | Windows automation with a capable free core plus premium modules — clocks, adverts, scheduled events, priority streams, remote studio/voice tracking, remote management, external-device integration, control API. | Different deployment/operating model rather than a strict superset in either direction; IsadoraAir currently has several of PlayIt's traditional-operations features (adverts/traffic, generalized external control) on its roadmap rather than shipped. |
| **[SAM Broadcaster](https://spacial.com/sam-broadcaster-pro/)** | Windows, principally Internet-radio focused — dual decks, crossfading/gap-killing, automation, voice tracking, encoders, statistics, audio processing. | IsadoraAir is substantially more oriented toward a complete terrestrial/community station workflow (scheduling, RDS, transmitter monitoring, regulatory reporting) rather than stream automation specifically. |

## Features

**Library Management**
- Import from disk or drag-and-drop upload, with automatic tag reading (ID3, Vorbis, MP4/AAC, WAV RIFF LIST-INFO) and CD ripping built in (see below)
- Waveform generation and automatic cue-in / next-start (mix) point detection, with per-category threshold overrides and a fast re-pick path when a threshold changes
- Related-artist rotation separation: a shared parser auto-discovers feat./with/collaboration credits so a solo cut and a duet by the same performer don't air back-to-back
- Searchable/filterable library UI with bulk actions (ready-to-air, categorize, holiday tags, delete) and per-track cue points, rotation weight, energy level, and RBDS overrides
- Album-art resolver with layered fallbacks (manual override, embedded art, station-hosted image, online lookup) and a station default

**CD Ripping** (built into Library Import)
- Insert a disc, click "Detect CD" — MusicBrainz supplies album/track metadata into an editable form before ripping; unmatched discs fall back to a blank editable table
- AccurateRip verification per track, with a configurable strictness gate for older/rare pressings that don't match the community database
- Drive offset and staging configuration are admin-editable — swapping drives doesn't need a redeploy

**Schedule Programming**
- A 7×24 schedule grid assigns either a weighted Rotation or a fixed Playlist to each hour, recurring by day-of-week or one-off for a specific date
- Rotations are ordered category slots; Playlists are hand-built ordered track lists
- Holiday-themed rotation weighting with configurable ramp-in/ramp-out

**Log Builder**
- Generates each hour automatically from its schedule, with configurable recency/artist separation (including related-artist identity) and no manual approval gate
- Lands each hour's end precisely against the real next-hour boundary, with a fallback category to top up a short hour and bounded live backfill for genuine gaps
- A schedule hour can be left intentionally blank to let the current program continue instead of forcing an artificial break

**Playback Engine**
- Dual-deck playout with real crossfading timed off each track's own analyzed cue-in/next-start points — not a scripted fade curve
- Broadcast-clock hour handling keeps playback locked to wall-clock top-of-hour
- Pause, resume, eject, and reliable live seek per deck, fully controllable from the dashboard waveform
- Automatic recovery from USB audio-device loss/re-enumeration, with failure containment that isolates a bad playback attempt rather than taking down the whole station

**Live Studio Mic + PTT + Ducking**
- Dashboard push-to-talk gates the studio mic into the on-air mix, with optional smooth program-audio ducking (no clicks) on toggle
- Mixer hardware controls are auto-enumerated from whatever's actually plugged in — no interface-specific configuration
- Automatic recovery when a supported mic interface is unplugged and reconnected

**Remote DJ over WebRTC**
- A browser-based remote console lets a DJ connect from any phone or laptop, hear program audio via mix-minus monitor return, and talk over via a gated remote mic
- Full queue authority for the connected DJ — search, play now, reorder, force-next — so they can run a complete show remotely
- Login-gated to a dedicated group with short-lived signaling tokens; the same ducking configuration applies to whichever mic (studio or remote) is live

**Live Dashboard**
- Dual-deck view with click-to-seek waveform, live position, and transport controls; the idle deck previews what's coming up next
- Full-hour "Coming Up" queue with drag-to-reorder, insert-by-search, and per-row force-next
- Manual playlist override, one-click engine restart, and live studio/remote mic PTT with a real-time VU meter on the button itself
- Live listener count, peak, and accumulated Total Listening Hours

**FX Carts / Hotkeys**
- A grid of one-shot audio buttons (drops, stingers, jingles, sound effects) on the main dashboard and Remote DJ console, with keyboard shortcuts and per-cart gain/retrigger/color configuration
- Low-latency, polyphonic, and isolated from ordinary deck playout — a fire (or a spam of them) can't destabilize on-air audio
- Fires are synchronized live across every open console, including unattended triggers like the weather-alert bridge

**Voice Tracking**
- Intro and outro voice tracks are browser-recorded and edited (waveform trim, undo, peak normalize — no server-side transcode needed) and sequenced against each track's own cue markers
- Independent ducking from the mic path, with safe restart behavior if the engine restarts mid-sequence
- Gated on cue markers being set, so a voice track can never land inside a track's vocal window

**Aircheck Recording**
- On-demand start/stop from any dashboard, capturing the actual on-air signal — not a re-decode of the library file
- An always-ready backend means no per-session subprocess and no contention with the streaming encoders
- Finalized recordings are compactly archived (HE-AAC) and can be auto-indexed into the library for operator review

**Streaming Encoders**
- Icecast and Shoutcast (v1/v2) relay, including Live365 and Radio.co provider presets, with multiple simultaneous streams grouped by input device
- Configuration is validated and health-qualified before a changed encoder replaces a running one, and automatically rolls back to the last known good configuration if the change doesn't come up healthy
- Live now-playing metadata pushed to every stream; self-reported silence detection surfaced to Monitoring

**RBDS/RDS**
- Program Service (PS) and RadioText (RT/RT+) to an RDS encoder via UECP or StereoTool's ASCII dialect, with Static, Manual, or Generated (rotating) PS modes
- Separately managed UECP Long PS, and per-category PTY/PTYN override so one rotation category can broadcast a different program type while its tracks are airing
- Promo rotation with priority-interrupt scheduling; Monitoring shows the current resolved PS/RT/PTY and connection state

**System Monitoring**
- Service, system (disk/CPU/memory/temperature), audio-silence, and transmitter health checks with configurable, debounced email/SMS alerting
- Runtime/version-skew visibility — distinguishes a service actually running the checked-out code from one that's fallen behind, without conflating deployment state with health
- Backup-recovery assurance: verifies the most recent backup's own database catalog is readable, its provenance, and successful remote promotion
- Independent self-health for the Monitoring service itself — a stale or wedged poller can't keep presenting an old "all clear" card, and systemd automatically recovers a Monitoring process that stops responding even if no one is watching the dashboard

**Reports** (`/reports/`)
- SoundExchange NCE Report of Use generator, plus a plain-summary format and a raw-CSV audit dump, built from an append-only, immutable `PlayEvent` ledger with a configurable retention window
- ISRC auto-populates from file tags at import, with a MusicBrainz backfill pass for what's missing
- Aggregate Tuning Hours computed from station-owned stream listener samples (not inflated by unrelated streams on a shared server), with manual-override reconciliation
- A Listener Stats chart with adjustable time buckets, and a Hidden Track Detection scan that flags likely CD-rip-style hidden tracks for manual review

**Admin & Configuration**
- Django admin organized into Library / Traffic / Config / Logs, with an editable nav menu and site-wide theme (colors, clock style, default album art) — no template edits
- Group-based access control: non-staff users see only the pages their groups are granted, admin-editable with no code change
- Selected operational settings (SMTP, library paths, MusicBrainz contact, weather/report directories) are admin-editable with explicit saved-vs-running state, rather than requiring `.env` edits
- Password-reset and admin-invite account flows, an audit log of every outgoing email, and login-lockout on repeated failed sign-ins

**Managed Update Center**
- Staff/superusers can inspect release state and available updates; installation is superuser-only
- Declarative release manifests validate checkout, schema, and prerequisites before an install is offered; a separately installed, protected backend independently re-authorizes and applies only release-declared safe actions
- Dirty, divergent, or otherwise untrustworthy source/release state fails closed rather than guessing

**Text-to-Speech**
- One engine-neutral logical-voice interface shared by weather, road-condition, and dedication speech, backed by Kokoro and optional Piper voice providers
- An unconfigured voice fails clearly rather than silently falling back to the wrong one

**Content Ingestion & Integrations**
- **Weather** (native): NWS-sourced current conditions and forecasts feed RadioText; active watches/warnings can fire a configured FX Cart automatically
- **Road conditions** (native): KDOT/KanDrive CARS ingestion generates consolidated spoken reports with configurable coverage filters and stale-feed retirement
- **Web song requests** (native, `webrequests` app): catalog sync, request lifecycle, and in-place recency-aware queue fulfillment are native features; only the public request *form* lives on a separate listener-facing website — see [`docs/WEB_REQUESTS_INTEGRATION.md`](docs/WEB_REQUESTS_INTEGRATION.md). Optional spoken dedication intros pair with the requested track
- **Syndicated program ingestion** (companion/external today): provider-specific fetchers that carry outside credentials or scraping logic stay outside the repo; a native, operator-managed recurring-ingestion system is roadmap work
- **ogremote** (receiving side only, native): the ogremote newsgathering/voiceover product itself is a separate upstream tool; IsadoraAir ships only the integration that receives its uploads and dispatches urgent-replay drops
- Now-playing pushed to Bluesky and TuneIn AIR automatically on every track change

## Architecture

```
IsadoraAir (Django 5.2 LTS)
├── isadoraair/                    # Project settings + shared operational config, logical TTS voices
├── library/                       # Library, scheduling, dashboard + playback app
│   ├── services/engine.py         # GStreamer playback engine (standalone process)
│   └── ...                        # Models, views, admin, CD ripping, log builder, related-artist logic
├── hardware/                      # Audio I/O, ducking, stable-device identity, Remote DJ audio input
├── aircheck/                      # Aircheck recording/session management
├── webrequests/                   # Native request sync, scheduling + dedication intros
├── ogremote/                      # Receiving-side ogremote integration
├── encoders/                      # Liquidsoap stream manager, provider presets + LKG rollback
├── monitoring/                    # Service/system/transmitter/audio health, release skew, self-health
├── rbds/                          # UECP/ASCII RDS client
├── updatecenter/                  # Release-chain planning + managed-update UI
├── weather/                       # Weather config, alert/FX-Cart bridge, RadioText feed
├── weather_ingest/                # Native NWS ingestion (in-repo since the monorepo migration)
├── road_conditions/                # KDOT/KanDrive CARS ingest + spoken report generation
├── deploy/                        # systemd units, nginx config, backup script, release manifests,
│                                  # protected updater backend, bare-machine restore tooling
└── docs/                          # Runtime baseline, disaster recovery, TTS/codec provenance
```

## Stack

- **Backend:** Django 5.2 LTS on Python 3.14, PostgreSQL 18, Gunicorn
- **Playback:** GStreamer 1.28.x (PyGObject) — standalone engine process, IPC with Django via JSON state/command files
- **Streaming:** Liquidsoap 2.4.x — standalone encoder manager relaying to Icecast/Shoutcast, including Live365/Radio.co provider presets
- **Hardware control:** ALSA (`amixer`/`arecord`/`aplay`) for device enumeration, stable identity resolution, and mixer control
- **Frontend:** Django templates, vanilla JavaScript (no framework)
- **Web Server:** nginx with HTTPS (self-signed cert for LAN; ordinary public TLS can be supplied by the operator)
- **Audio Analysis:** ffmpeg, mutagen
- **Supported runtime baseline:** Ubuntu 26.04 LTS; see [`docs/RUNTIME_BASELINE.md`](docs/RUNTIME_BASELINE.md) for the version/pinning policy

## Setup

Ubuntu 26.04 LTS is the supported and tested runtime baseline. Other
Debian/Ubuntu releases may work, but they are not part of the current
reproducibility/restore baseline. This walkthrough goes from a fresh box to
audible playback; [`deploy/packages-ubuntu-26.04.txt`](deploy/packages-ubuntu-26.04.txt)
is the authoritative package manifest for a full production/recovery install.

### 1. System packages

```bash
# Core runtime, build tools, database, web server, and Git
sudo apt install postgresql nginx git \
  python3 python3-venv python3-dev build-essential

# GStreamer playback + Remote DJ WebRTC. gstreamer1.0-nice is required
# by webrtcbin for ICE; gstreamer1.0-tools is used by deployment checks.
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools \
  gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-nice

# Streaming encoders, ALSA utilities, and audio analysis
sudo apt install liquidsoap alsa-utils ffmpeg

# CD ripping toolchain -- optional if the box has no optical drive
sudo apt install whipper cdparanoia flac libdiscid0
```

`postgresql-contrib` and `libpq-dev` are not direct IsadoraAir requirements
on the supported baseline: the project uses the `psycopg2-binary` wheel and
no PostgreSQL contrib extension. Likewise, `gstreamer1.0-plugins-ugly` is
not in the current element inventory.

For HE-AAC/HE-AACv2 Aircheck and encoder capability, use the pinned
`deploy/build_fdkaac.sh` recipe and validate it with
`deploy/check_he_aac.sh`; do not substitute an unverified codec package.
The authoritative package manifest also defines optional groups for the
HE-AAC build toolchain, CD ripping, Kokoro TTS, encrypted recovery
credentials, and companion-project Selenium jobs.

### 2. ALSA loopback module (only if using StereoTool or a similar external processor)

IsadoraAir's engine can feed a virtual ALSA loopback device that
StereoTool reads from -- the "canonical digital mix out to processor,
processed audio back in" path. If you're not running an external
processor, skip this step and send the engine straight to your real
sound card.

**Two files are required, not just `modprobe snd-aloop`** -- the
default single auto-numbered loopback instance is not enough; the
studio config (`deploy/asound.conf`) and StereoTool's own configured
device both reference specific card indices, which only exist if
`snd-aloop` is told to create three instances at those exact indices:

```bash
# 1. Load the module at boot at all
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf

# 2. Pin it to three instances at fixed indices (see
# deploy/isadoraair-aloop.conf's own header comment for exactly which
# card each one is for and why the indices must be pinned, not
# auto-assigned)
sudo cp deploy/isadoraair-aloop.conf /etc/modprobe.d/isadoraair-aloop.conf

sudo modprobe snd-aloop
# Verify: `cat /proc/asound/cards` should show three "Loopback" entries
# at indices 0, 3, 4 alongside your real hardware.
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
- `/srv/isadoraair/waveforms` — pre-analyzed waveform JSON/envelope data
  used by the deck display and fast cue-point re-pick path (generated by
  `analyze_tracks` in step 9).
- `/var/lib/isadoraair/weather` — cached forecasts + alerts polled by
  the weather-ingest timer.
- `/var/lib/isadoraair/reports` — generated royalty / SoundExchange
  filings (persisted from the `/reports/` web page; overridable via
  the `REPORTS_ROOT` env var).

`/run/isadoraair/` (used for the engine's live state JSON) is created
automatically by the tmpfiles config in `deploy/` — no manual step
needed if you install the systemd units.

### 5. Clone + Python environment

`/opt` itself is root-owned on a stock Ubuntu install, so — same as
`/srv/isadoraair`/`/var/lib/isadoraair` in step 4 — establish
`/opt/isadoraair` with the intended service account's ownership
*before* cloning into it, rather than cloning as root (which would
leave the whole tree, and everything the venv step below writes beneath
it, root-owned):

```bash
sudo mkdir -p /opt/isadoraair
# Replace 'youruser' with whichever account will run the services
sudo chown youruser:youruser /opt/isadoraair

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
- `MUSICBRAINZ_CONTACT` — a real contact email for MusicBrainz requests
  if you use CD metadata or MusicBrainz ISRC backfill; replace the
  placeholder value from `.env.example`.
- `LIBRARY_ROOT` — the path where the music library lives (probably
  `/srv/isadoraair/music` from step 4).

After bootstrap, selected operational keys remain stored in this same `.env`
file but can be edited safely from Django admin: SMTP transport settings,
`LIBRARY_ROOT`, `WAVEFORMS_DIR`, `MUSICBRAINZ_CONTACT`,
`WEATHER_DATA_DIR`, and `REPORTS_ROOT`. Admin shows Saved-vs-Running state
and indicates which long-running services need a restart.

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

Generates waveform JSON (including persisted envelope/display data) and
automatically detects cue-in and next-start/mix points. Voice-tracking
intro/outro markers are operator metadata and are not fabricated by this
analysis pass. The analyzer runs periodically if you install the systemd
timer; the initial pass on a large library can take hours.

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

### 12. Production: systemd units, nginx, and backup/recovery

See [`deploy/README.md`](deploy/README.md) for the production install
conventions and placeholder rendering. The long-running components
(`isadoraair-gunicorn`, `isadoraair-engine`, `isadoraair-encoders`,
`isadoraair-rbds`, `isadoraair-monitoring`) each run as their own systemd
unit; timer-driven jobs are separate `.service`/`.timer` pairs.

Managed release delivery has an additional protected-install boundary. See
[`docs/UPDATE_CENTER.md`](docs/UPDATE_CENTER.md) for protected updater
installation and release-management details; do not treat
`deploy/updater_runtime/` as another unit installed by the ordinary broad
`deploy/*.service` rendering loop.

Nightly backup is repo-managed (`deploy/backup_isadoraair.sh`,
`isadoraair-backup.service`): it captures PostgreSQL, the application/`.env`,
live configuration, and station content needed for recovery, then uploads
the completed archive off-box over SFTP with retention handling. Recurring
Monitoring backup assurance separately verifies the latest backup's own
PostgreSQL catalog is readable, its provenance, and successful remote
promotion; a weekly exact-object round-trip check confirms the last
successful backup is still intact on the remote. A full offline,
disposable whole-machine restore (00→95, under strict network isolation,
on a clean Ubuntu 26.04.1 target) has passed end to end — application,
database, and native TTS/HE-AAC/protected-updater reconstruction all
proven from a real backup archive — see
[`docs/DISASTER_RECOVERY_STATUS.md`](docs/DISASTER_RECOVERY_STATUS.md)
for the authoritative result and
[`docs/DISASTER_RECOVERY_RESTORE.md`](docs/DISASTER_RECOVERY_RESTORE.md)
for the operator procedure. The remaining closeout item is an operator-
friendly bootable physical recovery-media workflow, not proof that
bare-machine recovery works at all.

For a full production bring-up, also run the read-only baseline preflight:

```bash
python manage.py check_deploy_baseline
```

## Running tests

```bash
PYTHONUNBUFFERED=1 python manage.py test
```

Always set `PYTHONUNBUFFERED=1` (equivalently, `python -u manage.py test`)
when a run's output is being redirected to a file or piped rather than
watched live in a terminal. Without it, Python fully buffers `stdout`
once it isn't attached to a terminal, while `unittest`'s own progress
dots and final `Ran N tests` / `OK` summary go to `stderr`, which
flushes immediately. Application code throughout this project uses
plain `print()` for operational logging, exercised extensively by the
test suite — with stdout buffered, all of that output queues up and
gets dumped in one block only when the process exits, landing *after*
the already-flushed summary in the captured log. On a large run, piping
through `tail -N` can then miss the summary entirely, making a fully
healthy, passing run look like a silent hang or crash with no
traceback. `PYTHONUNBUFFERED=1` keeps everything in true chronological
order instead.

Run a subset the normal Django way, e.g. `python manage.py test
webrequests` or `python manage.py test webrequests.tests.test_dedication_intros`.

## Migrating from NextKast, Rivendell, or another automation system

Some things worth knowing if you're coming from an existing station
running something else. This isn't an import tool — it's a
lay-of-the-land so you know what maps to what.

**Your existing music library moves with you.** IsadoraAir doesn't care
what created the audio files. Point `import_songs` at the directory
tree you already have; the mutagen-based tag reader handles MP3,
FLAC, WAV, M4A, Ogg, MP2, AIFF, and ALAC. Category comes from the
top-level folder name, so a Rivendell `Group`-style organization
already carries over if your folders are grouped that way.

**Rotations and Playlists are separate concepts** — this parallels
Rivendell's Clocks vs Log approach. A `Rotation` is an ordered list of
category slots the log builder fills by weighted random pick,
respecting recency separation; a `Playlist` is a curated ordered list
of specific tracks copied verbatim. A `ScheduleBlock` maps either one
onto real time — recurring weekly, or one-off for a specific date.

**There's no separate workstation install per operator.** Everything
is web-based. Any operator with a browser and login credentials can do
anything they have permission for from anywhere, including a live DJ
shift via the WebRTC Remote DJ console.

**Commercial-style traffic (underwriting, affidavits, spot rotation)
is not part of the current implementation.** The existing "Traffic"
admin section is programming-side — Rotations, Playlists, and
ScheduleBlocks — rather than sponsor/campaign/affidavit management.

**No commercial license fee, no per-workstation seat.** IsadoraAir is
AGPLv3 — you can run it commercially, modify it, redistribute it. The
AGPL's network-service clause means if you host a modified version as
a public web service, that modified source has to be available to the
service's users. For an on-air station running an unmodified copy
internally, this changes nothing.

**Note on the `deploy/` unit files:** paths and the run-as user are
`@@PLACEHOLDER@@` tokens (`@@ISA_USER@@`, `@@ISA_ROOT@@`, etc). See
[`deploy/README.md`](deploy/README.md) for the full placeholder table
and a copy-pastable install snippet. `ISA_USER` and `ISA_ROOT` are the
only two that matter for a minimal install without the optional
syndicated-ingest/ogremote companion timers.

## Roadmap direction

This is a selected, public-facing view of where the product is headed
next — not an exhaustive engineering punchlist. Completed items are
removed rather than kept as checked-off entries.

### Near-term hardening

- Hands-off bootable recovery-media / operator closeout
- GStreamer 1.28.x upgrade and regression validation — current target 1.28.7
- Remote DJ connection/link-quality hardening
- Playback/accounting semantics — an authoritative definition of "played"
- Future-log readiness validation
- Aircheck `/run`/working-storage containment and retention guardrails

### Built features needing their next layer

- Scheduled Aircheck recording tied to `/schedule/` program blocks
- Granular talent roles, capabilities, and scheduled access
- Log-position voice tracking + remote talent job workflow
- Native managed recurring/syndicated ingestion
- Advanced music scheduling rules + category-health diagnostics
- Full-day log editor
- Web-request governance/scheduling policy
- Context-aware Audition/Preview workflow
- Overlay sweepers
- iPulse audience-response music weighting
- TimeFit / bounded timing optimization
- Dynamic hook/teaser generation

### Independent capability direction

- Multiple `/schedule/` profiles
- Portable configuration export/import for schedules, rotations, and playlists
- Interactive fresh-machine installer
- NCE-friendly underwriting / traffic / PSA scheduling + reconciliation
- General action/trigger automation framework
- Generalized LiveSource / satellite / network-program input
- External mixer integration + Studio↔Remote-DJ IFB/talkback
- Scheduled time-shift recording for external/live sources
- Sports/special-event override scheduling
- Library bulk operations + disk/database reconciliation
- Offline future-log/program renderer
- Terrestrial-vs-stream content substitution + external scheduler log import
- Audio-processing topology modes for StereoTool and non-StereoTool installs
- Dedicated STL/contribution transport outputs + a PAD/Now-Next event API
- Automated podcast/RSS publishing from Airchecks and program assets
- Two-node hot-standby playout architecture
- Community TV/Video Engine module

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
