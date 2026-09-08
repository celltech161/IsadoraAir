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

## Current state (as of r0045)

| Field | Value |
|---|---|
| Current production release | r0045 (see `git log -1` / `deploy/releases/r0045.json` for the exact commit -- never hardcoded here, since a commit cannot record its own future SHA) |
| Previous production release | r0044 |
| Current E8 acceptance candidate | r0045 |
| Last authoritative E8 result | r0042 -- **FAIL at Stage 80** (see incident record below -- still the last AUTHORITATIVE run; not yet superseded by a newer one) |
| Last completed stage (authoritative) | 75-protected-updater (PASS) |
| Failure stage (authoritative) | 80-companions |
| Failure classification | **Product** (a real cross-stage restore defect in IsadoraAir's own restore tooling -- not a runner/harness defect, not sandbox contamination; independently confirmed by the run's own clean-baseline gate) |

### Review finding (r0044 → r0045)

r0044's interactive Adopt/New/Show/Quit workflow made the ledger/adoption
gap usable, but it still broadcast the same `COMMON_ARGS` to every stage
except the pre-existing `--isa-user`/`--isa-uid`/`--isa-gid` routing --
it had no way to express the authoritative offline-E8 invocation
contract's stage-specific frozen-media inputs (Stage 10's local
apt/snap closure, Stage 20's local Git mirror, Stage 60/80's offline pip
wheelhouse, Stage 80's local companion Git mirrors). r0045 closes this
with ONE recovery-media root concept (discovered interactively, or
`--recovery-media-root PATH` for automation) instead of six separate
flags, and restructures `restore.sh` to build per-stage argument/env
arrays instead of broadcasting. See "Recovery media (r0045)" below. The
r0043/r0044 ledger/adoption design itself is unchanged by this release.

### Review finding (r0043 → r0044)

r0043's own `--resume` only converges a stage the ledger ALREADY records
complete for the exact current archive/target. The actual r0042 E8
sandbox's restore PREDATES the ledger entirely (r0043 didn't exist yet
when it ran) -- it has zero ledger entries, so plain `--resume` against
it falls through to ordinary (destructively-guarded) restore behavior
and hits `guard_env_overwrite`/`guard_db_overwrite` exactly as if
nothing had ever been restored. r0044 closes this gap with a distinct,
narrowly-scoped **pre-ledger adoption** mechanism -- see "Resumable
restore mechanism" below for the full algorithm. This is the reason two
formal releases (r0043, then r0044) were needed to close one real E8
defect: r0043 fixed the root cause and built the ledger/resume
scaffolding; r0044 makes that scaffolding actually able to recognize
and adopt the specific machine the defect was found on.

### Proven stages (authoritative E8, r0042 run)

00-preflight, 10-packages, 20-application, 30-postgresql, 40-station-content,
60-python, 50-native-deps, 70-tts, 75-protected-updater — all PASS.

### Unproven stages (authoritative E8, r0042 run)

80-companions (FAILED), 90-system-config, 95-validate — never reached.

### Open blockers

