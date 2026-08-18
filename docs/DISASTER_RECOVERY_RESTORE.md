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

**The `GIT_SSH_COMMAND` override above is not optional boilerplate —
verified live (2026-08-17, Phase 4.5) it is actually required.** This
box's `~/.ssh/config` sets a *default* identity for `github.com`
(`Host github.com` → `IdentityFile ~/.ssh/github_isadora_rw`), and that
default key is a **per-repository GitHub deploy key**, scoped to
`celltech161/IsadoraAir` only — confirmed by its own SSH auth banner
(`Hi celltech161/IsadoraAir!`, GitHub's deploy-key response format,
distinct from an account-level `Hi <username>!`) and by a real,
read-only `git ls-remote` against each of the three companion repos
using that identity: `syndicated-ingest` returns "Repository not
found" (the clean auth-scoped-out response); `weather-ingest` and
`ogremote-ingest` both stall. **A bare `deploy/restore/80-companions.sh
--apply` run with no `GIT_SSH_COMMAND` override — i.e. relying on
whatever `git`/SSH the operator has "already set up," per that script's
own header comment — will fail on all three companion repos on a
freshly provisioned restore host**, unless that host's `~/.ssh/config`
happens to default to a broader-scoped key.

**What does cover all four, verified live the same pass**: this
account's personal key (`~/.ssh/id_ed25519` on the current production
host, comment `celltech161@gmail.com`) authenticates at the *account*
level (`Hi celltech161!`, not a per-repo deploy-key banner) and a real
`git ls-remote HEAD` against all four repos with that identity
succeeded. Any key with equivalent account-level (or four-repo
multi-deploy-key) access works the same way — the identity used in
practice on this box happens to be the account key, not a requirement
of the tooling itself.

No credential or key is embedded anywhere in this repo or the backup
archive — provisioning the key itself (from wherever it's held outside
this station, e.g. a password manager or a second physical copy) is a
manual step this tooling assumes is already done before it runs. See
"Secrets and credentials inventory" below for this item's A/B/C
recovery classification.

## Secrets and credentials inventory

Consolidated from `docs/DISASTER_RECOVERY.md`'s "Secret reprovisioning
boundary" plus the companion projects' own credential patterns. No
values are recorded here or anywhere in this repo — names, destinations,
and (where known) an external recovery source only.

**Classification** (established 2026-08-17, Phase 4.5, against the
*actual* current host — file existence/owner/mode/expected key names
verified directly, values never displayed; vendor/provider workflows
verified only where a concrete mechanism was actually confirmed, never
assumed):

- **A — externally recoverable now**: a known external source
  independent of this host already exists (password manager, vendor
  account portal, etc.).
- **B — safely reprovisionable**: the original value doesn't need to
  survive — a fresh replacement can be generated through a verified
  provider/vendor workflow.
- **C — unresolved DR blocker**: exists only on this host, no verified
  external copy, no proven reprovisioning procedure. Real, current gaps
  — not filled in with a guess, per the same instruction Phase 1/2/4
  followed.

