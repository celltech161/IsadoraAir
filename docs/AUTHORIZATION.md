# Authorization — capability architecture (Roadmap 2.5)

This document covers what exists **today**, after Phase 2.5B (Scheduled
Talent Authorization). See `PROJECT_NOTES.md`'s "Roadmap 2.5" section for
the full audit history, the operator's binding corrections to the original
design, and the phase-by-phase continuation record. This document is the
durable, git-tracked contract; `PROJECT_NOTES.md` is the working scratchpad.

## The central distinction

**Page/workspace reachability is not the same thing as permission to
perform an operation.** IsadoraAir has two independent authorization
layers, and they must never be collapsed into one:

- `library.models.GroupAccess` (existing, unchanged by 2.5) — coarse,
  per-Group URL-prefix/exact/regex reachability, enforced by
  `library.middleware.GroupBasedAccessMiddleware`. Answers "can this
  user's browser reach this URL at all."
- `authz` (new, roadmap 2.5) — a capability evaluator answering "may this
  user perform this specific operation," enforced by an explicit
  `authorize()` call inside the view/service itself. A URL being reachable
  via `GroupAccess` never, by itself, implies any `authz.Capability`.

## Data model

```
User
  |
  +-- Group(s) ---- GroupAccess (unchanged: coarse reachability)
  |       |
  |       +-- GroupRole (one per Group, optional)
  |              |
  |              +-- Role
  |                     |
  |                     +-- RoleCapability -- Capability
  |
  +-- TalentAssignment(s) -- WHEN a schedule-restricted capability
         the user already holds may be exercised (roadmap 2.5B)
```

- **`Capability`** (`authz.models.Capability`) — a fixed, code-referenced
  vocabulary (see below). `requires_schedule=True` marks a capability
  that additionally requires an active `TalentAssignment` covering the
  current station-local moment (2.5B; see "Scheduled authorization"
  below).
- **`Role`** (`authz.models.Role`) — an admin-named bundle of Capabilities,
  via the `RoleCapability` through table.
- **`GroupRole`** (`authz.models.GroupRole`) — binds exactly one `Role` to
  one `auth.Group` (`OneToOneField` on `group`). A Group with no
  `GroupRole` row grants no capabilities at all, independent of whatever
  `GroupAccess` grants it.
- There is no direct User↔Role relationship. A user's effective
  capability set is always the union, across every Group they belong to,
  of that Group's bound Role's capabilities.
- **`TalentAssignment`** (`authz.models.TalentAssignment`, roadmap 2.5B) —
  a user's scheduled on-air window. **Not a role-grant mechanism**: there
  is no `role`/capability field on this model, and none should ever be
  added. It is temporal *scope* over a capability the user already holds
  through the Group→GroupRole→Role→RoleCapability chain above — it can
  never grant a capability the user otherwise lacks. See "TalentAssignment
  semantics" below for the full model.
- **`ScheduleAccessConfig`** (`authz.models.ScheduleAccessConfig`, roadmap
  2.5B) — singleton, station-wide `pre_schedule_allowance_minutes` /
  `post_schedule_allowance_minutes`. See "Scheduled authorization" below.

## The evaluator

`authz.evaluator.authorize(user, capability_slug, *, resource=None,
context=None, now=None) -> AuthzResult` is the one authoritative place
every view (and, in 2.5C, the Remote DJ signaling server) asks "may this
user do this." `AuthzResult` is `(allowed: bool, code: str, reason: str)`
and is truthy/falsy directly, so `if not authorize(...):` works as a
one-line guard. Stable `code` values: `allowed`, `unauthenticated`,
`disabled_account`, `capability_missing`, and (2.5B) `no_assignment`,
`outside_schedule_window`, `inactive_assignment` — see "Scheduled
authorization" below for exactly when each of the three schedule-related
codes is returned.

`authorize()` takes a plain Django user object, never an `HttpRequest` —
required because roadmap 2.5C calls it from the Remote DJ signaling
server, which runs on its own thread/asyncio loop entirely outside any
Django request/response cycle (see `library/services/remote_dj_signaling.py`).
`now`, when given, must be an aware `datetime` and is used as "the
current instant" for schedule evaluation instead of the real wall clock —
for deterministic tests, and for any future caller (2.5C) that already
has its own authoritative instant in hand rather than wanting the
evaluator to call `django.utils.timezone.now()` itself.

View-side convenience: `authz.evaluator.forbidden_response(result)` turns
a denied `AuthzResult` into the right `JsonResponse` (401 if never
authenticated, 403 otherwise).

