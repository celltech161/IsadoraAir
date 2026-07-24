# deploy/

systemd units, nginx site config, and misc drop-in configs for a
production IsadoraAir install. Every path or user that varies per
install is a `@@PLACEHOLDER@@` token — see below to render + install.

## Placeholders

| Token | What to set it to | Example |
|---|---|---|
| `@@ISA_USER@@` | Linux user + group that owns the install and runs every service | `isadoraair`, or your existing `deploy` user |
| `@@ISA_ROOT@@` | Repo checkout directory (contains `manage.py`, `venv/`, `.env`) | `/opt/isadoraair` |
| `@@ISA_HOME@@` | Home directory of `@@ISA_USER@@` — only used to locate the backup script | `/home/isadoraair` |
| `@@SYNDICATED_ROOT@@` | Root of the separate syndicated-ingest scripts + venv | `/home/isadoraair/syndicated-ingest` |
| `@@WEATHER_ROOT@@` | Root of the separate weather-ingest scripts + venv | `/home/isadoraair/weather-ingest` |
| `@@OGREMOTE_ROOT@@` | Root of the separate ogremote-ingest scripts + venv | `/home/isadoraair/ogremote-ingest` |

Only `ISA_USER` and `ISA_ROOT` are strictly required — if you don't
run syndicated shows, weather ingestion, ogremote polling, or the
nightly backup, you can skip installing those specific units and
never render the tokens they use.

## Install

Set the six variables, then render + install every unit file that
matches your setup. The example below installs everything; comment
out the individual `sudo tee` invocations for any subsystem you're
not using.

```bash
# 1. Set the values for your box
export ISA_USER=isadoraair
export ISA_ROOT=/opt/isadoraair
export ISA_HOME=/home/$ISA_USER
export SYNDICATED_ROOT=$ISA_HOME/syndicated-ingest
export WEATHER_ROOT=$ISA_HOME/weather-ingest
export OGREMOTE_ROOT=$ISA_HOME/ogremote-ingest

# 2. Render + install every deploy/*.service, *.timer, and *.conf
for f in deploy/*.service deploy/*.timer deploy/*.conf; do
  [ -f "$f" ] || continue
  sed \
    -e "s|@@ISA_USER@@|$ISA_USER|g" \
    -e "s|@@ISA_ROOT@@|$ISA_ROOT|g" \
    -e "s|@@ISA_HOME@@|$ISA_HOME|g" \
    -e "s|@@SYNDICATED_ROOT@@|$SYNDICATED_ROOT|g" \
    -e "s|@@WEATHER_ROOT@@|$WEATHER_ROOT|g" \
    -e "s|@@OGREMOTE_ROOT@@|$OGREMOTE_ROOT|g" \
    "$f" | sudo tee "/etc/systemd/system/$(basename "$f")" > /dev/null
done

# 3. nginx site (adjust server_name inside first if needed)
sed \
  -e "s|@@ISA_ROOT@@|$ISA_ROOT|g" \
  deploy/isadoraair.nginx | sudo tee /etc/nginx/sites-available/isadoraair > /dev/null
sudo ln -sf /etc/nginx/sites-available/isadoraair /etc/nginx/sites-enabled/isadoraair

# 4. Reload + enable the units you want running
sudo systemctl daemon-reload
sudo systemctl enable --now isadoraair-gunicorn isadoraair-engine \
  isadoraair-encoders isadoraair-monitoring isadoraair-rbds
sudo systemctl enable --now isadoraair-analyze.timer \
  isadoraair-prune-emaillog.timer isadoraair-prune-systemevents.timer
# Optional: sudo systemctl enable --now isadoraair-backup.timer
# Optional: sudo systemctl enable --now 'syndicated-*.timer' 'wx-*.timer'
sudo systemctl reload nginx
```

## What each unit does

Long-running services (one process each, restarted by systemd):

| Unit | Purpose |
|---|---|
| `isadoraair-gunicorn.service` | Web/API — Django app behind nginx |
| `isadoraair-engine.service` | Playback engine (GStreamer) + Remote-DJ WebRTC signaling |
| `isadoraair-encoders.service` | Streaming encoders (Liquidsoap → Icecast/Shoutcast) |
| `isadoraair-rbds.service` | RBDS/RDS client to StereoTool |
| `isadoraair-monitoring.service` | System / transmitter / audio health checks + alerting |

Timer-driven jobs (fire on a schedule, exit):

| Unit | Runs |
|---|---|
| `isadoraair-analyze.timer` | Every minute — waveforms + cue points for new tracks |
| `isadoraair-sample-icecast.timer` | Every minute — samples streaming-server listener counts for royalty-report ATH baseline (Icecast + Shoutcast 2). No-op if no enabled outbound encoders. |
| `isadoraair-backup.timer` | Nightly full backup (03:30) — needs the `backup_isadoraair.sh` script and remote-target creds outside the repo |
| `isadoraair-prune-emaillog.timer` | Daily (04:15) EmailLog retention prune, 90-day default |
| `isadoraair-prune-systemevents.timer` | Daily (04:24) SystemEvent retention prune |
| `isadoraair-mitd-prep.timer` | Weekly (Mon 10:15) MITD show file staging |
| `isadoraair-engine-boot-restart.timer` | Once, ~50s after boot — force-restarts the engine if silence is detected on first startup |
| `syndicated-*.timer` (20+) | Per-show ingestion on each source's real broadcast schedule — needs the separate syndicated-ingest scripts + venv at `@@SYNDICATED_ROOT@@` |
| `wx-*.timer` | Weather data + alert-beep checks — needs `@@WEATHER_ROOT@@` |
| `ogremote-poll.timer` / `ogremote-urgent-replay.timer` | Remote content polling + urgent-replay dispatch — needs `@@OGREMOTE_ROOT@@` |
| `syndicated-bsky-post.timer` | Every 2 minutes: now-playing → Bluesky |

Drop-in configs (installed as-is, no per-install variation apart from the ones already tokenized):

| File | Where it goes |
|---|---|
| `isadoraair.nginx` | `/etc/nginx/sites-available/isadoraair` |
| `isadoraair-tmpfiles.conf` | `/etc/tmpfiles.d/isadoraair.conf` — creates `/run/isadoraair` on boot with the right owner |
| `needrestart-isadoraair.conf` | `/etc/needrestart/conf.d/isadoraair.conf` — auto-restarts services on library upgrades so a matplotlib/psycopg2 refresh doesn't leave the engine on the old shared object |
| `asound.conf` | `/etc/asound.conf` — ALSA loopback / dsnoop config for the studio + streaming feeds |

## Subsystems outside this repo

`syndicated-ingest/`, `weather-ingest/`, and `ogremote-ingest/` are
separate projects (own venvs, own creds). The units in `deploy/`
reference them but do not include their source — this repo is
IsadoraAir itself; those three ingest paths are companion projects
that produce audio + metadata IsadoraAir then plays.

The backup script (`@@ISA_HOME@@/bin/backup_isadoraair.sh`) and its
credential file (`@@ISA_HOME@@/.iasboxbu.cred`) also live outside
the repo — the `.timer` here just fires the script, you supply
your own.

If you're not running any of the above, skip enabling the
corresponding units. The core five services + the three prune/
analyze timers are all you need for a working single-box install.
