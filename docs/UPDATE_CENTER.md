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
release's commit identity is discovered EXTERNALLY, by the unique
commit that first introduced `deploy/releases/<release_id>.json` on
trusted canonical release ancestry
(`git log --diff-filter=A`, see `release_chain.resolve_release_commit`).
That path must have exactly one introducing commit on trusted canonical
release ancestry: modifying, deleting, or re-adding an immutable manifest
on that ancestry makes identity unresolvable and planning fails closed.
Unrelated local, worktree, review, and stale remote-tracking refs do not
contribute to release identity. Each normal release must also have its own
introducing commit; adding two manifests in one commit is ambiguous and
rejected.

Application-side planning batches that canonical manifest-history inspection:
one additions-only history traversal and one all-touches traversal cover the
ordinary manifests in the chain, and Python applies the same per-path rule
described above. This is only a process-count/performance implementation
detail. Immutable identity still comes exclusively from the supplied exact
canonical tip and remains fail-closed for absent, touched, deleted, re-added,
or duplicate-commit manifests. The scalar resolver remains covered as the
semantic oracle for the batched result.

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

## Installed-release identification: ancestry vs. exact-commit execution eligibility

**Real incident, KOGR r0008→r0009**: the live checkout's HEAD was a
descendant of canonical r0008 (some ordinary, unreleased commits had
landed on top of it before r0009 was cut) rather than r0008's own
canonical commit. `release_chain.resolve_installed_release()`
correctly identified "installed = r0008" via **ancestry** (release
commit == HEAD, or an ancestor of HEAD) and `build_plan()` offered a
valid-looking Install button for r0008→r0009. After confirmation, the
protected updater's `derive_plan()` correctly refused: *"live HEAD
must exactly equal one independently resolved release commit"* — its
own exact-match rule, unrelated to and stricter than the Django
planner's ancestry-based identification. Only after the checkout was
manually repositioned to r0008's exact canonical commit did the same
update succeed normally.

