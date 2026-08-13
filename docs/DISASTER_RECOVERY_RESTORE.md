# Disaster recovery: restore procedure

Roadmap item 1.2, Phase 4 (2026-08-12). This is the human-readable
procedure an administrator follows during a real recovery — usable when
the original machine is dead, given: the private GitHub repositories,
one known-good `isadoraair-backup-*.tar.gz` archive, the externally
stored credentials/licenses this document inventories below, and
replacement Ubuntu 26.04 hardware. It does **not** assume access to the
dead host for anything.

See `docs/DISASTER_RECOVERY.md` for *what's backed up and why* (the
Phase 1/2 audit); this document is *how to put it back together*, built
on `deploy/restore/`'s tooling (Phase 4). `deploy/restore/README.md`
covers that tooling's own architecture/safety-mode design in detail —
this document is the operator-facing walkthrough, not a restatement of
it.

**Phase 4 built and validated this tooling in staging. Phase 5 is the
actual bare-machine proof.** Nothing here has been run against a real
clean machine yet — every stage was exercised via `--staging-root
--apply` against isolated paths, described in the Phase 4 completion
report.

## Overview

```
clean Ubuntu 26.04
  |
  v  deploy/restore/00-preflight.sh    -- OS check, validate the backup archive
  v  deploy/restore/10-packages.sh     -- apt packages
  v  deploy/restore/20-application.sh  -- git clone + SHA checkout, .env + media/
  v  deploy/restore/30-postgresql.sh   -- role/DB bootstrap, pg_restore
  v  deploy/restore/40-station-content.sh -- carts/voicetracks/reports/StereoTool profile
  v  deploy/restore/50-native-deps.sh  -- fdk-aac/fdkaac build + HE-AAC validation
  v  deploy/restore/60-python.sh       -- IsadoraAir venv
  v  deploy/restore/70-tts.sh          -- Kokoro + Piper
  v  deploy/restore/80-companions.sh   -- syndicated-ingest/weather-ingest/ogremote-ingest
  v  deploy/restore/90-system-config.sh -- nginx + systemd units installed, NOT started
  v  deploy/restore/95-validate.sh     -- read-only final check (manage.py check,
  |                                       check_deploy_baseline, migration state)
  v
[everything below this line is MANUAL -- see "Manual checkpoints"]
  v  attach/mount persistent music storage
  v  obtain + install StereoTool binary + license
  v  controlled service bring-up (see "Service bring-up order" below)
  v  confirm sound hardware
  v  confirm transmitter/output path
  v  issue or restore certificates
```

Run each stage with `--plan` first (always safe, never writes anything),
then `--apply` once the plan looks right. `deploy/restore/restore.sh`
chains all eleven stages if you'd rather run them in one shot; running
them individually and reviewing each is recommended for the actual
Phase 5 drill.

## Manual checkpoints

These are deliberately **not** automated — the tooling makes them
obvious rather than pretending the whole restore is unattended:

