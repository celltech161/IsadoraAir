# r0136 installation, verification and recovery

Publication does not authorize deployment. This runbook applies only after separate authorization for the named
station. KOGR and WRJE must be authorized, preflighted and accepted independently.

## Exact identity and trusted plan

- Previous release: r0135 at `c5d3f85f1ca526f3e80e1bddc7b857d6b6555111`.
- Accepted engineering history, in exact order:
  - `ba3f8a4160fe56f5a4b8263c1349943e01fa0f06`;
  - `6e191cf5249747c74a4b16e77fdfb220f2cbf7e4`, tree
    `151e025b0fc7b8c9a0f70faf868b2ed14c0fb337`.
- R is the unique canonical commit introducing `deploy/releases/r0136.json`, directly after that accepted history.
- A station already at r0135 must preview exactly one release, r0136: advance source to R, apply no migrations or
  dependency changes, and restart `isadoraair-gunicorn` then `isadoraair-engine`.
- There is no migration-authorization companion, protected-runtime transition, collectstatic, nginx or unit action.
- A station below r0135 must include every intervening release and its own actions. Do not shorten that plan.

Never replace source directly, install an engineering branch, rebuild an equivalent commit, or manipulate the live
checkout. Use only the protected Update Center after separate station authorization.

## Common read-only preflight

1. Establish the installed release from the Update Center installation record and verify the installed source is
   clean. Do not infer release identity from `HEAD` alone.
2. Require `schema_current`, no pending intervention, and no running Update Center job.
3. Verify current backups and recovery evidence, protocol compatibility and protected-runtime health.
4. Record service PIDs and activation times for Gunicorn, engine, encoders, Monitoring, RBDS, Active Messages and
   spoken rendering. All must be healthy before starting.
5. Record the current and next approved logs, current item, queue position and whether a log build is running. Avoid
   installation near the top of the hour or during a current/next-hour build.
6. Record Log fill config and its fallback Category. Confirm it still has suitable Ready-to-Air material. Do not
   alter the strategy, Category or media for this release.
7. Confirm there is no pending urgent Weather/AMBER announcement, WxAlert, other urgent insertion or native Active
   Message. If one is pending, defer until it airs.
8. Confirm `remote_dj_connected: false`. If a Remote DJ session is active, defer or obtain the presenter's explicit
   agreement that the engine restart will end the session.
9. Preview the exact target R, release chain, action fingerprint and plan. From r0135 it must contain only source
   advancement and the Gunicorn-then-engine restarts.
10. Record encoder connections, program audio, Monitoring health and the current Hour Refill policy/arming state.

An engine restart interrupts program audio and may end an active Remote DJ session. Schedule it at an operationally
acceptable time and keep an operator present.

## Installation

After separate authorization, reconfirm the exact target and fingerprint immediately before starting. Use the
protected Update Center once. It advances source to R and restarts:

1. `isadoraair-gunicorn`
2. `isadoraair-engine`

No migration, package, requirements, static, nginx, systemd-unit or protected-runtime action is expected. Do not
restart any service manually and do not retry automatically after a failure.

## KOGR

Preserve KOGR's complete Roadmap 2.27 Stage 3 observation state:

- Hour Refill remains observation-only;
- `dry_run_only` checked;
- host installation arming disabled and `HOUR_REFILL_INSTALL_ARMED` false;
- existing cause/capture switches unchanged;
- existing observation evidence and scheduled evidence-collection timers preserved;
- current Log fill config preserved;
- existing Weather and urgent-insertion configuration preserved.

Record the policy row, runner status, latest evidence and timers before installation. The engine restart is an authorized interruption of Hour Refill observation, not a reset. Afterward, confirm the runner resumes in
observation-only mode and the next scheduled evidence collection succeeds. Do not arm 2.27, clear `dry_run_only`,
reset evidence or reschedule timers.

## WRJE

WRJE requires a separate authorization and preflight. Do not assume its installed release, fallback Category,
available PSA durations, schedule, Remote DJ state, Weather state or Hour Refill policy matches KOGR. Success on KOGR
is neither authorization nor evidence for WRJE.

## Passive post-install acceptance

Do not induce a short hour, empty a queue, alter an approved log, generate a test alert or force a production failure.
Acceptance on air is passive.

1. Verify the Update Center job succeeded, installed release is r0136, source is exact canonical R and the checkout
   is clean.
2. Verify only Gunicorn and engine have new PIDs. Encoders, Monitoring, RBDS, Active Messages and spoken rendering
   retain their recorded identities and remain healthy.
3. Confirm no migration or other undeclared action occurred.
4. Confirm program audio and encoder continuity after the engine restart.
5. Inspect engine startup and resume evidence. The current approved log and queue position must be correct; no aired
   item may start again.
6. Through the next top of hour, passively confirm ordinary queue handoff occurs once, mix-out/crossfade remains
   normal, there are no duplicate starts, and an older hour is not reloaded after adoption.
7. If a genuine short hour occurs naturally, confirm PSA fallback uses the configured Category, no closing item is
   planned past the hour, and no `Live-fill worker crashed` event appears. Do not manufacture this condition.
8. Confirm Monitoring remains normal and no unexpected urgent, Weather, Active Message or Hour Refill event appears.
9. On KOGR, confirm observation resumes, timers retain their schedule and no Hour Refill installation record exists.

An unused old-hour PSA row that was appended but superseded by timely new-hour adoption is acceptable. It must not
play as part of the new hour or cause a duplicate start.

## Stop and escalation conditions

Stop, preserve the Update Center job, engine journal, engine state and Monitoring evidence, and escalate on:

- any plan or action beyond source advancement and the two declared restarts;
- target or fingerprint mismatch;
- engine start failure, missing audio or encoder disconnection;
- overlapping or duplicate starts;
- a closing item planned or played beyond the protected boundary;
- repeated `Live-fill worker crashed`, exhausted-queue or older-hour reload evidence;
- a pending urgent insertion that disappears;
- any Log fill config, Weather, Active Messages or Hour Refill policy/arming change;
- KOGR observation or evidence collection not resuming.

The Update Center makes no automatic rollback promise, and no automatic source rollback is authorized. A further
engine restart is not harmless: it interrupts audio again and may end another Remote DJ session. Do not reset Git,
edit station data, alter approved logs, move media or restart services ad hoc as recovery.

## Remaining limitations

- The exhausted-queue recovery limitation documented in the release notes is deferred and unchanged from r0135.
- Closing fill intentionally does not handle deficits of 20 seconds or less.
- Installation has not been accepted on real station playout until the passive checks above are completed under a
  separate authorization.