### Staff/superuser compatibility

Both `user.is_superuser` and `user.is_staff` unconditionally satisfy
every operational capability, checked *before* any Role lookup:

- **Superuser** is IsadoraAir's unconditional break-glass authority by
  explicit operator decision — never required to hold a Role.
- **Staff** gets the same unconditional pass as a *compatibility*
  decision: `library.middleware.GroupBasedAccessMiddleware` already lets
  staff bypass `GroupAccess` reachability entirely (its own docstring:
  "Staff/superuser bypass this table entirely"), so mirroring that exact
  convention in `authorize()` preserves every existing staff account's
  operational authority with zero migration/backfill and zero risk of a
  staff account silently losing access to something it could do
  yesterday. The alternative (backfilling staff into a Role via a Group)
  would only cover staff accounts existing at migration time, not staff
  accounts created afterward — rejected for exactly that reason.

**The implication is strictly one way.** Staff/superuser status grants
capability authority; no capability ever grants `is_staff`,
`is_superuser`, Django Admin access, or Update Center authority. A
powerful talent Role (e.g. one holding every scheduled on-air capability)
is not and can never become a system administrator through this
mechanism. This bypass applies to schedule-restricted capabilities too
(2.5B): staff/superuser are never required to hold a `TalentAssignment`.
This is a deliberate compatibility policy for the current migration, not
a statement that talent Roles may ever gain administrative authority —
the two remain unrelated.

## Scheduled authorization (Phase 2.5B)

A `Capability` with `requires_schedule=True` requires, in addition to
ordinary Role/Group possession, an active `TalentAssignment` whose
**effective window** currently covers the moment being evaluated:

```
account valid
+ capability possessed (Group -> GroupRole -> Role -> RoleCapability)
+ an active TalentAssignment whose effective window covers "now"
= allowed
```

Capability possession is checked **first and independently**. A missing
`TalentAssignment` can never be compensated for by holding the
capability; holding the capability is never skipped just because a
matching assignment exists. Concretely, `authorize()` returns:

- `capability_missing` if the Role/Group chain doesn't grant the
  capability at all — before any assignment is even looked up.
- `no_assignment` if the capability IS granted but the user has zero
  `TalentAssignment` rows.
- `outside_schedule_window` if the user has at least one assignment, but
  none of them currently cover "now".