None currently known, pending the r0045 acceptance run (see "Next single
action" below) proving Stage 80 → 90 → 95 on the existing partially
restored E8 sandbox via the pre-ledger adoption mechanism, this time
driven fully offline through r0045's recovery-media discovery.

### Next release

r0045 has been implemented and is pending its own E8 acceptance run (see
"Next single action"). Do **not** produce a fresh E8 export until that
acceptance run's result is reviewed.

### Next single action

Resume the EXISTING partially-restored E8 sandbox (the one whose r0042
run completed through Stage 75 and failed at Stage 80 -- do **not** wipe
it, and do **not** manually delete `/home/jreed/weather-ingest`) using
r0045's restore tooling, transferred to the sandbox through the allowed
operator-PC management path, and the SAME frozen r0042 backup archive
that produced the existing state
(`isadoraair-backup-20260907-181431-formal-r0042.tar.gz`, SHA256
`e40d33d747b8e55f690be75f275c150dc9df53688985dd6f9c03830f89cbeef8` --
**not** a fresh backup, which would be a different archive identity and
could never adopt this machine's existing state). The sandbox already
has the matching r0042 frozen media (`e8-inputs/`, containing the
backup, apt/snap closures, wheelhouse, and the IsadoraAir + companion
Git mirrors) staged locally -- see "Recovery media (r0045)" below.
Launch the normal interactive workflow (a real terminal, `--apply`, no
flags):

```bash
deploy/restore/restore.sh --archive /path/to/isadoraair-backup-20260907-181431-formal-r0042.tar.gz --apply
```

Expected: restore.sh first resolves recovery media -- discovering the
local `e8-inputs` root automatically (or prompting/listing candidates if
it can't find exactly one) -- and shows what it found before anything
else happens. Then: no matching ledger exists yet (this restore
predates it), but the target already shows restore progress (a real
`.git` checkout, a non-empty `.env`) -- the interactive workflow detects
this and offers `[A] Verify and adopt this interrupted recovery`.
Choosing it independently verifies Stages 20/30's durable output (Git
HEAD, `.env`, database content) against this exact archive and, only
once proven, records them complete in a freshly-created ledger -- no
`--force-env`, no `--force-db`. Stage 10 then runs safely/idempotently
against the resolved recovery media's local apt/snap closure (no
Internet). Stage 40 then runs for real (never needed adoption --
always idempotent), re-normalizing `WEATHER_DATA_DIR` in the
already-restored `.env` in place. Stages 50/70/75's own existing
runtime-recovery receipts (already durable evidence from the original
r0042 run, entirely independent of the ledger) let them adopt too
without republishing anything. Stage 80, seeing durable ledger proof
that Stage 40 already ran for this exact archive/target, recognizes
`/home/jreed/weather-ingest` as the known r0042-era legacy-scaffold
defect's own artifact (empty directory tree, no `.git`, no regular
files anywhere) and repairs it before cloning -- provisioning every
companion from the resolved recovery media's local Git mirrors (no
manual deletion, no Internet). Stage 60's convergence check, and Stage
80's own pip install, both run constrained to the recovery media's
wheelhouse (`PIP_NO_INDEX=1`). Stage 90 and 95 proceed normally to PASS,
95 gated on the actual r0042 backup receipt. E8 network isolation
remains intact throughout. See "Incident: Stage 80 weather-ingest
collision (r0042)" below for the full root-cause record, "Resumable
restore mechanism" for the complete adoption algorithm and interactive
workflow, and "Recovery media (r0045)" for the offline-media contract.

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
| 12 | r0043's `--resume` could not help a restore that BEGAN before the ledger existed at all (e.g. the actual r0042 E8 sandbox) -- it has no ledger entries, so `--resume` fell through to ordinary destructive-guarded behavior and still hit `guard_env_overwrite`/`guard_db_overwrite` | r0044 | New `--adopt-pre-ledger` mechanism: each stage independently verifies pre-ledger durable output against the supplied archive and, only if proven, adopts it into a freshly-created ledger -- never inferred from mere file existence. Plus: an interactive TTY workflow so an operator never needs to know these flags exist. See "Resumable restore mechanism" below. |
| 13 | r0044's interactive workflow broadcast identical `COMMON_ARGS` to every stage (aside from the pre-existing identity routing), so it could not express the authoritative offline-E8 invocation contract's stage-specific frozen-media inputs (Stage 10 apt/snap closure, Stage 20 Git mirror, Stage 60/80 pip wheelhouse, Stage 80 companion mirrors) | r0045 | One recovery-media root concept (`recovery_media.py`'s `validate`/`discover`/`detect-apt-groups`, `--recovery-media-root`, or interactive discovery); `restore.sh` now builds per-stage argument/env arrays (`_restore_build_media_stage_args`) instead of broadcasting. See "Recovery media (r0045)" below. |

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

**r0043's own gap** (see "Review finding" above): a restore that began
before the ledger existed at all has no ledger entries whatsoever, so
`--resume` alone falls straight through to the "not yet recorded
complete" branch and hits today's ordinary `guard_env_overwrite`/
`guard_db_overwrite` refusals -- indistinguishable, to r0043's own
`--resume`, from a genuinely unrelated pre-existing installation.

## Pre-ledger adoption (r0044)

**`--adopt-pre-ledger` flag**, combined with `--resume`. Where
`--resume` alone only converges a stage the ledger ALREADY records
complete, `--adopt-pre-ledger` additionally allows a stage whose ledger
state is `absent` to independently verify a PRE-ledger restore's
durable output against the supplied archive and, only if that
verification proves it, adopt it: record the stage complete in a
freshly-created ledger without redoing the underlying work. Completion
is NEVER inferred merely because files exist.

**Per-stage adoption evidence (the actual algorithm):**

- **20-application.sh**: real Git checkout (`$TARGET/.git` exists),
  HEAD exactly equals the archive's own recorded Git SHA, `.env` exists
  and is non-empty, AND `git status --porcelain --untracked-files=no`
  is clean (no uncommitted changes to TRACKED files -- untracked
  output from later stages, e.g. `venv/`, is fine and ignored). Any
  failure here is a fail-closed adoption error with a precise
  diagnostic -- never a silent fallback.
- **30-postgresql.sh**: the target database is reachable with the
  restored credentials, has tables in its public schema, and
  `django_migrations` exists there -- the exact same evidence the
  stage's own post-`pg_restore` verification already establishes for a
  fresh restore. No stronger archive-to-database provenance signal is
  currently exposed by the backup/restore format itself (pg_dump does
  not embed an archive identity marker); this is the strongest evidence
  practically available today.
- **40-station-content.sh**: no adoption branch needed at all -- every
  operation here (directory establishment, srv-content extraction,
  `WEATHER_DATA_DIR` normalization) is already idempotent/convergent
  regardless of ledger state, so it always just runs for real. This is
  precisely what lets it re-normalize an adopted, still-legacy `.env`
  in place.
- **50-native-deps.sh / 70-tts.sh / 75-protected-updater.sh**: the
  EXISTING Runtime Foundation E7 runtime-recovery receipt
  (`/var/lib/isadoraair/restore/runtime-recovery.json`, written by
  `restore_record_recovery_components` on any real publish -- entirely
  independent of, and pre-dating, the r0043 ledger) already proves a
  component was recovered from a specific archive/payload identity. A
  new `runtime_recovery_archive.py verify-component-receipt` subcommand
  (wrapped as `restore_verify_component_receipt` in `lib.sh`) checks
  that receipt's `payload_id`/`archive_format_version` against the
  CURRENT archive's own embedded metadata and confirms the specific
  component this stage owns is listed recovered -- never merely "some
  receipt exists somewhere". This same evidence is used for BOTH
  `--resume`'s own "ledger already says complete" verification (fails
  closed on disagreement, since publish is genuinely NOT safe to
  blindly re-run -- `publish_phase_d_component`/
  `NativeRuntimeProvisioner.publish` both refuse to overwrite
  pre-existing destination content) and `--adopt-pre-ledger`'s "nothing
  in the ledger yet" case (falls through to a normal restore attempt if
  adoption can't be proven, relying on that same refuse-to-overwrite
  behavior as the fail-closed backstop for real pre-existing content).
  70-tts.sh's own check additionally requires EVERY TTS component
  (kokoro and/or piper) the archive declares to be individually
  receipt-proven, not just one.
