# r0134 installation, cutover, verification and recovery

This runbook describes a later installation that must be authorized separately. Publishing r0134 does not
authorize any work on KOGR or WRJE, and publication alone changes neither station.

## Exact identity and trusted plan

- **Previous release:** r0133 at `55c69a33393f0c5b00c3d10ad60c5e3a0ce1ae83`.
- **R:** the unique canonical commit introducing `deploy/releases/r0134.json` (the metadata-only release commit that
  adds this runbook). Record its full SHA from canonical `main`, and require the Update Center preview to target that
  exact commit. R's ancestors back to r0133 are the accepted implementation
  `14b1fce3cf70793756d4cffb93436a065268473b` and the accepted correction
  `3b1634c9b0d70a3402a0df44a58c4d4da64890f0` (tree `f6e840357e5b52ee2776c661a64de149e968f862`). R's parent carries no
  r0134 manifest; it is not a release and must never be installed.
- **No migration and no companion.** r0134 must not have a migration-authorization companion.
- **Expected r0133 → r0134 plan:** one release. Advance source to R through the protected Update Center, apply no
  migration, install no package or Python requirement, perform no collectstatic, nginx, systemd or protected-runtime
  action, then restart `isadoraair-gunicorn` and then `isadoraair-monitoring`, in that order. **No engine restart.**
- **A station below r0133:** the trusted plan spans every intervening release, and each one brings its own declared
  actions (for example r0132 also restarts the engine). Each intervening release's own runbook applies in addition to
  this one. Do not assume the shorter plan applies. Any action not declared by a manifest in the chain is a no-go.

Do not install a feature branch, the release-preparation branch, a rebuilt equivalent commit, a dirty checkout, or a
plan with additional actions. Any unexpected migration, protected-runtime action, engine restart or other undeclared
action is a no-go.

## Operator decisions before installation (not automated)

Installing r0134 performs none of these. Complete them for the station being installed:

1. **Playlists and rotations.** List every active playlist and rotation that references a Weather MP3 Track
   (WxTemp, WxObs, WxForecast, WxAlert). These references are not migrated and will keep airing the old MP3, which
   nothing regenerates after the cutover.
2. **Already-approved logs.** Review approved or generated logs that already hold those Track references.
3. **Retirement method.** Decide how each old MP3 Track leaves future selection (clear Ready to Air, remove from the
   rotation, or another method the operator uses) and who does it at which step of the cutover. Do not delete Tracks
   or files as part of the installation.
4. **Weather voice configuration.** Confirm which announcer voice each product uses at this station. Calibrated rows
   exist for `Max_Weatherly` and `Claira_Sky` only; any other voice uses Max's gains and logs a warning.
5. **Listening acceptance.** Listen to the calibrated audio and accept the levels (see Known limitations: Claira was
   measured, not auditioned, and her alert gain is provisional).
6. **Window.** Choose an ordinary operational window with **no active or imminent alert** (see WxAlert).

## Common read-only preflight (perform independently on each station)

1. **Establish the station's actual installed release from its Update Center installation record.** Do not infer it
   from the checkout's `HEAD` alone or from archived evidence.
2. Verify the installed source identity and that the checkout is clean.
3. Schema health on the Update Center page must be `schema_current`. Any other status is a no-go until understood.
4. Confirm that no Update Center job is running, failed or awaiting intervention.
5. Confirm trusted runtime and protocol compatibility (protected runtime generation matches the signed policy). If the
   generation changed since the last recovery payload, refresh the payload first (existing nightly-backup rule).
6. Confirm current backup and recovery readiness (database and media). r0134 alters no schema, rows or media, but the
   release process makes no automatic rollback promise.
7. Confirm service and monitoring health. Record service PIDs and activation timestamps for Gunicorn, the engine,
   encoders, Monitoring, RBDS, Active Messages and spoken rendering.
8. **Station ffmpeg.** The Weather producers call the system `ffmpeg`. Confirm it provides the `alimiter` filter and
   the `flac` encoder (`ffmpeg -hide_banner -filters | grep alimiter`, `ffmpeg -hide_banner -encoders | grep -w flac`).
   Calibration was measured on KOGR's ffmpeg; WRJE's build must be checked on its own.
