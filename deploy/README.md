# deploy/

systemd units, nginx site config, and misc drop-in configs for a
production IsadoraAir install. Every path or user that varies per
install is a `@@PLACEHOLDER@@` token — see below to render + install.

## Placeholders

| Token | What to set it to | Example |
|---|---|---|
| `@@ISA_USER@@` | Linux user + group that owns the install and runs every service | `isadoraair`, or your existing `deploy` user |
| `@@ISA_ROOT@@` | Repo checkout directory (contains `manage.py`, `venv/`, `.env`) | `/opt/isadoraair` |
| `@@ISA_HOME@@` | Home directory of `@@ISA_USER@@` — only used in `isadoraair-backup.service`'s comment documenting where the backup credential file lives | `/home/isadoraair` |
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

# 3. nginx site (adjust server_name inside first if needed) + its shared
# location-block snippet -- isadoraair.nginx `include`s this, so both
# files need to land in nginx's config tree, not just the site file.
# sites-enabled MUST be a symlink to sites-available, never a second
# copy of the file -- see this file's own "One authoritative nginx
# config" note below for why that matters.
sudo mkdir -p /etc/nginx/snippets
sed \
  -e "s|@@ISA_ROOT@@|$ISA_ROOT|g" \
  deploy/isadoraair-locations.conf | sudo tee /etc/nginx/snippets/isadoraair-locations.conf > /dev/null
sed \
  -e "s|@@ISA_ROOT@@|$ISA_ROOT|g" \
  deploy/isadoraair.nginx | sudo tee /etc/nginx/sites-available/isadoraair > /dev/null
sudo ln -sf /etc/nginx/sites-available/isadoraair /etc/nginx/sites-enabled/isadoraair

# 4. Reload + enable the units you want running
sudo systemctl daemon-reload
sudo systemctl enable --now isadoraair-gunicorn isadoraair-engine \
  isadoraair-encoders isadoraair-monitoring isadoraair-rbds
sudo systemctl enable --now isadoraair-analyze.timer \
  isadoraair-prune-emaillog.timer isadoraair-prune-systemevents.timer \
  isadoraair-aircheck-buffer.timer
