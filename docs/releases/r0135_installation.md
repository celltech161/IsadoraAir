# r0135 installation, verification and recovery

This runbook describes a later installation that must be authorized separately for each station. Publishing r0135
does not authorize any work on KOGR or WRJE, and publication alone changes neither station.

**Unlike r0134, r0135 restarts the playout engine.** Plan for a brief interruption of program audio and for the end
of any active Remote DJ session.

## Exact identity and trusted plan

- **Previous release:** r0134 at `fc58ba6c96e5a62914f94ff34ec0dda9d8d75a4b`.
- **R:** the unique canonical commit introducing `deploy/releases/r0135.json` (the metadata-only release commit that
  adds this runbook). Record its full SHA from canonical `main`, and require the Update Center preview to target that
  exact commit.
- **R's ancestors back to r0134** are the three accepted engineering commits:
  - `f5d8ad471bbae0a45c498e21b99a7221ebe1c48b`, the live-fill and single-retry fix;
  - `622033f96d9f37c6cc9be27f1fc36e030eb33403`, the unplayable successor and Manual corrections;
  - `43b521bc9dba16301a34850851f41d8e157954ca`, tree `ce4909aede84c6a29ba16e7a3f2c8ee86fc27e67`: pending insertions
    and the side-effect-free poison check.

  None of them carries an r0135 manifest. They are not releases and must never be installed.
- **No migration and no companion.** r0135 must not have a migration-authorization companion.
- **Expected r0134 → r0135 plan:** one release.
  1. Advance source to R through the protected Update Center.
  2. Apply no migration; install no package or Python requirement; perform no collectstatic, nginx, systemd or
     protected-runtime action.
  3. Restart `isadoraair-gunicorn`, then `isadoraair-engine`, in that order.

  Nothing else is restarted.
- **A station below r0134:** the trusted plan spans every intervening release, and each one brings its own declared
  actions:
  - r0134 restarts Monitoring and needs its Weather FLAC operator decisions;
  - r0132 also restarts the engine;
  - r0131 applies migration `library.0100_dst_fallback_replay_pass`.

  Each intervening release's own runbook applies in addition to this one. The combined restart order is the canonical
  Update Center order (Gunicorn, engine, Monitoring, …). Do not assume the shorter plan applies. Any action not declared
  by a manifest in the chain is a no-go.

Do not install a feature branch, the release-preparation branch, a rebuilt equivalent commit, a dirty checkout, or a
plan with additional actions. Never replace source directly or start, stop or restart services by hand as part of the
installation.

## Common read-only preflight (perform independently on each station)

1. **Installed release:** establish it from the station's Update Center installation record. Do not infer it from the
   checkout's `HEAD` alone or from archived evidence. Verify the installed source identity and that the checkout is
   clean.
2. **Schema health** on the Update Center page must be `schema_current`. Any other status is a no-go until understood.
3. **No pending intervention:** no Update Center job is running, failed or awaiting intervention.
4. **Trusted runtime:** runtime and protocol are compatible (the protected runtime generation matches the signed
   policy). If the generation changed since the last recovery payload, refresh the payload first (existing
   nightly-backup rule).
5. **Backup and recovery readiness:** the latest database and media backups succeeded. r0135 alters no schema, rows or
   media, but the release process makes no automatic rollback promise.
6. **Service health:** the engine, Gunicorn, encoders (stream connections up) and Monitoring are healthy. Record the
   PIDs and activation timestamps for Gunicorn, engine, encoders, Monitoring, RBDS, Active Messages and spoken
   rendering.
7. **Approved-log state:** record the current hour's and the next hour's approved logs (date, hour, status, item
   count) and the engine's current item and queue position from engine state or the Workstation queue. Do not start the
   installation within about five minutes of the top of the hour, or while a log for the current or next hour is still
   being built.
8. **Legacy fill configuration:** open Django Admin → Log fill config and record its strategy and fallback Category
   (for example the PSA Category). Confirm the Category has Ready-to-Air material whose durations fit a short gap. r0135
   must leave this unchanged.
9. **Urgent and weather state:** confirm there is no active or imminent NWS or AMBER-family alert, no WxAlert or other
   urgent item pending in the queue, and no native Active Message waiting to air. If one is, defer: let it air first.
   A restart is designed to keep a pending inserted item, but do not depend on that on purpose.
