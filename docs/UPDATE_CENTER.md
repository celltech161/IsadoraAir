# Update Center — design & usage (Phase A)

[P0] Bucket 1 / 1.1. This document covers what exists **today**
(Phase A: non-privileged foundation only) and the design constraints
future phases must respect. The private/gitignored architecture notes
used during review are not part of the deployable product; this
Git-owned document is the authoritative shipped contract.

## What Phase A actually is

A read-only `/updates/` status page (staff/superuser only) showing
installed source, running-software version skew (reusing
`isadoraair/version_info.py` and `monitoring/services/release_status.py`
unmodified), and — when a release chain is present and safe to plan —
a computed update plan. **There is no working "Update IsadoraAir"
button, no privileged code, and no code path that changes the
checkout, runs a migration, runs pip, installs/reloads systemd,
restarts a service, writes nginx config, or runs apt.** This is
enforced, not just documented — see `updatecenter/tests/
test_security_contract.py`.

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

## Station-config contract (schema only — nothing installed by Phase A)

The architecture report originally proposed `deploy/station_config.json`
for persisting the six `@@PLACEHOLDER@@` render values
(`ISA_USER`/`ISA_ROOT`/`ISA_HOME`/`SYNDICATED_ROOT`/`WEATHER_ROOT`/
`OGREMOTE_ROOT`) a future systemd-reconciliation step would need.
Review corrected this: a file under the git checkout, writable by the
same account Gunicorn runs as, is exactly the wrong place for a value
a future privileged executor would trust for rendering system-level
unit files — a compromised Gunicorn process could redirect where units
get rendered. **Phase A does not create this file.** The intended
shape, for whenever it is actually built:

```json
{
  "schema_version": 1,
  "isa_user": "isadoraair",
  "isa_root": "/opt/isadoraair",
  "isa_home": "/home/isadoraair",
  "syndicated_root": "/home/isadoraair/syndicated-ingest",
  "weather_root": "/home/isadoraair/weather-ingest",
  "ogremote_root": "/home/isadoraair/ogremote-ingest"
}
```

Intended location: **outside the git checkout**, e.g.
`/etc/isadoraair/station.json` — root-owned, root-writable only,
world/group-readable is fine (no secrets in it, just paths). The
future privileged executor reads this directly; Gunicorn must never be
able to write it.

## Privilege boundary (Phase B, not built yet — encoded here so Phase A doesn't foreclose it)

The future privileged executor **must not** run from
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
needs root (systemd install/`daemon-reload`/enable/restart, possibly
apt) runs as root.

**Existing, unrelated gap this must not extend**: `hardware/admin.py`,
`rbds/admin.py`, and `monitoring/views.py` already call `sudo systemctl
restart <unit>` directly from Django, backed by this box's `jreed`
account having unrestricted `(ALL) NOPASSWD: ALL` sudo — meaning
Gunicorn is not meaningfully unprivileged **today**, independent of
this feature. Before Phase B/C's Update button is ever enabled in
production: (1) these three existing restart paths must move to
whatever constrained mechanism Phase B builds, and (2) the unrestricted
`NOPASSWD: ALL` grant must be removed. Building a narrow updater while
leaving an equivalent unrestricted root path open through Gunicorn
would not actually close the exposure.

## Other locked decisions

- **No automatic rollback is promised**, anywhere in the UI or this
  documentation. Every failure case in the architecture report's own
  §17 resolves to "operator required" beyond the trivially-safe
  nothing-changed-yet cases.
- **apt packages are never auto-installed.** A manifest declaring
  `apt_packages_new` makes the plan `manual_system_package_action_required`
  — a hard stop, not a prompt to `apt install` anything.
- **Pre-migration DB checkpoint retention (Phase B, not built)**: keep
  the last 5, or 30 days, whichever is less restrictive at the moment
  of pruning; always retain the newest successful checkpoint until a
  newer one exists. Not implemented in Phase A — `pg_dump` execution is
  entirely a Phase B concern.

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

The same logic applies to the future Phase B systemd unit
(`isadoraair-updater.service`) — it, too, must be installed manually
the first time, following whatever install instructions Phase C adds
to `deploy/README.md`.