9. Fetch canonical `main` through the normal Update Center path. Verify that the target is exactly R and that r0133 is
   its direct release-chain predecessor.
10. Preview the installation and record the target commit, action fingerprint and exact plan. For an r0133 station it
    must contain exactly the Gunicorn restart then the Monitoring restart. Anything else is a no-go.
11. Record Hour Refill and native Active Messages policy and arming. r0134 must not change or activate either.
12. Record the current Weather baseline: the old MP3 Tracks (ids, categories, Ready to Air state), the Weather Setup
    Status artifact states, and the modification times of the existing MP3 files.

## Installation

Use only the protected Update Center. After separate station authorization, reconfirm R and the action fingerprint
immediately before starting. The plan advances source to R, applies no migration or dependency change, restarts
`isadoraair-gunicorn`, then `isadoraair-monitoring`. **Do not restart the engine.** The Weather producers, the publish
command and the Dedication intro generator run as fresh timer-started processes from the same checkout and use the new
source on their next invocation; nothing else needs a restart.

Gunicorn requests may fail briefly during its restart, and Monitoring's probes pause briefly during its restart. Audio
is not interrupted.

## Cutover sequence

1. **Before installation:** confirm no alert transition or urgent insertion is underway; if one is, defer.
2. Install through the Update Center (above).
3. **WxAlert, before the new workflow is in service:** make the old `WxAlert/wx_alert.mp3` Track ineligible for urgent
   selection. The engine's `insert_urgent` takes the first ready Track in the category with no ordering and this
   release does not change that, so an eligible old MP3 can be chosen instead of the new FLAC, and it would be stale.
4. Let the routine producers run on their timers. Do not generate a test alert on the live station.
5. Verify the routine FLACs (post-install verification below).
6. Move playlist and rotation references from each old MP3 Track to its FLAC Track, then retire the old MP3 Tracks
   (WxTemp, WxObs, WxForecast) by the method chosen above. Re-check approved logs.
7. Delete the old MP3 files only as a separate later decision. Until then do not re-import the directory expecting
   retired MP3 Tracks to stay retired: a library scan registers any MP3 left on disk.

### WxAlert: expected state and recovery

`WxAlert` is event-driven. `wx_alert.flac` is created only when a real qualifying NWS or AMBER-family alert is
processed. With the old MP3 retired (step 3), the category has **no ready alert Track** until that first alert; there is
no automatic fallback to the MP3, and none is promised. Weather Setup Status reports the alert artifact as not
applicable while no alert is active.

- **Expected during the first real alert:** the producer builds and publishes `wx_alert.flac`, a new Track appears in
  `WxAlert` with Ready to Air set, and the urgent insert selects it. Verify exactly that when it happens.
- **If an alert is active and the status reports the alert FLAC never generated, missing, or its Track not ready:**
  the alert statement is not airing. Check the producer's log and the publication failure notification, correct the
  cause, and let the next producer cycle publish. The operator decides whether to temporarily restore the old MP3 Track
  to eligibility as a manual stopgap, knowing its text is stale and may not match the current alert. It is never
  restored automatically.
- **If the deferral rule in step 1 is not met** (an alert is already active), postpone the cutover.

## KOGR

KOGR has Roadmap 2.27 Hour Refill Stage 3 shadow observation active. r0134 does not authorize changing any Hour Refill
configuration, and the installation must preserve all of the following:

- the existing Hour Refill Policy;
- Observation-only mode, with `dry_run_only` checked;
- the host unarmed (`HOUR_REFILL_INSTALL_ARMED` false in a fresh process and in the engine);
- residual PSA disabled;
- the cause switches at their separately authorized values;
- runner health and engine continuity: the engine process is not restarted and keeps its PID and start timestamp;
- the Stage 3 evidence-collection user timers, running on their existing schedule;
- the existing observation evidence and timestamps.

The expected Gunicorn and Monitoring restarts are not an engine restart and are not an Hour Refill event; they must
not be represented as resetting or restarting the observation period. If the engine restarts, a policy value changes,
evidence collection is interrupted, or any Hour Refill installation record appears, stop and investigate. Do not pause,
reset or reschedule the evidence timers as part of the installation. Record the Hour Refill baseline (policy row, runner
status, latest evidence snapshot, timer states) immediately before starting and compare it after.

