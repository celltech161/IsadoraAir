# r0116 installation readiness (operator handoff — not executed)

**Status: preparation only.** Nothing in this document has been run on a
station. Installing r0116 at Oak Grove Radio (KOGR) or WRJE requires a
separate production-installation authorization, the published (pushed)
release, and this preflight passing on **that** station. Release notes:
`docs/releases/r0116.md`.

## 1. Supported starting points

| Installed release | Live HEAD (must be exactly this commit) | Qualification |
| --- | --- | --- |
| r0115 | `fd3ac805e774f6039636f3f8178f5770ea038572` | planned by the installed runtime 12 itself: r0116 only; no blockers |
| r0114 | `9da1b4ff74d3389fbfd3955e7ed3dcd0adca341d` | planned by runtime 12: r0115 + r0116 in one job; no blockers; **no bridge release needed** |
| r0113 / r0112 | their release commits | derived plans have no blockers (more migrations, collectstatic; from r0112 also the spoken render worker restart) — review that station's plan |
| r0111 or older | — | not qualified here; review that station's own plan before anything else |

The manifest's floor is `minimum_supported_release_id: r0081`, but only the
rows above were derived for r0116. **Do not presume any station's release**:
WRJE's r0114 → r0115 transition previously reported `exact fast-forward
failed`; its installed release, live checkout state and protected generation
are preflight facts to establish on WRJE itself. Never repair a station's Git
state as part of installing r0116.

## 2. Station preflight (read-only; all must pass)

1. **Exact identity.** `/opt/isadoraair` HEAD is exactly the installed
   release commit above, on `main`. A companion commit (`29f6a335` for
   r0115) or any non-release commit is refused by the planner.
2. **Clean, collision-free checkout** — the cause of `exact fast-forward
   failed` (`LIVE_SOURCE_ADVANCE_FAILED`): the updater checks ancestry, then
   runs `git merge --ff-only`, **after** applying migrations. `git status
   --porcelain` must be empty, and none of the paths r0116 adds may exist
   untracked:
   ```
   active_messages/diagnostics.py
   active_messages/heartbeat.py
   active_messages/management/commands/active_message_retention.py
   active_messages/management/commands/active_message_worker_ready.py
   active_messages/quarantine.py
   active_messages/reconcile.py
   active_messages/retention.py
   active_messages/supervision.py
   active_messages/templates/active_messages/board_diagnostics.html
   active_messages/track_guard.py
   deploy/isadoraair-active-message-worker.service
   deploy/migration_authorizations/r0116.json
   deploy/releases/r0116.json
   deploy/updater_attestations/r0116-primary.json
   docs/releases/r0116.md
   docs/releases/r0116_installation.md
   docs/releases/r0116_readiness.md
   monitoring/management/commands/provision_active_message_worker_check.py
   monitoring/migrations/0014_provision_active_message_worker_check.py
   ```
   plus 21 new test modules under `active_messages/tests/`,
   `monitoring/tests/` and `updatecenter/tests/` (`git diff --name-only
   --diff-filter=A <installed> <r0116>` lists them exactly). A from-r0114
   station must also be free of r0115's added paths. If anything is found,
   stop: that is a station-state correction needing its own authorization.
3. **Protected runtime.** Active generation **14** (runtime 12), no
   activation in progress, root execution armed. The station trust policy
   `/etc/isadoraair/updater-trust.json` names `primary-release` with
   `/etc/isadoraair/updater-trust/primary-release.pem`, SHA-256
   `732505be4de3b2dbedf0b9863070cde6e4bcd3ba60af6988b9e7f530ad1850f1`.
4. **No hand-installed worker.** No `isadoraair-active-message-worker`
   unit file or enablement exists on the host (only the updater installs it).
5. **Update Center's plan** shows exactly (from r0115): releases `r0116`;
   migration `monitoring.0014_provision_active_message_worker_check` with
   one manual `RunPython` covered by the r0116 companion; new required unit
   `isadoraair-active-message-worker.service`; restarts `isadoraair-gunicorn`,
   `isadoraair-monitoring`; protected-runtime transition to generation 15; no
   collectstatic, package, nginx or requirements step. From r0114 it adds
   r0115's `active_messages.0005` (two manual `AddIndex`, r0115's companion)
   — run r0115's read-only preflight (`docs/releases/r0115_preflight.sql`)
   first, as r0115 requires.
6. **Backups.** A fresh, restorable pre-upgrade backup with a passing remote
   round-trip, Recovery Assurance `ok`, and headroom for the `pg_dump`
   checkpoint.
7. **Services.** Core services healthy; `isadoraair-render` and
   `isadoraair-validation` running (the worker wants them; without them it
   still starts and retries renders); `/run/isadoraair` present (tmpfiles)
   and owned by the station account. A quiet window: Gunicorn and Monitoring
   restart; the engine, OGRemote and the audio chain do not.

## 3. Expected Update Center milestones

`trusted_source_fetched` → `trusted_plan_validated` →
`runtime_descriptor_validated` → `runtime_candidate_staged` →
`runtime_candidate_verified` → `runtime_activation_requested` →
`runtime_activation_accepted` → `current_schema_validated` →
`target_staged` → `target_schema_validated` → `migration_preflight_passed`
→ `checkpoint_created` → `migration_started` → `database_verified` →
`source_advanced` → `systemd_reconciled` (worker installed, `enable --now`,
start gate passed) → `services_restarted` → `postflight_complete` →
`staging_cleaned`; the runtime generation commit follows the protected
updater's own contract.

## 4. Post-install acceptance (each must pass as stated)

1. The Update Center job completed with `postflight_complete`; live HEAD is
   exactly the r0116 release commit; the protected runtime reports
   generation 15, runtime 13.
2. `isadoraair-active-message-worker.service` is enabled and active; its
   journal shows the start gate passed; no other unit was started, stopped or
   restarted except Gunicorn and Monitoring (the engine's PID is unchanged).
3. `/run/isadoraair/active_message_worker.json` exists, owned by the station
   account, mode 0600.
4. Monitoring shows the "Active Message Worker" card `ok` after a poll, with
   no Restart button.
5. On the Operator Board: the worker chip reads "Worker OK" (e.g. "Worker OK
   — idle"); the diagnostics page shows process identity **verified** and
   heartbeat **current**; "Native airing closed"; Replay Now unavailable.
6. A fresh Runtime Recovery payload exists for generation 15.
7. On-air audio continued throughout; OGRemote remains the broadcast
   authority.

## 5. Incomplete, stale or failed states

| What the station shows | Meaning | Response |
| --- | --- | --- |
| Board "Worker check not provisioned" | `monitoring.0014` not applied | expected before r0116; after a failed job, fix forward through the release (never by hand) |
| Monitoring critical for the worker (`service_inactive` / `service_failed`) | unit installed but not running | it retries with back-off; harmless with the gate closed; read the journal; fix forward |
| `no_heartbeat` / `stale` / `heartbeat_not_current`; board identity unknown / not current | no current heartbeat from the running process | never inferred healthy; investigate the worker process; there is no manual restart path on the board |
| Board "Monitoring status unavailable" | Monitoring's own state is missing, stale, future-dated or inconsistent | not a worker failure: check `isadoraair-monitoring` |
| Job failed at `source_advanced` (`exact fast-forward failed`) | migration applied, checkout still at the old release | stop; the check row may show the not-yet-installed worker as inactive (an alert only). Correct the station checkout under its own authorization, then retry |
| Job failed at `systemd_reconciled` | start gate failed; nothing restarted | the unit stays enabled and retries; fix forward |
| Updater refuses the unit name (`UNKNOWN_MANAGED_UNIT`) | generation 15 not active | expected until generation 15 verifies; never declare the worker restart in r0116 |

There is no automatic rollback. Fix forward with a corrected signed release;
a full restoration is disaster recovery only (code and database together from
the job's checkpoint, Recovery Assurance afterwards). Never hand-install or
edit units, the protected policy, trust configuration or migrations, never
replace protected runtime slots by hand, and never open native airing.
