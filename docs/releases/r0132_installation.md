# r0132 installation, verification and recovery

This runbook describes a later installation that must be authorized separately. Publishing r0132 does not
authorize any work on KOGR or WRJE.

## Exact identity and trusted plan

- **Previous release:** r0131 at `c89ae56a15667a458b1cebb897c068b774256727`.
- **R:** the unique canonical commit introducing `deploy/releases/r0132.json` (the release-packaging commit that
  adds this runbook). Record its full SHA from canonical `main`, and require the Update Center preview to target
  that exact commit. R's parent, the integration commit
  `c82fc57af8dc1feb5f2a283401d72e0084eeebf5`, carries no r0132 manifest; it is not a release and must never be
  installed.
- **No migration and no companion.** r0132 must not have a migration-authorization companion.
- **Expected r0131 → r0132 plan:** one release with no migration, package, requirements, static, nginx, systemd or
  protected-runtime action, then restart `isadoraair-gunicorn` and `isadoraair-engine` in trusted canonical order.
- **A station below r0131:** the trusted plan spans every intervening release, and each one brings its own declared
  actions (for example r0131's additive `library.0100_dst_fallback_replay_pass`). Each intervening release's own
  installation runbook applies in addition to this one. r0132 itself adds nothing beyond the two restarts. Any action
  not declared by a manifest in the chain is a no-go.

Do not install a feature branch, the integration branch, a rebuilt equivalent commit, a dirty checkout, or a plan with
additional actions.

## Common read-only preflight (perform independently on each station)

1. **Establish the station's actual installed release from its Update Center installation record.** Do not infer it
   from archived incident evidence (WRJE's 2026-11-08 snapshot showed `runtime_commit` r0124 `69d53bc`, which proves
   nothing about today). Do not infer it from the checkout's `HEAD` alone: the live checkout's branch can advance
   without a completed installation (applied migrations and restarts are what distinguish an installed release).
2. Fetch canonical `main` through the normal Update Center path. Verify that the target is R and that r0131 is its
   direct release-chain predecessor.
3. Confirm that no Update Center job is running or awaiting intervention. Review backup and recovery readiness, schema
   health and the protected generation/policy. If the protected runtime generation changed since the last recovery
   payload, the payload must be refreshed first (existing nightly-backup rule).
4. Preview the installation and record the target commit, action fingerprint and exact plan. For an r0131 station it
   must contain only the two restarts. Anything else is a no-go.
5. Confirm current database and media backups. r0132 alters no schema, media or rows, but the release process makes no
   automatic rollback promise.
6. Confirm audio, the engine queue, encoders and Monitoring are normal. Record service PIDs and activation timestamps
   for Gunicorn, engine, encoders, Monitoring, RBDS, Active Messages and spoken rendering.
7. Record Hour Refill and native Active Messages policy/arming. r0132 must not change or activate either.
8. Agree with operators that no Remote DJ session will be connected during the installation window. Record whether
   Remote DJ is currently idle (engine state `remote_dj_connected: false`, no active session).
9. **Running-engine teardown fingerprint (read-only).** Using a thread listing such as
   `ps -L -o tid,stat,wchan,comm -p <engine MainPID>`, check for a `webrtcbin<N>:pc` thread in `futex_do_wait` while no
   Remote DJ session is active and no matching `webrtcbin<N>:ice` thread exists. That is the deadlocked-teardown
   fingerprint of the r0131-and-earlier engine. Also check the journal for repeated "all persistent Remote DJ slots are
   occupied" refusals with `active_attempt=none`. If either is present, expect the engine stop below to reach the
   90-second stop timeout and SIGKILL. The new code cannot help with that stop, because it is the *old* engine that is
   stopping. Capture a stack sample first if the operator authorizes it, and plan for a longer, attended interruption.
   Never shorten or bypass the configured stop policy, and never signal the engine by hand to "pre-clear" it.
10. Choose a quiet operational window. The engine restart briefly interrupts playout, and Gunicorn requests may fail
    during its restart. Encoders keep running; listeners hear the engine's restart gap only.

Any identity, hash, schema, service-health or preview mismatch blocks installation.

## KOGR

**Preflight.** Run steps 1–10. Observation for planning only, not proof of installation: on 2026-11-10 the KOGR live
checkout was at `c89ae56` (r0131 source), and its engine (started 2026-11-09 21:00 CST) showed no Remote DJ deadlock
fingerprint and no accumulated `pool-N` threads. Re-establish both at the actual preflight. If the Update Center
record shows KOGR below r0131, apply the intervening runbooks (r0131 includes migration 0100).

**Installation.** Only after separate KOGR authorization, use the protected Update Center. Reconfirm R and the action
fingerprint immediately before starting. The plan advances source to R, restarts Gunicorn, then restarts and verifies
the engine.

**Verification.** Perform the common post-install checks below. Record the engine stop duration from the journal
(expected: a normal, short stop).

## WRJE

**Preflight.** Run steps 1–10, with these additions.

- WRJE has history:
  - On 2026-11-08 a Remote DJ failure left the cleanup deadlocked and the slot busy.
  - The following engine stop reached the 90-second timeout and needed SIGKILL.
  - r0131's runbook already required planning for that stop behavior.
- Treat step 9 as mandatory. Run it close to the window, and again immediately before starting, because a Remote DJ
  failure in between would re-create the condition.
- If the fingerprint is present, the installation's engine stop is expected to take the full timeout. Keep an operator
  present through engine recovery.
- The 2026-11-08 snapshot also showed 342 idle `pool-N` threads that accumulated after the incident. Their cause is
  unidentified. Record a thread count at preflight. If the count is large, capture a stack sample (`py-spy dump` or
  gdb `thread apply all bt`) *before* the restart, if authorized, since the restart destroys the evidence.
- Establish WRJE's installed release from its Update Center record. If it is below r0131, the plan includes every
  intervening release's actions and runbooks. In particular, confirm whether r0131 and earlier releases' migrations
  and restarts are pending.

**Installation.** Only after separate WRJE authorization, the same protected Update Center procedure as KOGR.

**Verification.** Perform the common post-install checks below, plus:

- Record the engine stop duration and whether SIGKILL occurred. This was the old engine's stop, so a SIGKILL here is
  expected if the step-9 fingerprint was present. It is not an r0132 failure.
- After the new engine is verified, the first Remote DJ connect/disconnect (common check 6) is the acceptance evidence
  that WRJE's slot is free under r0132.

## Common post-install verification

1. Confirm that the job succeeded, the installed release is r0132, `HEAD` is exactly R on clean canonical `main`, and
   Django system and migration checks are clean, with no migration applied by this release.
2. Confirm Gunicorn and the engine have new PIDs and timestamps and are healthy. Encoders, Monitoring, RBDS, Active
   Messages and spoken rendering keep their recorded identities.
3. Confirm audio is present, engine state is fresh, the queue advances, encoders are connected, and Monitoring reports
   normal station health.
4. **Ordinary schedule building.** Let the engine auto-build the next hour as usual. Confirm the new hour log builds and
   approves normally, its summary includes `lookahead_picks` / `landed_unused_slots`, and no scheduler-attention event
   is raised for an ordinary hour.
   - Do not rebuild an already-approved or airing hour just to test.
   - If an admin build check is wanted, use a future, not-yet-approved hour through the normal admin page.
   - Confirm requests still place and replace normally.
5. **Remote DJ idle state.** Confirm `remote_dj_connected: false` and no quarantine: no critical "Remote DJ cleanup
   failed/blocked" event since the restart.
6. **Remote DJ availability (ordinary use only).** With an authorized operator:
   1. Connect one normal Remote DJ session and confirm audio and the mic gate behave as before.
   2. Disconnect normally.
   3. Confirm the journal shows `session stopped (native cleanup <N> ms)`.
   4. Confirm that a second connection a few seconds later is admitted (not refused as busy), then disconnect it.

   Never deliberately induce an ICE failure, a network cut, or a hung cleanup on a production station.
7. Confirm Hour Refill, native Active Messages, station configuration, database content and media are unchanged.

## Quarantine visibility and handling (normal operation after install)

A failed or blocked Remote DJ cleanup produces:
- a critical Monitoring event, "Remote DJ cleanup failed; Remote DJ unavailable until engine restart" (or
  "... blocked; ..."), with detail fields `attempt_id`, `slot`, `teardown_event`, `error`,
  `restart_required_to_reuse_remote_dj: true` and `detached_from_program_audio: true`;
- refusal of new Remote DJ connections with "previous session cleanup failed or blocked; engine restart required";
- journal lines `native_cleanup_failed …` or `native_cleanup_blocked …`.

Program audio, automation and the local microphone are unaffected, and the station does not restart itself. Restore
Remote DJ by restarting the engine at an operationally acceptable time through the station's established procedure.
Before the restart, preserve the Monitoring event, journal and (if authorized) a thread listing. The r0132 engine stop
does not wait on the stuck cleanup.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise. Preserve its job record and journal evidence at every failure,
and do not retry automatically.

| Failure point | Expected state | Required response |
|---|---|---|
| Before source advancement | Previous release remains installed | Correct the external cause and obtain a reviewed retry |
| Source advanced, restarts not begun | r0132 source, old processes still running old code | Preserve job evidence; resume only through an authorized recovery plan |
| Gunicorn restart fails | Source current; web UI unavailable | Do not reset Git or restart ad hoc; use recorded protected recovery |
| Engine stop reaches 90-second timeout / SIGKILL | Old engine's deadlocked Remote DJ teardown (WRJE history); playout interrupted; r0132 source present | Keep the station attended, capture journal/state, follow the existing service recovery procedure; do not repeat the install |
| Engine restart or verification fails | r0132 source; audio may be unavailable | Escalate immediately; preserve queue, service and job evidence; no automatic rollback |
| Post-install hour build fails or alarms abnormally | r0132 installed | Preserve the build event/diagnostics; do not hand-edit logs; prepare a reviewed forward correction |
| Post-install Remote DJ quarantine | r0132 installed; Remote DJ unavailable; program audio unaffected | Preserve the event and journal; restart the engine at a chosen time; report for review |

r0132 has no schema change, so earlier source is schema-compatible. Source rollback is still outside the normal updater
workflow and needs separate authorization. Never reset Git, edit the database, restart services ad hoc or manipulate
production media as an installation recovery step.

## Station separation

Authorize, preflight, install and verify KOGR and WRJE independently. Success at one station is neither authorization
nor evidence for the other. Publication alone changes neither station.