- **80-companions.sh**: unchanged from r0043 -- its scaffold repair
  already keys off `restore_ledger_stage_state("40-station-content")`,
  which becomes `complete` the moment Stage 40 actually runs (adopted
  target or not), so no separate Stage-80-specific adoption logic was
  needed.
- **00-preflight.sh / 10-packages.sh**: no adoption logic -- both are
  either read-only (00) or purely additive/idempotent (10, installs
  only what `dpkg -s` reports missing), so they always just run for
  real regardless of ledger state; absence of an old ledger is never
  treated as contamination.

A different archive, or existing state that fails any of the above
verifications, always fails closed with a specific diagnostic -- never
a guess, and never silently treated as "must be fine, files exist."

## Interactive recovery workflow (r0044)

`restore.sh --apply`, run from a real terminal (stdin AND stdout both a
TTY), with neither `--non-interactive` nor `--resume` already given,
inspects the target root and any existing ledger BEFORE doing anything
destructive:

- **Ledger exists and matches this exact archive/target**: prints the
  archive SHA256, target, last completed stage, and last incomplete
  stage, then offers `[R] Resume recovery` (adds `--resume`),
  `[V] Verify completed stages` (re-runs each already-`complete`
  stage's own `--resume` verification and stops at the first
  not-yet-complete one -- never advances the restore, purely an
  integrity check), `[S] Show recovery status` (dumps the ledger,
  read-only), or `[Q] Quit` (no changes).
- **No ledger, but the target shows restore progress** (a `.git`
  checkout and/or a non-empty `.env`): offers `[A] Verify and adopt
  this interrupted recovery` (adds `--resume --adopt-pre-ledger`),
  `[N] Treat this as a new recovery` (proceeds exactly as before
  r0043/r0044 -- real existing content still requires
  `--force-env`/`--force-db`), `[S] Show detected state`, or `[Q] Quit`.
- **Ledger exists but belongs to a DIFFERENT archive or target root, or
  is corrupt/schema-invalid**: always a hard, immediate failure --
  never a menu, never silently ignored.
- **No ledger and no detected pre-existing state**: proceeds directly to
  the ledger/adoption question above (there is nothing to ask about
  there), but as of r0045 recovery-media resolution (below) still runs
  first regardless of ledger/target state -- it is orthogonal to
  adoption and applies to the most common E8 scenario, a brand-new box.

The prompt is never itself authorization to weaken any safety check --
every menu choice still runs through the exact same fail-closed
verification the flags above describe. No TTY (piped/redirected stdin,
the normal CI/automation shape) never prompts at all; `--non-interactive`
forces the same even under a real TTY. Deterministic automation/tests
should prefer passing `--resume`/`--adopt-pre-ledger` explicitly.

---

## Recovery media (r0045)

