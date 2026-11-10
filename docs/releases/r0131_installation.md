# r0131 installation, verification and recovery

This runbook describes a later, separately authorized installation. Publication of
r0131 does not authorize work on KOGR or WRJE.

## Exact identity and trusted plan

- **Previous release:** r0130 at `58e67cb247bd75ed489213dfc7e0bd6800596d3a`.
- **R:** the unique canonical commit introducing `deploy/releases/r0131.json`. Record
  its full SHA from canonical `main`; require the Update Center preview to target that
  exact commit.
- **Migration:** `library.0100_dst_fallback_replay_pass`, file SHA-256
  `7b5559d6b05865bef42bae1e66c34359bb67f02a00fd54a0542262359ee08b94`.
- **No companion:** migration 0100 has no manual operations, so r0131 must not have a
  migration-authorization companion.
- **Expected r0130 to r0131 plan:** one release, the exact additive migration above,
  no packages, requirements, static, nginx, systemd or protected-runtime action, then
  restart `isadoraair-gunicorn` and `isadoraair-engine` in trusted canonical order.

Do not install a feature branch, equivalent rebuilt commit, dirty checkout or plan with
additional actions.

## Read-only preflight — perform independently on each station

1. Fetch canonical `main` through the normal Update Center path. Verify the target is
   R, r0130 is its direct release-chain predecessor, and the installed source is the
   expected clean r0130 release.
2. Confirm no Update Center job is running or awaiting intervention. Review backup and
   recovery readiness, schema health and protected generation/policy.
3. Preview the installation. Record the target commit, action fingerprint and exact
   plan. It must match the declaration above. Any other migration, restart, package,
   static, unit, nginx or protected-runtime action is a no-go.
4. Verify migration 0100 has the expected SHA-256 and exactly one additive `AddField`
   schema operation; the choices-only model-state operation requires no SQL. Confirm
   there is no r0131 companion and no unexpected pending migration or drift.
5. Confirm audio, engine queue, encoders and Monitoring are normal. Record service PIDs
   and activation timestamps for Gunicorn, engine, encoders, Monitoring, RBDS, Active
   Messages and spoken rendering.
6. Confirm no import or other Library maintenance is in progress. `import_songs` is not
   run by installation.
7. Confirm current database and media backups. Migration 0100 does not alter media or
   existing playlist rows, but the release process has no automatic rollback promise.
8. Record Hour Refill and native Active Messages policy/arming. r0131 must not change or
   activate either feature.
9. Select a quiet operational window. The engine restart briefly interrupts playout.
   Gunicorn requests may also fail during its restart.

For **WRJE**, additionally plan for its November 9 engine stop behavior: the stop reached
the 90-second systemd timeout and required SIGKILL. Treat a repeat as a potentially long
audio interruption, keep the operator present through engine recovery, and do not shorten
or bypass the configured stop policy as part of this release. KOGR does not inherit WRJE's
observation; run and record its preflight separately.

Any identity, hash, schema, service-health or preview mismatch blocks installation.

## Installation

After separate authorization for that station, use only the protected Update Center.
Reconfirm R and the action fingerprint immediately before starting.

The trusted plan advances source to R, applies migration 0100, restarts Gunicorn, then
restarts and verifies the engine. It does not run `collectstatic`, install packages,
change systemd or nginx, transition protected runtime, restart encoders or Monitoring,
run `import_songs`, alter station media, or activate Hour Refill or native Active Messages.

Do not manually run `migrate`, restart services, edit the database or repair a failed job
outside the recorded Update Center recovery process.

## Post-install verification

1. Confirm the job succeeded, the installed release is r0131, `HEAD` is exactly R on
   clean canonical `main`, and Django system and migration checks are clean.
2. Confirm migration 0100 is applied and its nullable column exists. Verify no unrelated
   migration was applied and no historical PlaylistLog was assigned a replay marker.
3. Confirm Gunicorn and engine have new PIDs/timestamps and are healthy. Encoders,
   Monitoring, RBDS, Active Messages and spoken rendering retain their recorded identities.
4. Confirm audio is present, engine state is fresh, the queue advances normally, encoders
   are connected and Monitoring reports normal station health.
5. Confirm ordinary current-hour and next-hour logs, requests and first-buffer accounting
   behave normally. Do not fabricate a clock transition or edit production logs to test DST.
6. Confirm no replay authorization or `dst_replay` rows were created on an ordinary day.
7. If importer acceptance is separately desired, place one known-valid disposable audio
   file plus malformed and AppleDouble samples in a temporary directory outside the media
   Library, run `import_songs <temporary-directory> --dry-run`, and verify only the valid
   candidate is reported eligible. `--dry-run` must create or update no Track and the test
   files must never be copied into production media. Remove the temporary directory after
   recording the result. Do not perform a real import as part of release acceptance.
8. Confirm Hour Refill, native Active Messages, station configuration, database content
   other than migration state, and media remain unchanged.

Because the next real fall-back is not available during qualification, steps 1–8 plus the
reviewed deterministic regression evidence constitute immediate acceptance. The first real
fall-back after release remains an observed field event, not a prerequisite for installation.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise. Preserve its job record and journal
evidence at every failure; do not retry automatically.

| Failure point | Expected state | Required response |
|---|---|---|
| Before source advancement | r0130 remains installed | Correct the external cause and obtain a reviewed retry |
| Source advanced, migration not applied | r0131 source with r0130 schema | Stop; preserve evidence; do not start the engine on an unreviewed mixed state |
| Migration fails | Source may be r0131; schema operation failed or rolled back | Stop; inspect exact migration state; do not mark it applied or edit schema by hand |
| Migration applied, restart not begun | Additive nullable column present; r0131 source present | Preserve job evidence; resume only through an authorized recovery plan |
| Gunicorn restart fails | Schema and source may be current; web UI unavailable | Do not reset Git or restart ad hoc; use recorded protected recovery |
| Engine stop reaches 90-second timeout or SIGKILL | Playout is interrupted; r0131 source/schema remain | Keep the station attended, capture journal/state, and follow the existing service recovery procedure; do not repeat the install |
| Engine restart or verification fails | Source/schema remain r0131; audio may be unavailable | Escalate immediately and preserve queue, service and job evidence; no automatic rollback |
| Import dry-run reports an unexpected write or accepts malformed input | Release remains installed; no real import was authorized | Stop importer acceptance, preserve samples/output and prepare a forward correction |

The additive nullable column is compatible with r0130 code, but source rollback is outside
the normal updater workflow and needs separate authorization. Reverting source does not
remove the column or undo any replay airings. Never reverse migration 0100, delete replay
rows, clear its authorization marker, reset Git or manipulate production media as an
installation recovery step.

## Station separation

Authorize, preflight, install and verify KOGR and WRJE independently. Success at one is not
authorization or evidence for the other. Publication alone changes neither station.