## WRJE

WRJE requires its own independent station preflight, installation approval and acceptance. Do not presume it has the
same Weather configuration, voices, media, ffmpeg build, playlists, rotations, approved logs or active scheduling
references as KOGR; do the operator decisions and the ffmpeg check on WRJE itself. If WRJE's installed release is
below r0133, the plan includes every intervening release's actions and runbooks. Success on KOGR is neither
authorization nor evidence for WRJE.

## Post-install verification

1. The Update Center job succeeded, the installed release is r0134, and the installed source identity and `HEAD` are
   exactly R on clean canonical `main`.
2. Gunicorn and Monitoring have new PIDs and timestamps and are healthy. The engine, encoders, RBDS, Active Messages
   and spoken rendering keep their recorded identities.
3. No migration was applied; Django system and migration checks are clean.
4. After the routine producers have run, each published asset is a FLAC at the permanent path (`current_temp.flac`,
   `current_obs.flac`, `forecast.flac`) in its category, with 44.1 kHz stereo 16-bit format, the expected title and
   artist, a duration equal to the speech plus one second, and one second of leading silence. A FLAC Track exists in
   the correct category with Ready to Air set.
5. The next generation of the same product updates the same FLAC file path and the same FLAC Track (no new Track).
6. Weather Setup Status and the Monitoring Weather probe recognize the FLAC artifacts, with the correct freshness and
   provenance; the alert artifact is not applicable until a real alert occurs.
7. The existing MP3 files and Tracks are unchanged from the recorded baseline (same files, timestamps, Track rows).
8. After the operator completes the rotation cutover: no Weather product is scheduled twice in an hour, and no log
   still references a retired MP3 Track for a future slot.
9. Listen once on air to each product and compare its level to a KanDrive segment; confirm audio, queue advancement,
   encoders and stream delivery are healthy and Monitoring reports normal health.
10. KOGR: Hour Refill state and evidence are identical to the recorded baseline.

## Rollback

Returning to r0133 after any FLAC has been generated is not an automatic procedure and no automatic source or Track
rollback is authorized. r0133 publishes `.mp3` files under the old names; it does not know the FLAC Tracks and does not
retire them. A deliberate operator restoration needs all of: an authorized source rollback outside the normal updater
workflow; making the FLAC Tracks ineligible; and making the MP3 Tracks (and, for WxAlert, the MP3 alert Track)
eligible and referenced again, followed by a fresh producer run. Neither format is deleted during installation, which
is what keeps this possible. Never reset Git, edit the database, restart services ad hoc or manipulate production media
as a recovery step.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise. Preserve its job record and journal evidence at every failure,
and do not retry automatically.

| Failure point | Expected state | Required response |
|---|---|---|
| Before source advancement | r0133 remains installed | Correct the external cause and obtain a reviewed retry |
| Source advanced, restarts not begun | r0134 source, old Gunicorn and Monitoring on old code | Preserve job evidence; resume only through an authorized recovery plan |
| Gunicorn or Monitoring restart fails | Source current; web UI or probes unavailable; audio unaffected | Do not reset Git or restart ad hoc; use recorded protected recovery |
| Unexpected engine restart or Hour Refill change (KOGR) | Not an r0134 effect | Preserve evidence, stop, escalate |
| A producer fails to publish a FLAC | The previous published file and Track stay (last-known-good); a failure notification is raised | Fix the cause; the next cycle publishes. The old MP3, if still eligible, keeps airing stale |
| FLAC level or content judged wrong | Operator listening rejects the calibration | Retire the FLAC Track, restore the MP3 eligibility by hand, and escalate for a calibration change |

r0134 has no schema change, so earlier source is schema-compatible.

## Known limitations and conditions outstanding

- Operator listening acceptance of the calibrated audio is outstanding (Claira was measured, not auditioned).
- Claira's alert gain (+6.5 dB) is provisional until a real Claira alert is measured.
- The stale `wx_alert.mp3` help text in `weather/models.py` is a cosmetic follow-up needing a migration.

## Station separation

Authorize, preflight, install and verify KOGR and WRJE independently.