- `inactive_assignment` if an assignment would otherwise cover "now" but
  is marked `active=False` — a more specific diagnosis than
  `outside_schedule_window` for exactly this case (useful for "why can't
  this DJ connect" troubleshooting).
- `allowed` if an *active* assignment's effective window covers "now".

### TalentAssignment semantics

Fields: `user`, `day_of_week` (recurring, 0=Monday..6=Sunday) OR
`specific_date` (one-off override) — a `CheckConstraint` enforces
**exactly one** of the two, mirroring `library.models.ScheduleBlock`'s
own exclusivity pattern (the vocabulary is shared; the model is not —
see the audit finding in `PROJECT_NOTES.md` on why `ScheduleBlock`'s
hourly, row-per-hour content-scheduling shape is not an appropriate
parent for a human show assignment) — plus `start_time`, `end_time`, and
`active`. No `role` field, deliberately, and none should ever be added.

**Cross-midnight**: if `end_time <= start_time`, the assignment is
interpreted as crossing midnight (`TalentAssignment.crosses_midnight`
property). Saturday 22:00→02:00 means Saturday night through Sunday
02:00.

### Effective window (half-open interval)

```
effective_start = scheduled_start - pre_schedule_allowance
effective_end   = scheduled_end   + post_schedule_allowance

authorized iff:  effective_start <= now < effective_end
```

The **end is exclusive** — deliberately, so two back-to-back assignments
can never both claim the exact boundary instant. Example (schedule
18:00–20:00, pre=10min, post=15min): `17:49:59` denied, `17:50:00`
allowed, `20:14:59` allowed, `20:15:00` denied. Works identically when
either allowance is `0` (e.g. schedule 18:00–20:00, pre=post=0: `18:00:00`
allowed, `19:59:59` allowed, `20:00:00` denied).

### Cross-midnight / allowance-boundary algorithm

Rather than special-casing "this assignment crosses midnight" separately
from "an allowance pushes the window across a calendar-day boundary,"
`authz.evaluator._assignment_covers()` checks each assignment against
**three candidate calendar-day anchors** — yesterday, today, and tomorrow
relative to station-local "now" — and computes the effective window
fresh for whichever anchor(s) the assignment recurs/applies on. This one
mechanism, uniformly, handles:

- an ordinary same-day assignment;
- a recurring or specific-date assignment that itself crosses midnight
  (anchor = yesterday, e.g. "is this Sunday 00:30 covered by Saturday's
  22:00→02:00 assignment");
- a pre-allowance pulling a just-after-midnight assignment's window back
  into the previous calendar day (anchor = tomorrow, e.g. "is Friday
  23:55 covered by Saturday's 00:05 assignment with a 15-minute
  pre-allowance");
- a post-allowance pushing a late assignment's window into the next day
  (anchor = today, e.g. "is Thursday 00:04 covered by Wednesday's
  22:00–23:50 assignment with a 15-minute post-allowance").

Explicitly rejected as an implementation approach: filtering only
"today's" assignment rows at the database level and applying allowances
afterward — this misses every boundary-crossing case above. Assignment
row counts are small for a real station, so this is deliberately a plain
Python loop over `TalentAssignment.objects.filter(user=user)`, not a
clever, boundary-crossing-unsafe SQL query.

### Station time authority

`authz.evaluator._station_local_now()` converts to the configured
station timezone (`library.middleware.get_station_timezone()` /
`library.models.StationTimeConfig`) directly via `zoneinfo` — **not**
`django.utils.timezone.get_current_timezone()`, which depends on a
per-request `timezone.activate()` call that never happens outside a
Django request (required for 2.5C's engine/signaling-server caller).
Local wall-clock times are attached to the station zone via plain
`datetime.combine(...).replace(tzinfo=...)` and compared as aware
datetimes — standard zoneinfo/Django behavior, using Python's default
(`fold=0`) interpretation for the rare instant that falls in a
DST-transition's ambiguous or nonexistent hour. No separate DST
disambiguation policy exists; a station whose scheduled handoff moment
lands exactly inside a DST transition is a known, accepted edge case,
not specially handled.

### Station-wide access allowances

`ScheduleAccessConfig` (singleton, same `pk=1` `load()`/`save()` pattern
as `library.models.StationTimeConfig`) — `pre_schedule_allowance_minutes`
(default **10**) and `post_schedule_allowance_minutes` (default **15**).
Both are `PositiveIntegerField` (accepts `0`, rejects negative), editable
only through Django Admin, never hard-coded in the evaluator. Station-wide
only — no per-user or per-assignment override in 2.5B; not justified by
any repository finding, and would make the first implementation harder
for a small-station operator to reason about. The 10/15 defaults match
the exact example numbers used throughout the roadmap 2.5 audit and
workorder discussion.

### Production transition policy: existing Remote Hosts

**No real endpoint's enforcement was migrated to schedule-checking in
2.5B.** Every existing `remote_dj` account today has zero
`TalentAssignment` rows (the concept didn't exist before 2.5B). Every
`requires_schedule=True` capability (`remote_dj.connect`,
`playout.control`, `playout.manual_mode`, `remote_dj.mic_gate`,
`studio.mic_ptt`, `fx.fire`) is held **only** by the `Remote Host` Role,
bound to the `remote_dj` Group — meaning there is no currently-`
requires_schedule` endpoint where enforcing the schedule check wouldn't
immediately lock out every existing production Remote Host with
`no_assignment` the moment this ships. Wiring any of these into their
real view (e.g. `library.views.api_remote_dj_token`,
`library.views.api_engine_manual_mode`) is therefore **explicitly
deferred to 2.5C**, after station operators have had the opportunity to
review this phase and populate `TalentAssignment` rows for their actual
existing Remote Hosts. This is the first of the three transition
approaches the roadmap workorder itself named as acceptable; the
evaluator's `requires_schedule` semantics were not weakened, relaxed, or
made conditional in any way to work around this — the mechanism is fully
correct and fully enforced wherever it IS called (proven by 2.5B's own
comprehensive test suite,
`authz.tests.test_scheduled_evaluator`/`test_admin`); it is simply not
yet called from any currently-open production endpoint.

## Capability vocabulary

| slug | requires_schedule | Enforced at a call site? |
|---|---|---|
| `library.view` | no | no (existing checks unchanged) |
| `library.upload` | no | no (existing checks unchanged) |
| `library.manage_tracks` | no | **yes** (2.5A) |
| `library.manage_categories` | no | **yes** (2.5A) |
| `schedule.edit` | no | no (established, not wired up) |
| `rotations_playlists.edit` | no | no (established, not wired up) |
| `voicetrack.record` | no | no (existing `_can_edit_voicetracks` unchanged) |
| `remote_dj.connect` | yes | no — deferred to 2.5C, see transition policy above |
| `playout.control` | yes | no — deferred to 2.5C |
| `playout.manual_mode` | yes | no — deferred to 2.5C |
| `remote_dj.mic_gate` | yes | no — deferred to 2.5C |
| `studio.mic_ptt` | yes | no — deferred to 2.5C |
| `fx.fire` | yes | no — deferred to 2.5C |
| `fx.manage_carts` | no | no (existing staff-only check unchanged) |
| `reports.view` | no | no (existing staff-only check unchanged) |
| `webrequests.administer` | no | no (existing staff-only check unchanged) |
| `monitoring.restart_service` | no | **yes** (2.5A) |
| `system.administer` | no | no (reserved; nothing is gated on it yet) |

The vocabulary itself is unchanged from 2.5A — no slug was added, split,
or renamed in 2.5B.

## Security defects closed in 2.5A

Both were reachable purely because a group's `GroupAccess` prefix,
granted for an unrelated reason, happened to also reach a mutating
endpoint with no other check at all:

1. **`monitoring/views.py::api_restart_check`** — restarts a real
   systemd unit through the root-owned protected update broker.
   Previously reachable by any `remote_dj` account (their seeded
   `GroupAccess` row includes `/monitoring/`, granted for dashboard
   viewing). Now requires `monitoring.restart_service`.
2. **`library/views.py::api_category_detail`** (PATCH/DELETE) and
   **`api_category_list`** (POST) — category mutation/deletion.
   Previously reachable by any `Contributor` account (their seeded
   `GroupAccess` row includes `/api/categories/`, granted only so the
   upload page's category dropdown could read the list). Reads (GET on
   both endpoints) are untouched. Now requires `library.manage_categories`.
3. **A cluster of track-mutation endpoints**, found while closing (2) —
   same shape, same `/api/tracks/` prefix already granted to *both*
   `Contributor` and `remote_dj`: `api_track_bulk` (including its bulk
   "delete" action), `api_track_reanalyze`, `api_track_write_metadata`,
   `api_track_repick_cue_points`, and the three blocked-slot toggle
   endpoints. Now requires `library.manage_tracks`.

## Seeded Roles (migration `authz.0002`)

| Role | Bound Group | Capabilities |
|---|---|---|
| Contributor | `Contributor` | `library.view`, `library.upload` |
| Remote Host | `remote_dj` | `library.view`, `voicetrack.record`, `remote_dj.connect`, `playout.control`, `playout.manual_mode`, `remote_dj.mic_gate`, `fx.fire` |
| Station Administrator | *(none — not auto-bound to anything)* | every capability in the vocabulary |

The seed migration only binds `GroupRole` rows for `Contributor` and
`remote_dj` because those are the only groups with real, existing
effective behavior to preserve. `Station Administrator` exists as a Role
an operator can bind to a Group of their own choosing (e.g. a future
"Station Managers" Group) — it is deliberately not auto-bound to
anything, since `is_staff`/`is_superuser` already cover that need today
(see the staff/superuser compatibility rule above).

## Admin

`Role` and `Capability` have their own admin pages
(`authz.admin.RoleAdmin`, `CapabilityAdmin`); `RoleCapability` is managed
only as an inline on the `Role` change page. `GroupRoleInline` is
attached to the existing customized `auth.Group` admin
(`library.admin.GroupAdminWithAccess`), right alongside the pre-existing
`GroupAccessInline` — an operator editing a Group sees both "where can
this group go" (`GroupAccess`) and "what can this group do" (`GroupRole`
→ `Role` → capabilities) on the same page, as two clearly separate
inlines/models, never merged into one row.

**Roadmap 2.5B**: `authz.admin.TalentAssignmentInline` is attached to the
existing customized User admin (`library.admin.InviteCapableUserAdmin`)
— an operator looking at a talent account sees/edits that person's show
windows directly on the same page as their Group memberships, without
the inline ever implying an assignment grants anything by itself.
`TalentAssignmentAdmin` also exists standalone (`Role`/`Capability`-style
list+search) for station-wide schedule management without opening each
user individually. `ScheduleAccessConfigAdmin` follows the same singleton
pattern as `library.admin.StationTimeConfigAdmin`: add is blocked once
the one row exists, delete is blocked entirely, and the changelist
redirects straight to that row's own change page.