The fix (`SafetyStatus.CHECKOUT_NOT_AT_RELEASE_COMMIT`): `build_plan()`
now compares `head_sha` against the inferred installed release's
canonical commit *after* ancestry resolution but *before* deciding
`UP_TO_DATE`/`READY_TO_PLAN`. A mismatch refuses with this distinct
status — never implying corruption, an unknown release, git
divergence, or a dirty tree, and never repositioning the checkout
automatically (detection/refusal only). This closes both directions
ancestry-only resolution could get wrong: an unreleased descendant of
an *older* release (would otherwise wrongly proceed to
`READY_TO_PLAN`) and an unreleased descendant of the *latest* release
(would otherwise wrongly report `UP_TO_DATE` — "the latest release is
an ancestor of HEAD" is not the same claim as "the checkout is exactly
on the latest released source").

**The two concepts stay distinct, on purpose:**

```
ANCESTRY-BASED IDENTIFICATION   <- "which release is this checkout descended
                                    from" -- informational, used to build the
                                    release-chain diagnosis in the first place
EXACT-COMMIT ELIGIBILITY        <- "can the protected executor actually
                                    install from here" -- HEAD must equal
                                    that release's own canonical commit,
                                    nothing looser
```

**Multi-release catch-up is unaffected.** This rule applies only to
the *current* installed boundary, not to every intermediate release in
between: a station whose HEAD is *exactly* an older release's
canonical commit (e.g. exactly r0007) can still aggregate straight to
a later target (e.g. r0007→r0008→r0009 in one plan) — see
`MultiReleaseAggregateTests`/`CheckoutNotAtReleaseCommitTests` in
`updatecenter/tests/test_planner.py`. Nothing about this fix requires
stepping through and checking out every intermediate release.

The protected updater's own exact-match check
(`deploy/updater_runtime/isadoraair_updater/release.py`'s
`derive_plan()`) remains the authoritative root-side safety boundary,
unchanged by this fix — this Django-side check is defense-in-depth/
operator correctness (the operator should never be offered a plan the
root executor is guaranteed to reject), not a replacement for it.

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

## Managed-unit activation policy (r0022+)

Every unit declared in `systemd_units_changed`/`systemd_units_new_required` is
installed from the trusted staged target and (if any bytes actually changed)
daemon-reloaded, exactly as always. What happens next to a
`systemd_units_new_required` unit is no longer "always `enable --now`" —
it is decided per unit name by a **closed, protected-runtime-compiled
map**, `isadoraair_updater.release.MANAGED_UNIT_POLICIES`, never by
manifest text and never inferred from the unit's own `.service`/`.timer`
suffix:

- **`ENABLE_NOW`** — installed, then `systemctl enable --now`'d and
  verified active/healthy (`SystemdManager.verify_unit`). This is the
  only behavior a required unit had before this policy existed, and it
  is still exactly how the five core services (and each companion
  `.timer` below) work.
- **`INSTALL_ONLY`** — installed and covered by the same daemon-reload,
  but **never enabled and never started** by this updater. Only
  confirmed to have loaded successfully
  (`SystemdManager.verify_unit_loaded`, a fixed, read-only
  `systemctl show <unit> --property=LoadState` check — never
  `ActiveState`, and never anything that could execute the unit). This
  exists for a `Type=oneshot` `.service` that is meant only to be
  triggered by its own paired `.timer`.

`KNOWN_MANAGED_UNITS` (the allowlist `_install_one`/`manual_blockers`
enforce) is `frozenset(MANAGED_UNIT_POLICIES)` — the same map decides
both "may this updater ever touch this unit" and "what does touching it
actually do," so the two can never drift apart. As of r0022 the map
covers the five core services (all `ENABLE_NOW`) plus the Road
Conditions/KanDrive companion pairs:

```python
"isadoraair-sync-road-conditions.service": INSTALL_ONLY,
"isadoraair-sync-road-conditions.timer":   ENABLE_NOW,
"isadoraair-generate-road-condition-audio.service": INSTALL_ONLY,
"isadoraair-generate-road-condition-audio.timer":   ENABLE_NOW,
```

Both units of a pair are always installed in the same first pass, before
any activation happens at all — by the time an `ENABLE_NOW` `.timer` is
actually enabled, its paired `INSTALL_ONLY` `.service` is already on
disk (and already covered by the one daemon-reload), regardless of which
name a manifest happens to list first. A future companion unit requires
an explicit `MANAGED_UNIT_POLICIES` entry — a real, reviewable protected-
runtime code change — never an inferred default.

### Promoting a pre-existing template into the required contract

`_cross_check()`'s predecessor-diff match (above) used to assume every
`systemd_units_new_required`/`_new_optional` unit was genuinely **added**
in that release's own diff. That is too narrow for a unit that has been
tracked in `deploy/` for a long time (in Road Conditions' real case,
since before the release-manifest system existed at all) and is only now
being deliberately promoted into the managed/required deployment
contract — declaring that intent must never require a fake content edit
just to make the predecessor diff show an "A".

The revised invariant: each unit declared in
`systemd_units_new_required`/`_new_optional` is either

- **NEW FILE** — absent from the immediate predecessor commit — must
  appear as an actual `A` (added) in the predecessor diff, exactly as
  before; or
- **PROMOTED EXISTING FILE** — already present, byte-identical, at the
  immediate predecessor commit — permitted **only** when the unit name
  is already in `MANAGED_UNIT_POLICIES`/`KNOWN_MANAGED_UNITS`, so an
  unrelated pre-existing file can never be smuggled into the managed
  contract merely by naming it in a manifest. A "promoted" unit that was
  actually modified in this release (its bytes differ from the
  predecessor) still fails closed — it lands in the actual-changed set,
  which is compared only against `systemd_units_changed`, so declaring
  it solely as new-required/new-optional while its bytes differ is
  exactly the undeclared-modification case this check exists to catch.

Everything else about the predecessor-diff check is unchanged and still
fails closed: an actually-added unit omitted from every intent list, an
actually-modified unit omitted from `systemd_units_changed`, an
actually-removed unit omitted from `systemd_units_removed_or_renamed`,
and a unit appearing in more than one intent list at once (rejected by
the existing manifest-parsing duplicate check).

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
field set and protocol version 3 (`isadoraair_updater.PROTOCOL_VERSION` —
the client<->daemon wire protocol; kept a deliberately DIFFERENT number
from `MANIFEST_PROTOCOL_VERSION` below, which is a different boundary
entirely — see "Release-manifest protocol version" further down).
Exactly seven actions exist:

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

**Static-permission invariant (r0108).** nginx serves `STATIC_ROOT` directly as
another account, so after every collectstatic every directory there must be at
least `0755` and every file at least `0644` — whatever umask the run inherits.
The updater runs it under `UMask=0077`, which before r0108 left every *newly
created* static directory `0700` (nginx 403: r0107's `production/iportal/`,
and `weather/css`/`weather/js` before it). The application owns the fix, not
the protected runtime: the `staticfiles` storage
(`isadoraair/static_storage.py`, `settings.STORAGES`) creates under umask 022
(new entries are 0755/0644 from the moment they exist) and, as collectstatic's
post-process step, repairs the whole tree, adding bits only. **Nothing in this
path may change permissions, owner or group by pathname** — a pathname checked
and then changed can be swapped for a symlink in between (r0108 corrective:
Codex widened an external 0600 file through exactly that). The repair pins
`STATIC_ROOT` with `O_DIRECTORY|O_NOFOLLOW` relative to its parent, opens every
child relative to its parent's descriptor with `O_NOFOLLOW`, judges it by
`fstat()` and changes it with `fchmod()`; symlinks are skipped, a symlinked
root is refused, and without these mechanisms it fails closed (no path
fallback). Django's own post-create `chmod`/`chown` is disabled for the same
reason. A release therefore repairs earlier damage only when it declares
`collectstatic_required`. Do not "fix" this with the global
`FILE_UPLOAD_PERMISSIONS`/`FILE_UPLOAD_DIRECTORY_PERMISSIONS`: those govern
uploads and every other storage, which must stay restrictive. Regression:
`isadoraair/tests/test_static_permissions.py`.

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

## Release-manifest protocol version

A second, independent "protocol version" exists alongside the daemon's
own socket wire protocol (see "Narrow IPC and durable root state"
above) — each release manifest's `minimum_updater_protocol_version`,
compared against the protected runtime's own
`isadoraair_updater.MANIFEST_PROTOCOL_VERSION` and the application
planner's `updatecenter.manifest.UPDATER_PROTOCOL_VERSION` (the two are
maintained as one number, bumped together). A manifest declaring a
number higher than what THIS code understands is refused —
`UPDATER_UPGRADE_REQUIRED` from `manual_blockers()`, the
`UNSUPPORTED_PROTOCOL` check from `validate_manifest_dict` — never
best-effort interpreted.

