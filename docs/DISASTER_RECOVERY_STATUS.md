# Disaster recovery status ledger (P0 1.2)

**This is the authoritative handoff/status document for P0 1.2 (bare-metal
disaster recovery).** Read this FIRST before investigating any E8
acceptance-run failure or planning the next restore-tooling release --
it exists specifically so a genuine defect already found, root-caused,
and fixed is never re-discovered from scratch by a later session. It is
updated at the end of every formal release (rXXXX) that touches
`deploy/restore/` and at the end of every authoritative E8 acceptance
run, whether that run passes or fails.

See `docs/DISASTER_RECOVERY.md` (what's backed up and why) and
`docs/DISASTER_RECOVERY_RESTORE.md` (the operator-facing restore
procedure) for the underlying material this status ledger tracks
against.

---

## Current state (as of r0043)

| Field | Value |
|---|---|
| Current production release | r0043, commit `<filled in at release time -- see bottom of this file>` |
| Previous production release | r0042, commit `1f3cd2a524be94bd1f2c4657a5a2b91bc1e684f0` |
| Current E8 acceptance candidate | r0043 |
| Last authoritative E8 result | r0042 -- **FAIL at Stage 80** (see incident record below) |
| Last completed stage (authoritative) | 75-protected-updater (PASS) |
| Failure stage (authoritative) | 80-companions |
| Failure classification | **Product** (a real cross-stage restore defect in IsadoraAir's own restore tooling -- not a runner/harness defect, not sandbox contamination; independently confirmed by the run's own clean-baseline gate) |

### Proven stages (authoritative E8, r0042 run)

00-preflight, 10-packages, 20-application, 30-postgresql, 40-station-content,
60-python, 50-native-deps, 70-tts, 75-protected-updater — all PASS.

### Unproven stages (authoritative E8, r0042 run)

80-companions (FAILED), 90-system-config, 95-validate — never reached.

### Open blockers

None currently known, pending the r0043 acceptance run (see "Next single
action" below) proving Stage 80 → 90 → 95 on the existing partially
restored E8 sandbox via the new `--resume` mechanism.

### Next release

r0043 has been implemented and is pending its own E8 acceptance run (see
"Next single action"). Do **not** produce a fresh E8 export until that
acceptance run's result is reviewed.

### Next single action

Resume the EXISTING partially-restored E8 sandbox (the one whose r0042
run completed through Stage 75 and failed at Stage 80 -- do **not** wipe
it) using r0043's restore tooling and its `--archive` pointed at a fresh
r0043 backup, with `--resume` on every stage from 00 onward:

```bash
deploy/restore/restore.sh --archive /path/to/r0043-backup.tar.gz --apply --resume
```

Expected: 00 through 75 verify their own already-durable r0042 output
and converge without destructively re-running (Stage 20 does NOT
re-clone/re-extract .env; Stage 30 does NOT re-run pg_restore); Stage 40
re-normalizes `WEATHER_DATA_DIR` in the already-restored `.env` (a
no-op if it's already canonical, otherwise fixes it in place) and
records itself in the new restore ledger; Stage 80, seeing durable
ledger proof that Stage 40 already ran for this exact archive/target,
recognizes `/home/jreed/weather-ingest` as the known r0042-era
legacy-scaffold defect's own artifact (empty directory tree, no `.git`,
no regular files anywhere) and repairs it before cloning; Stage 90 and
95 proceed normally to PASS. See "Incident: Stage 80 weather-ingest
collision (r0042)" below for the full root-cause record, and
`deploy/restore/lib.sh`'s `restore_ledger_*` functions /
`deploy/restore/restore_ledger.py` for the resume mechanism itself.

---

## Important defects already solved -- do not rediscover

These are closed. If a future E8 run appears to hit one of these again,
suspect a REGRESSION (bisect against the release that introduced it),
not a fresh discovery -- read the cited section/commit first.

| # | Defect | Fixed in | One-line summary |
|---|---|---|---|
| 1 | `createuser --pwprompt` reads the wrong input under a real controlling TTY (blocks on /dev/tty, not stdin) | r0040 | Role password set via a private mode-0600 temp file + static SQL, never `--pwprompt` |
| 2 | Backup's `runtime-recovery/` archive root at umask-dependent 0775 instead of 0755 | r0041 | Explicit `chmod 0755` + new `verify-extractable` pre-upload gate |
| 3 | Canonical (non-staging) native fdkaac/TTS publication ran unprivileged, refused by E4/E3 | r0041 | Stage 50/70 split into unprivileged prepare + sudo-escalated publish |
| 4 | Recovery receipt directory (`/var/lib/isadoraair/restore`) can't be created unprivileged on a fresh machine | r0041 | `lib.sh` establishes it narrowly via sudo before the unprivileged receipt write |
| 5 | Stage 75's `--fake-root` ends up root-owned inside the caller's own unprivileged WORKDIR for a real publish, so the caller's EXIT trap can't remove it even after success | r0042 | `restore_phase_d_component` removes its own fake-root itself, on every path |
| 6 | Stage 80 logged a companion-repo failure but always exited 0 regardless | r0042 | `ANY_FAILED` tracking; stage now fails closed |
| 7 | `install_rendered()` (Stage 90) propagated `mktemp`'s 0600 mode onto real `/etc` destinations | r0042 | `install -m 0644`, deterministic, independent of umask |
| 8 | Stage 90: missing self-signed TLS cert, and `nginx -t` failure never failed the stage | r0042 | Cert generated deterministically; `nginx -t` failure now fails the stage closed |
| 9 | `deploy_baseline.py`: snd-aloop live-module-state and TTS-scratch-surface pre-boot absence hard-failed Stage 95 even though Stage 95's own acceptance point is pre-boot/pre-service-activation | r0042 | Both now deferred/non-gating; their STRUCTURAL declarations (modprobe.d config, tmpfiles.d config) gate instead |
| 10 | `DeploymentBaselineEvidence.result` stayed UNRESOLVED forever when a resolved station tier had already superseded the exact structural package-selection uncertainty that caused it | r0042 | Narrowly superseded; identity ambiguity and every other structural FAIL still always gate |
| 11 | **Stage 80 weather-ingest collision** -- see full record below | r0043 | `WEATHER_DATA_DIR` legacy-value normalization (Stage 40) + `--resume`/ledger scaffold repair (Stage 80) |

---

## Incident: Stage 80 weather-ingest collision (r0042)

**Confirmed root cause.** The restored production `.env` carries a
legacy `WEATHER_DATA_DIR` value pointing INSIDE the weather-ingest
companion project's own source-checkout namespace
(`$HOME/weather-ingest/data` in the confirmed case -- this is also this
production host's own actual, currently-live configuration; see
"Deferred post-acceptance work" below). `weather/services.py` resolves
`settings.WEATHER_DATA_DIR` and calls `DATA_DIR.mkdir(parents=True,
exist_ok=True)` at Django **module import time**. Stage 60
(`60-python.sh`) runs `manage.py check` as its own verification step --
the FIRST Django-importing operation anywhere in the restore sequence --
which loads the full URLconf, which imports `weather.views`, which
imports `weather.services` at module level, which then manufactures the
entire `$HOME/weather-ingest/data` directory tree on disk, including
the `weather-ingest` parent itself, purely as an import side effect,
with zero files inside it. Stage 80 (`80-companions.sh`) runs later and
refuses to clone into that pre-existing, non-empty, non-Git directory --
correctly, since nothing about that refusal itself was ever wrong; the
directory it found was genuinely non-Git and IsadoraAir's own restore
tooling genuinely put it there.

**Confirmed authoritative evidence (r0042 E8 run, clean Ubuntu 26.04.1,
network-isolated):**

- Clean-baseline gate confirmed `/home/jreed/weather-ingest` did NOT
  exist before the numbered restore began.
- Stage sequence: 00, 10, 20, 30, 40, 60, 50, 70, 75 all PASS; 80 FAILED
  with `/home/jreed/weather-ingest exists, is non-empty, and is not a
  Git checkout. Refusing to clone into it.`
- Forensics after failure: `/home/jreed/weather-ingest` birth time
  `2026-09-08T00:37:16Z`; contents exactly `weather-ingest/` and
  `weather-ingest/data/` (both empty); both `jreed:jreed` mode 0755; no
  `.git` anywhere in the tree.
- Restored `.env` contained exactly `WEATHER_DATA_DIR=/home/jreed/weather-ingest/data`.

**Reproduced directly from source** (not merely inferred): a real,
unmocked `weather.services` import with `WEATHER_DATA_DIR` set to a
disposable temp-dir equivalent of the legacy value manufactures the
exact same empty-directory-tree, no-`.git`, zero-regular-files
signature Stage 80 then refuses to clone into.

**Fix (r0043), at the correct architectural boundary:**

1. **`deploy/restore/40-station-content.sh`** (not `20-application.sh`
   -- that stage's own `.env` remains byte-faithful, unmodified, exactly
   as documented; this stage already reads `.env` non-destructively for
   the analogous `REPORTS_ROOT` establishment, and runs after Stage 20
   / before Stage 60, which is the actual hard requirement here): a new
   section reads the just-restored `.env`'s `WEATHER_DATA_DIR`. If, and
   only if, it resolves inside `<companions-default-root>/weather-ingest/`
   (the exact, known legacy namespace -- `restore_default_companions_root`
   in `lib.sh`, the SAME default `80-companions.sh` itself resolves
   `COMPANIONS_ROOT` from), the `.env` is rewritten in place -- ONLY that
   one key, every other key byte-for-byte untouched -- to the canonical
   `/var/lib/isadoraair/weather`. Any other value (the current canonical
   default, or a genuine operator-chosen custom path outside that one
   namespace) is left completely alone. The resulting directory
   (canonical or custom) is then established with owner
   `$(id -un):$(id -gn)` and explicit, deterministic mode 0755 (never
   umask-dependent).
2. **`deploy/restore/lib.sh` / `deploy/restore/restore_ledger.py`**: a
   new restore-session ledger (see "Resumable restore mechanism"
   below), so a machine that already has the PRE-r0043 damage sitting on
   disk (like the one this incident's own E8 run left behind) doesn't
   have to be wiped to prove the fix.
3. **`deploy/restore/80-companions.sh`**: a narrowly-scoped,
   `--resume`-gated repair -- see below. Stage 80's own non-Git-collision
   refusal is **unchanged** and still fires for every other case.

**Deferred post-acceptance work (explicitly NOT done by r0043, and not
required for E8 software acceptance):** this production host's own
currently-live `.env` still carries the legacy `WEATHER_DATA_DIR` value,
and `/home/jreed/weather-ingest/data` is genuinely the live,
actively-written data directory the real weather-ingest process uses
right now (confirmed: files there have recent mtimes). r0043
deliberately does not touch production's own live `.env` or move that
live data -- that is a live-service-affecting operational change (data
migration + a `.env` edit + a gunicorn restart), out of scope for
restore-time tooling and not requested. `/var/lib/isadoraair/weather`
already exists on this host (pre-dating r0043, currently empty) as the
canonical target for whenever that migration is eventually done
manually. Tracked here so it is not confused with the restore-tooling
fix itself.

---

## Resumable restore mechanism (r0043, first implementation step)

**Ledger.** `deploy/restore/restore_ledger.py` owns one small JSON file,
`/var/lib/isadoraair/restore/ledger.json` (or the equivalent path under
`--staging-root`) -- the SAME directory the Runtime Foundation E7
recovery receipt already lives in. It binds one restore session to:

- `archive_sha256` -- the exact backup archive's own SHA256.
- `target_root` -- the resolved restore target.
- `git_sha` / `payload_id` / `product_contract_sha256` -- recorded
  opportunistically by the stages that already know them (never a
  secret).
- `started_at` / `updated_at` -- UTC timestamps.
- `stages` -- a map of stage name -> `{state: "complete", completed_at, detail}`.

Identity is exactly the pair `(archive_sha256, target_root)`. Every
ledger operation fails closed (nonzero exit, clear message) if an
EXISTING ledger's identity does not match the current invocation's --
a different archive (or a different target root) never silently
inherits a prior session's ledger; a corrupt/schema-invalid/incomplete
existing ledger is the same, never silently discarded and overwritten.
No ledger at all is not itself an error -- it just means there is
nothing yet to resume.

**`--resume` flag.** Recognized by every stage via
`restore_parse_common_args` (so `restore.sh --resume` threads it through
automatically, exactly like `--apply`/`--staging-root` already do).
Concrete behavior added in r0043:

- **20-application.sh**: with `--resume` and a ledger recording this
  stage already complete for the exact current archive/target, the
  stage independently VERIFIES (checked-out Git HEAD actually equals
  the archive's own recorded SHA; `.env` actually present and
  non-empty) rather than trusting the ledger alone, then skips the
  clone/`.env`-extraction and exits PASS. A verification MISMATCH
  (ledger says done, filesystem disagrees) is a precise, fail-closed
  ambiguity error, never silently resolved either way. Without a
  matching completed ledger entry, behavior is 100% unchanged from
  before r0043 (an existing non-empty `.env` still requires
  `--force-env`).
- **30-postgresql.sh**: the same pattern, verifying table count > 0 and
  `django_migrations` present via a direct query (the same evidence the
  stage's own post-restore check already establishes) instead of
  re-running `pg_restore`. Without a matching completed ledger entry, a
  non-empty database still requires `--force-db`, unchanged.
- **80-companions.sh**: a narrowly-scoped, provenance-checked scaffold
  repair -- see the incident record above for the exact conditions
  (empty directory tree signature + matching ledger proof that Stage 40
  already normalized `WEATHER_DATA_DIR` for this exact archive/target).
  Every other pre-existing-content case -- a real file anywhere in the
  tree, an existing `.git`, no `--resume`, or no matching ledger
  provenance -- is completely unaffected and still fails exactly as
  before r0043.
- **Every stage** now records its own completion into the ledger
  unconditionally (harmless bookkeeping, `--resume` or not) -- this is
  what lets a LATER stage in the same or a later invocation ask "did an
  earlier stage already durably complete, for this exact archive?"

**Explicitly NOT solved by r0043** (first implementation step, not the
final system): 00-preflight/10-packages/60-python/50-native-deps/
70-tts/75-protected-updater/90-system-config/95-validate were not given
new `--resume`-specific skip logic, because each was already safe to
re-run (idempotent verify-rather-than-recreate venv/package/role
bootstrap, or an existing durable Foundation-E component receipt) --
only 20/30 actually hard-failed on legitimate pre-existing content
before r0043, and only 80 needed a repair mechanism at all. A more
general per-stage "verify durable output, converge, or fail with a
precise diagnostic" contract, and ledger-recorded verification detail
beyond bare stage completion, remain future work.

---

## Format notes for future updates to this file

- Update the "Current state" table and "Next single action" at the end
  of every formal release touching `deploy/restore/`, and after every
  authoritative E8 run (pass or fail).
- Add a new row to "Important defects already solved" for every genuine
  product defect fixed, however small -- this table's entire purpose is
  making a re-discovery cheap to rule out.
- Keep incident records (like the one above) even after the defect they
  describe is fixed -- they are the evidence trail a future regression
  bisect needs.
