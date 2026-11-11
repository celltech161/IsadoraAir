# r0133 installation, verification and recovery

This runbook describes a later installation that must be authorized separately. Publishing r0133 does not
authorize any work on KOGR or WRJE.

## Exact identity and trusted plan

- **Previous release:** r0132 at `653c5bfd09972858fc529d00a28deee7890607a3`.
- **R:** the unique canonical commit introducing `deploy/releases/r0133.json` (the release-packaging commit that adds
  this runbook). Record its full SHA from canonical `main`, and require the Update Center preview to target that
  exact commit. R's ancestors back to r0132 are the accepted test correction
  `8724da92a85104a9a7ea914b22cbabd4e5c45ebd`, the accepted implementation
  `8dd2abdc6edf86a51e543e610f8fb53a6410bfae` and a documentation commit. R's parent carries no r0133 manifest; it is
  not a release and must never be installed.
- **No migration and no companion.** r0133 must not have a migration-authorization companion.
- **Expected r0132 → r0133 plan:** one release. Advance source to R through the protected Update Center, apply no
  migration, install no package or Python requirement, perform no collectstatic, nginx, systemd or protected-runtime
  action, then restart `isadoraair-gunicorn` only.
- **A station below r0132:** the trusted plan spans every intervening release, and each one brings its own declared
  actions. Each intervening release's own installation runbook applies in addition to this one. Do not assume the
  shorter plan applies. Any action not declared by a manifest in the chain is a no-go.

Do not install a feature branch, the release-preparation branch, a rebuilt equivalent commit, a dirty checkout, or a
plan with additional actions.

Any unexpected migration, protected-runtime action, engine restart or other undeclared action is a no-go.

## Common read-only preflight (perform independently on each station)

1. **Establish the station's actual installed release from its Update Center installation record.** Do not infer it
   from the checkout's `HEAD` alone or from archived evidence.
2. Verify the installed source identity and that the checkout is clean.
3. Review current schema health on the Update Center page. It must be `schema_current` on a healthy station. A station
   showing `inconsistent_migration_history`, `unapplied_migrations_detected` or `migration_state_indeterminate` is a
   no-go until its cause is understood through a reviewed procedure.
4. Confirm that no Update Center job is running, failed or awaiting intervention.
5. Confirm trusted runtime and protocol compatibility (the protected runtime generation matches the signed policy).
   If the protected runtime generation changed since the last recovery payload, the payload must be refreshed first
   (existing nightly-backup rule).
6. Confirm current backup and recovery readiness (database and media). r0133 alters no schema, media or rows, but the
   release process makes no automatic rollback promise.
7. Confirm service and monitoring health. Record service PIDs and activation timestamps for Gunicorn, the engine,
   encoders, Monitoring, RBDS, Active Messages and spoken rendering.
8. Fetch canonical `main` through the normal Update Center path. Verify that the target is exactly R and that r0132 is
   its direct release-chain predecessor.
9. Preview the installation and record the target commit, action fingerprint and exact plan. For an r0132 station it
   must contain only the Gunicorn restart. Anything else is a no-go.
10. Record Hour Refill and native Active Messages policy and arming. r0133 must not change or activate either.
11. Choose an ordinary operational window. Gunicorn requests may fail briefly during its restart; the engine and
    audio are not restarted.

Any identity, hash, schema, service-health or preview mismatch blocks installation.

## KOGR

KOGR has Roadmap 2.27 Hour Refill Stage 3 shadow observation active. r0133 does not authorize changing any Hour
Refill configuration, and the installation must preserve all of the following:

- the existing Hour Refill Policy;
- Observation-only mode, with `dry_run_only` checked;
- the host unarmed (`HOUR_REFILL_INSTALL_ARMED` false in a fresh process and in the engine);
- residual PSA disabled;
- the cause switches at their separately authorized values;
- runner health and engine continuity: the engine process is not restarted and keeps its PID and start timestamp;
- the Stage 3 evidence-collection user timers, running on their existing schedule;
- the existing observation evidence and timestamps.

The expected Gunicorn restart is not an Hour Refill event and must not be represented as resetting or restarting the
observation period. If the engine restarts, a policy value changes, evidence collection is interrupted, or any Hour
Refill installation record appears, stop and investigate. None of these is an expected effect of r0133. Do not pause,
reset or reschedule the evidence timers as part of the installation.

**Preflight.** Run steps 1–11, and record the Hour Refill baseline (policy row, runner status, latest evidence
snapshot, timer states) immediately before starting.

**Installation.** Only after separate KOGR authorization, use the protected Update Center. Reconfirm R and the action
fingerprint immediately before starting. The plan advances source to R and restarts Gunicorn.

**Verification.** Perform the common post-install checks below, and compare the Hour Refill baseline: policy, mode,
arming, PSA, causes, engine PID and start time, runner status and evidence timers must be identical.

## WRJE

WRJE requires its own independent station preflight and installation approval. Publication alters neither its
runtime state nor its configuration, and r0133 does not configure Hour Refill there.

**Preflight.** Run steps 1–11. Establish WRJE's installed release from its Update Center record; if it is below r0132,
the plan includes every intervening release's actions and runbooks, and r0133 alone does not describe it.

**Installation.** Only after separate WRJE authorization, the same protected Update Center procedure as KOGR.

**Verification.** Perform the common post-install checks below.

## Common post-install verification

1. Confirm the Update Center job succeeded, the installed release is r0133, and the installed source identity and
   `HEAD` are exactly R on clean canonical `main`.
2. Confirm Gunicorn was restarted (new PID and timestamp) and is healthy. The engine, encoders, Monitoring, RBDS,
   Active Messages and spoken rendering keep their recorded identities.
3. Confirm no migration was applied by r0133, and that Django system and migration checks are clean.
4. Confirm schema health is `schema_current` on a healthy station, and that Update Center planning and presentation
   work normally (the page loads and shows the expected up-to-date state).
5. Confirm audio is present, engine state is fresh, the queue advances, encoders are connected and the stream is
   delivered, and Monitoring reports normal station health.
6. Confirm station configuration, database content and media are unchanged, and that Hour Refill state and
   observation evidence remain intact.

Do not deliberately corrupt a station's migration recorder to test the new protection. All invalid-history testing
stays in disposable test environments.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise. Preserve its job record and journal evidence at every failure,
and do not retry automatically.

| Failure point | Expected state | Required response |
|---|---|---|
| Before source advancement (including a probe refusal) | Previous release remains installed | Correct the external cause and obtain a reviewed retry |
| Source advanced, restart not begun | r0133 source, old Gunicorn still running old code | Preserve job evidence; resume only through an authorized recovery plan |
| Gunicorn restart fails | Source current; web UI unavailable; audio unaffected | Do not reset Git or restart ad hoc; use recorded protected recovery |
| Unexpected engine restart or Hour Refill change (KOGR) | Not an r0133 effect | Preserve evidence, stop, escalate for review |
| Station reports `inconsistent_migration_history` | Update Center fails closed; nothing repaired | Preserve the diagnostic; correct only through a separately reviewed procedure |

r0133 has no schema change, so earlier source is schema-compatible. Source rollback is still outside the normal
updater workflow and needs separate authorization. Never reset Git, edit the database, restart services ad hoc or
manipulate production media as an installation recovery step.

## Station separation

Authorize, preflight, install and verify KOGR and WRJE independently. Success at one station is neither authorization
nor evidence for the other. Publication alone changes neither station.
