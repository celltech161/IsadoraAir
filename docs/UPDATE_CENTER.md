# Update Center — design & usage (Phases A–C)

[P0] Bucket 1 / 1.1. This document covers what exists **today**:
Phase A's non-privileged planning foundation, Phase B's protected execution
backend, and Phase C's application integration and manual privileged bootstrap.
The private/gitignored architecture notes
used during review are not part of the deployable product; this
Git-owned document is the authoritative shipped contract.

## Phase A planning foundation

Phase A introduced a read-only `/updates/` status page (staff/superuser only) showing
installed source, running-software version skew (reusing
`isadoraair/version_info.py` and `monitoring/services/release_status.py`
unmodified), and — when a release chain is present and safe to plan —
a computed update plan. Phase C retains that read access and adds one
superuser-only, POST/CSRF-protected submission route. Django still cannot run a
command as root: execution crosses a strict Unix-socket protocol into a
separately installed protected runtime.

## The release manifest

Each deployable release is one file: `deploy/releases/<release_id>.json`.
`release_id` follows a simple monotonic sequence (`r0001`, `r0002`,
...) — deliberately not semver, since this project has no versioning
scheme to extend (confirmed by inspection during the architecture
report; see `isadoraair/version_info.py`'s own docstring).

A manifest is **declarative only** — it states facts about what a
release needs (migrations, systemd changes, restarts, apt
prerequisites, ...), never *how* to do it. There is no hook, script,
or command field anywhere in the schema, on purpose:
`updatecenter/manifest.py`'s `FORBIDDEN_FIELDS` rejects
`pre_update_hooks`/`post_update_hooks`/`hooks`/`commands`/`shell`/
`script`/`exec` outright, with a specific error message, not a generic
"unknown field": release metadata must never become an executable-code
channel.

### No self-referential commit SHA

A manifest committed as part of commit X cannot embed X's own SHA —
the SHA is a hash of the commit's content, which would include the
embedded SHA. `updatecenter/manifest.py` forbids `release_commit`/
`commit`/`sha`/`git_sha` as fields entirely. Instead: a non-bootstrap
release's commit identity is discovered EXTERNALLY, by whichever
commit's tree first introduced `deploy/releases/<release_id>.json`
(`git log --diff-filter=A`, see `release_chain.resolve_release_commit`).
That path must have exactly one reachable history commit: modifying,
deleting, or re-adding an immutable manifest makes identity
unresolvable and planning fails closed. Each normal release must also
have its own introducing commit; adding two manifests in one commit is
ambiguous and rejected.
Local-only commits, detached HEAD, dirty trees, and local/remote
divergence are non-authoritative states and block planning. A station
cleanly behind `origin` remains supported: manifests and target files
are read from fetched Git objects without touching its working tree.
**A release's manifest and everything it describes (requirements.txt
changes, new systemd unit templates, new migration files) must land in
the same commit** — that's what makes this resolution correct; see
`updatecenter/tests/test_planner.py`'s own fixture comments for a
worked example of getting this wrong (and the fix).

The one exception is the single **bootstrap release** (the one with
`previous_release_id: null`) — it carries an explicit `bootstrap_commit`
field naming a real, already-immutable, pre-manifest-era commit (this
project's actual production baseline the day manifests were
introduced: `deploy/releases/r0001.json` names `5a0cb0e...`). This is
not a self-reference — `r0001.json` itself lives in a *later* commit
(whenever this Phase A work is committed), describing a distinct,
already-fixed ancestor. `bootstrap_commit` is rejected on every other
release.

### Release chain