| Checkpoint | Why it's manual |
|---|---|
| GitHub private-repo access | `deploy/restore/20-application.sh` and `80-companions.sh` need working, non-interactive `git`/SSH access to `celltech161/IsadoraAir` + the three companion repos. Provision an SSH key with read access before starting — see "GitHub access" below. |
| Attach/identify persistent music storage | The 717+ GB library is never part of any backup (see `docs/DISASTER_RECOVERY.md`'s "Music library" section) — mount the real disk (or its current replacement/replica) at `LIBRARY_ROOT` separately. `40-station-content.sh` only creates the empty mountpoint. |
| Recover secrets | See "Secrets and credentials inventory" below — several have no established external recovery source as of this writing. |
| Obtain StereoTool binary + license | Proprietary, paid software — never in Git or backups. See "StereoTool" below. |
| Certificate issuance | See "Certificates" below — this restore does not assume `acme.sh` state is recoverable. |
| Confirm sound hardware | A staging VM or spare host may not have the Topping D10s / studio card / Mixcast / transmitter path — see "Software-complete vs. audio-hardware-ready" below. |
| Confirm transmitter/output path | Physical RF chain, entirely outside this repo's scope. |

## GitHub access

`git@github.com:celltech161/IsadoraAir.git`,
`.../syndicated-ingest.git`, `.../weather-ingest.git`,
`.../ogremote-ingest.git` are all **private**. A restore needs an SSH
key with read access to all four, usable non-interactively (no
passphrase prompt) — e.g.:

```bash
GIT_SSH_COMMAND="ssh -i /path/to/key -o IdentitiesOnly=yes" deploy/restore/restore.sh --archive ... --apply
```

No credential or key is embedded anywhere in this repo or the backup
archive — provisioning the key itself (from wherever it's held outside
this station, e.g. a password manager or a second physical copy) is a
manual step this tooling assumes is already done before it runs.

## Secrets and credentials inventory

Consolidated from `docs/DISASTER_RECOVERY.md`'s "Secret reprovisioning
boundary" plus the companion projects' own credential patterns. No
values are recorded here or anywhere in this repo — names, destinations,
and (where known) an external recovery source only. Rows still marked
**no external source established** are real, current DR readiness
blockers, not filled in with a guess.

| Secret | Purpose | Destination | Owner/mode | External source established? |
|---|---|---|---|---|
| `.env` (`SECRET_KEY`, `DB_PASSWORD`, email creds, etc.) | Django/DB/email auth | `$ISA_ROOT/.env` | `$ISA_USER`, `0600` | **Yes** — inside the backup's `app.tar.gz`, restored by `20-application.sh`. `SECRET_KEY` can be freshly regenerated if the backup is ever unavailable (invalidates sessions, otherwise harmless); `DB_PASSWORD` must match whatever the restored/recreated PostgreSQL role actually has. |
| `~/.iasboxbu.cred` (`BAK_HOST`, `BAK_USER`, `BAK_PORT`, `BAK_PATH`, `BAK_PASS`) | `backup_isadoraair.sh`'s own SFTP upload credentials | `$ISA_HOME/.iasboxbu.cred`, `0600` | — | **No** — intentionally excluded from its own backup (avoids circularity). Not resolved by this audit. |
| `~/.syndicated_ingest.cred` | Syndicated-show fetch credentials, Bluesky app password | syndicated-ingest's own cred file, `0600` | — | **No** |
| ogremote-ingest credentials (`~/.ogremote_ingest.cred`) | Remote-content polling auth | ogremote-ingest's own cred file, `0600` | — | **No** |
| weather-ingest config | GW3000/weather API access | **Not a file** — lives in IsadoraAir's own database (`WeatherConfig`/`AmberAlertConfig` admin singletons), reachable only once `20-application.sh`/`30-postgresql.sh`/`60-python.sh` have restored code+DB+venv | N/A | **Yes, implicitly** — restored as part of the database dump itself; re-enter manually only if the DB restore is unavailable and a fresh DB was created instead. |
| acme.sh / DNS-01 provider credentials (IONOS) | Let's Encrypt cert issuance for `radio.oakgroveradio.com` | `~/.acme.sh/account.conf` / `acme.sh.env` | — | **No** — falls back to the self-signed cert (already restored as part of the nginx config by `90-system-config.sh`) if unavailable; degraded public HTTPS, not a hard blocker. |
| StereoTool license | Unlocks the processor | Bundled with the StereoTool install itself | — | **No** — contact the vendor. |
| GitHub SSH deploy key (read access to all 4 private repos) | Cloning code during restore | Operator's own `~/.ssh/`, outside this repo entirely | — | Depends entirely on where the operator keeps it — see "GitHub access" above. Not tracked further by this table. |

None of the "No" rows above were resolved by Phase 4 — recorded as open
items, per the same instruction Phase 1/2 followed: don't invent a
source that isn't actually known. **Any row still "No" here is a
meaningful DR readiness blocker even though the software side is fully
reproducible** — worth resolving before treating this station as fully
disaster-recovered, not just before Phase 5.

## Persistent storage mount

**Generic requirement**: some persistent, writable path at
`LIBRARY_ROOT` (and its `/srv/isadoraair` siblings — see
`docs/DISASTER_RECOVERY.md`'s subtree table) must exist with correct
ownership before the services that need it start. `40-station-content.sh`
creates the directory structure (including an empty `music/` mountpoint)
but never the mount itself.

**Oak Grove production reference** (an example, not a generic default —
see `docs/DISASTER_RECOVERY.md`'s own "reference evidence, not a
requirement" framing): a dedicated 1.8 TB ext4 partition
(`/dev/nvme0n1p1`), `/etc/fstab` entry:
```
UUID=ca4da361-7210-4ed6-8e74-5ddb9c92b5c4  /srv/isadoraair  ext4  defaults,noatime,nofail  0  2
```
`nofail` matters generically, not just as Oak Grove trivia: boot must
not hang if the disk is ever missing. A fresh restore target's real
UUID will differ — `blkid` the actual replacement/replica disk and
write its own `/etc/fstab` line, then `sudo mount -a` and verify with
`mount | grep isadoraair` and `df -h /srv/isadoraair`.

**Staging without the full music disk**: `95-validate.sh` and
`check_deploy_baseline` both tolerate an empty `music/` — they report it
as a warning (see `docs/DISASTER_RECOVERY.md`'s "Music library" section
on why this is a deliberately separate readiness gate), not a failure.
A restore can proceed through every automated stage, and even through
early service bring-up (web UI, library catalog browsing), with the
disk still unattached — the station just can't actually air until the
audio content itself is present.

## StereoTool

The binary and license are **never** part of this repo, the backup, or
any restore step — proprietary, externally reprovisioned (contact the
vendor). `40-station-content.sh` restores the `.sts` processing profile
and prints a checklist:

```
[ ] Profile (.sts) restored?       -- automated by 40-station-content.sh
[ ] Binary installed?              -- manual, obtain from vendor
[ ] License/config present?        -- manual, obtain from vendor
[ ] Service unit valid?            -- automated by 90-system-config.sh
                                       (deploy/stereotool.service.example,
                                       copy + fill in placeholders + rename
                                       to stereotool.service deliberately --
                                       not matched by the *.service install
                                       glob on purpose)
```
Do not claim full station readiness until all four are checked. See
`deploy/stereotool.service.example`'s own header comment for the
realtime-scheduling parameters (`CPUSchedulingPolicy=fifo`, priority 80,
`LimitRTPRIO=95`) that are load-bearing for glitch-free audio once it's
running.

## Certificates

`deploy/restore/90-system-config.sh` installs the **generic** nginx
template — self-signed cert only, `default_server`. It does not assume
`acme.sh`/Let's Encrypt state is recoverable (see the secrets table
above). Three options for the public HTTPS hostname, in order of
preference:

1. **Issue a fresh certificate** — re-run `acme.sh`'s DNS-01 issuance
   against the DNS provider (IONOS, for `radio.oakgroveradio.com`) once
   its own credentials are reprovisioned. Preferred: no dependency on
   anything from the dead host.
2. **Restore externally-preserved cert material**, if a copy of the
   actual `fullchain.pem`/`privkey.pem` exists somewhere outside the
   dead host (a password manager, a separate secrets vault). Faster,
   but only as good as whatever preserved it.
3. **Run temporarily on the self-signed cert only** — LAN/local access
   works immediately (see `deploy/README.md`'s "Public HTTPS with your
   own domain" section for the exact second-`server`-block pattern to
   add once a real cert is available). Public HTTPS is degraded, not
   broken — self-signed still serves the same content.

## Service bring-up order

Derived from the actual `After=` dependencies declared in `deploy/`'s
unit files (not assumed) plus each companion timer's real runtime
dependency on IsadoraAir's own DB/venv:

```
PostgreSQL (running, migrated -- stages 30 + 95 already confirmed this)
  |
  v
isadoraair-gunicorn                 (After= network, postgresql)
  |
  +--> isadoraair-monitoring        (After= ...gunicorn)
  |
  +--> StereoTool (external, manual -- see "StereoTool" above;
  |     After=sound.target, no IsadoraAir-side dependency)
  |       |
  |       +--> isadoraair-engine    (After= ...gunicorn)
  |       |       |
  |       |       v
  |       |     isadoraair-rbds     (After= ...gunicorn, engine, stereotool)
  |       |
  |       +--> isadoraair-encoders  (After= ...gunicorn, stereotool)
  |
  +--> companion timers/services    (syndicated-*, wx-*, ogremote-*,
  |     isadoraair-generate-dedication-intros -- all shell out to
  |     $ISA_ROOT/venv/bin/python manage.py <command>, so need
  |     gunicorn-confirmed-healthy as a practical readiness signal even
  |     though the systemd units themselves don't declare it)
  |
  +--> isadoraair-backup.timer, prune-*.timer, isadoraair-analyze.timer
  |     (low-risk, no strict ordering beyond DB+venv existing)
  |
  v
nginx (public interface -- reload only once gunicorn is confirmed
  listening on 127.0.0.1:8000; this repo's install step deliberately
  never auto-reloads nginx, see 90-system-config.sh)
```

**Reconciling with `deploy/*.service`'s own declared `After=` lines is
the source of truth if this ever needs re-deriving** — the graph above
is not the Phase 4 spec's own suggested example order verbatim; it's
what the actual units say, cross-checked.

### Software-complete vs. audio-hardware-ready

`95-validate.sh`'s PASS means the *software* restore is complete and
internally consistent — it does **not** mean the station can air. A
staging VM or spare host commonly lacks:

- the Topping D10s (or equivalent DAC/interface),
- the studio sound card itself,
- a Mixcast or external mixer,
- the actual transmitter path.

Services safe to bring up and exercise meaningfully **without** any of
that hardware: `isadoraair-gunicorn` (web UI, library browsing, admin),
`isadoraair-monitoring` (most checks — audio-silence detection excepted),
the companion timers (fetch/deliver into the library, independent of
playback), `nginx`. Services that need real hardware to do anything
useful: `isadoraair-engine` (GStreamer needs a real ALSA sink to produce
audible output — it may start cleanly and simply produce no sound, or
fail depending on ALSA device availability), `isadoraair-encoders`
(needs a real capture device), `isadoraair-rbds`/StereoTool (need the
real processing chain). Treat their non-function on hardware-less
staging as an expected, hardware-specific readiness gap, not a software
restore failure — this distinction matters specifically because Phase
5's drill environment may not reproduce every USB device exactly.

## ALSA / snd-aloop

`90-system-config.sh` installs `deploy/isadoraair-aloop.conf` to
`/etc/modprobe.d/` and `deploy/asound.conf` to `/etc/asound.conf`. A
kernel module reload (or reboot) is required afterward for the pinned
3-instance loopback layout to take effect —
`echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf` (loads at
boot) plus `sudo modprobe -r snd_aloop && sudo modprobe snd-aloop` (or
just reboot). Verify with `cat /proc/asound/cards` — three "Loopback"
entries at indices 0/3/4 alongside real hardware, or via
`manage.py check_deploy_baseline`'s own snd-aloop check. The unstable
real USB sound-card numbering (`docs/ALSA_DEVICE_INVENTORY.md`'s
`plughw:2,0` note) is **not** solved by this tooling — remains roadmap
item 1.3.

## After bring-up: confirming real readiness

Once services are up and hardware is attached:

```bash
sudo systemctl status isadoraair-gunicorn isadoraair-engine \
  isadoraair-encoders isadoraair-monitoring isadoraair-rbds
```
plus a real end-to-end check from the dashboard (`https://<host>/`) —
build an hour's log, confirm the engine actually plays audio, confirm a
stream connects if encoders are enabled. `manage.py
check_deploy_baseline` is read-only and safe to re-run at any point
during this process for a fast recheck.

## Known limitations (honest, not silently assumed away)

- `MANIFEST.txt` does not currently record whether the source tree was
  clean (no uncommitted changes) at backup time — a restore checks out
  exactly the recorded SHA and cannot recover anything that was
  uncommitted when the backup ran. Extending the backup script to
  record `git status --porcelain` output (or refuse to back up a dirty
  tree) is a natural future improvement, not implemented in this pass.
- `deploy/restore/90-system-config.sh`'s `nginx -t` and snd-aloop
  verification only run meaningfully against a REAL (non-staging)
  target — an isolated staging tree has no full nginx config context or
  kernel to load a module into, so those two checks are explicitly
  skipped (not faked) under `--staging-root`.
- `manage.py check_deploy_baseline` checks this HOST's actual configured
  paths (e.g. `/usr/local/bin/fdkaac`, `~/kokoro`), not a
  `--staging-root`'s isolated copies — during Phase 4's own staging
  validation this meant it was confirming production's real, already-
  correct state rather than the freshly-staged build/venv from earlier
  stages. Still a genuine, useful check; just not staging-root-aware by
  design (it's meant to run on the real target after a real restore).