10. **Remote DJ:** confirm no Remote DJ session is active (engine state `remote_dj_connected: false`), or obtain the
    remote presenter's agreement that the session will be ended. The engine restart ends it.
11. **Older engine code:** if the running engine predates r0132's code (check its start time against the r0132
    installation record), perform r0132's running-engine teardown fingerprint check. That older engine's stop can reach
    the 90-second stop timeout and need SIGKILL; plan an attended, longer interruption if the fingerprint is present.
12. **Target:** fetch canonical `main` through the normal Update Center path. Verify that the target is exactly R and
    that r0134 is its direct release-chain predecessor.
13. **Plan preview:** record the target commit, action fingerprint and exact plan. For an r0134 station it must contain
    exactly the Gunicorn restart then the engine restart. Anything else is a no-go.
14. **Policies:** record Hour Refill and native Active Messages policy and arming. r0135 must not change or activate
    either.

## Installation

Use only the protected Update Center.

1. After separate station authorization, reconfirm R and the action fingerprint immediately before starting.
2. The plan advances source to R, applies no migration or dependency change, then restarts `isadoraair-gunicorn` and
   then `isadoraair-engine`.

Gunicorn requests (Workstation, Admin) may fail briefly during its restart. The engine restart stops program audio
until the new engine starts playing; the encoders keep running, so listeners hear a short gap rather than a dropped
stream. On start, the engine resumes the interrupted item from its saved position when its saved state is valid (the
existing restart-resume behaviour), and otherwise continues the current approved log after the last item that aired.
An active Remote DJ session ends and must be reconnected by the presenter.

## KOGR

KOGR runs Roadmap 2.27 Hour Refill Stage 3 shadow observation. r0135 does not authorize changing any Hour Refill
configuration. Preserve all of the following:

- the existing Hour Refill Policy;
- Observation-only mode, with `dry_run_only` checked;
- the host unarmed (`HOUR_REFILL_INSTALL_ARMED` false in a fresh process and in the engine);
- residual PSA disabled;
- the cause switches at their separately authorized values;
- the Stage 3 evidence-collection user timers on their existing schedule;
- the existing observation evidence and timestamps.

**The engine restart is a process interruption of the Hour Refill runner, not a reset of the observation period.** The
runner lives in the engine process, so it stops with the old engine and starts with the new one.
- Before starting, record the Hour Refill baseline: policy row, runner status, latest evidence snapshot and timer
  states.
- Record the engine restart time in the Stage 3 evidence notes as an authorized release interruption.
- After installation, confirm the runner is running again in observation-only mode and that evidence collection
  resumes on the next scheduled run.
- Do not pause, reset, reschedule or clear the evidence timers or historical evidence. Do not arm 2.27 and do not clear
  `dry_run_only`.
- If a policy value changes, evidence collection does not resume, or any Hour Refill installation record appears,
  stop and escalate.

KOGR is the incident station: the 21:00 log of 2026-11-11 (PlaylistLog 3193) still holds rows whose Track was deleted.
That is expected history. Do not edit or delete it.

## WRJE

WRJE requires its own independent station preflight, installation approval and acceptance.
- Do not presume it has the same release, log fill configuration, PSA material, Hour Refill state, Remote DJ usage or
  scheduling as KOGR.
- If WRJE's installed release is below r0134, the plan includes every intervening release's actions and runbooks. That
  includes r0134's Weather FLAC operator decisions and, if WRJE's engine predates r0132's code, r0132's teardown
  fingerprint check (WRJE has had that deadlock).
- Success on KOGR is neither authorization nor evidence for WRJE.

## Post-install acceptance (read-only)

Do not create dead air, delete production Tracks, empty or edit approved logs, generate test alerts or Active
Messages, or force playback failures on a live station. The incident behaviour is covered by the guarded regression
evidence; on air, acceptance is passive observation.

1. **Release:** the Update Center job succeeded, the installed release is r0135, and the installed source identity and
   `HEAD` are exactly R on clean canonical `main`.
2. **Restarts:** Gunicorn and the engine have new PIDs and activation timestamps and are healthy. Encoders,
   Monitoring, RBDS, Active Messages and spoken rendering keep their recorded identities.
