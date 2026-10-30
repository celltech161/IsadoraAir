# r0121 installation and verification

**This is guidance for a later, separately authorized installation.**
Preparing or publishing r0121 authorizes no KOGR or WRJE installation. Each
station needs its own explicit operator authorization and Update Center action.

## Identities

| What | Value |
| --- | --- |
| Expected predecessor | r0120 release commit `07fd80bf44406b9219dc91d144f8a336c50e1796` |
| r0121 release target **R** | the unique commit introducing `deploy/releases/r0121.json` |
| Trusted tip **C** | R's direct, metadata-only child adding `deploy/migration_authorizations/r0121.json` |

After installation the station checkout is **R**, not C. C is trusted release
metadata used to authorize the reviewed migration operations.

## Maintenance window

The install collects static files and restarts the web application and audio
engine. The engine restart briefly interrupts playout and ends any Remote DJ
session. Use a quiet, explicitly approved window with no Remote DJ connected.

## Read-only preflight

Before authorizing a station, confirm without changing state:

1. The checkout is exactly the expected clean r0120 release target, and the
   trusted repository tip contains C on a fast-forward history. **STOP if the
   installed target is not r0120.** An older station produces a multi-release
   plan whose migrations and operations require their own independent review;
   this guide does not authorize that plan.
2. Backup and recovery evidence is current and healthy.
3. Update Center, Gunicorn, engine, encoders, and monitoring are healthy; no
   update job is pending or failed.
4. No Remote DJ session is connected.
5. The Hour Refill policy is absent or remains disabled and dry-run-only, with
   the two r0120 cause switches Off. Before 0097/0098, the Start Next,
   automatic-skip, and media-failure columns do not exist and must not be
   reported as though they do. Confirm separately that the host is unarmed.
6. Run `docs/releases/r0121_verify.sql` as the application database role. Its
   pre-install result must show no 0097/0098 migration rows, only the 0096-era
   known-type cause constraint, and a complete JSON policy object containing
   only the fields available at 0096. It must make no change.

## Review the trusted plan

From an r0120 station, the Update Center plan must show:

- release `[r0121]`, target R;
- migrations, in order:
  `library.0097_hour_refill_start_next_cause`, then
  `library.0098_hour_refill_automatic_failure_cause`;
- exactly seven manual operations:
  - 0097 index 4 `RemoveConstraint`;
  - 0097 indices 5 and 6 `AddConstraint`;
  - 0098 index 6 `RemoveConstraint`;
  - 0098 indices 7, 8, and 9 `AddConstraint`;
- the trusted r0121 companion covers all seven exact identities;
- static collection required;
- no package, unit, daemon-reload, nginx, requirements, or protected-runtime
  action; and
- restarts exactly Gunicorn, then engine.

Stop if the plan differs.

## Installation

Use only the normal Update Center workflow against the reviewed trusted tip.
It applies 0097/0098 under the existing checkpoint and plan/provenance gates,
uses the r0121 companion for the seven reviewed manual operations, collects
static files, and restarts:

1. `isadoraair-gunicorn`
2. `isadoraair-engine`

If the job reports `MIGRATION_OPERATION_MANUAL`, incomplete central coverage,
different migration bytes, or any other migration gate, stop and investigate.
Do not approve locally, edit the migration, retry blindly, or run
`manage.py migrate` manually as a workaround.

## Post-install verification

1. Confirm the Update Center job succeeded and recorded central coverage of
   all seven manual operations. Confirm station HEAD is R, not C.
2. Confirm `manage.py check` is clean apart from any explicitly accepted
   existing deployment warning, and Gunicorn and engine are active.
3. Confirm the engine restarted cleanly and ordinary playout resumed.
4. Run `docs/releases/r0121_verify.sql` as the application role. Expect both
   migrations once, all four named cause constraints present, no new policy
   row, and report (not assume) current cause/install/origin counts. If a
   policy row exists, its JSON object must show master Off, all five cause
   switches Off, and `dry_run_only` true.
5. In Admin, inspect Hour Refill status without saving. Confirm master Off,
   all five cause switches Off, observation-only On, and host unarmed.
6. Confirm current iPortal JavaScript is served after static collection and
   the workstation clock does not render `:60`.
7. Passively observe normal requests, live fill, protected closers, and an hour
   transition. Do not manufacture on-air failures for verification.

Record R, C, plan identity, database verification output, static collection,
restart results, and health evidence separately for each station.

## Not authorized

r0121 does not authorize enabling any cause, changing a provisional threshold,
arming the host, leaving dry-run observation, or installing refill material.
KOGR and WRJE retain independent audit, observation, timing, and activation
decisions. The 06:00 pair-landing alert remains a separate P2 follow-up.

## Recovery and rollback

Source rollback and schema reversal are separate, independently reviewed
recovery paths. A source rollback must preserve the 0097/0098 schema and all
append-only evidence. r0120 source does not have blanket write compatibility
with the new NOT NULL columns after their migration-time defaults are removed
at database level. In the intended feature-Off state, with no policy row, the
older source does not normally write the affected policy or new evidence
fields; that does not establish safety for every operational configuration,
and best-effort capture failure is not a universal compatibility guarantee.

Once Start Next, automatic-skip, or media-failure evidence exists, reversing
0097/0098 may be refused by the older known-type constraints. Despite 0097's
historical migration comment, **never delete evidence** as a routine rollback
prerequisite. Destructive schema reversal, forward-only recovery, or restoring
the pre-migration checkpoint each requires a separate operator decision.
