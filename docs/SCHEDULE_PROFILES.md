# Schedule profiles (roadmap 3.1)

3.1A is the foundation only: a stable profile identity, a profile-scoped
resolver, and truthful log provenance. A station that never creates a second
profile behaves exactly as before. The multi-profile `/schedule/` UI, date
override editor and minute-level scheduling arrive in later 3.1 releases.

## Model

- `ScheduleProfile` -- `uuid` (immutable, the portable identity), unique
  `name`, `description`, `sort_order`, `is_archived` (reserved for the later
  lifecycle UI), timestamps.
- `ScheduleProfileState` -- singleton (`pk=1`) with two distinct pointers:
  `active_profile` (used for NEW schedule resolution now) and
  `default_profile` (the persistent normal/fallback profile). Both point at
  "Default Schedule" after migration. Never model "active" as a per-row flag.
- `ScheduleBlock.profile` -- required, `PROTECT`. Every block, recurring
  (`day_of_week`) and one-off (`specific_date`), belongs to exactly one profile.
- `PlaylistLog.schedule_profile` -- nullable, `PROTECT`. The profile actually
  used when the log was generated. `NULL` means legacy/unknown (logs from
  before 3.1A, and logs built directly from a chosen playlist rather than by
  schedule resolution). Historical logs are never backfilled.

Uniqueness (partial unique constraints, by exact `start_time`, not by hour):
one recurring block per `(profile, day_of_week, start_time)` and one dated
block per `(profile, specific_date, start_time)`.

## Resolution

`resolve_schedule_block(target_date, hour, profile=None)`:

1. that profile's `specific_date` block starting exactly at `HH:00`;
2. else that profile's recurring `day_of_week` block starting at `HH:00`;
3. else `None`.

It never falls through to another profile, and a specific-date block in one
profile never affects another. Omitting `profile` uses the active profile.

A multi-step operation captures the profile ONCE at its boundary
(`build_hour_log` does) and threads that concrete profile through resolution
and persistence. Do not re-read the active pointer per step.

## Not changed by 3.1A

One `PlaylistLog` per `(date, hour)`; approved-log reuse; admin rebuild
semantics; active/imminent-hour guard; advisory locking; recency (station-wide,
not per profile); the `schedule.edit` capability. There is no Clock/ClockSlot
layer -- old comments describing one are stale.

## Migrations

`0086` additive schema (nullable FKs, new tables) -> `0087` bounded RunPython
backfill (creates "Default Schedule", assigns every existing block, sets
active/default; aborts naming the exact rows if existing data is ambiguous and
never deletes or merges) -> `0088` enforcement (NOT NULL, partial unique
constraints). Update Center's classifier treats `0086`'s operations as additive
(new tables, nullable fields) but classifies `0087`'s RunPython, `0088`'s three
NOT NULL AlterFields and its two AddConstraints as manual, so migration-plan
review is expected.

### Deployment window (first profile release)

Update Center applies a release in this order: `migrate` runs from the staged
target source, the target schema is re-probed, the live source is
fast-forwarded, remaining deployment work (collectstatic, unit reconciliation)
runs, and only then are the manifest's declared services restarted on the new
source. Running Gunicorn and engine processes keep their old code loaded until
that restart.

So, in the release that first introduces required `ScheduleBlock.profile`
ownership, there is a narrow interval that starts once `0088` has made
`ScheduleBlock.profile_id` NOT NULL and lasts until the application services
restart. Pre-3.1A (r0095) code that is still running cannot supply a profile,
so creating a ScheduleBlock in that interval (the `/schedule/` POST or Django
admin) can fail. Reads, log generation and playout are unaffected.

**Operations guidance:** do not edit `/schedule/` while this release is
actively installing. Existing playout and schedule reads can continue. Resume
schedule editing after Update Center reports success and the application
services have restarted on the new source.

This window is accepted deliberately. The migrations are not redesigned around
it (no triggers, database defaults, placeholder profile ids or intermediate
nullable release). Which services the release restarts (Gunicorn and the
playout/log-generation engine both load this code) and the correct
`migration_compatibility` declaration for `0086`-`0088` (which include NOT NULL
and uniqueness enforcement) are release-packaging decisions.

## Profile state recovery

`ScheduleProfileState.load()` never guesses. If the singleton row is missing:
exactly one profile exists -> it is recreated with that profile as both active
and default; several profiles exist -> `ScheduleProfileStateError` (which one is
active is a station decision, and choosing by pk, sort order, name or archive
flag could silently switch the on-air schedule); no profile exists ->
`ScheduleProfileStateError` (configuration is never manufactured at runtime).
Resolution with an explicitly supplied profile does not read the state row.
Admin displays only read the row and never recreate it.