3. **Checks:** no migration was applied; Django system and migration checks are clean.
4. **Audio:** program audio is present, the encoders are connected and listeners receive the stream.
5. **Engine restart log:** the engine journal (`journalctl -u isadoraair-engine`) shows the normal startup:
   - `Loaded log for …`, then `Resume state accepted` and `Resume occurrence …` when it resumed an interrupted item;
   - the current approved log is the expected hour;
   - the queue position continues after the items already aired;
   - nothing that aired before the restart starts again from the top.

   A line `Resume keeps pending engine insertion …` means an inserted urgent, native or dedication item was still
   waiting and will air next.
6. **Through the next top of hour** (and preferably several hours), confirm with the journal and Workstation:
   - the next hour's log is built and approved as usual and rolls over once (`Advanced active queue to next hour ahead
     of TOH` or `Rolled over to next hour's log`);
   - no item starts while another is already playing, other than a normal crossfade;
   - no item appears twice;
   - `Log exhausted` lines do not multiply on successive 10-second ticks;
   - no repeated `Loaded log for` lines for an older hour after the new hour is installed.
7. **Live fill:** no `Live-fill worker crashed` lines or Monitoring events. When an hour runs short, PSAs from the
   configured fallback Category are appended as before.
8. **Fill configuration:** Log fill config (strategy and fallback Category) is unchanged from the preflight record.
9. **Protections:** urgent insertion and the poison guard are intact; no unexpected "Refused poison-guarded LogItem"
   or "Approved log has no playable item" events. If a real alert occurs later, confirm it aired once, ahead of ordinary
   programming.
10. **Monitoring:** Monitoring reports normal engine health and events are recorded as usual.
11. **KOGR:** Hour Refill remains observation-only with `dry_run_only` checked and the host unarmed. The runner is
    running, the evidence timers keep their schedule, the next evidence run produces new evidence, and no Hour Refill
    installation record exists.

## Stop and escalation conditions

Stop, preserve the Update Center job record, the engine journal, `/run/isadoraair/engine_state.json` and Monitoring
events, and escalate. Do not retry automatically. Escalate on any of:

- any plan or action other than the declared Gunicorn then engine restart;
- the engine fails to start, or there is no program audio after the restart;
- overlapping starts of different items;
- an aired item played again;
- a pending urgent item that never airs;
- a growing number of `Log exhausted` or retry lines;
- repeated `Live-fill worker crashed` events;
- any change to Log fill config, Hour Refill policy or arming;
- the KOGR evidence collection not resuming.

## Recovery boundaries

The Update Center makes no automatic rollback promise, and no automatic source rollback is authorized. A further
engine restart is not harmless: it interrupts audio again and ends any Remote DJ session. Use one only through the
station's established recovery procedure, at an operationally acceptable time. Never reset Git, edit the database,
edit approved logs, restart services ad hoc or manipulate production media as a recovery step.

| Failure point | Expected state | Required response |
|---|---|---|
| Before source advancement | r0134 remains installed | Correct the external cause and obtain a reviewed retry |
| Source advanced, restarts not begun | r0135 source, old Gunicorn and engine on old code | Preserve job evidence; resume only through an authorized recovery plan |
| Gunicorn restart fails | Source current; web UI unavailable; audio unaffected | Do not reset Git or restart ad hoc; use recorded protected recovery |
| Engine stop reaches the 90-second timeout / SIGKILL | Old engine's stop (see preflight step 11); audio interrupted; r0135 source present | Keep the station attended, capture journal and state, follow the existing service recovery procedure; do not repeat the install |
| Engine restart or verification fails | r0135 source; audio may be unavailable | Escalate immediately; preserve queue, service and job evidence; no automatic rollback |
| Playout misbehaviour after install (stop conditions above) | r0135 running | Preserve evidence and escalate; use Manual mode or Start Next as an operator stopgap |
| Hour Refill state changed or evidence not resuming (KOGR) | Not an r0135 effect | Preserve evidence, stop, escalate |

r0135 has no schema change, so r0134 source is schema-compatible if an authorized source rollback is ever decided.

## Known limitations

- The 30-second duration-fit margin can still leave short silence before the next approved hour.
- An entirely unplayable approved log is still stepped over by each 10-second recovery check. This is bounded, with
  de-duplicated warnings and automatic-skip cause rows.
- An inserted item that was skipped because its media was unavailable is reconsidered after a restart and can air late
  if its media has returned.
- Native Active Message integration tests remain subject to the validated-media environment.
- No end-to-end acceptance with real GStreamer playout has taken place; station acceptance is the passive observation
  above.

## Station separation

Authorize, preflight, install and verify KOGR and WRJE independently.