| Secret | Purpose | Destination | Owner/mode (verified) | Class | External source / reprovision method | Needed at |
|---|---|---|---|---|---|---|
| `.env` (`SECRET_KEY`, `DB_PASSWORD`, email creds, etc.) | Django/DB/email auth | `$ISA_ROOT/.env` | `$ISA_USER`, `0600` | **A** | Inside the backup's `app.tar.gz`, restored by `20-application.sh`. `SECRET_KEY` can be freshly regenerated if ever unavailable (invalidates sessions, otherwise harmless); `DB_PASSWORD` must match whatever the restored/recreated PostgreSQL role actually has. | Automated restore (stage 20) |
| GitHub SSH access (read, all 4 private repos) | Cloning code during restore (`20-application.sh`, `80-companions.sh`) | Operator's own `~/.ssh/`, outside this repo entirely | Verified live: `id_ed25519` (celltech161 account key), `0600`, reaches all 4 repos. Default `~/.ssh/config` identity (`github_isadora_rw`, an `IsadoraAir`-only deploy key) reaches only 1 of 4. | **B** | Log into the `celltech161` GitHub account via the normal web UI (independent of any file on this host — needs only the account's own login/2FA, not this specific key) and generate a fresh SSH key or per-repo deploy key with read access to all 4 repos. See "GitHub access" above for the verified evidence. | Automated restore (stage 20 — the very first write) |
| `~/.iasboxbu.cred` (`BAK_HOST`, `BAK_USER`, `BAK_PORT`, `BAK_PATH`, `BAK_PASS`) | `backup_isadoraair.sh`'s own SFTP upload/retrieval credentials | `$ISA_HOME/.iasboxbu.cred`, verified `0600` | — | **C** | Intentionally excluded from its own backup (avoids circularity — confirmed by design: `MANIFEST.txt`'s own "Secrets NOT included" list names this file explicitly). Not resolved by this audit — see "Backup credential recovery" note below for why this is two related but distinct needs. | **Pre-restore** (retrieving the archive at all) and service bring-up (resuming nightly backups) |
| `~/.syndicated_ingest.cred` | Syndicated-show fetch credentials (RadioPush, per-show site logins), SFTP art upload, SMTP, Bluesky app password | syndicated-ingest's own cred file, verified `0600` | — | **C** | Not resolved by this audit. | Full production readiness (syndicated-`*` timers only — IsadoraAir itself doesn't need it) |
| `~/.ogremote_ingest.cred` (`API_KEY`) | Remote-content polling auth | ogremote-ingest's own cred file, verified `0600` | — | **C** | Not resolved by this audit. | Full production readiness (ogremote-`*` timers only) |
| weather-ingest config | GW3000/weather API access | **Not a file** — lives in IsadoraAir's own database (`WeatherConfig`/`AmberAlertConfig` admin singletons — verified present in `weather/models.py`; `dump_weather_config`/`dump_amber_alert_config` commands verified to exist and are the exact cross-venv calls `weather-ingest/lib/wxconfig.py` makes), reachable only once `20-application.sh`/`30-postgresql.sh`/`60-python.sh` have restored code+DB+venv | N/A | **A** | Restored as part of the database dump itself — no separate secret-recovery action needed. Re-enter manually only if the DB restore is unavailable and a fresh empty DB was created instead. | Automated restore (rides along with stage 30) |
| acme.sh / DNS-01 provider credentials (IONOS: `IONOS_PREFIX`, `IONOS_SECRET`) | Let's Encrypt cert issuance for `radio.oakgroveradio.com` | `~/.acme.sh/account.conf`, verified `0600`; DNS plugin `~/.acme.sh/dnsapi/dns_ionos.sh` present | — | **B** | These are IONOS Developer Portal API credentials (DNS scope), not a value that has to survive — a fresh `IONOS_PREFIX`/`IONOS_SECRET` pair can be generated from the IONOS account portal and re-entered for a fresh `acme.sh --issue --dns dns_ionos ...` run, provided the operator retains their own IONOS account login (a separate, much higher-stakes credential than this one — IONOS is also the domain registrar). Falls back to the self-signed cert (already restored as part of the nginx config by `90-system-config.sh`) if unavailable either way — degraded public HTTPS, never a hard blocker. | Full production readiness only (not automated restore, not service bring-up — self-signed cert covers both) |
| StereoTool license | Unlocks the processor beyond trial/demo limits | Bundled with the StereoTool install itself — no separate license file found anywhere on this host by name search | — | **C** | **Not resolved by this audit, and deliberately not assumed resolvable** — this audit did not verify StereoTool's actual reactivation mechanism (account/purchase-order based reissue vs. hardware-fingerprint-bound single activation vs. something else). "Contact the vendor" is not treated as sufficient per this pass's own instructions until that mechanism is actually confirmed. | Service bring-up (real on-air audio through the processor) — software-only restore (`95-validate.sh` PASS) does not need it |

**Any row still classified C is a real, current DR readiness gap** —
worth resolving before treating this station as fully
disaster-recovered, not just before Phase 5. B rows are not blockers in
the same sense (the workflow to replace them is known and doesn't
depend on anything surviving from the dead host) but are worth
confirming once, in a controlled setting, rather than trusting the
workflow untested during a real incident.

## Backup credential recovery

`~/.iasboxbu.cred` (`BAK_HOST`/`BAK_USER`/`BAK_PORT`/`BAK_PATH`/
`BAK_PASS`) is two related but genuinely distinct needs, not one:

1. **Retrieving an existing backup archive after total host loss.**
   Before Phase 5's automated stages can even start (`00-preflight.sh`
   needs `--archive PATH`), an operator has to get a
   `isadoraair-backup-*.tar.gz` off the remote SFTP target — which
   needs these same values.
2. **Resuming future nightly backups once the restore is complete.**
   `isadoraair-backup.timer` will run
   `deploy/backup_isadoraair.sh` on its normal schedule once services
   are back up (see "Service bring-up order" below) — that script reads
   `$HOME/.iasboxbu.cred` itself and will simply fail (loudly, exit 1 —
   see the script's own `[ ! -f "$CONFIG_FILE" ]` check) until this file
   exists again on the restored host.

**Both need the exact same values**, so resolving the external source
once covers both — there's no separate recovery path required for one
vs. the other. The credential file is deliberately excluded from its
own backup by design (confirmed: `MANIFEST.txt`'s "Secrets NOT
included" list names it explicitly) specifically to avoid a circular
dependency — "the backup can't be used to recover the credentials
needed to reach the backup." That anti-circularity design is correct
and intact; what's still open is that **no external copy of these
values was found documented anywhere this audit could inspect** (not
this repo, not any doc file, not the host outside `~/.iasboxbu.cred`
itself) — a real, current gap (Class C, see the table above), not a
flaw in the backup mechanism's own design.

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
any restore step — proprietary. **The license specifically is
classified C (unresolved DR blocker) in the table above, not B** — this
audit found no license file anywhere on the host by name search (it
appears bound into the binary/its own runtime state, not a separate
extractable artifact) and did not verify StereoTool's actual
reactivation workflow (account/purchase-order reissue vs.
hardware-fingerprint-bound single activation vs. something else).
"Contact the vendor" is the honest current answer, not a confirmed
recovery procedure — resolving this (a real conversation with the
vendor about what reactivation after hardware loss actually looks like)
is future work, not assumed done here. `40-station-content.sh` restores
the `.sts` processing profile (the one piece of this that genuinely is
backed up) and prints a checklist:

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