An authoritative offline E8 restore needs several stage-specific frozen
inputs that a fresh backup archive alone cannot supply: Stage 10's local
apt/snap closure, Stage 20's local IsadoraAir Git mirror, Stage 60/80's
offline pip wheelhouse, and Stage 80's local companion Git mirrors.
r0045 establishes ONE **recovery-media root** concept for all of these,
derived directly from `deploy/restore/build_offline_closure.py`'s own
`--out-dir` layout and the real E8 export procedure -- never a guessed
or separately-documented layout:

```
<recovery-media-root>/            (an "e8-inputs"-style directory)
  backups/<archive>.tar.gz
  offline/apt-repo/                (dpkg-scanpackages-indexed .deb files)
  offline/snaps/                   (+ snap-manifest.json)
  offline/manifests/               (apt-closure-manifest.json, snap-manifest.json,
                                     direct-apt-packages.txt)
  offline/wheelhouse/               (pip wheels/sdists)
  repos/IsadoraAir.git             (bare mirror)
  repos/<companion>.git            (bare mirrors, one per companion)
```

`deploy/restore/recovery_media.py` (stdlib-only, mirrors
`restore_ledger.py`'s own style) provides three subcommands:

- `validate --root R [--archive A]`: structural completeness check
  (`valid`) is reported separately from whether `A` specifically is
  present in `R`'s own `backups/` (`archive_match`) -- `discover()` below
  needs a structurally-sound-but-non-matching root reported as a real,
  ranked candidate, never silently dropped. The CLI's own exit code
  (what `restore.sh`'s explicit `--recovery-media-root` path actually
  gates on) is the stricter combination: `valid AND archive_match is not
  False`.
- `discover --archive A --search-root R...`: returns every structurally
  valid candidate under the given search roots (globbing for an
  `e8-inputs`-named directory one/two levels down, plus checking whether
  `A` already lives inside a media root's own `backups/`), ranked with
  archive-matching candidates first.
- `detect-apt-groups --root R --packages-file F`: reads `R`'s own
  `offline/manifests/direct-apt-packages.txt` and intersects it against
  each `OPTIONAL_*` group in `deploy/packages-ubuntu-26.04.txt` --
  data-driven, so the `--with-*` flags `restore.sh` derives always match
  what this SPECIFIC media root's own manifest says, never a hardcoded
  guess.

`restore.sh` resolves recovery media once, before the stage loop and
before the ledger/adoption preflight:

1. `--recovery-media-root PATH` (deterministic automation/CI path):
   validated immediately; a bad/incomplete tree fails closed before any
   stage runs.
2. Otherwise, if interactively eligible (apply mode, a real TTY, not
   `--non-interactive`, not `--resume`, a real `--archive`): discovers
   candidates under the archive's own directory and `$HOME`. Exactly one
   archive-matching candidate is used automatically, after showing the
   operator what was found. Zero candidates prompts for a path (blank
   proceeds with online/default sources for any stage that needs them).
   More than one (or matches that aren't exact) shows a numbered list
   and asks the operator to choose (or decline).
3. Otherwise (non-interactive, no explicit root): no-op, exactly
   pre-r0045 behavior -- online/default sources.

Once resolved, `_restore_build_media_stage_args` builds per-stage
argument/env arrays -- `restore.sh` never broadcasts a stage-specific
flag to a stage that doesn't recognize it:

| Array | Routed to | Contents |
|---|---|---|
| `STAGE10_ARGS` | `10-packages.sh` | `--apt-repo-dir`, `--snap-dir`, plus `--with-cd-rip`/`--with-kokoro-tts`/`--with-syndicated-selenium`/`--with-backup-encryption` per `detect-apt-groups` (HE-AAC is never skipped) |
| `STAGE20_ARGS` | `20-application.sh` | `--repo-url file://<root>/repos/IsadoraAir.git` |
| `STAGE60_ENV` | `60-python.sh` (via `env`) | `PIP_NO_INDEX=1`, `PIP_FIND_LINKS=<root>/offline/wheelhouse`, `PIP_DISABLE_PIP_VERSION_CHECK=1` |
| `STAGE80_ARGS` | `80-companions.sh` | `--repo-url-prefix file://<root>/repos` |
| `STAGE80_ENV` | `80-companions.sh` (via `env`) | same as `STAGE60_ENV` |

`OWNER_ARGS` (`--owner USER:GROUP`) and `IDENTITY_ARGS`
(`--isa-user`/`--isa-uid`/`--isa-gid`, pre-existing) are routed the same
way, via one unified `_restore_run_stage` dispatcher -- this also fixed
a latent pre-r0045 bug where `--owner` would have broken any stage other
than 20/40 had it ever been passed through the orchestrator, since only
those two stage scripts recognize it. No stage's own argument validation
or network fail-closed behavior is weakened by any of this -- recovery
media only ever SUPPLIES the same flags an operator could type by hand.

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
