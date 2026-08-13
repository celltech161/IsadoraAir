# Disaster recovery

Roadmap item 1.2. This document is the durable record of what a
bare-machine restore of IsadoraAir actually requires, split explicitly
into **generic IsadoraAir requirements** (anyone's install) and
**Oak Grove production specifics** (this station's own current setup) —
mixing the two together is exactly how a generic install ends up
accidentally assuming one station's topology. See `README.md` for the
from-scratch install walkthrough and `deploy/README.md` for what each
systemd/nginx/config file does; this document covers *recovery*
specifically — what's backed up, what isn't, why, and what a human still
has to do by hand.

Written from a two-phase audit: **Phase 1** (2026-08-12, read-only
discovery across the repo and the live production host) established the
evidence base below; **Phase 2** (same day) closed the deterministic,
low-risk gaps Phase 1 found. Both phases' own completion reports have
the full command-by-command evidence trail if you need it; this doc is
the distilled, durable result.

## What's backed up, and by what

`deploy/backup_isadoraair.sh` — the authoritative, repo-versioned
implementation as of Phase 2 (previously host-only, at
`~/bin/backup_isadoraair.sh`, with no version control on the script
itself; that host copy has not yet been repointed at this file, see
"Known deployment follow-up" below) — runs nightly via
`isadoraair-backup.service`/`.timer`, producing one timestamped
`isadoraair-backup-YYYYMMDD-HHMMSS.tar.gz`, uploaded over SFTP to a
remote target configured in `~/.iasboxbu.cred` (never in the repo), with
30-day retention. Every archive contains a `MANIFEST.txt` listing its
own exact contents, exclusions, script version, and the IsadoraAir Git
SHA it was taken alongside.

**Covered:**

- Full `pg_dump -Fc` of the database (see "Database restore" below —
  the dump alone is not sufficient for a bare-metal restore).
- The application tree: code, `.env`, `media/` (excludes `.git/`,
  `venv/`, `__pycache__/`, `staticfiles/`, `media/album_art_cache/` —
  all either redundant with GitHub or cheaply regenerable).
- The **live**, actually-serving nginx config
  (`/etc/nginx/sites-available/isadoraair`, now the sole authoritative
  copy — see "nginx: one authoritative source" below) and its shared
  `snippets/isadoraair-locations.conf`.
- The live systemd unit files for the 5 core services plus
  `stereotool.service` if present.
- StereoTool's `.sts` processing profile(s), by explicit glob — never
  the whole StereoTool install (binaries/license are external, see
  "StereoTool" below).
- Small, operator-created `/srv/isadoraair` content: `carts/`
  (FX Cart audio, admin-uploaded) and `voicetracks/` (recorded
  voicetrack audio).
- Royalty/SoundExchange report filings (`REPORTS_ROOT`, default
  `/var/lib/isadoraair/reports`) — see "Reports" below for why these
  are treated as backup-required rather than regenerable.

**Deliberately NOT covered** (see the dedicated sections below for each):

- `/srv/isadoraair/music` — the audio library itself.
- `/srv/isadoraair/waveforms` — regenerable (`manage.py analyze_tracks`).
- `/srv/isadoraair/aircheck`, `rip_staging` — high-growth/transient.
- `/srv/isadoraair/mitd_artbell` and similar large syndicated-show
  staging trees — large, own future sizing decision.
- The three companion ingest projects (`syndicated-ingest/`,
  `weather-ingest/`, `ogremote-ingest/`) — see "Companion projects"
  below; not yet even under version control, tracked as separate
  follow-up work, not silently ignored.
- Kokoro TTS's model files (`~/kokoro/`, ~587 MB) — see "External
  components" below.
- All the secrets listed under "Secret reprovisioning boundary".

### Known deployment follow-up

`isadoraair-backup.service` (the live systemd unit) still points at
`~/bin/backup_isadoraair.sh`, not this repo's `deploy/backup_isadoraair.sh`.
Repointing it (symlink the old path here, or update the unit's
`ExecStart`) is an intentional, separate deployment step — not done
automatically by adding this file to the repo, and not done as part of
Phase 2 (which explicitly stopped short of restarting/redeploying
anything pending review).

## Music library — explicit scope decision

**`/srv/isadoraair/music` (currently ~717 GB) is NOT part of the
IsadoraAir nightly disaster-recovery backup, and this is intentional, not
an oversight.** The library is too large for the current backup
destination's available space, and this backup exists to protect the
*application* (code, config, database, small station state) — library
storage resilience is a related but separate concern with its own,
not-yet-formalized plan (a second disk, and/or regular replication or
mirroring — future work, tracked separately, see roadmap item 1.2's own
phase plan).

**What this means concretely for a restore:**

- Restoring the database restores every `Track` row — filenames, tags,
  cue points, category assignments, everything Django knows about the
  library — correctly and completely.
- It does **not** restore the audio files those `Track.filepath` values
  point to. A freshly-restored IsadoraAir install has a fully correct
  library *catalog* pointing at files that don't exist yet.
- Getting a **working station** back requires separately restoring or
  re-mounting the actual audio content at `LIBRARY_ROOT`
  (`/srv/isadoraair/music` in production) — from whatever other
  copy/copies of the library currently exist. **This audit did not
  verify where that other copy is, or confirm it's current** — that's
  explicitly flagged as unresolved, not assumed. Do not treat "the
  audio exists on other user-owned drives/machines" as a formal backup
  system; it isn't one until it's been verified and documented as such.

**The bare-machine restore acceptance test (roadmap 1.2 Phase 5) should
therefore distinguish two separate success criteria:**

1. **Application reconstruction** — IsadoraAir software, configuration,
   database, and services restore correctly and the station *can* run.
2. **Full station-content recovery** — the actual library media is
   present, which requires a separate, currently-undefined restore path.

Absence of a full 717 GB media backup should not block progress on (1).

## nginx: one authoritative source

**Before Phase 2, the file actually serving traffic
(`/etc/nginx/sites-enabled/isadoraair`) was a *plain file*, not a
symlink to `/etc/nginx/sites-available/isadoraair` as Debian/Ubuntu's own
convention assumes** — and the (then host-only) backup script was
capturing `sites-available`, which had silently diverged: it was
missing the public `radio.oakgroveradio.com` Let's Encrypt vhost, the
GW3000 weather-gateway proxy special-case, and the shared
`snippets/isadoraair-locations.conf` include entirely. Confirmed via a
direct `diff` immediately before Phase 2's fix (see the Phase 1 and
Phase 2 completion reports for the exact diff output).

**Fixed in Phase 2**: the live, actually-serving content was promoted
verbatim into `sites-available/isadoraair` (zero behavioral change —
confirmed via `nginx -t` and a `nginx -T` before/after comparison, no
reload performed), and `sites-enabled/isadoraair` was replaced with a
proper symlink to it. `sites-available/isadoraair` is now genuinely the
one authoritative file, and it's what the backup script captures.

**If this ever happens again** (someone edits `sites-enabled` directly
during an incident, under time pressure) — that's the bug to fix
immediately, not something to route around. `deploy/README.md` documents
this explicitly.

The **generic** nginx template lives in `deploy/isadoraair.nginx` +
`deploy/isadoraair-locations.conf` (self-signed cert only, no
station-specific hostnames). The **station-specific** live behavior
(the actual `radio.oakgroveradio.com` public vhost, real certificate
paths) is preserved only as backup/DR evidence — never hardcoded into
the generic template. See `deploy/README.md`'s "Public HTTPS with your
own domain" section for the documented, genericized pattern this
follows.

## Database restore

**Package baseline** (IsadoraAir 1.2 Phase 3, verified 2026-08-12):
`postgresql` (meta-package `18+290ubuntu1`) pulls in `postgresql-18`
(server, `18.4-0ubuntu0.26.04.1`), `postgresql-client-18`, and
`postgresql-client-common` — all from Ubuntu 26.04's own repos, no
third-party APT source (unlike some distros' PGDG-repo convention).
Major version (`18`) should be treated as **pinned** — IsadoraAir's
schema/migrations are only verified against PostgreSQL 18; minor patch
version is OS-managed/current-for-release. `apt install postgresql
postgresql-contrib libpq-dev` (already documented in the main
`README.md`'s fresh-install walkthrough) is the correct, complete
package set.

**Encoding/locale assumptions**: the live `isadoraair` database is
`UTF8` encoding, `libc` locale provider, `en_US.UTF-8` collate/ctype —
Postgres' own installation defaults on a standard Ubuntu box with the
`en_US.UTF-8` system locale generated (`locale -a` should list it; if
not, `sudo locale-gen en_US.UTF-8` before `initdb`/first cluster
creation). Not explicitly forced by any `CREATE DATABASE` flag in the
restore commands below — if a target box's default locale differs,
add `ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8'
TEMPLATE template0` to the `CREATE DATABASE` statement rather than
relying on the cluster default matching.

The nightly `pg_dump -Fc` captures the database's data completely, but a
**bare-metal restore needs manual bootstrap first** — `pg_dump` alone
does not capture roles/ownership:

```bash
# 1. Create the role and (empty) database -- same statement README.md's
#    own fresh-install walkthrough uses, reused here for restore:
sudo -u postgres psql <<'SQL'
CREATE USER isadoraair WITH PASSWORD 'from your recovery secret source';
CREATE DATABASE isadoraair OWNER isadoraair;
GRANT ALL PRIVILEGES ON DATABASE isadoraair TO isadoraair;
SQL

# 2. Restore the dump from the backup archive into that empty database:
pg_restore -h localhost -U isadoraair -d isadoraair --no-owner database.dump
```

The password used in step 1 is not itself in the backup (it's inside
`.env`, which *is* backed up as part of the app tree — so in practice
you'd read it back out of the restored `.env` and use the same value,
rather than inventing a new one, unless you're deliberately rotating
it). No `pg_dumpall --globals-only` is currently taken — the single
`CREATE USER`/`CREATE DATABASE` pair above is a sufficient, deterministic
substitute for this project's single-role, single-database setup, and
avoids the secret-handling complexity a full globals dump would add for
no material benefit here.

**Application state that's easy to mistake for "in the database" but
isn't**: the entire `/srv/isadoraair` tree (see below), `/run/isadoraair/*`
heartbeat files (correctly transient, regenerate automatically), and
`~/stereotool/*.sts` (StereoTool's own state — see "StereoTool" below).

## `/srv/isadoraair` and its subtrees

**The mount itself**: production uses a dedicated 1.8 TB ext4 partition
(`/dev/nvme0n1p1`) mounted at `/srv/isadoraair` via `/etc/fstab`
(`UUID=ca4da361-7210-4ed6-8e74-5ddb9c92b5c4`, options
`defaults,noatime,nofail` — `nofail` matters: boot doesn't hang if the
disk is ever missing). This is recorded here as **Oak Grove production
evidence**, not a generic requirement — a fresh install just needs *some*
configured, persistent path at `LIBRARY_ROOT` (and the sibling paths
below) to exist with the right ownership before the services that need
it start; `README.md` step 4 already documents that generic requirement
and doesn't (and shouldn't) assume any particular disk/UUID.

**Subtree classification** (sizes as measured 2026-08-12; re-measure
before relying on these for a real restore, they'll have grown):

| Subtree | Size | Classification | Backed up? | Reason |
|---|---|---|---|---|
| `music/` | ~717 GB | Persistent application data | No | See "Music library" above |
| `waveforms/` | 5.7 GB, 36k files | Regenerable | No | `manage.py analyze_tracks` rebuilds it from the (backed-up) audio catalog + (not-backed-up) audio itself |
| `carts/` | 1.9 MB, 1 file | Operator-created | **Yes** | `FXCart.filepath` DB rows reference these files directly; not regenerable |
| `voicetracks/` | ~12 KB, currently empty (2 empty dated subfolders) | Operator-created | **Yes** | Same reasoning as carts — empty today, structurally will contain irreplaceable recordings |
| `aircheck/` | 132 KB currently, 2 files | High-growth / policy-dependent | No | Small today but explicitly high-growth by design (continuous on-air recording); own retention policy is the right home for this, not the core DR backup — not silently excluded, this is a deliberate call |
| `rip_staging/` | ~4 KB, empty | Transient working directory | No | CD-rip staging area, cleared once processing completes |
| `mitd_artbell/` | 44 GB, 382 files, root-owned | Large syndicated-show staging | No | Fed by `manage.py prep_mitd_show` / `isadoraair-mitd-prep.timer`; too large for this pass's "small/medium" scope, own future sizing decision, not silently dropped |

## Reports (`/var/lib/isadoraair/reports`)

192 KB, 8 files as measured — SoundExchange NCE royalty filing CSVs and
summaries, generated from the `/reports/` page. **Classified as
backup-required**, not regenerable-so-skip: `deploy/README.md`'s own
existing documentation of `isadoraair-prune-royalty-ledger.timer` notes
that `RoyaltyReport` rows and their generated files are "kept forever"
specifically *because* the underlying `PlayEvent`/`IcecastSample` source
data they were computed from is eventually pruned (3-year default
retention) — meaning a report generated today could become
**non-regenerable in the future** even though it technically could be
regenerated right now. Small (192 KB), unambiguous win to include; now
covered by `deploy/backup_isadoraair.sh`.

`/var/lib/isadoraair/encoders/` (the encoder Last-Known-Good state,
`ENCODER_STATE_ROOT`) was also inspected — 52 KB, purely derived/cache
state the encoder reconciliation system is designed to regenerate on its
own next successful launch. **Not backed up, correctly** — no
irreplaceable content there.

## StereoTool

**Explicit boundary**: the StereoTool *binaries* and *license* are
externally reprovisioned — proprietary, paid software, not this repo's
or this backup's job to reproduce. What *is* this project's job to
protect is the **`.sts` processing profile** — the actual on-air EQ /
loudness / AESMPX processing chain tuning, small (currently ~200 KB) and
effectively irreplaceable by ear alone. `deploy/backup_isadoraair.sh`
now captures every `*.sts` file directly under the StereoTool directory
by explicit glob, never the whole install.

`stereotool.service` (the systemd unit supervising it, including the
realtime-scheduling parameters — `CPUSchedulingPolicy=fifo`, priority
80, `LimitRTPRIO=95`, `LimitMEMLOCK=infinity`, `CAP_SYS_NICE` — that are
load-bearing for glitch-free audio) was host-only before Phase 2, absent
from the repo even though `isadoraair-rbds.service` and
`isadoraair-encoders.service` both explicitly order themselves
`After=stereotool.service`. `deploy/stereotool.service.example` is now a
genericized reference template (placeholders for user/group/paths/binary)
documenting how IsadoraAir expects such a processor to be supervised,
without implying StereoTool ships with IsadoraAir. The real, live,
station-specific unit is protected as backup/DR evidence via the
`stereotool.service` entry in `deploy/backup_isadoraair.sh`'s live-unit
loop.

## Companion projects

`syndicated-ingest/`, `weather-ingest/`, and `ogremote-ingest/`
(`/home/jreed/*-ingest/`) are real, currently-scheduled production code
(20+ syndicated-show timers, weather polling, remote-content polling)
with **no version control at all** and no backup coverage. This is a
known, tracked gap — explicitly **not** addressed in this Phase 2 pass
(per its own scope boundary: these need a secrets/`.gitignore` audit
*before* `git init`, which is separate controlled follow-up work, not
done here). See each project's own git-readiness notes under
`docs/companion-projects/` for what that follow-up pass will need.

## Secret reprovisioning boundary

Every secret below is intentionally **not** in any backup or the repo.
For each: where production expects it, whether the backup contains it,
and where a human retrieves/recreates it if this box is lost.

| Secret | Purpose | Production location | In backup? | Reprovision from |
|---|---|---|---|---|
| `~/.iasboxbu.cred` | This backup script's own SFTP upload credentials | `$HOME/.iasboxbu.cred`, mode 0600 | No (intentionally — avoids circularity: it can't back up the credentials it needs to reach the backup destination) | **External reprovision source must be established** — not currently documented anywhere this audit found |
| `~/.syndicated_ingest.cred` | Syndicated-show fetch credentials, Bluesky app password | Companion project's own cred file | No | **External reprovision source must be established** |
| weather-ingest credentials | GW3000/weather API access | Companion project's own config | No | **External reprovision source must be established** |
| ogremote-ingest credentials | Remote-content polling auth | Companion project's own config | No | **External reprovision source must be established** |
| acme.sh / DNS-01 provider credentials | Let's Encrypt cert issuance for `radio.oakgroveradio.com` (IONOS DNS-01) | `~/.acme.sh/account.conf` / `acme.sh.env` | No | **External reprovision source must be established** — falls back to the self-signed cert (already backed up as part of the nginx config) if unavailable, so this isn't a hard blocker, just degraded HTTPS on the public hostname |
| StereoTool license | Unlocks the processor beyond any trial/demo limits | Bundled with the StereoTool install itself, not separately extracted/inspected by this audit | No | **External reprovision source must be established** — contact the vendor |
| `.env` secrets (`SECRET_KEY`, `DB_PASSWORD`, email credentials) | Django/DB/email auth | `.env`, mode 0600 | **Yes** — inside the app tar, as part of the normal backup | N/A unless the backup itself is unavailable, in which case: `SECRET_KEY` can be freshly generated (invalidates existing sessions, otherwise harmless), `DB_PASSWORD` must match whatever the restored/recreated PostgreSQL role actually has |

None of the "external reprovision source must be established" rows above
were resolved by this audit — they're recorded as open items rather than
guessed at, per the instruction not to invent a source that isn't
actually known.

## HE-AAC / fdkaac provenance

See `docs/HE_AAC_FDKAAC_PROVENANCE.md` for the full investigation. Short
version: `/usr/local/bin/fdkaac` (upstream `nu774/fdkaac`) and
`/usr/local/lib/libfdk-aac.so.2` (upstream `mstorsjo/fdk-aac`) are
custom source builds, required because Ubuntu's packaged versions have
HE-AAC/HE-AACv2 (SBR/PS) stripped for patent reasons. No build script or
documented build flags currently exist — that's the actual Phase 3 target,
not attempted here.

## Generic vs. Oak Grove-specific — summary

**Generic (repo/`deploy/`-appropriate):**
`deploy/backup_isadoraair.sh`'s framework (paths overridable via env
vars, no station specifics), `deploy/isadoraair.nginx` +
`deploy/isadoraair-locations.conf`, `deploy/stereotool.service.example`,
this document's structure and generic requirements, native-dependency
documentation (`docs/HE_AAC_FDKAAC_PROVENANCE.md`).

**Oak Grove production-specific (backup/documentation, not hardcoded
into generic defaults):** `radio.oakgroveradio.com` and its Let's
Encrypt certificate, the actual `/srv/isadoraair` disk/UUID, the current
`.sts` profile content, `.env`'s real values, the actual companion-ingest
credentials, the specific StereoTool license.