Bumped only when a manifest field's *execution meaning* changes in a
way old planning/execution code could misinterpret — never for "a new
optional field was added." r0022 bumps it 3 → 4:
`systemd_units_new_required` stopped meaning "always `enable --now`"
and started meaning "install always; activate per
`MANAGED_UNIT_POLICIES`" (see "Managed-unit activation policy" above).
An updater still running protocol-3 code that fetched an r0022+ release
(as it would immediately after `fetch()`, before its own manual-
bootstrap upgrade) must refuse to plan it with `UPDATER_UPGRADE_
REQUIRED`, not silently `enable --now` a companion `.service` that was
only ever meant to be installed.

Deliberately a **different number** from the daemon's own wire protocol
(still 3, unchanged by r0022) — the two cover genuinely different
boundaries (an operator's CLI/Django client talking to this daemon,
versus this daemon's own interpretation of a release author's manifest)
and must be free to change independently; conflating them once already
broke every existing socket client during this feature's own
development (see `test_phase_b_protocol.py`'s
`test_runtime_v4_keeps_wire_protocol_v3`, and
`isadoraair_updater/__init__.py`'s own comments on both constants). The
manifest schema itself is unchanged by this bump — no executable or
activation-policy field was added to it; activation policy is, and
remains, compiled protected-runtime code, never manifest-controlled.

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

## Reviewed migration approval

Motivated by the real r0089 release (roadmap 2.5's `authz` app + `library.0085`):
its migration set contains six operations (one `ManyToManyField`-through
`AddField`, five `RunPython` data-seed operations) that are genuinely safe
against the actual pre-release production schema but that the protected
executor's deliberately narrow mechanical classifier cannot prove automatic on
its own. Before this section, a job hitting `MIGRATION_OPERATION_MANUAL` had no
path forward except `manual_intervention_required` forever, or manually running
`manage.py migrate` outside the protected updater entirely -- exactly the
shortcut this whole architecture exists to make unnecessary. This section
closes that gap as a **general** capability, not an r0089-specific bypass nor
any weakening of the classifier: it never becomes more permissive, and this
mechanism works for any future release with genuinely reviewed non-additive
operations.

### Three-tier migration safety model

```
Tier 1: manifest author intent      migration_compatibility ("additive"/"destructive")
Tier 2: mechanical classification   updatecenter_probe's per-operation classifier
Tier 3: privileged human review     protected ApprovalStore (this section)
```

Tier 1 is what a release author *declares*. It is trusted as authored, reviewed
intent (see "Cross-checking: machine-verifiable facts vs. release-author intent"
above) and gates the coarse `MIGRATION_NOT_AUTOMATABLE` check -- a release
declaring `"destructive"` is blocked outright, before Tier 3 is ever consulted.
Tier 2 is what the executor can *mechanically prove* about each individual
operation without a human: only `CreateModel` and a narrow, specific shape of
`AddField`/`AlterField` (see `updatecenter_probe.py`'s `_classify_add_field`/
`_classify_alter_field`) are ever classified `"additive"`; everything else
(every `RunPython`, every relational/unique/indexed/no-default `AddField`, every
database-affecting `AlterField`) is `"manual"` -- deliberately, permanently, and
this section does not loosen that rule. Tier 3 is a privileged human's explicit,
recorded review of the SPECIFIC operations Tier 2 could not prove, for a SPECIFIC
release and a SPECIFIC exact plan. Tier 3 can never substitute for Tier 1 or
Tier 2 -- it only ever supplies the one missing fact Tier 2 cannot derive on its
own ("a human looked at these exact operations against the actual production
schema and confirms they're safe"), and it is consulted only after Tier 1 and
Tier 2 both already say "otherwise fine."

### Protected authority and the r0092 bootstrap correction

The original Django-model authority could not bootstrap itself. The first plan
requiring review also contained `updatecenter.0003`, which creates
`updatecenter_migrationplanapproval`; the staged probe queried that table before
any migration was allowed to run. Merely catching `UndefinedTable` would have
made discovery work but still left nowhere to persist an approval.

r0092 therefore moves authority to `ApprovalStore`, a separate protected-
runtime component under `/var/lib/isadoraair-updater/migration-approvals`.
The directory is root-owned mode 0700 and records are mode 0600, bounded to
1,000 records and 256 KiB each. Creation uses an exclusive temporary file,
`fsync`, a no-replace hard-link publication, and directory `fsync`. Corrupt or
wrong-mode exact records fail closed. There is deliberately no replacement or
revocation operation; an exact duplicate is idempotent and preserves the first
audit record.

Approval creation reads all authoritative facts from a terminal root-owned Job
A record. A client supplies only the discovery UUID, a digest confirmation,
operator name, and mandatory reason. Root rejects missing/nonterminal jobs,
the wrong failure classification, incomplete/malformed evidence, a mismatched
trusted plan, a wrong digest, or oversized data. The durable approval copies
the full manual-operation and release-chain snapshots, so consumption no
longer depends on Job A remaining within bounded job retention.

`updatecenter_probe` is discovery-only: it computes the real graph, operation
classifications, manifest hash, digest, and manual-operation list, but never
queries an approval table. The executor performs the protected lookup only
after conflict, replacement, dependency-closure, pre-applied, and destructive-
manifest gates have passed. `MigrationPlanApproval` remains because published
migration history is immutable, but it is only a deprecated application audit
mirror and has no authorization effect.

Wire protocol 4 adds only `APPROVE_MIGRATION_PLAN`. Generation 8 also accepts
protocol 3 for the still-installed r0088 client; PING replies in the caller's
version and candidate readiness advertises `[3, 4]`. The protected CLI uses v4.
The later web UI retains its Django-superuser boundary and calls the same
protected action before writing its optional audit mirror.

### Canonical migration plan digest

`updatecenter_probe.compute_migration_plan_digest()` binds, as one canonical
JSON object hashed with SHA-256:

- the target `release_id` and trusted `target_commit`;
- a SHA-256 of the release manifest's own bytes at that commit;
- the COMPLETE ordered plan -- every migration (not just the manual subset),
  each migration's own ref, a SHA-256 of that migration FILE's actual bytes (so
  a same-named migration with different content never collides), and every
  operation's type/classification/detail, in plan order.

Any change anywhere in this -- a different target commit, a different manifest,
a different migration's bytes, an added/removed/reordered/reclassified
operation -- produces a different digest. There is no separate invalidation
logic for "the plan changed": a stale approval simply stops matching a freshly
computed digest. See `test_migration_plan_digest.py` for the exhaustive proof
of each sensitivity the workorder required.

### Approval scope and identity

The protected record is keyed by the SHA-256 of a canonical five-part tuple:

- target release ID;
- target commit;
- target manifest SHA-256;
- complete migration-plan digest;
- trusted-plan fingerprint (which binds the release chain and aggregate
  execution intent).

It also retains the discovery-job UUID, exact manual-operation snapshot,
release and migration snapshots, immutable operator name, UTC timestamp, and
mandatory reason. The plan digest already changes for migration bytes, order,
classification, additions, and removals; the other tuple fields independently
bind release/commit/manifest/chain identity.

There is no reusable wildcard approval. An approval only ever matches the EXACT
plan it was created against; it says nothing about any other release, any other
target commit, or the same release with even one migration operation changed.

### Executor gate

```
target staged
  -> migration plan mechanically classified (discovery-only updatecenter_probe)
  -> all operations additive?
       YES -> continue exactly as before (unchanged automatic path)
       NO  -> executor constructs the exact five-part identity
              protected ApprovalStore exact match -> log, continue
              no match -> MIGRATION_OPERATION_MANUAL, terminal Job A,
                          with complete review evidence recorded by root
```

This is `executor.py`'s `_validate_target_schema` -- the approval check is the
LAST thing in that function, after every existing check (conflicts,
replacements, dependency closure, pre-applied-migration detection, the
manifest-level `"destructive"` gate). None of those become weaker or
conditional on an approval; an approval can only ever satisfy the one narrow
"this operation is outside the mechanical automatic allowlist" gate, nothing
else. A conflicting graph, a destructive manifest, a pre-applied migration, an
unreachable protected runtime, or a failed signature check all still fail
exactly as before regardless of any approval.

### Terminal jobs and the new-job requirement

`manual_intervention_required` remains a terminal `UpdateJob`/root job state --
unchanged, and this section adds no resume/retry/override action to either.
The job that first discovers a manual-operation set is never mutated or
resumed. Approving creates a separate protected ApprovalStore record and
nothing else -- the operator must start a genuinely new `UpdateJob`. That new
job's executor performs its own complete, independent staging and
classification from scratch; it happens to find a matching approval this time
only because nothing about the plan has changed since review.

### Bootstrap CLI and later web workflow

1. Job A stops `MIGRATION_OPERATION_MANUAL`. On an r0088 application host, run
   the generation-8 CLI from the active protected slot as OS root:

   ```bash
   sudo /usr/bin/python3 -I -B \
     /var/lib/isadoraair-updater-bootstrap/runtime-slots/<active-slot>/updaterctl.py \
     migration-review <job-a-uuid>
   sudo /usr/bin/python3 -I -B \
     /var/lib/isadoraair-updater-bootstrap/runtime-slots/<active-slot>/updaterctl.py \
     approve-migration-plan <job-a-uuid> --operator <name>
   ```

   The command obtains evidence from root, displays release, commit, manifest
   hash, trusted-plan fingerprint, digest, every manual operation and an
   explicit warning. It requires typing the complete digest and a nonempty
   reason. The CLI refuses review/approval unless effective UID is 0; files are
   never written through a user-controlled directory.
2. Start Job B with a fresh UUID. If nothing
   about the plan has changed, the executor's fresh, independent recomputation
   finds the matching approval and proceeds through the ordinary checkpoint/
   migrate/advance/restart pipeline exactly as any fully-automatic release would.
3. After application source advances, the existing review page remains
   staff-viewable/superuser-approvable. Its POST is a narrow client of protocol
   4 and root derives authority from Job A; Django never supplies the five-part
   tuple wholesale. A successful protected decision may be mirrored into the
   legacy table for display/audit only.

The application service identity is already authorized on the updater socket
for Update Center operations; the web route adds the existing authenticated
superuser/CSRF boundary. The bootstrap CLI adds the stronger local root check
because r0088 cannot render the new UI.

### Runtime 11: companion authorization, migration preflights, partial-prefix recovery

Protected runtime 11 is the capability boundary. **Release manifests do not
change.** They stay manifest protocol 5 indefinitely
(`MANIFEST_PROTOCOL_VERSION = 5`, `UPDATER_PROTOCOL_VERSION = 5`; every
`minimum_updater_protocol_version` stays at most 5). No manifest field points at
any of the new behavior. Both runtime 11 and the Django validator reject
`migration_authorization` and `migration_preflight_checks` as unknown manifest
fields; they were briefly proposed and then withdrawn.

#### Why runtime 10 can always bootstrap

Before handing off, a runtime-10 worker runs, in order:

1. `derive_plan()`, which parses **every** manifest on the trusted tip;
2. the expected-fingerprint check;
3. the minimum-protocol gate (`manual_blockers`).

Because every manifest stays protocol-5 shaped, and companions live where
runtime 10 never looks, a station that skipped r0105 still:

* parses the whole chain, including later releases' companions;
* derives its plan (for example r0104 → r0107);
* selects r0105 as the required intermediate protected-runtime transition;
* hands off to runtime 11 before staging or probing.

Runtime 11 then re-derives the **same** fingerprint, because companions are
never fingerprint input, and performs the migration work.
`updatecenter/tests/test_runtime11_bootstrap.py` proves this with the actual
r0104 runtime, extracted from Git, on the real release chain.

The r0105 protected-runtime descriptor advertises `runtime_version: 11` with
`manifest_protocol_version: 5`.

#### Trusted companion migration authorization

Path convention: `deploy/migration_authorizations/<release_id>.json`, at most
one per release.

* **Why runtime 10 ignores it:** runtime 10 enumerates only
  `deploy/releases/*.json`, and its predecessor-diff rules match only:
  * new files in a directory literally named `migrations`;
  * top-level `deploy/*.service` / `deploy/*.timer`;
  * anything under `deploy/updater_runtime/`;
  * `requirements.txt` and `deploy/isadoraair.nginx`.
* **Provenance:**
  * the release must exist in the trusted chain;
  * the companion is added exactly once, strictly after that release's
    introducing (target) commit, on the trusted tip's history;
  * it is introduced by a single-parent commit that changes only
    `deploy/migration_authorizations/`;
  * any later modification, deletion or re-addition **fails closed**.
* **Trust:** it inherits the trust of the root-configured, fast-forward-only
  trusted repository. It has **no signature of its own**.
* **Closed schema (`schema_version: 1`):**
  * `release_id`
  * `target_commit` (the release's own introducing commit)
  * `manifest_sha256` (SHA-256 of that release's manifest bytes, computed from
    Git)
  * `authorized_manual_operations`: entries of `{ref, migration_file_sha256,
    operation_index, operation, classification: "manual"}`
* **Release-local:** a companion for release R may authorize only operations
  in migrations R itself declares in `migrations_required`, pinned to their
  exact bytes at R's commit.
* **Discovery (runtime 11):** for a plan with manual operations, every release
  in `releases_in_plan` is checked. Each present companion is validated
  against its own release, and their authorized sets are unioned. Central
  approval applies only if **every** manual operation in the station's own
  derived plan (exact ref, file SHA-256, index, operation, classification) is
  in that union. Extra authorized operations never add anything to a plan. If
  coverage is incomplete, exact station-local approval still applies.
* **Absence and failure:** an absent companion contributes nothing. A
  malformed or badly provenanced companion fails the job before any mutation.
* **Fingerprint and digest:** companions are never part of the trusted-plan
  fingerprint or the migration-plan digest.
* **Validation:** `manage.py validate_release_manifests` applies the same rules
  through an independent mirror (`updatecenter/migration_authorization.py`)
  and rejects stray files in the directory. Runtime/Django parity is
  regression-tested case by case.

**Publication sequence.** The remote must learn both commits in one ref
update:

1. Create the release commit and manifest.
2. Record its immutable commit SHA and the manifest's SHA-256.
3. Create the metadata-only companion commit immediately after it.
4. Run `manage.py validate_release_manifests`.
5. Push the branch **once**, advancing the ref directly to the companion
   commit.

A station that installs before a companion exists simply needs local
approval; preflights do not depend on companions.

#### Read-only migration preflights

Preflights live in the target application code, not in manifests.
`updatecenter_migration_preflight --pending <ref>…` receives the trusted
pending migration refs from runtime 11. It runs the checks registered for
those refs in an explicit registry, inside one PostgreSQL `READ ONLY`
transaction that is always rolled back. If a ref it was told is pending is
already recorded as applied, it fails closed. Each registered check models
exactly what that pending migration will reject:

* **`library.0087_backfill_default_schedule_profile` pending:**
  `library.0087.global_slot_ambiguity`. This is 0087's own pre-check:
  duplicate `(day_of_week, start_time)` or `(specific_date, start_time)`
  **globally, ignoring profile**.
* **`library.0088_enforce_schedule_profile_integrity` pending:**
  `library.0088.profile_integrity`, covering 0088's per-profile uniqueness
  and its NOT NULL profile and `ScheduleProfileState` pointers. If 0087 is
  also pending, rows are evaluated as 0087 will leave them.
* **0088 applied:** neither check runs.
* **`production.0001_initial` pending (r0107, the first release with
  ProductionMedia):** two read-only **host** checks
  (`production/services/host_capability.py`) rather than data checks --
  `production.0001.validation_host_capability` (unified cgroup v2; `cpu`,
  `memory`, `pids`; `cgroup.kill`, `memory.swap.max`, `memory.oom.group`,
  `pids.max`, `cpu.max`; systemd >= 254; a functional Landlock + seccomp
  self-test) and `production.0001.media_root_establishable`. They move the
  refusal of a host that cannot run `isadoraair-validation` ahead of every
  application mutation. Writability is judged from ownership and mode, not
  `os.access()`, because the updater's own `ProtectSystem=strict` namespace
  makes `/srv` read-only to it. See `docs/releases/r0107.md`.

Evidence names the migration and check, the global or profile scope, the
conflicting key, the true count, bounded row IDs and truncation.

#### Partial-prefix recovery

Migrations run one at a time.

**During execution.** Before each migration, root-owned evidence records it
as `in_flight_migration` together with a fresh uuid4 `in_flight_nonce`. On
its own this claims nothing. The worker then runs the staged target's
`manage.py updatecenter_apply_migration_guarded`, the ownership boundary. In
**one PostgreSQL transaction on one session**, it:

1. sets `SET LOCAL lock_timeout = '120s'` and takes
   `LOCK TABLE django_migrations IN ACCESS EXCLUSIVE MODE`;
2. proves that the live transition rows are exactly the owned prefix, with the
   same `id` and `applied` for every row. The requested migration must be the
   first unowned one, it must be absent, and there may be no duplicate or
   extra transition rows. The nonce must be unused;
3. requires Django's own plan to that target to be exactly that one forward
   migration, and the migration to be atomic. Otherwise the result is
   `NON_ATOMIC_UNSUPPORTED`, manual, and fails closed;
4. applies it with Django's real `migrate`, in-process, on the same
   connection;
5. requires exactly one new recorder row (that migration) and nothing else
   changed;
6. inserts a receipt into `updatecenter_guarded_migration_receipt`. The
   receipt holds the nonce (primary key), `job_id`, the migration, the
   `django_migrations.id` (unique) and the exact `applied` timestamp.

Schema change, recorder row and receipt commit together or not at all. A
refusal is raised inside the transaction, so everything rolls back, including
a first-time creation of the receipt table. It is then reported as
`{"status": "refused", "reason"}`. The receipt table therefore exists only once
a guarded apply has committed; a missing table means "no receipt". Runtime
code only creates, inserts and reads receipts. Nothing updates or deletes
them.

The command's JSON output is never trusted alone. The worker re-reads
`django_migrations` and the receipt directly via `psql` (read-only, using the
checkpoint's database identity). It extends the owned prefix only when the
applied set is exactly the prefix plus this migration, and when the row and
the receipt match the nonce, job, migration, `id` and `applied` exactly. It
then persists, in one write:

* the contiguous applied prefix;
* each prefix row's exact `id` and `applied` timestamp;
* the cleared in-flight migration and nonce.

The outcomes of a race on that migration are:

* **An external migration that commits before the lock.** The guard
  refuses. The job fails `TARGET_MIGRATION_PREAPPLIED` with no receipt, and
  nothing is ever claimed.
* **An external `migrate` that starts after the lock.** It blocks on its
  first read of `django_migrations` until the updater commits, then finds the
  migration applied.
* **A raw insert that starts after the lock.** It also blocks, and can only
  land after the commit. The result is a duplicate row, which observation
  reports as `MIGRATION_RECORD_AMBIGUOUS`, and it is never credited.

**Finalization after any interruption.** This covers a migration failure, any
other exception, and `AMBIGUOUS_INTERRUPTED_MIGRATION` after a hard kill.
Evidence is finalized from database observation only. The observation may
extend the recorded prefix by **at most the single recorded in-flight
migration, and only with that nonce's exact receipt**. Several receipt states
leave the evidence unfinalized:

* a missing receipt;
* a duplicate receipt;
* a receipt that contradicts the evidence;
* a receipt that cannot be read;
* any other applied transition migration.

A receipt therefore never makes a later migration updater-owned, and nothing
an operator applies after a crash can be claimed.
PostgreSQL that is still recovering after a power loss gets a bounded
readiness wait (about 60 s). If it is still unavailable, the evidence stays
unfinalized, and a later exact retry repeats the same observation-based
finalization before deciding.

**Continuation.** Recovery requires finalized evidence for the exact release,
target, manifest and fingerprint, plus a verified checkpoint. The owned prefix
is then re-proven by `_assert_owned_prefix`: observed transition rows must be
exactly the prefix, with identical row identity. This runs:

* at recovery discovery;
* during recovery target validation, where the recovery probe may not widen
  the applied set;
* immediately before `migration_started`;
* for a complete prefix, after the verification probe and immediately before
  `database_verified`.

Any discrepancy is `TARGET_MIGRATION_PREAPPLIED`. Migration records are never
faked, rewritten or rolled back.

Every operation in a non-atomic (`atomic=False`) migration is classified
manual, because such a migration can partially commit before Django records
it. No current IsadoraAir migration is non-atomic.

The receipt proves ownership only, not correctness. Target-schema
verification, migration-plan completion, `database_verified` and the
source-advance gate run unchanged. `source_advanced` still happens strictly
after full database verification.

**Operational notes for the guarded apply:**

* **Brief blocking.** `ACCESS EXCLUSIVE` on `django_migrations` blocks every
  reader of that table for the duration of one migration. This includes
  `manage.py migrate`, `showmigrations` and `pg_dump`. Application requests do
  not read it.
* **Bounded lock waits.** Lock waits are bounded by `lock_timeout = 120s`.
  This covers the initial lock and every lock the migration's own DDL waits
  for. A timeout rolls everything back. The job fails `MIGRATION_LOCK_TIMEOUT`,
  which is retryable rather than manual: nothing changed and there is no
  receipt.
* **Backups.** A concurrent backup (`pg_dump` holds `ACCESS SHARE`) or a
  concurrent migration can cause that retryable timeout. Schedule installs
  outside backup windows.
* **Deadlocks.** PostgreSQL may choose either competing transaction as a
  deadlock victim. Both outcomes are safe:
  * if the updater is the victim, the guard rolls back completely, reports
    `deadlock_victim`, and the job fails `MIGRATION_LOCK_TIMEOUT`;
  * if the other session is the victim, the updater commits with its receipt.
* **Never kill sessions.** Never terminate database sessions to "help" an
  install along. Let the bounded timeout expire and retry later.

Update Center shows the recovery block only for a failed job whose finalized,
proven evidence permits a retry. It shows the executor-recorded authorization
source: `central`, `local` or `not_required`.

### Migration declaration completeness (r0092+)

Both application release cross-checking and the protected executor now compare
each r0092-or-later release to its predecessor. Every newly added file matching
`*/migrations/NNNN_name.py` must map to an entry in that release's own
`migrations_required`. This is prospective so immutable r0001-r0091 history is
not reinterpreted. Existing declared-path and aggregate dependency-closure
checks remain independent. r0092 explicitly declares the previously omitted
`updatecenter.0003`; aggregating from r0088 therefore yields r0089's eight
migrations plus that migration, exactly nine.

### r0092 production recovery

Do not retry either terminal r0091 job. Publish and activate signed protected
generation 8/runtime 9 first through the ordinary protected handoff. Start a
fresh r0088→r0092 Job A, confirm it stops before `migration_started`, review and
approve via the root CLI, then start a fresh Job B. Job B must independently
produce the same identity, apply all nine migrations, verify the database,
advance source, and perform declared restarts. Keep execution disarmed on any
identity mismatch or unexpected graph.

### r0093 exact-active runtime idempotency recovery

Production proved one further lifecycle requirement after r0092 generation 8
activated successfully and Job A stopped at manual review: the required fresh
Job B reconstructs the same trusted release plan while application source is
still r0088. Generation 8 treated that already-completed transition as a new
candidate, replaced the inactive slot, and then correctly rejected `8 > 8`.

Generation 9/runtime 10 distinguishes three authoritative supervisor states
before publishing candidate bytes:

- exact active `(generation, descriptor)` equality records a per-job
  `runtime_already_authoritative` milestone plus a narrow root-owned evidence
  record bound to the introducing release/commit and trusted-plan fingerprint;
  no slot, activation, or handoff operation occurs;
- same/higher active generation with any nonmatching identity fails closed as
  replay/rollback ambiguity before either slot is touched;
- a genuinely newer target is fully materialized and independently verifies
  its signature, descriptor, inventory, bundle, and candidate policy under
  `.staging` before the inactive/previous-LKG slot is replaced. The supervisor
  still repeats its own independent verification before activation.

The exact-active milestone is deliberately distinct from
`runtime_activation_accepted`, but both satisfy the same central and per-
mutator invariant. On resume, an exact-active job re-derives and fingerprint-
checks the trusted plan and re-reads the supervisor; the persisted evidence is
not accepted by itself. Equality of generation alone is never sufficient.
Wire protocols remain 3/4 and manifest protocol remains 5.

### r0094 atomic release packaging (release-identity closure)

r0093's first production attempt failed with `RUNTIME_HANDOFF_FAILED:
attestation 'deploy/updater_attestations/r0093-primary.json' is unreadable at
4643760`. Nothing cryptographic was wrong. r0093's manifest and descriptor were
committed unsigned (`4643760`) and its attestation was added by a later commit
(`f43266c`). A release's immutable identity is the single commit that added
`deploy/releases/<id>.json` (`TrustedRepository.introducing_commit()`, which
also requires the manifest to be touched exactly once, ever), and the worker
reads the descriptor, every runtime file and every attestation from **that**
commit. A later commit can never repair it, and touching the manifest again
makes the release unresolvable rather than fixed. r0093 therefore stays in
published history unmodified; r0094 supersedes it.

Packaging rule: **the first commit that introduces `deploy/releases/<id>.json`
must also contain the descriptor, every inventory file and every attestation it
declares.** Never commit an unsigned release. The order is: build the
descriptor and manifest in the working tree, emit the statement, stop for
offline signing, write the attestation into the same working tree, confirm the
descriptor is unchanged, then make one release-introduction commit.

`manage.py validate_protected_runtime_release` enforces this ("release-identity
closure") and reports it as `release_identity` in its evidence:

- `prospective_atomic` -- the manifest has never been committed (the correct
  state during signing). Every artifact the manifest declares must exist in the
  working tree and must not be Git-ignored.
- `committed` -- the manifest is in history. The validator resolves the release
  with production's own `introducing_commit()`, runs production's own
  `materialize_candidate()` and `stage_attestations()` against that commit, and
  then performs the complete authoring validation on bytes read from that
  commit, never from the working tree. `--identity-tip` (default `HEAD`)
  stands in for production's fetched canonical tip.

Re-run the validator in `committed` mode on the release commit before any
publication. The r0090/r0091/r0092 releases satisfy it; r0093 is rejected with
production's exact error.

### Publication never advances the live checkout (P0 1.2)

Invariant: **publishing or signing a candidate may advance canonical
repository metadata, but must never advance the production application
checkout. Only the protected Update Center installation path may advance
`/opt/isadoraair`.**

Why this needs stating: every development worktree on the station host
shares one Git repository with the production checkout, and that
checkout has `main` checked out. `refs/heads/main` is therefore not "a
local publication ref" -- it *is* the live checkout's branch. During the
r0105 publication (2026-10-03, 14:04:18 CDT) the publishing agent ran
`git merge --ff-only <r0105 SHA>` in the production checkout in order to
push `main:main`, which advanced the live application source about eight
minutes before the Update Center was asked to install it. The operator
reset it at 14:10:55; the Update Center's own install fast-forward
followed at 14:12:33. No versioned tool did this -- the publication
procedure itself fast-forwarded and then verified local `main`.

Canonical publication, from the release worktree (never from
`/opt/isadoraair`), pushing the exact approved SHA without touching any
local branch:

```bash
LIVE_BEFORE="$(git -C /opt/isadoraair rev-parse HEAD refs/heads/main)"
git push github-write <release-sha>:refs/heads/main
git ls-remote github-write refs/heads/main
git ls-remote origin refs/heads/main
test "$(git -C /opt/isadoraair rev-parse HEAD refs/heads/main)" = "$LIVE_BEFORE" \
  && test -z "$(git -C /opt/isadoraair status --porcelain)" \
  && echo "publication invariant held: /opt/isadoraair unchanged"
```

Both `ls-remote` lines must print exactly the release SHA. A plain
(non-`--force`) push is refused unless it is a fast-forward of the
remote branch, so no local fast-forward is needed to get that
guarantee. `git fetch` into the shared repository is fine (it updates
only `refs/remotes/*`). Never, as part of publication: `git merge`,
`git pull`, `git reset`, `git checkout`/`switch`, `git branch -f main`
or `git update-ref refs/heads/main` -- in or against the production
checkout, or from any worktree sharing its repository. Likewise never
make "local `main` equals the release SHA" a publication acceptance
check: before the Update Center installs, local `main` correctly stays
at the installed release.

### Post-install obligation: protected-runtime generation changes

A release that changes `deploy/updater_runtime/protected-runtime-descriptor.json`
(its manifest declares `protected_runtime` with a new generation) leaves
every station's activated disaster-recovery payload stale the moment the
Update Center installs it, and the next nightly backup fails closed by
design. The operator must, on every station that installed it, refresh
and activate the Phase-D recovery payload -- see
`docs/RUNTIME_BACKUP_PAYLOAD.md`, "Publishing a schema-2 Phase-D recovery
payload", "When to re-run this" -- then run a backup and a round-trip
verification before treating the station's recovery assurance as
restored. r0105 (generation 10 -> 11) was the first release where this
step was omitted.

### Remaining limitations

- The classifier itself is unchanged and deliberately conservative -- a
  `ManyToManyField`-through `AddField` (like r0089's `authz.0001_initial`,
  which Django's own schema editor never emits any SQL for at all) is still
  classified `"manual"` and still requires review every time it recurs, unless
  a future, SEPARATE, narrowly-scoped classifier improvement is made (the
  original workorder explicitly left this optional and separate from this
  section; not done here).
- Review quality is only as good as the human doing it -- this section
  supplies binding, audit, and a hard technical guarantee that an approval can
  never apply to a changed plan, but it cannot verify that the written
  `reason` is actually correct. That judgment remains the approving
  superuser's own responsibility, same as every other privileged Update Center
  action.
- A first approval still requires Job A to exist at creation time. Once
  created, the protected record is self-contained and survives later JobStore
  retention of its source job.