# Only needed if Web Requests is in use (WebRequestConfig.enabled):
sudo systemctl enable --now isadoraair-generate-dedication-intros.timer
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
| `isadoraair-prune-royalty-ledger.timer` | Daily (04:35) — prunes PlayEvent + IcecastSample rows past retention (default 3 years each). RoyaltyReport rows and their generated files are kept forever. |
| `isadoraair-tunein-push.timer` | Every 30s — pushes now-playing to TuneIn's AIR API when the current PlayEvent id differs from the last successful push. No-op until credentials are entered at Config > TuneIn AIR and `enabled` is checked. |
| `isadoraair-generate-dedication-intros.timer` | Every 15s — synthesizes spoken dedication intros (Kokoro) for scheduled web song requests. `Nice=19`, independent of the 20s web-requests-ingest poll cycle (Kokoro+ffmpeg can take tens of seconds). Only relevant if Web Requests is enabled. |
| `isadoraair-aircheck-buffer.timer` | Every minute — rolls over the always-on Aircheck idle working buffer (`/run/isadoraair/aircheck-current.audio`) via the existing `aircheck.reopen` telnet call once it grows past a hard-coded 64 MiB safety limit, but only when no Aircheck session is active. `Nice=19`. Never starts/restarts encoders and fails safely (retries next cycle) if Liquidsoap is unreachable. |
| `isadoraair-backup.timer` | Nightly full backup (03:30) — runs the repo-managed `deploy/backup_isadoraair.sh`; needs remote-target creds (`@@ISA_HOME@@/.iasboxbu.cred`) outside the repo |
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
| `isadoraair.nginx` | `/etc/nginx/sites-available/isadoraair`, symlinked from `sites-enabled/isadoraair` |
| `isadoraair-locations.conf` | `/etc/nginx/snippets/isadoraair-locations.conf` — shared location blocks `include`d by every HTTPS server block in `isadoraair.nginx` |
| `isadoraair-tmpfiles.conf` | `/etc/tmpfiles.d/isadoraair.conf` — creates `/run/isadoraair` on boot with the right owner |
| `needrestart-isadoraair.conf` | `/etc/needrestart/conf.d/isadoraair.conf` — auto-restarts services on library upgrades so a matplotlib/psycopg2 refresh doesn't leave the engine on the old shared object |
| `asound.conf` | `/etc/asound.conf` — ALSA loopback / dsnoop config for the studio + streaming feeds |
| `isadoraair-aloop.conf` | `/etc/modprobe.d/isadoraair-aloop.conf` — pins `snd-aloop` to three loopback cards at fixed indices 0/3/4, which `asound.conf`'s `airtap`/`airtap_ds` aliases (and StereoTool's configured device) depend on by exact card number. Also requires `/etc/modules-load.d/snd-aloop.conf` containing the single line `snd-aloop` (`echo snd-aloop \| sudo tee /etc/modules-load.d/snd-aloop.conf`) so the module loads at boot at all -- see the main `README.md`'s "ALSA loopback module" step and this file's own header comment for the full reasoning. |
| `stereotool.service.example` | `/etc/systemd/system/stereotool.service` — **only if** you run StereoTool (or a similar external processor) as a supervised systemd service; see its own header comment. Not installed by the loop in step 2 (`.example` isn't matched by `deploy/*.service`) — copy and rename it deliberately. |

### One authoritative nginx config

`sites-enabled/isadoraair` must be a **symlink** to `sites-available/isadoraair`,
never a second copy of the file. If the two ever diverge (someone edits
`sites-enabled` directly, e.g. under time pressure during an incident),
whichever one nginx is actually serving from silently becomes the only
correct copy, and anything that backs up or version-controls the *other*
one is protecting the wrong file. Confirmed to have happened for real
here — the disaster-recovery Phase 1 audit (2026-08-12) found the two
files had diverged (`sites-enabled` had picked up a second HTTPS vhost
for a public domain name + Let's Encrypt cert that `sites-available` — the
one the nightly backup was actually reading — never had), reconciled in
Phase 2 by making `sites-enabled` a proper symlink again. `nginx -t`
after any manual edit, before reloading, catches syntax errors but
**not** this kind of split — only `diff <(readlink -f sites-enabled/X)
<(echo sites-available/X)`-style checks (or just never breaking the
symlink in the first place) catch that.

### Public HTTPS with your own domain

The `isadoraair.nginx` template ships with one HTTPS server block, on
the self-signed cert, marked `default_server`. If you also want a real
public hostname (Let's Encrypt or any other CA), add a **second**
`listen 443 ssl;` block rather than replacing the first one — this
keeps LAN/legacy access on the self-signed cert working via SNI fallback
to the `default_server` block:

```nginx
server {
    listen 443 ssl;
    server_name your.public.hostname.example;

    ssl_certificate     /path/to/your/fullchain.pem;
    ssl_certificate_key /path/to/your/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    include snippets/isadoraair-locations.conf;
}
```

Production here uses this exact pattern with a Let's Encrypt cert issued
by `acme.sh` via DNS-01 (cron-driven, entirely outside this repo — see
`PROJECT_NOTES.md`/the disaster-recovery documentation for the station's
own specifics). Whatever ACME client or CA you use, the cert/key paths
are yours to choose; nothing in this repo assumes a particular one.

## Subsystems outside this repo

`syndicated-ingest/`, `weather-ingest/`, and `ogremote-ingest/` are
separate projects (own venvs, own creds). The units in `deploy/`
reference them but do not include their source — this repo is
IsadoraAir itself; those three ingest paths are companion projects
that produce audio + metadata IsadoraAir then plays.

The backup **script** (`deploy/backup_isadoraair.sh`) is repo-managed —
`isadoraair-backup.service`'s `ExecStart` runs it directly from your
checkout (`@@ISA_ROOT@@/deploy/backup_isadoraair.sh`), same as every
other unit in this directory. Only its **credential file**
(`@@ISA_HOME@@/.iasboxbu.cred` — `BAK_HOST`/`BAK_USER`/`BAK_PORT`/
`BAK_PATH`/`BAK_PASS`, mode `0600`) lives outside the repo, since it's
a station-specific secret, not code.

If you're not running any of the above, skip enabling the
corresponding units. The core five services + the three prune/
analyze timers are all you need for a working single-box install.
