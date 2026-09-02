# deploy/restore/ — IsadoraAir bare-machine restore tooling

Roadmap item 1.2, Phase 4 (2026-08-12). This directory turns the DR
assets Phases 1–3 produced (`deploy/backup_isadoraair.sh`,
`deploy/packages-ubuntu-26.04.txt`, `deploy/build_fdkaac.sh` +
`deploy/check_he_aac.sh`, `deploy/isadoraair-aloop.conf`,
`docs/DISASTER_RECOVERY.md`, `docs/RUNTIME_BASELINE.md`, the three
companion projects' own `requirements.txt`) into an actual, ordered,
runnable restore procedure — the thing Phase 5's bare-machine drill will
follow literally.

**This is tooling, not a magic button.** A handful of things are
deliberately manual (see "Manual checkpoints" in
`docs/DISASTER_RECOVERY_RESTORE.md`) — StereoTool's binary/license, the
music library disk, cert issuance, GitHub access. What's automated here
is everything *around* those: packages, code, database, native deps,
companion repos, config, and a read-only validation pass at the end.

Runtime Foundation D adds a GitHub-free fdkaac route without pretending the
current backup already contains its private source payload. Run stage 50 with
`--source-dir /path/to/native/fdkaac` (or `FDKAAC_SOURCE_DIR`) for immutable
local archives. If neither is supplied, stage 50 explicitly selects and warns
about optional connected acquisition; once local mode is selected it never
falls back to the network.

For the all-stage orchestrator, use the environment form because stage-specific
arguments are intentionally not accepted by unrelated stages:

```bash
FDKAAC_SOURCE_DIR=/path/to/native/fdkaac \
  deploy/restore/restore.sh --archive /path/to/backup.tar.gz --apply ...
```

## The three safety modes

Every stage script (`NN-*.sh`) and `inspect_backup.sh` sources
[`lib.sh`](lib.sh), which establishes one consistent flag vocabulary:

| Flag | Effect |
|---|---|
| `--plan` (default) | Never writes anything. Every stage prints what it WOULD do (`[PLAN]` log lines) and exits 0. Always safe, including against this very production box. |
| `--apply` | Actually performs the stage's writes. |
| `--staging-root PATH` | Redirects the target application root to `PATH/opt/isadoraair` instead of the real `/opt/isadoraair`, and the target database to `isadoraair_restore_test` instead of `isadoraair`. Combined with `--apply`, this is how a stage's real logic gets exercised without ever touching production — this is how Phase 4 itself validated the tooling (see "Phase 4 staging validation" below). |
| `--target-root PATH` | Overrides the target root directly (rarely needed outside `--staging-root`; mostly for a stage script calling another one internally). |
| `--archive PATH` | Points a stage at a specific backup archive (required by any stage that reads from one). |
| `--db-name NAME` | Overrides the target database name directly. |
| `--force-production-target` | Required *in addition to* `--apply` before any stage will write to the real `/opt/isadoraair` — see "Production-target protection" below. |
| `--force-db` | Required before `30-postgresql.sh` will `pg_restore` over a database that already has tables in it. |
| `--force-env` | Required before `20-application.sh` will overwrite an existing non-empty `.env` at the target. |

`--staging-root ... --apply` and bare `--plan` are the two modes Phase 4
itself used. Bare `--apply` with no `--staging-root` is what Phase 5's
actual clean-machine drill will use — on a genuinely clean box, the
production-target guard below never even triggers, because a clean box
has no IsadoraAir systemd units loaded yet.

### Production-target protection

`--apply` with no `--staging-root` and a target root of `/opt/isadoraair`
(the default) is refused outright if this host currently has
`isadoraair-gunicorn.service` or `isadoraair-engine.service` **loaded**
(not necessarily running — `systemctl show -p LoadState`, no root
needed) — i.e. it looks like it's already a live IsadoraAir install.
`--force-production-target` overrides this for the one case where it's
legitimate: Phase 5's real bare-machine drill, run on a host where
`/opt/isadoraair` genuinely is the intended target and nothing is loaded
there yet (so the guard wouldn't even trigger) — the flag exists for
re-running the *later* stages of a restore that's already partially
progressed far enough to have units loaded, not for casually overriding
safety on a developer's own box. See `lib.sh`'s `guard_production_target`
for the exact logic.

Two more guards apply regardless of the above:
- **`.env` overwrite** (`guard_env_overwrite`): refused if the target
  already has a non-empty `.env`, unless `--force-env`.
- **Database overwrite** (`guard_db_overwrite`): refused if the target
  database already has any tables in its `public` schema, unless
  `--force-db`. An empty, freshly-`CREATE DATABASE`'d shell (exactly
  what `30-postgresql.sh`'s own bootstrap step produces) does NOT count
  as non-empty, so the normal create-then-restore flow never needs the
  flag.

**The music library is different: there is no override, ever.** No
stage writes to `/srv/isadoraair/music` under any flag combination —
`guard_never_touch_music_library` is a hard-coded refusal with no
`--force-*` escape hatch. See `docs/DISASTER_RECOVERY.md`'s "Music
library" section for why: the 717+ GB library is explicitly out of
scope for this tooling, permanently, not just for this phase.

## Stage layout

```
deploy/restore/
  README.md              This file.
  lib.sh                 Shared logging/mode/guard helpers (sourced, not run directly).
  inspect_backup.sh       Standalone archive validator -- also called by 00-preflight.sh.
  00-preflight.sh         OS check, archive validation, mode/target resolution.
  10-packages.sh          OS package bootstrap (deploy/packages-ubuntu-26.04.txt).
  20-application.sh       Git clone/SHA checkout, .env + app-tree restore from app.tar.gz.
  30-postgresql.sh        PG bootstrap + pg_restore.
  40-station-content.sh   /srv/isadoraair reconstruction (carts/voicetracks/waveforms/etc).
  50-native-deps.sh       Backup-based DR: delegates to Foundation E4's real prepare/
                          publish authority using the embedded recovery payload.
                          Explicit connected install: unchanged HE-AAC exact-archive
                          build (builder performs shared validation). Runs AFTER
                          60-python.sh despite the numbering -- see the dependency
                          map below.
  60-python.sh            IsadoraAir venv creation + requirements.txt + safe checks.
  70-tts.sh               Backup-based DR: delegates to Foundation E3's real
                          provisioning authority using the embedded recovery
                          payload. Explicit connected install (--legacy-connected-
                          install): unchanged Kokoro + Piper venv/pip provisioning
                          + smoke test.
  75-protected-updater.sh Backup-based DR only (no connected-install path exists
                          for this component): restores the Phase-D protected
                          updater component from the embedded recovery payload
                          -- offline, non-privileged verification into a
                          throwaway fake root, then publishes onto the real/
                          staging target (root-owned via sudo for a real
                          target, matching 90-system-config.sh's own USE_SUDO
                          idiom). Never starts/enables/reloads anything;
                          activation stays a separate, privileged step.
  80-companions.sh        syndicated-ingest/weather-ingest/ogremote-ingest clone+venv.
  90-system-config.sh     nginx + systemd install/validation (never starts/reloads);
                          also establishes Runtime Foundation E5's system
                          surfaces (installed launcher, canonical
                          runtime/data directories, both tmpfiles
                          configs at their correct, distinct
                          destinations) -- see docs/RUNTIME_DEPLOY_BASELINE.md.
  95-validate.sh          Post-restore validation. Canonical / receives
                          Django/live-station/migration checks; a staging
                          root receives target-mapped structural/filesystem
                          validation only (Runtime Foundation E6 --
                          docs/RUNTIME_DEPLOY_BASELINE.md).
  restore.sh              Orchestrator -- runs 00 through 95 in order, same flags passed through.
```

Numbered so `ls deploy/restore/*.sh` sorts in execution order. Each
stage is independently runnable (`./NN-foo.sh --plan ...`) as well as
callable from `restore.sh` — useful for re-running just one stage after
fixing something Phase 5 finds, without re-running everything before it.
Stages are additive/idempotent where practical (re-running a stage that
already completed should verify and no-op, not fail) — see each script's
own header for its specific idempotence behavior.

This is the structure Phase 4 spec section 4's example suggested,
adopted essentially as-is — it already matched how `deploy/`'s existing
files are organized (one file per concern, numbered timers aside), so
there was no reason to deviate.

## Restore-order dependency map

Based on `docs/RUNTIME_BASELINE.md`'s general map, with one deliberate
deviation — see the 2026-08-29 note below:

```
00-preflight   OS check, archive validation
  v
10-packages    apt packages (deploy/packages-ubuntu-26.04.txt)
  v
20-application Git clone + SHA checkout + .env/app-tree restore
  v
30-postgresql  PG bootstrap + pg_restore
  v
40-station-content  /srv/isadoraair carts/voicetracks/waveforms/etc
  v
60-python      IsadoraAir venv + requirements.txt
  v
50-native-deps HE-AAC (fdkaac/libfdk-aac)
  v
70-tts         Kokoro + Piper
  v
75-protected-updater  Phase-D protected updater (backup-based DR only)
  v
80-companions  syndicated-ingest/weather-ingest/ogremote-ingest
  v
90-system-config  nginx + systemd units installed, NOT started
  v
95-validate    canonical live checks OR offline target structural checks
```

**2026-08-29, Runtime Foundation E7B:** 60-python now runs *before*
50-native-deps, reversing the numeric order the stage filenames imply.
`docs/RUNTIME_BASELINE.md`'s general statement that "fdkaac/Kokoro/
Piper/snd-aloop can be built/installed any time before the services
that need them start... none of them are Python-venv dependencies"
remains true for the **generic/connected-install** path (plain
`build_fdkaac.sh --source-dir`/`--download-sources`, still exactly what
50-native-deps.sh falls back to when no `--archive` is given, or
`--source-dir`/`--download-sources` is passed explicitly — see that
script's own header). It is deliberately **not true** for
**backup-based disaster recovery**: that path now delegates to Runtime
Foundation E4's real prepare/publish authority via `manage.py
provision_runtime_components --fdkaac`, which needs the restored app's
Django environment to run at all. 60-python.sh has no dependency on
50-native-deps and is idempotent (verifies rather than recreates an
existing venv), so pulling it earlier costs nothing when the chain
reaches its usual numeric position again. The stage file **numbers**
remain stable identifiers (`ls` sort order, individual invocation) —
they no longer imply a strict execution order on their own; `restore.sh`
is the authority for the actual order it runs stages in.

**r0030:** `75-protected-updater.sh` runs immediately after `70-tts.sh`
and before `80-companions.sh` — the same app-source/venv prerequisite
70/50 already have (`manage.py restore_phase_d_component` runs as a
manage.py command), no dependency on 30/40/80/90, and nothing after it
can invalidate its runtime-recovery receipt entry before `95-validate.sh`
checks it. Its stage NUMBER (75) is likewise a stable identifier only —
`restore.sh`'s `STAGES` array is what actually places it there.

`docs/DISASTER_RECOVERY_RESTORE.md` picks up from here for the parts
that stay manual (StereoTool binary/license, controlled service
bring-up order, certs, music library) — this map is the automated
portion only.

## Runtime recovery payload (Runtime Foundation E7B)

A self-contained backup-v3 archive is identified by
`runtime-recovery-archive.json` as format 3.0.0 / `self_contained_v3`;
that classification positively requires a policy-satisfying
`runtime-recovery/` directory — an operator-prepared, already-validated
Runtime Foundation E7 disaster-recovery payload (see
`docs/RUNTIME_BACKUP_PAYLOAD.md`). Stages 50, 70, and (r0030) 75 are the
only three consumers, and none locates it independently: all three call
`lib.sh`'s `restore_locate_recovery_payload`, the one shared contract
for "where did this archive's payload end up." See that function's own
header comment for the exact extraction/confinement behavior, and
`docs/DISASTER_RECOVERY_RESTORE.md`'s "Runtime recovery payload" and
"Backward compatibility" sections for the operator-facing picture
(including why a pre-E7B/non-self-contained archive fails the default
backup-based stages and requires an explicit connected/manual path; a
schema-1 payload with no `protected_updater` component is likewise a
clean no-op for stage 75 specifically, never a failure).

## Application-source recovery model (Git vs. `app.tar.gz`)

Answered explicitly, since the backup contains both a Git-clonable repo
*and* a tarball of the actual on-disk tree, and naively overlaying one
onto the other is exactly how a restore resurrects stale files:

1. **Git is the source of code.** `20-application.sh` clones
   `celltech161/IsadoraAir` fresh, then checks out the exact SHA
   recorded in the backup's `MANIFEST.txt` (`IsadoraAir Git SHA:` line)
   — never `main`, even though `main` will usually be at or ahead of
   that SHA. A backup taken against an older commit restores that older
   commit's code, matching the database dump it was taken alongside;
   moving forward from there (if desired) is a deliberate, separate
   `git pull`/migrate step after the restore is verified working, not
   implied by the restore itself.
2. **`app.tar.gz` is the source of everything Git doesn't track.** After
   the SHA checkout, exactly two things are pulled from `app.tar.gz`:
   `.env` (the real, station-specific secrets/config) and `media/`
   (UI Theme logos etc., see `docs/DISASTER_RECOVERY.md`'s coverage
   table). Nothing else in `app.tar.gz` is used — it also contains
   `manage.py`, every `.py` file, templates, and so on (the backup
   script tars the whole checkout minus `.git/`/`venv/`/caches), but
   restoring code from the tarball would silently reintroduce
   uncommitted local changes or drift the tree away from the Git SHA
   `MANIFEST.txt` says the backup matches. The tarball's code is
   redundant with Git by design (see `deploy/backup_isadoraair.sh`'s own
   header comment) — Git is what this tooling trusts for it.
3. **`.env.bak`/`.env.lock` are never restored**, even if they somehow
   ended up in a given archive (shouldn't happen — the backup script
   excludes them, and `inspect_backup.sh` flags it as a WARN if it ever
   does) — `20-application.sh` extracts `.env` by exact name only, never
   a glob that could catch its siblings.
4. **SHA verification, not blind trust**: `20-application.sh` runs
   `git cat-file -e <sha>^{commit}` against the freshly-cloned repo
   before checking it out, and fails clearly (rather than silently
   falling back to `main`) if the recorded SHA isn't reachable — e.g. a
   force-push rewrote history, or the manifest is corrupt.
5. **Dirty-tree state**: `MANIFEST.txt` as currently written
   (`deploy/backup_isadoraair.sh`) does not record whether the tree was
   clean at backup time. This is a real, acknowledged limitation, not
   silently assumed away — `20-application.sh` documents it in its own
   output, and `docs/DISASTER_RECOVERY_RESTORE.md` repeats it as a known
   gap rather than a solved problem. A future backup-script enhancement
   (recording `git status --porcelain` output, or refusing to back up a
   dirty tree at all) is the right fix; out of scope for this pass.

## Logging

Every stage logs to stdout/stderr via `lib.sh`'s `log_info`/`log_warn`/
`log_error`/`log_plan`/`log_apply` — plain text, UTC timestamps, safe to
redirect to a file and keep for troubleshooting. **No stage ever logs a
secret value** — `.env` contents, database passwords, credential-file
contents. Where a stage needs to reference that such a value exists
(e.g. confirming `.env` was restored), it names the *key*, never the
value (`lib.sh`'s `redact()` helper exists as a reminder/placeholder for
this, though the actual discipline is "just don't print the variable").

## Phase 4 staging validation

Every stage in this directory was exercised via `--staging-root
/tmp/isadoraair-restore-test --apply` (or `--plan` where a real external
dependency made `--apply` impractical to run twice, e.g. re-cloning
large companion repos) against this box, without ever touching
`/opt/isadoraair`, the live `isadoraair` database, or any live service —
see the Phase 4 completion report's "Staging validation" section for the
exact commands and results per stage.

For Runtime Foundation E6, stage 95 does not borrow a healthy installer
host to validate a mounted target. It passes the staging root explicitly
to `check_deploy_baseline --structural-only`; missing target launchers,
E5 directories, either tmpfiles configuration, unsafe scratch ancestry,
or an unresolved target service identity fail the staged acceptance.
This does not claim a fully offline runtime restore: stages 50/70 remain
pre-Foundation-E payload consumers until E7.