Releases form a strict, singly-linked, cycle-free chain via
`previous_release_id` — never inferred from git commit ancestry alone
(a release manifest describes *deployment* semantics; git commits
identify *source objects* — related, not the same thing, per this
task's own §"RELEASE CHAIN MUST BE SOLVED" instruction). Exactly one
release has `previous_release_id: null` (the bootstrap); every other
release's predecessor must exist in the set, no two releases may share
a predecessor (that would fork the chain), and no cycle is permitted.
`release_chain.build_chain()` enforces all of this and fails closed
(raises `ChainError`) on any violation — see
`updatecenter/tests/test_release_chain.py` for the full failure-mode
coverage.

**Skipped releases are supported by construction.** A station several
releases behind (e.g. WRJE on r0001 when r0004 is current) gets a plan
that aggregates every action across r0002, r0003, AND r0004, in order
— never just the newest manifest. See `updatecenter/tests/
test_planner.py`'s `MultiReleaseAggregateTests`.

### Reading the release set: git history, not the working-tree disk

This matters and is easy to get backwards: a station behind on
releases has, correctly, never checked out a newer release's manifest
file — only `git fetch` (never checkout) brings that commit into the
object database. `planner.build_plan` therefore reads the release set
via `release_chain.load_manifest_files_at_ref`, resolving
`origin/<branch>` when available (falling back to `HEAD`), using
`git_adapter.list_files_at_commit`/`read_bytes_at_commit` — never the
literal filesystem `deploy/releases/*.json` on disk. (The disk-based
`release_chain.load_manifest_files` still exists, used only by
`manage.py validate_release_manifests`'s default mode — validating a
manifest a human is authoring, before it's ever committed.)

## Migration policy — the core guarantee (CORRECTED 2026-08-23)

**This section was corrected after a real production incident.** The
original version of this document said the four migrations below were
"intentionally deferred" and that unlisted unapplied migrations were
simply ignored. That was wrong, and it broke production. The corrected
principle and evidence are preserved here.

### Schema vs. feature activation — the distinction that matters

```
SCHEMA PRESENT/APPLIED   !=   FEATURE ACTIVE/CUT OVER
```

An additive migration (a new nullable/defaulted column, a new table)
can be applied WITHOUT activating anything that uses it — a null FK
stays null, an off-by-default flag stays off, a new table can exist
with zero rows and zero readers. **Only feature activation may be
deferred.** The corrected expand/cutover model:

1. introduce additive/backward-compatible schema;
2. **apply that schema when the source containing that model state is
   deployed** — not later, not "when the feature cuts over";
3. leave the new feature's behavior disabled/inert through config or
   feature selection;
4. cut the behavior over later, whenever that's actually ready.

### What actually broke, and why "the feature is off" didn't save it

At commit `5a0cb0e`, `webrequests/models.py` already declared
`WebRequestConfig.dedication_tts_voice`/`dedication_tts_timeout_seconds`
— but `webrequests.0008_webrequestconfig_dedication_tts` (the migration
that creates those columns) was left unapplied on the theory that the
shared-TTS dedication feature hadn't cut over yet. Django doesn't know
or care about feature cutover — an ordinary `WebRequestConfig.load()`
call (run every ~20s by the `web-requests-ingest` timer) selects every
column the model class declares, unconditionally. The moment `5a0cb0e`
was deployed and the timer/process restarted, every such read failed:
`column webrequests_webrequestconfig.dedication_tts_voice_id does not
exist`.

Re-inspected all four migrations independently after this incident
(not just the one that visibly broke) — each is schema-required for
its own distinct, verified reason:

| Migration | Why it's schema-required |
|---|---|
| `webrequests.0008_...` | `AddField` on `WebRequestConfig`, an actively-`.load()`-ed singleton config, polled every ~20s by a live timer. **Confirmed by the real production failure.** |
| `road_conditions.0010_...` | Same shape: `AddField` on `RoadConditionsConfiguration`, `.load()`-ed by the `sync_road_conditions` management command/timer AND by its own Django admin page (`RoadConditionsConfigurationAdmin.has_add_permission` calls `.load()` too) — same failure mode, not yet observed only because this station's road-conditions timer/admin page hadn't been exercised since the restart. |
| `weather.0007_...` | `CreateModel` only (no field added to `WeatherConfig`) — but `WeatherVoicePersona` is Django-admin-registered (`@admin.register`) and reachable at `/admin/weather/weathervoicepersona/`, and queried directly by the real `dump_weather_config` management command. |
| `tts.0001_initial` | `CreateModel` only, but structurally required regardless: it's a Django migration-graph **dependency** of all three migrations above — none of them can apply without it. Also independently admin-registered (`StationTTSVoice`, `PiperVoiceModel`). |

`deploy/releases/r0001.json` now correctly lists all four in
`migrations_required` — see that file's own `summary` field, which
states both halves of the distinction explicitly: schema required,
feature not cut over.

**`r0001` is a bootstrap ANCHOR for an already-installed system, not a
from-empty-database recipe.** This does not mean stuffing every
historical migration this project has ever shipped into
`migrations_required` — everything else at `5a0cb0e` (`library.0079`
included) was already correctly applied. The four listed here are
specifically the ones that were genuinely at risk of being left
unapplied, and now aren't.

### The planner's corrected rule

The OLD (wrong) rule: only explicitly-listed migrations ever get
applied; anything else stays deferred indefinitely, forever ignored.
**Rejected.** The corrected rule:

- A release manifest's `migrations_required` declares the migrations
  **expected when entering that release transition** — kept as the
  field name (renaming it was considered and rejected as unnecessary
  churn; the name itself was never the problem, the *interpretation*
  was).
- Across skipped releases, the planner aggregates every expected
  migration in order — unchanged from before.
- `schema_health.py` independently asks Django for the CURRENT
  checkout's *actual* pending-migration plan
  (`MigrationExecutor.migration_plan()` — the same read-only mechanism
  `migrate --check` uses internally), entirely independent of release
  manifests. **Any pending migration here means CURRENT SCHEMA
  UNHEALTHY** (`SafetyStatus.SCHEMA_DRIFT_DETECTED`) and blocks planning
  outright. An installed or future manifest cannot "account for" a
  currently missing column/table and thereby make the live ORM/DB
  mismatch healthy. This is exactly the check that would have caught
  the `WebRequestConfig` incident.
- The updater must never blindly `manage.py migrate` (apply
  everything pending, unexamined) and must never silently leave an
  unlisted pending migration in place forever because no manifest
  named it. Both failure modes are now guarded against; see
  `updatecenter/tests/test_planner.py`'s `SchemaDriftDetectionTests`
  and `MigrationAggregationTests`.
- Phase A's release-chain migration list is declarative expected
  transition work. Its dependency expansion is explicitly labelled a
  CURRENT-graph preview; it is not final target migration validation.
  A target release can contain migration modules/dependency edges that
  do not exist in the running checkout and therefore cannot be loaded
  by this process's `MigrationExecutor`.
- A future Phase B executor's safe sequence, replacing the earlier
  draft's `manage.py migrate <app> <migration>` targeted-command idea
  (correctly flagged as risky — a targeted migrate's semantics depend
  on graph state in ways that don't compose cleanly as a "apply
  exactly these" contract): derive the expected set from the validated
  release chain → ask Django for the actual migration plan once the
  target source is safely staged/materialized → run the TARGET SOURCE's
  read-only Django planner in a controlled environment → compare
  expected vs. actual including
  dependency closure → proceed only on an exact match → run an
  ordinary, whole, ungapped Django migration operation → verify the
  expected state afterward. Not implemented in Phase A; this is the
  contract Phase A's planner/tests now establish for it.

Migration compatibility is declared per release
(`migration_compatibility: "additive"` or `"destructive"`), required
whenever `migrations_required` is non-empty. A plan whose aggregate
migration set includes any `"destructive"` release is marked
`migration_manual_gate_required` — never eligible for an unattended
path (Phase B, not yet built).

## Current schema health vs. target schema validation

`/updates/` now shows database schema health as its own signal,
independent of `safety_status` (git/manifest-chain state) — never
conflated into one field, on purpose. Two different questions:

```
SOURCE/MODEL CURRENT      <- safety_status (git checkout + manifest chain)
DATABASE SCHEMA CURRENT   <- schema_health_status (schema_health.py)
```

A station can have a perfectly clean, up-to-date git checkout while
its database schema is behind — that combination is exactly what broke
production, and the two signals must be visibly separate so an
operator (or Phase B) can't mistake "source is current" for "database
is current." Values: `schema_current`, `unapplied_migrations_detected`,
`migration_state_indeterminate` (never silently treated as "fine" —
see `schema_health.check_schema_health`'s own docstring). Computed via
Django's `MigrationExecutor.migration_plan()` — read-only, the same
mechanism `migrate --check` uses internally, never mutates anything,
computed unconditionally on every `/updates/` page load regardless of
git state.

The UI separately reports
`TARGET_SCHEMA_PLAN_VALIDATION_PENDING` when a newer release exists.
That does not mean the target is unsafe; it means Phase A truthfully
stops at manifest/Git-object inspection. Phase B must validate the
target source's actual graph before APPLY. No current-process
`MigrationExecutor` result is presented as proof of a not-yet-loaded
target graph.

## Cross-checking: machine-verifiable facts vs. release-author intent

Two different kinds of claim live in a manifest. `updatecenter/
cross_check.py` verifies only the ones with an objective, git-
inspectable answer, against the release's own resolved commit —
**never against the working tree**, via `git_adapter.path_exists_at_commit`/
`read_bytes_at_commit`:

- `migrations_required` refs → does that migration file actually exist
  at the target commit?
- `requirements_sha256` (when `python_requirements_changed`) → does
  `requirements.txt` at the target commit actually hash to that value?
- `systemd_units_changed`/`systemd_units_new_required`/
  `systemd_units_new_optional` → does `deploy/<unit>` actually exist at
  the target commit?
- `systemd_units_removed_or_renamed` → does `deploy/<unit>` correctly
  NOT exist at the target commit?

A mismatch fails planning (`cross_check_failed`) — the manifest is
never trusted over reality for these facts. Everything else (which
services need restarting, whether a schema change counts as
"additive") has no independent verification and is trusted as
authored, reviewed intent — this codebase has no way to know that
without running the code.

## Systemd intent, never auto-activated

Unit names in a manifest are validated two ways: shape
(`manifest.UNIT_NAME_PATTERN` — no path separators, no `..`, must end
`.service`/`.timer`) and, separately, existence at the target commit
(`cross_check.py`). `services_requiring_restart` is validated against
a **fixed, closed set** of the 5 core services
(`manifest.CORE_RESTARTABLE_SERVICES`) — deliberately not derived from
scanning `deploy/*.service` (which would make any of the 100+ optional/
companion units eligible for an unattended restart).

`systemd_units_new_optional` is surfaced in the plan as a notice only
— **nothing in this codebase auto-installs or auto-enables an optional
unit just because its template exists.** This matches
`deploy/README.md`'s own existing convention (optional timers are
opt-in, one `sudo systemctl enable --now` at a time).

## Station-config trust contract

The architecture report originally proposed an application-owned station file
for the six `@@PLACEHOLDER@@` render values. Review corrected this: a file under
the Git checkout, writable by Gunicorn's account, is exactly the wrong authority
for rendering root systemd units. The complete strict schema is shown in
`deploy/updater-station.example.json`. Its production location is
`/etc/isadoraair/station.json`, outside the checkout, root-owned and mode 0600
(it may identify a pgpass file). The privileged executor reads it directly;
Gunicorn cannot write it. Unknown fields, overlapping protected/application
paths, non-loopback health targets, malformed account/database values and
invalid operator units fail config loading.

## Privilege boundary

The privileged executor **must not** run from
`/opt/isadoraair`, the station's physical checkout path, or the shared
application venv — anywhere the Gunicorn/application service account
can write. A compromised Django process that can modify
`updatecenter/` or the shared venv, combined with a root service that
later imports/executes code from either, collapses the whole "narrow
IPC boundary" idea into arbitrary root code execution. The rejected
shape:

```
root systemd service -> /opt/isadoraair/venv/bin/python -> import updatecenter.daemon   # NEVER THIS
```

The executor belongs in a root-owned, application-unwritable install
location (conceptually `/usr/local/libexec/isadoraair-updater/`), using
either system Python/stdlib only or its own root-owned minimal
runtime. Application-level operations (`manage.py migrate`,
`collectstatic`, git working-tree actions, `pip install` into the
application venv) are launched explicitly as the unprivileged
`ISA_USER` where appropriate — only the narrow slice that genuinely
needs root (systemd install/`daemon-reload`/enable/restart) runs as root.
Automatic apt/package mutation remains outside the updater contract.

Phase C removes the source-level direct `sudo` calls formerly present in
`hardware/admin.py`, `rbds/admin.py`, and `monitoring/views.py`. They now use
the same protected broker's two maintenance operations. Production execution
must remain disarmed until the historical unrestricted `(ALL) NOPASSWD: ALL`
grant has been removed and effective policy verified; installing these source
changes does not edit sudoers automatically.

## Other locked decisions

- **No automatic rollback is promised**, anywhere in the UI or this
  documentation. Every failure case in the architecture report's own
  §17 resolves to "operator required" beyond the trivially-safe
  nothing-changed-yet cases.
- **apt packages are never auto-installed.** A manifest declaring
  `apt_packages_new` makes the plan `manual_system_package_action_required`
  — a hard stop, not a prompt to `apt install` anything.
- **Pre-migration DB checkpoint retention (Phase B)**: keep
  the last 5, or 30 days, whichever is less restrictive at the moment
  of pruning; always retain the newest successful checkpoint until a
  newer one exists. `pg_dump` execution remains entirely inside the protected
  Phase B runtime.

## Bootstrap release sequence

This feature cannot install itself. The release chain therefore has
two distinct entries:

- `r0001` anchors commit `5a0cb0e...` and defines the healthy schema
  expected when entering that installed baseline. Its four TTS-adjacent
  migrations are baseline health requirements, not transition actions
  to replay when moving from `r0001` to `r0002`.
- `r0002` is this Phase A checkpoint. Its transition contains only
  `updatecenter.0001_initial`, no packages/systemd/nginx/static work,
  and a Gunicorn restart because Django settings, URLs, templates, and
  web code changed.

The exact conceptual manual bootstrap is:

```
source reaches the Phase A/r0002 checkpoint
  -> verify the healthy r0001 schema is already present
  -> manually apply updatecenter.0001_initial
  -> restart Gunicorn
  -> station is installed at r0002
  -> future r0003+ releases may be managed only after Phase B/C
     execution infrastructure is separately deployed and enabled
```

`r0001 -> r0002` planning aggregates only the `r0002` transition; the
planner always excludes the installed release's own migration set.

`updatecenter`'s own
`0001_initial` migration (a plain, additive `CreateModel` for
`UpdateJob` — no data migration, no dependency on any other app's
migration beyond Django's own swappable `AUTH_USER_MODEL`) must be
applied manually, once, by an operator, the same way every migration
was applied before this feature existed — `manage.py migrate
updatecenter`. Only *after* that one manual step can any future
release be managed through `/updates/` at all. Do not attempt to
route this specific bootstrap step through the Update Center itself —
the sequence above is the unavoidable bootstrap boundary, not an
execution feature omitted by accident.

The same logic applies to the Phase B systemd unit
(`isadoraair-updater.service`) — it, too, must be installed manually
the first time, following the protected-copy bootstrap in
`deploy/updater_runtime/README.md`. The Phase C source checkpoint still does
not activate it automatically.

## Phase B safe-execution backend

Phase B added the complete backend trust boundary without installing it.
Phase C supplies the narrow application integration, while protected code
installation, configuration, service activation, sudo cleanup, and final
arming remain explicit manual operator work.

### Protected runtime and root trust boundary

The standalone source under `deploy/updater_runtime/` uses only the Python
standard library and imports no Django/application module. Its Git location is
for code review and distribution only. Production execution is permitted only
after a reviewed copy has been installed root-owned and application-unwritable:

```
/usr/bin/python3 -I \
  /usr/local/libexec/isadoraair-updater/updaterd.py \
  --config /etc/isadoraair/station.json
```

The shipped optional `isadoraair-updater.service` uses that exact protected
shape. It never uses `/opt/isadoraair/venv`, never imports from the application
checkout, and never executes target Python as root. The source README documents
bootstrap using fixed `install` invocations; it never tells an operator to run
repo-owned Python with `sudo`.

The root-owned station configuration is strict JSON with a closed field set:
trusted upstream URL and branch, application identity/root, protected state
paths, finite station render values, database connection identity, and a
loopback Gunicorn health URL. It contains no executable command, hook, arbitrary
destination, or client-controlled value. The loader refuses symlinks, oversized
files, non-root ownership, group/world writability, path overlap with the
application tree, unknown keys, malformed branches/accounts, non-loopback
health URLs, and unsafe repository identities.

### Independently trusted Git and release plan

Root never treats the application checkout's `.git`, PostgreSQL, or an
`UpdateJob` row as authority. It owns a separate bare repository, normally:

```
/var/lib/isadoraair-updater/repository.git
```

Its origin and branch come only from root-owned station configuration. Fetches
use fixed argv, no shell, no hooks, controlled environment, bounded output, and
hard timeouts. Previously accepted upstream history may advance only by
fast-forward; a force-push or divergence fails closed. An SSH upstream requires
a separately provisioned root-owned read-only deploy credential and trusted
host key; the updater never borrows an application-writable SSH identity.

The protected runtime independently parses the strict manifest schema, builds
the one linear chain, resolves immutable introducing commits, rejects modified
or re-added manifests and shared introducing commits, verifies every release
commit lies on the trusted branch, and cross-checks migration/unit/requirements
claims against trusted Git objects. It independently derives the installed and
latest releases, complete skipped-release action set, target commit, and
version-2 canonical execution fingerprint under protocol v3. The
`START_UPDATE` release/fingerprint are
requests for comparison, not authorization facts. Any mismatch is fatal.
Each transition is also compared with its predecessor: requirements, systemd
unit, nginx, and runtime-component bytes must agree with the corresponding
manifest change flags/lists. A falsely undeclared change therefore fails closed
instead of bypassing a manual gate.

Python requirement changes, apt prerequisites, destructive migrations,
unknown required/changed units, removed/renamed units, nginx changes,
runtime-component changes, or a newer updater protocol are Phase B manual
blockers. Phase B never runs pip against the live venv, never installs apt
packages, and never replaces itself.

### Narrow IPC and durable root state

The daemon owns a Unix socket under `/run/isadoraair-updater/`, a systemd-created
root-owned runtime directory. `SO_PEERCRED` must identify root or the configured
application UID/GID. The protocol is one bounded UTF-8 JSON object with an exact
field set and protocol version 3. Exactly seven actions exist:

- `PING`;
- `START_UPDATE` with canonical UUID, `r####` target, and SHA-256 plan
  fingerprint;
- `GET_JOB_STATUS` for that UUID;
- `GET_JOB_LOG` with a maximum tail size no greater than 64 KiB.
- `RESTART_OPERATOR_SERVICE` with one exact service name that must also be an
  exact member of the root-owned station allowlist;
- `STORE_ALSA_STATE`, which accepts no arguments and always executes fixed
  `/usr/sbin/alsactl store`.
- `GET_MAINTENANCE_STATUS` for one root-generated maintenance UUID.

There is no `RUN_COMMAND`, shell, arbitrary systemctl argv, write-file, or path
operation. A service string is data only until root independently matches it
against `operator_restart_units`. Unknown fields/actions and oversized or
malformed messages are rejected. One bounded asynchronous maintenance worker
exists; requests are never accumulated in an unbounded queue.

Each admitted maintenance action receives a root-generated UUID and a mode-0600
result record under the protected job-state tree. At most 100 records are
retained. The record contains only the fixed action, the already-allowlisted
service when applicable, state, timestamps, and a sanitized result
classification—never command output. Fast completion/failure is returned in
the admission response; longer work remains observable through the single
`GET_MAINTENANCE_STATUS` query without keeping Gunicorn attached to systemd's
stop timeout.

Authoritative state is atomic mode-0600 JSON under
`/var/lib/isadoraair-updater/jobs/`; logs are append-only mode-0600 files under
`/var/lib/isadoraair-updater/logs/`. Keeping both roots beneath the root-owned
`/var/lib/isadoraair-updater/` state directory satisfies the protected-parent
policy; `/var/log` is group-writable by `syslog` on Ubuntu and is therefore not
an acceptable parent for this runtime. A daemon-wide filesystem lock prevents two
updater daemons. At most one active job is accepted. Starting the same UUID with
identical authorization facts is idempotent; reusing it with different facts or
starting a different concurrent job is rejected. Safe completed milestones are
durable and support restart recovery. A migration-started job lacking a durable
database-verified milestone is intentionally ambiguous and becomes
manual-intervention-required rather than blindly rerunning migration.

The Django `UpdateJob` remains a UI/audit mirror. The superuser POST recomputes
the plan and creates the row before submitting only its UUID, logical target
release, and fingerprint. Root neither reads nor writes the row. Reconciliation
compares root-derived target/fingerprint with the mirror before accepting
status. A lost response—or any generic negative START response—becomes
`submission_uncertain`, not `failed`, unless `GET_JOB_STATUS` explicitly proves
that the UUID does not exist. A durable accepted state therefore remains root
truth even when a later acceptance-log write fails. The
active database lock remains held. The POST retries only the same UUID, and
later GET reconciliation releases the lock only after root reports a terminal
state or explicitly proves that UUID does not exist.

### Staging and schema-before-source ordering

The mandatory execution order is:

```
validate clean exact live release
  -> fetch and independently validate trusted release chain
  -> require CURRENT live-source schema plan clean
  -> git-archive exact target from root repository
  -> securely extract root-owned, application-read-only staged target
  -> run TARGET migration probe as ISA_USER
  -> compare target plan with manifest set + target dependency closure
  -> mechanically prove v1 additive compatibility
  -> create valid pg_dump checkpoint
  -> run target-source migrate as ISA_USER
  -> re-probe target schema clean
  -> only then fast-forward live source as ISA_USER
  -> collectstatic if declared
  -> reconcile required/changed systemd units from immutable staging
  -> restart exactly declared core services
  -> postflight and durable success
```

Secure extraction accepts only regular files/directories, caps archive/member/
expanded sizes, rejects duplicate/traversal/absolute names, links, devices and
special files, and publishes source mode read-only. Job directories are
canonical UUID children of one configured staging root. Cleanup verifies that
relationship and refuses symlinks, so it cannot escape via a client path.

The management command `updatecenter_probe` is deliberately machine-readable
and read-only. It reports the target source's actual Django forward plan,
complete dependency map, applied set, conflicts, replacement migrations, and
mechanical operation classification in bounded strict JSON. It is always run
from the staged target as the application user, with bytecode writes disabled,
a controlled environment, and the existing application venv. Root never
imports or executes it directly.

Expected manifest migrations must exist in the target graph. Their full target
dependency closure minus already-applied nodes must equal the actual Django
plan exactly—no missing or unexpected migration. Conflicts, replacement/
squash ambiguity, cycles, missing dependencies, and target migrations applied
outside the job fail closed. The v1 mechanical auto-allowlist is intentionally
small: `CreateModel` and nullable `AddField`. Every other operation is manual,
even if a manifest calls the release additive. Current schema drift is checked
first and cannot be explained away by target work; the WebRequestConfig incident
remains the canonical reason.

### Database checkpoint and migration failure

Immediately before the first actual migration, Phase B runs fixed-path
`/usr/bin/pg_dump` as the application user with fixed custom-format/no-owner/
no-ACL arguments. Database identity comes from root configuration; an optional
pgpass path is passed only via the controlled environment. Password and secret
values are never placed in argv or logs. Root streams the result into an
internally generated `.partial` file with timeout and size bounds. Only a
non-empty successful dump is atomically promoted, mode 0600, with metadata
recording SHA-256, size, job, source release/commit, and target release/commit.

Retention is 30 days and at most five valid checkpoints. The newest valid
checkpoint is always kept even after 30 days until another valid checkpoint
supersedes it. Retention runs only after validating a new dump. Incomplete or
invalid dumps never count as checkpoints.

Migration is one ordinary staged-target `migrate --noinput`, run as ISA_USER,
only after the exact plan and checkpoint gates. Failure leaves live source and
service state untouched and retains the checkpoint/evidence. Phase B never
automatically reverses migrations or restores a dump. Expanded additive schema
with old source still active is the intentional recoverable state.

### Live source, static files, systemd and restarts

Live Git is never manipulated as root. Immediately before advancement, fixed
application-user Git commands recheck expected branch, exact installed HEAD,
clean tree, configured origin identity, exact target object, and fast-forward
relationship. Hooks are disabled. The application user fetches the configured
branch and performs `merge --ff-only` to the independently pinned target SHA;
then root verifies exact HEAD and cleanliness. Dirty work, local commits,
branch/remote changes, a moved HEAD, non-fast-forward target, or target absence
all fail closed without stash/reset/clean/force.

`collectstatic --noinput` runs as ISA_USER after source advancement because the
project's `STATIC_ROOT` belongs to the live release layout. A failure is manual
intervention after the point of no fake rollback, and no service restarts occur.

Systemd input bytes come only from the root-owned immutable staged target.
Phase B automatically handles only a compiled closed allowlist of core
IsadoraAir units that are also declared changed/new-required by the complete
validated chain. Rendering accepts only the six finite station tokens.
Destination names are basenames in the configured unit directory; symlinks,
non-regular files, unexpected ownership, unknown tokens/units, arbitrary
destinations and drop-ins are refused. Identical bytes are not rewritten;
changes use a mode-0644 atomic replacement, followed by at most one
`daemon-reload`. New required units may be enabled/started. Optional units,
including the Phase B updater service itself in `r0003`, are report-only and
never automatically activated. Removal/rename remains a manual gate.

Restarts use only the manifest's closed five-service set in deterministic
dependency order. No source-change inference adds another service. Each restart
is checked through typed systemd properties; exited successful oneshots are not
misclassified as failed. Postflight verifies exact target HEAD, clean live
target-source schema, installed unit state, declared service health, durable job
state, and a bounded loopback HTTP response when Gunicorn restarted.

### Failure, retry, and rollback

Before migration, failure leaves production unchanged. After verified additive
migration but before live advancement, extra backward-compatible schema may
remain while old source continues. After source advancement there is no
automatic rollback promise: failures are `failed` or
`manual_intervention_required` with exact evidence. No reverse migrations,
dump restore, forced Git reset, broad cancellation, or updater self-replacement
exists.

Fetch/validation/staging are repeatable. A valid root-recorded checkpoint can be
reused, applied migrations and source-at-target are recognized only alongside
durable milestones, identical units are not rewritten, and each service has
started/completed restart milestones so completed restarts are skipped while an
ambiguous interrupted restart becomes manual. Ambiguous migration interruption
likewise cannot auto-resume. Cancellation is intentionally unsupported.

## Phase C application contract

`GET /updates/` remains staff/superuser-visible and never fetches from the
network. `POST /updates/check-for-updates/` remains a CSRF-protected fetch of
remote refs only. `POST /updates/start/` is superuser-only. The browser confirms
the displayed release and fingerprint, but these values never authorize a
target: the POST rebuilds schema health, checkout state, release chain, plan,
backend readiness and arming state, then rejects a stale confirmation.

The Install control is offered only for an actionable plan with healthy current
schema, no active job, no manual/package/runtime/nginx/removal blocker, a
compatible reachable protected backend, and root execution armed. Staff can
inspect the reason it is blocked but cannot submit it. No `GET`, query string,
model permission, hidden target SHA, or caller path can start an update.

The initiating request does not own execution. The browser polls the UUID status
endpoint with bounded backoff. PostgreSQL locates the active job after a page or
Gunicorn restart; root JSON state remains execution truth. Live root log access
is superuser-only and capped at 32 KiB in the response; terminal logs come from
the durable bounded `completed_log_snapshot`. The UI uses `textContent`, never
HTML insertion. Updater outage is a temporary status and never a Gunicorn
startup dependency.

`GET /healthz/` is a small, unauthenticated Django + `SELECT 1` probe. It is the
configured postflight URL (`http://127.0.0.1:8000/healthz/`) and is narrowly
exempt from Django's HTTP redirect so a direct loopback Gunicorn probe returns
200. All ordinary public HTTP paths retain the existing HTTPS redirect. The
response is only `ok` or `unhealthy` and reveals no commit, path, or credential.

The `library.0080_seed_updates_nav_item` data migration creates `Updates` /
`updatecenter:dashboard` only when that URL identity is absent. It never edits an
existing row, so station label/order/enabled customization survives. Any new
first-party page intended for the main navigation must ship a similarly
idempotent `NavMenuItem` data migration in the same release. The migration must
create and enable the product-default row automatically, identify it by a stable
product identity such as `url_name`, avoid duplicates, and preserve any
pre-existing operator-created or customized row. Do not reconcile navigation at
startup: repeatedly recreating or re-enabling a row would override an operator's
intent on every boot. A reverse migration may remove only an untouched
product-created default that it can positively identify; otherwise it must leave
the row alone. The Reports and Updates migrations are the established examples.

## Root configuration and arming

`/etc/isadoraair/station.json` remains root-owned and application-unwritable.
Phase C adds two fields:

```json
{
  "update_execution_enabled": false,
  "operator_restart_units": [
    "isadoraair-engine.service",
    "isadoraair-rbds.service",
    "isadoraair-gunicorn.service",
    "isadoraair-encoders.service",
    "nginx.service",
    "stereotool.service"
  ]
}
```

Missing `update_execution_enabled` means false. It is never database- or
Django-controlled. `PING` reports protocol/runtime, protected-runtime and config
validity, trusted-repository readiness, and armed/disarmed state. Root rejects
`START_UPDATE` while false. Maintenance broker actions have their own exact
policy and do not imply update authorization.

The restart list is finite station policy, not a pattern. Every request must be
one exact member; wildcards, paths, malformed names and command-like strings are
rejected. The root runtime alone constructs `/usr/bin/systemctl restart UNIT`
and validates the result. ALSA persistence accepts no input and constructs only
`/usr/sbin/alsactl store`. Hardware live mixer saves remain successful when
persistence submission fails, with a warning/SystemEvent. Hardware/RBDS admin
saves likewise persist their DB change if the broker is unavailable.

## r0004 manual protected-runtime bootstrap

`r0004` is the manual bridge, not an unattended-update demonstration. It ships
source, migrations, protocol v3, the service template, config example and this
runbook, but never copies root code, edits sudoers, starts a service, or arms
execution. Substitute station values; these examples match the current checkout
layout without making it an authority:

Its two migrations are truthfully classified `additive`. Manual installation is
declared independently by `manual_bootstrap_required: true`. That boolean is
OR-aggregated across every skipped transition, included in fingerprint contract
v2, blocks the Django Install control, and produces the root
`MANUAL_BOOTSTRAP_REQUIRED` blocker. Older application/root runtimes reject the
unknown field, while r0004's minimum protocol 3 prevents an older helper from
attempting unattended execution. The existing protected-systemd-work blocker
remains independent.

```bash
export ISA_ROOT=/home/jreed/isadoraair-django
export ISA_USER=jreed

# Fetch objects as the unprivileged application owner. Resolve the one commit
# that introduced r0004 and inspect it before materializing anything.
git -C "$ISA_ROOT" fetch origin main
git -C "$ISA_ROOT" log --format='%H' --diff-filter=A origin/main -- deploy/releases/r0004.json
export R0004_COMMIT=REPLACE_WITH_THE_SINGLE_REVIEWED_SHA
git -C "$ISA_ROOT" show "$R0004_COMMIT:deploy/releases/r0004.json"
git -C "$ISA_ROOT" merge-base --is-ancestor "$R0004_COMMIT" origin/main

# Materialize reviewed files without root and without executing checkout code.
export UPDATER_STAGE="$(mktemp -d)"
git -C "$ISA_ROOT" archive "$R0004_COMMIT" \
  deploy/updater_runtime deploy/isadoraair-updater.service \
  deploy/updater-station.example.json | tar -x -C "$UPDATER_STAGE"
find "$UPDATER_STAGE/deploy/updater_runtime" -maxdepth 3 -type f -print
```

Review every staged file. Then use only fixed system utilities to install the
specific reviewed artifacts; do **not** run `sudo python`, a repo-owned install
script, or any program from the application-writable checkout:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec/isadoraair-updater
sudo install -d -o root -g root -m 0755 /usr/local/libexec/isadoraair-updater/isadoraair_updater
sudo install -o root -g root -m 0755 "$UPDATER_STAGE/deploy/updater_runtime/updaterd.py" \
  /usr/local/libexec/isadoraair-updater/updaterd.py
sudo install -o root -g root -m 0755 "$UPDATER_STAGE/deploy/updater_runtime/updaterctl.py" \
  /usr/local/libexec/isadoraair-updater/updaterctl.py
for module in __init__ checkpoint config daemon executor jobs process protocol release security staging systemd; do
  sudo install -o root -g root -m 0644 \
    "$UPDATER_STAGE/deploy/updater_runtime/isadoraair_updater/$module.py" \
    "/usr/local/libexec/isadoraair-updater/isadoraair_updater/$module.py"
done
sudo install -d -o root -g root -m 0755 /var/backups/isadoraair
sudo install -d -o root -g root -m 0700 /var/backups/isadoraair/update-checkpoints
```

Prepare a station-specific JSON copy as the unprivileged user, retaining
`update_execution_enabled: false` and an exact minimal restart allowlist. Then:

```bash
sudo install -d -o root -g root -m 0755 /etc/isadoraair
sudo install -o root -g root -m 0600 /path/to/reviewed-station.json /etc/isadoraair/station.json

# Render only the known user token without root, review, then install.
sed -e "s|@@ISA_USER@@|$ISA_USER|g" -e "s|@@ISA_ROOT@@|$ISA_ROOT|g" \
  "$UPDATER_STAGE/deploy/isadoraair-updater.service" > "$UPDATER_STAGE/isadoraair-updater.service"
systemd-analyze verify "$UPDATER_STAGE/isadoraair-updater.service"
sudo install -o root -g root -m 0644 "$UPDATER_STAGE/isadoraair-updater.service" \
  /etc/systemd/system/isadoraair-updater.service
sudo systemctl daemon-reload
sudo systemctl enable --now isadoraair-updater.service

# The socket is created asynchronously after systemd starts the service. Poll
# for at most 30 seconds instead of treating a harmless startup race as failure.
(
  updater_ready=false
  for attempt in $(seq 1 30); do
    if /usr/bin/python3 -I /usr/local/libexec/isadoraair-updater/updaterctl.py ping; then
      updater_ready=true
      break
    fi
    sleep 1
  done
  if [ "$updater_ready" = true ]; then
    echo "Updater readiness check succeeded"
  else
    echo "Updater did not become ready within 30 seconds" >&2
    exit 1
  fi
)
```

Do not continue unless this check succeeds.

The PING must show protocol 3, protected/config/repository readiness, and
`update_execution_enabled: false`. Only then manually fast-forward the reviewed
application source to r0004, apply `updatecenter.0002_alter_updatejob_state` and
`library.0080_seed_updates_nav_item` through the normal station migration
procedure, restart Gunicorn, and verify `/healthz/`, `/updates/`, hardware mixer
persistence, AudioPipeline restart, RBDS topology restart, and each allowed
monitoring restart.

After restarting Gunicorn, use a bounded readiness check before continuing:

```bash
(
  gunicorn_ready=false
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error http://127.0.0.1:8000/healthz/ >/dev/null 2>&1; then
      gunicorn_ready=true
      break
    fi
    sleep 1
  done
  if [ "$gunicorn_ready" = true ]; then
    echo "Gunicorn readiness check succeeded"
  else
    echo "Gunicorn /healthz/ did not become ready within 30 seconds" >&2
    exit 1
  fi
)
```

Do not continue unless this check succeeds.

The shipped service definition still contains now-redundant
`LogsDirectory=/var/log/isadoraair-updater` and `/var/log/isadoraair-updater`
`ReadWritePaths` entries. Removing those protected-unit allowances is deliberately
deferred to a separately reviewed updater-runtime/systemd release; r0005 does not
modify or claim a systemd-unit change.

Next remove the historical unrestricted sudo grant manually. Do not use
`sudo -n true` as proof because a credential timestamp can mislead. Inspect the
effective policy with the fixed operator command:

```bash
sudo -l -U "$ISA_USER"
```

Require that no `(ALL) NOPASSWD: ALL`, equivalent all-command rule, wildcard
`systemctl`, or wildcard `alsactl` authorization remains. Re-test broker-backed
web operations with that policy removed. Only after all those checks may root
edit `/etc/isadoraair/station.json` to set `update_execution_enabled: true`,
restart `isadoraair-updater.service`, and verify `/updates/` reports READY /
ARMED. Django never performs any of these bootstrap or sudo-policy steps.

`r0005` will be the first deliberate end-to-end Update-button release. Keep it
boring: no requirements, apt, native runtime, dangerous systemd, nginx, or
preferably migration change; use a harmless visible application change and a
Gunicorn-only restart. Automatic rollback, apt installation, live-venv pip
mutation, destructive migration handling, and protected-updater self-update all
remain unsupported and manual.

## r0006 protected-updater hardening and manual bridge

The first real r0005 Update Center run exposed two protected-runtime boundaries.
First, the updater service's complete security context prevented root's
`runuser` process from changing to the application UID. The production-proven
base-unit correction is `AmbientCapabilities=CAP_SETUID CAP_SETGID`, which keeps
those capabilities available across the required exec boundary; it does not
claim that the User=root parent has only those two capabilities in its permitted
or effective sets. The shipped unit has no `CapabilityBoundingSet` restriction,
and production inspection showed the parent retains the broader root capability
set. The application-user child has zero permitted, effective, and ambient
capabilities. Second, `UMask=0077` reduced the
new staging job directory to `0700`, preventing the application user from
traversing to the staged source. Runtime v4 explicitly changes that root-owned
job directory to `0711`; it remains unlistable by group/other, `target.tar`
remains `0600`, and the source remains read-only.

`r0006` keeps updater protocol 3 but declares
`manual_bootstrap_required: true`. Any predecessor diff below
`deploy/updater_runtime/` is rejected unless that declaration is true, by both
the web planner and root-side trusted-plan derivation. The updater must never
replace its own protected runtime.

### Manual systemd capability acceptance (do not automate in CI)

Run this only during an approved production maintenance review. A generic
transient `User=root`, `NoNewPrivileges=yes` unit that omits ambient
capabilities is not a valid negative acceptance test: it does not reproduce the
updater unit's complete capability/security context and may still execute
`runuser` successfully. Do not require that simplified negative case to fail.

The authoritative acceptance is the actual updater runtime v4 startup
behavioral self-check: it executes fixed `/usr/bin/id -u` as the configured
application user, validates the exact UID, and reports
`protected_runtime_valid` only after that succeeds. The following positive
transient check is supplementary and should print the application UID; it
retains `NoNewPrivileges=yes` and uses fixed system executables with no shell:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=isadoraair-runuser-positive \
  --property=User=root --property=Group=jreed \
  --property=NoNewPrivileges=yes \
  --property='AmbientCapabilities=CAP_SETUID CAP_SETGID' \
  /usr/sbin/runuser --user jreed -- /usr/bin/id -u
```

For the positive case, inspect the actual application-user child rather than
assuming capability clearing from the unit configuration:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=isadoraair-runuser-capability-proof \
  --property=User=root --property=Group=jreed \
  --property=NoNewPrivileges=yes \
  --property='AmbientCapabilities=CAP_SETUID CAP_SETGID' \
  /usr/sbin/runuser --user jreed -- /usr/bin/cat /proc/self/status
```

Require the resulting `jreed` child to report zero in `CapPrm`, `CapEff`, and
`CapAmb`. Do not continue if the actual updater startup self-check fails, the
positive supplementary check fails, `protected_runtime_valid` is false, or any
child capability set is nonzero.

### Historical exact r0005 to r0006 manual production bridge

This historical procedure applies only to a station first proven to be on the
exact clean r0005 baseline. Do not rerun it blindly on an r0006-or-newer station
or any other baseline.

This procedure is deliberately checkpointed for interactive SSH. Every
pasteable block that can fail the checkpoint runs in a subshell, so `exit 1`
cannot terminate the parent login shell. Substitute the one reviewed r0006
commit only after it exists on the trusted remote.

1. From any operator shell, prove the live checkout is the exact clean r0005
release. Every Git process explicitly runs as the configured application user;
do not add a root `safe.directory` exception:

```bash
export ISA_ROOT=/home/jreed/isadoraair-django
export ISA_USER=jreed
export R0005_COMMIT=2eea69817d7894118b1473b0693a5c1514c5f54d
export R0006_COMMIT=REPLACE_WITH_THE_SINGLE_REVIEWED_R0006_SHA

(
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" branch --show-current)" = main || {
    echo "Production checkout is not on main" >&2
    exit 1
  }
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" rev-parse HEAD)" = "$R0005_COMMIT" || {
    echo "Production checkout is not exact r0005" >&2
    exit 1
  }
  test -z "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" status --porcelain)" || {
    echo "Production checkout is not clean" >&2
    exit 1
  }
  echo "Exact clean r0005 production baseline verified"
)
```

Stop the r0006 bridge unless this checkpoint succeeds.

2. Fetch and authenticate the one r0006 release commit as `ISA_USER`, then
materialize reviewed artifacts outside the live checkout as that same user:

```bash
sudo -u "$ISA_USER" git -C "$ISA_ROOT" fetch origin main
(
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" log --format='%H' --diff-filter=A origin/main -- \
    deploy/releases/r0006.json)" = "$R0006_COMMIT" || {
    echo "r0006 does not have the expected unique introducing commit" >&2
    exit 1
  }
  sudo -u "$ISA_USER" git -C "$ISA_ROOT" merge-base --is-ancestor \
    "$R0005_COMMIT" "$R0006_COMMIT" || exit 1
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" rev-list --count \
    "$R0005_COMMIT..$R0006_COMMIT")" = 1 || exit 1
  sudo -u "$ISA_USER" git -C "$ISA_ROOT" show \
    "$R0006_COMMIT:deploy/releases/r0006.json"
)
```

Stop the r0006 bridge unless this checkpoint succeeds. Then materialize without executing
repository code:

```bash
export UPDATER_STAGE="$(sudo -u "$ISA_USER" mktemp -d)"
sudo -u "$ISA_USER" git -C "$ISA_ROOT" archive "$R0006_COMMIT" \
  deploy/updater_runtime deploy/isadoraair-updater.service \
  deploy/releases/r0006.json | sudo -u "$ISA_USER" tar -x -C "$UPDATER_STAGE"
sudo -u "$ISA_USER" find \
  "$UPDATER_STAGE/deploy/updater_runtime" -maxdepth 3 -type f -print
```

Review the manifest, every runtime module, and the service candidate.

3. Before replacing protected files, root must edit
`/etc/isadoraair/station.json` so `update_execution_enabled` is `false`, then
restart only `isadoraair-updater.service`. Poll its settled response and require
PING to show execution is disarmed. Do not proceed from a single immediate
post-restart sample.

4. With execution confirmed disarmed, install only the reviewed files with
fixed system utilities:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec/isadoraair-updater
sudo install -d -o root -g root -m 0755 /usr/local/libexec/isadoraair-updater/isadoraair_updater
sudo install -o root -g root -m 0755 \
  "$UPDATER_STAGE/deploy/updater_runtime/updaterd.py" \
  /usr/local/libexec/isadoraair-updater/updaterd.py
sudo install -o root -g root -m 0755 \
  "$UPDATER_STAGE/deploy/updater_runtime/updaterctl.py" \
  /usr/local/libexec/isadoraair-updater/updaterctl.py
for module in __init__ checkpoint config daemon executor jobs process protocol release security staging systemd; do
  sudo install -o root -g root -m 0644 \
    "$UPDATER_STAGE/deploy/updater_runtime/isadoraair_updater/$module.py" \
    "/usr/local/libexec/isadoraair-updater/isadoraair_updater/$module.py"
done
```

5. Render and verify the exact r0006 unit outside `/etc`, explicitly confirm its
capability and sandbox lines, then install it:

```bash
sed -e "s|@@ISA_USER@@|$ISA_USER|g" -e "s|@@ISA_ROOT@@|$ISA_ROOT|g" \
  "$UPDATER_STAGE/deploy/isadoraair-updater.service" \
  > "$UPDATER_STAGE/isadoraair-updater.service"
systemd-analyze verify "$UPDATER_STAGE/isadoraair-updater.service"
(
  grep -Fx 'AmbientCapabilities=CAP_SETUID CAP_SETGID' \
    "$UPDATER_STAGE/isadoraair-updater.service" >/dev/null || exit 1
  grep -Fx 'NoNewPrivileges=true' \
    "$UPDATER_STAGE/isadoraair-updater.service" >/dev/null || exit 1
  grep -Fx 'UMask=0077' "$UPDATER_STAGE/isadoraair-updater.service" >/dev/null || exit 1
  echo "Rendered updater unit capability and sandbox contract verified"
)
```

Do not continue unless both verification commands succeed.

```bash
sudo install -o root -g root -m 0644 \
  "$UPDATER_STAGE/isadoraair-updater.service" \
  /etc/systemd/system/isadoraair-updater.service
sudo systemctl daemon-reload
sudo systemctl restart isadoraair-updater.service
```

No other service is restarted in this checkpoint.

6. Poll readiness for at most 30 seconds and inspect settled service state:

```bash
(
  updater_ready=false
  for attempt in $(seq 1 30); do
    if /usr/bin/python3 -I \
      /usr/local/libexec/isadoraair-updater/updaterctl.py ping; then
      updater_ready=true
      break
    fi
    sleep 1
  done
  if [ "$updater_ready" = true ]; then
    echo "Updater readiness check succeeded"
  else
    echo "Updater did not become ready within 30 seconds" >&2
    exit 1
  fi
)
```

Stop the r0006 bridge unless this checkpoint succeeds. PING must report protocol 3,
runtime 4, protected/config/repository readiness true, and
`update_execution_enabled: false`. Successful startup itself proves the fixed
`/usr/bin/id -u` application-user self-check passed. Then inspect the settled
unit rather than racing the restart:

```bash
systemctl show isadoraair-updater.service \
  --property=ActiveState --property=SubState --property=Result \
  --property=NoNewPrivileges --property=AmbientCapabilities
systemctl cat isadoraair-updater.service
```

Require active/running/success, `NoNewPrivileges=yes`, and ambient
`cap_setuid cap_setgid`. Run the manual transient-unit capability acceptance
above and require zero `CapPrm`, `CapEff`, and `CapAmb` in the `jreed` child.

7. Remove `/etc/systemd/system/isadoraair-updater.service.d/10-privilege-drop.conf`
only after `systemctl cat` proves the installed base unit itself contains the
ambient-capability setting and the checks above pass. After removal, run
`daemon-reload`, restart only the updater, repeat bounded readiness polling,
and repeat all settled capability/self-check assertions.

8. Fast-forward the live application source manually to the exact reviewed
r0006 commit. Every Git command remains explicitly constrained to `ISA_USER`;
the Update Center must not perform this protected-runtime transition:

```bash
(
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" rev-parse HEAD)" = \
    "$R0005_COMMIT" || exit 1
  test -z "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" status --porcelain)" || exit 1
  sudo -u "$ISA_USER" git -C "$ISA_ROOT" merge --ff-only "$R0006_COMMIT" || exit 1
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" rev-parse HEAD)" = \
    "$R0006_COMMIT" || exit 1
  test -z "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" status --porcelain)" || exit 1
  echo "Exact r0006 application source installed"
)
```

Stop the r0006 bridge unless this checkpoint succeeds. r0006 requires no
migrations, `collectstatic`, dependency installation, or nginx change. Because
r0006 changes Django code loaded by Gunicorn, restart only Gunicorn after source
advancement:

```bash
sudo systemctl restart isadoraair-gunicorn.service
(
  gunicorn_ready=false
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error \
      http://127.0.0.1:8000/healthz/ >/dev/null 2>&1; then
      gunicorn_ready=true
      break
    fi
    sleep 1
  done
  if [ "$gunicorn_ready" = true ]; then
    echo "Gunicorn readiness check succeeded"
  else
    echo "Gunicorn /healthz/ did not become ready within 30 seconds" >&2
    exit 1
  fi
)
systemctl show isadoraair-gunicorn.service \
  --property=ActiveState --property=SubState --property=Result
```

Stop the r0006 bridge unless the health probe succeeds and Gunicorn is settled
active/running with a successful result. Do not restart the engine, monitoring,
encoders, or RBDS. The protected updater was restarted separately during its
earlier root-owned runtime and unit replacement.

9. Verify Django health, exact Git identity, `/updates/` current release r0006,
and all station core services. The final Git identity check is again explicit
about application-user ownership:

```bash
(
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" branch --show-current)" = main || exit 1
  test "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" rev-parse HEAD)" = \
    "$R0006_COMMIT" || exit 1
  test -z "$(sudo -u "$ISA_USER" git -C "$ISA_ROOT" status --porcelain)" || exit 1
  echo "Final clean r0006 Git identity verified as $ISA_USER"
)
```

Keep execution disarmed until every check is healthy. Only then may root edit
`/etc/isadoraair/station.json` to set
`update_execution_enabled` to `true`, restart only
`isadoraair-updater.service`, repeat the bounded PING, and require `/updates/`
to report READY / ARMED.
