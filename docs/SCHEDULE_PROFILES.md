# Schedule profiles (roadmap 3.1)

3.1A established the stable profile identity, profile-scoped resolver and
truthful log provenance. 3.1B adds the operator workflow on the existing
`/schedule/` page: named profile lifecycle, explicit activation/default
selection, inactive-profile editing and hourly one-date overrides. 3.1C adds
minute-resolution scheduling (see "Minute resolution (3.1C)" below). A station
that never creates a second profile, or never adds a minute transition, behaves
exactly as before.

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

`resolve_schedule_block(target_date, hour, profile=None)` is the **legacy
exact-hour compatibility resolver**. It only ever looks at a row starting
exactly at `HH:00` and is deliberately unchanged by 3.1C (the engine's
blank-continuation-hour logic depends on that meaning). Minute detail is
resolved by the separate `resolve_schedule_segments` described below.

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

## Operator workflow (3.1B)

Selecting a profile in `/schedule/` changes only what the operator is viewing
and editing. It never activates that profile. Active and default are separate,
explicit actions; either can change without rebuilding, deleting, approving or
otherwise reinterpreting a `PlaylistLog`. Activation uses the active UUID the
browser observed as an optimistic-concurrency token and returns a conflict if
another session activated a profile first.

Profile creation starts empty and inactive. Clone creates a new UUID and copies
the source's exact recurring `ScheduleBlock` rows; date rows are included only
when explicitly requested. Rotations and Playlists remain shared definitions,
and generated logs are never copied. Archived profiles remain inspectable but
cannot be edited, activated or set as default. Active/default profiles cannot
be archived. Hard deletion is limited to a profile that is neither active nor
default and has no blocks or log-provenance references.

The schedule API remains backward compatible: omitting a profile means the
active profile. `profile=<uuid>` on reads/deletes and `profile_uuid` on writes
target an inactive profile without activating it. A date read returns 24
server-resolved hourly cells whose origin is `date_override`, `weekly` or
`none`. Assigning content to a date cell creates/updates only its dated row;
"Revert to Weekly" deletes only that row and reveals the recurring row again.
There is intentionally no representation for an explicitly blank one-date
override in 3.1B.

All lifecycle mutations require `schedule.edit` and emit privacy-safe
`SystemEvent` audit records with stable profile UUIDs. The
`ScheduleProfileState` Django admin remains read-only; lifecycle authority is
the schedule workflow, not an unaudited admin pointer edit.

## Minute resolution (3.1C)

**Storage: transition rows, no migration.** `ScheduleBlock.start_time` was
always a `TimeField` and uniqueness is by exact `start_time`, so a schedule may
hold several rows in one hour. `10:00 A`, `10:30 B`, `10:45 A` is exactly three
rows -- never sixty. The `HH:00` row is the *base* of the hour; later rows are
explicit transitions layered on it. Only rows that start exactly on a minute
take part (a hand-entered `10:30:15` is ignored).

**Layering** (`library/services/schedule_resolution.py`, always inside ONE
profile): the recurring weekly layer is the lower layer and the selected date's
`specific_date` rows are the override layer. Within a layer the latest row at or
before a minute wins. The date layer takes precedence from its first row onward:
a *later* weekly transition does not punch through an already-active dated row,
and a later dated row supersedes an earlier dated row. Before a date layer's
first row, weekly inheritance remains visible. Example -- weekly `10:00 A`,
`10:30 B`, `10:45 C` with dated `10:20 D`, `10:50 E` gives `A` until `10:19`,
`D` from `10:20`, `E` from `10:50`. There are no blank/tombstone overrides;
"Revert" deletes exactly that dated row and reveals the lower layer.

**Base-hour invariant.** A buildable hour needs an effective assignment at
`HH:00`; an hour holding only later rows resolves to no segments, so a lone
`10:30` row cannot make a blank hour "partially scheduled". Writes enforce it: a
non-zero weekly transition needs a weekly `HH:00` base, and a non-zero dated
transition needs a dated base or a weekly base for that date (409 otherwise).
Deleting a base while later transitions depend on it is a 409 with the blockers.

**One PlaylistLog per hour, built once.** `build_hour_log` captures the profile
once, resolves the hour's ordered segments once, builds them into one pick list
and persists it exactly once. The segments share one build context, so tracks,
artist/related-artist identities, the identity cache, holiday state and the
accumulated clock carry across boundaries (explicit Playlist items seed the
exclusions for a later Rotation, but are never altered by recency).

**Boundaries are programming intent, not hard cuts.** A segment's budget is the
next transition (shifted by `late` seconds when clock-drift recovery shortens the
hour), so the existing pair-landing/exact-fit logic lands near the boundary at a
natural track edge; nothing is truncated and playback is untouched. A Playlist is
never cut: if it overruns, the next segment starts late (deterministic, recorded
as `delay_seconds`, shown in the preview). A segment whose whole window was
consumed by an overrun still begins -- one Rotation track or the whole Playlist --
never skipped. A segment whose window ended before a late-started hour began is
reported `elapsed_before_start` and not built.

**Unchanged contracts.** Blank continuation hours stay blank (a block that
started earlier is never carried into a later hour, and no extra log is built
there); schedule edits never rewrite, rebuild or unapprove an existing log; the
`(date, hour)` advisory lock remains the single build authority; the engine still
builds one wall-clock hour at a time. Cloning copies the exact rows, minute
transitions included.

**API.** Writes accept `minute` (integer 0-59, default 0; no rounding or
quantizing). Block payloads gain `start_minute`. `GET /api/schedule/hour-detail/`
(`?day_of_week=N` or `?date=YYYY-MM-DD`, `&hour=H`, optional `&profile=<uuid>`)
returns the server-derived sixty-minute detail for the Hour Detail panel, marking
explicit transitions, inherited weekly transitions (date mode) and whether each
minute is a date override, weekly or empty. `preview_hour_log` runs the same
segment builder without persisting and adds a `segments` list.

**UI.** The weekly 7x24 overview is unchanged; an hour holding minute transitions
is drawn as a neutral striped cell with a `+N` count. The Hour Detail panel (a
detail button on a cell, or *Detail* on mobile; *Hour detail* in Date Override)
shows the minutes as the server resolved them. Only the marked transitions
persist.

## Operator presentation (3.1D)

The Date Override view is a vertical 24-hour operator schedule on both desktop
and mobile. Each row labels the effective `HH:00` assignment as **WEEKLY**,
**OVERRIDE** or **EMPTY** using the server's `origin` value. A striped row and
`+N` badge mean that the hour has additional effective minute transitions, so
the assignment shown at `:00` must not be read as owning the whole hour. Open
**Hour Detail** for the authoritative server-resolved 60-minute timeline;
solid marked cells are explicit transitions and dashed cells are effective
inherited weekly transitions. Shadowed weekly rows are not displayed as
effective inherited transitions.

The Previous, Next, Today and native date controls change only the date being
inspected inside the selected profile. The same Rotation/Playlist picker is
shared with Weekly mode and remains available while Hour Detail is open.
Selecting content and pressing an editable hour creates or updates its dated
`:00` row. **Revert to Weekly** retains its narrow meaning: it deletes only the
identified explicit dated base row, subject to the existing dependency/conflict
checks, and then reveals the recurring weekly layer. Nonzero dated transitions
continue to be edited individually in Hour Detail. Archived profiles remain
inspectable while assignment, revert and minute-write controls are read-only.
