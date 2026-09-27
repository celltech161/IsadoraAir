# Authorization — capability architecture (Roadmap 2.5)

This document covers what exists **today**, after Phase 2.5D (bounded
authorization closeout). See
`PROJECT_NOTES.md`'s "Roadmap 2.5" section for the full audit history, the
operator's binding corrections to the original design, and the
phase-by-phase continuation record. This document is the durable,
git-tracked contract; `PROJECT_NOTES.md` is the working scratchpad.

## Operator procedure: enabling scheduled enforcement

```
Upgrade/install (enforcement defaults OFF -- current behavior preserved)
   ->
verify Roles (Config > Roles -- confirm Contributor/Remote Host/Station
   Administrator capabilities match what you expect)
   ->
configure active Talent Assignments (Django Admin > Authentication and
   Authorization > Users > the account's Talent Assignments inline, or
   Config > Talent Assignments station-wide) for every account that needs a
   requires_schedule=True capability (today: Remote Host)
   ->
review pre/post allowances (Django Admin > Config > Schedule Access)
   ->
enable Scheduled Talent Enforcement (the same Schedule Access page's
   checkbox -- refused if
   any account would be immediately locked out; see "Safe activation"
   below)
   ->
verify Remote DJ access (have a real Remote Host attempt to connect
   inside and outside their configured window)
```

Do not enable step 5 before step 3 is genuinely done for every existing
Remote Host -- the form itself refuses to let you, but the underlying
reason (a real account would be locked out) is what to actually check.

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
context=None, now=None, schedule_policy="enforce", bypass_cache=False)
-> AuthzResult` is the one authoritative place every view, and (2.5C) the
Remote DJ signaling server and the engine's periodic re-authorization
tick, ask "may this user do this." `AuthzResult` is `(allowed: bool,
code: str, reason: str)` and is truthy/falsy directly, so
`if not authorize(...):` works as a one-line guard. Stable `code` values:
`allowed`, `unauthenticated`, `disabled_account`, `capability_missing`,
and `no_assignment`, `outside_schedule_window`, `inactive_assignment` —
see "Scheduled authorization" below for exactly when each of the three
schedule-related codes is returned.

`authorize()` takes a plain Django user object, never an `HttpRequest` —
required because 2.5C calls it from the Remote DJ signaling server and
engine tick, which run on their own thread/GLib loop entirely outside any
Django request/response cycle (see `library/services/remote_dj_signaling.py`
and `library/services/engine.py::_remote_dj_authorization_tick`). `now`,
when given, must be an aware `datetime` and is used as "the current
instant" for schedule evaluation instead of the real wall clock — for
deterministic tests, and for any future caller that already has its own
authoritative instant in hand rather than wanting the evaluator to call
`django.utils.timezone.now()` itself.

**`schedule_policy`** (roadmap 2.5C) — one of three module constants,
controlling how a call site relates to the station-wide activation switch
(see "Safe activation" below):

- `SCHEDULE_POLICY_ENFORCE` (`"enforce"`, the default) — respect
  `ScheduleAccessConfig.scheduled_enforcement_enabled`. OFF: a
  `requires_schedule=True` capability behaves like an ordinary one
  (compatibility bypass — capability possession is still mandatory, the
  assignment/window check is skipped). ON: full schedule evaluation. Every
  real production call site (views, token issuance, signaling admission,
  the session re-authorization tick) uses this default.
- `SCHEDULE_POLICY_STRICT` (`"strict"`) — always fully evaluate the
  schedule window regardless of the station switch. Used by tests
  asserting the scheduling *mechanism* is correct independent of
  activation, and by the activation-safety validation itself.
- `SCHEDULE_POLICY_IGNORE` (`"ignore"`) — never evaluate the schedule
  window; capability possession alone is sufficient. Used for
  page-reachability-style checks (`library.views.remote_dj_page`) where
  the UX goal is "a Remote Host can always see their own console," with
  the real schedule gate enforced at the actual privileged operation
  (token issuance) instead.

This is a *different* knob from the staff/superuser bypass or ordinary
capability check — `scheduled_enforcement_enabled` being OFF never means
"authorization is off." Capability possession, `is_active`, and
`is_authenticated` are checked unconditionally regardless of
`schedule_policy` or the station switch.

**`bypass_cache=True`** (roadmap 2.5C) skips the process-local
Group→capability cache and reads fresh from the database. Real Django
request handlers never need this (the cache is correctly invalidated by
Django signals within that process). It exists for the Remote DJ
signaling admission check and the session re-authorization tick, both of
which run inside the separate `manage.py run_engine` process — a Role/
GroupRole edit made through the web (gunicorn) process's admin fires
Django signals only in *that* process, so the engine process's cached
copy would otherwise never see the change short of an engine restart.

View-side convenience: `authz.evaluator.forbidden_response(result, *,
user=None, capability_slug=None)` turns a denied `AuthzResult` into the
right `JsonResponse` (401 if never authenticated, 403 otherwise). When
`user`/`capability_slug` are given (every 2.5C call site passes both),
it also records a lightweight audit event — see "Audit trail" below.

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

### Safe activation (roadmap 2.5C, tightened in 2.5D)

2.5B established the mechanism but deliberately called it from no real
endpoint (every existing `remote_dj` account had zero `TalentAssignment`
rows, and enforcing on day one would have locked every one of them out).
2.5C wires the mechanism into every real endpoint (see "Endpoint-to-
capability mapping" below) but keeps the SAME safety property by gating
all of it behind one station-wide switch:

`ScheduleAccessConfig.scheduled_enforcement_enabled` (`BooleanField`,
**default `False`** on every existing and fresh install — added by
`authz.migrations.0005`, a schema-only migration; `AddField` backfills
the existing singleton row with the default automatically, so an
upgraded station's behavior is byte-for-byte unchanged until an operator
deliberately flips it):

```
enforcement OFF (the default):
    user must still possess the capability (Group -> GroupRole -> Role)
    the TalentAssignment/window requirement is compatibility-bypassed

enforcement ON:
    user must possess the capability
    AND satisfy the TalentAssignment/window requirement
```

**Admin validation before enabling**: `authz.admin.ScheduleAccessConfigForm.
clean_scheduled_enforcement_enabled()` refuses to save the field as `True`
if `authz.evaluator.users_missing_talent_assignments_for_scheduled_
capabilities()` returns any account — every active, non-staff,
non-superuser user who holds a `requires_schedule=True` capability
through their ordinary Group→Role chain but has **zero active**
`TalentAssignment` rows. This is an *active-row existence* check, not a
live-window check: an active future specific-date assignment or an active
recurring assignment satisfies it (a DJ scheduled for next Tuesday should
not block activation today), while inactive-only rows do not. Turning
enforcement back **OFF** is never blocked and never touches any
`TalentAssignment` row.

**The evaluator's `requires_schedule` semantics were never weakened** to
make this safe — `schedule_policy="strict"` (used by tests and by the
validation function itself) always evaluates the real window regardless
of the switch; only `schedule_policy="enforce"` (the default at real
call sites) respects it. The mechanism is fully, unconditionally correct
wherever it is asked to be strict; the switch only controls whether
ordinary production call sites ask for that yet.

### Production transition policy: existing Remote Hosts — CLOSED in 2.5C

Every real endpoint named in "Endpoint-to-capability mapping" below now
calls `authorize()` with the default `schedule_policy="enforce"`. Because
the station switch defaults to OFF, this is **not a behavior change** for
any existing production station until an operator deliberately: (1)
configures `TalentAssignment` rows for their real Remote Hosts, and (2)
flips the switch, which the Admin form will refuse to accept until step 1
is genuinely done. See the "Operator procedure" section at the top of
this document.

## Capability vocabulary

| slug | requires_schedule | Enforced at a call site? |
|---|---|---|
| `library.view` | no | no (existing checks unchanged) |
| `library.upload` | no | no (existing checks unchanged) |
| `library.manage_tracks` | no | **yes** (2.5A) |
| `library.manage_categories` | no | **yes** (2.5A) |
| `schedule.edit` | no | **yes** (2.5D) — schedule and PlaylistLog mutations |
| `rotations_playlists.edit` | no | **yes** (2.5D) — rotation and playlist-definition mutations |
| `voicetrack.record` | no | **yes** (2.5C — `_can_edit_voicetracks` now calls `authorize()`) |
| `remote_dj.connect` | yes | **yes** (2.5C) — token issuance, signaling admission, session re-check |
| `playout.control` | yes | **yes** (2.5C) — seek, deck commands (transport control; operator-only) |
| `playout.queue_manage` | yes | **yes** (2.5C, new — see below) — queue insert, force-next, playlist play-now |
| `playout.manual_mode` | yes | **yes** (2.5C) |
| `remote_dj.mic_gate` | yes | **yes** (2.5C) |
| `studio.mic_ptt` | yes | **yes** (2.5C) |
| `fx.fire` | yes | **yes** (2.5C) |
| `fx.manage_carts` | no | no (existing staff-only check unchanged) |
| `reports.view` | no | no (existing staff-only check unchanged) |
| `webrequests.administer` | no | no (existing staff-only check unchanged) |
| `monitoring.restart_service` | no | **yes** (2.5A) |
| `monitoring.reset_listener_counters` | no | **yes** (2.5D) — peak/TLH resets |
| `aircheck.control` | no | **yes** (2.5D) — start/stop Aircheck recording |
| `system.administer` | no | no (reserved; nothing is gated on it yet) |

**Vocabulary correction in 2.5C** (`authz.migrations.0006_correct_remote_host_
playout_capability`): 2.5A's seed migration granted the Remote Host Role the
single, bundled `playout.control` capability ("queue/seek/deck commands").
Re-auditing the real endpoints before wiring them up (per the workorder's own
instruction) found this would have been a real permission *expansion* once
`playout.control` was actually enforced at `api_engine_seek`/
`api_engine_deck_command` — `remote_dj_page`'s own docstring is explicit that
remote_dj mode "hides ... deck eject/pause, waveform click-seek" (operator-
only), while separately keeping "force-next" and "Play Now" available. 2.5C
therefore split the vocabulary: `playout.control` now means transport
control only (seek, deck pause/resume/eject — Station Administrator only,
matching today's real `GroupAccess` grant, which never included those
paths); the new `playout.queue_manage` covers queue insert, force-next, and
playlist play-now, and is what the Remote Host Role actually holds. This is
a correction to an inaccurate mapping discovered before it caused harm, not
an expansion of anyone's real access.

## Endpoint-to-capability mapping (roadmap 2.5A–2.5D)

Every mutation/control endpoint below enforces `authorize()` server-side;
none of them rely on `GroupAccess` for anything but coarse page
reachability (which remains independently required — see "The central
distinction").

| Endpoint | Capability | Notes |
|---|---|---|
| `POST /api/remote-dj/token/` (`api_remote_dj_token`) | `remote_dj.connect` | `schedule_policy="enforce"` (default) |
| `GET /remote-dj/` (`remote_dj_page`) | `remote_dj.connect` | `schedule_policy="ignore"` — reachability only, see "Safe activation" |
| `POST /api/engine/queue/set-next/` (`api_engine_set_next`) | `playout.queue_manage` | |
| `POST /api/engine/queue/insert/` (`api_engine_insert_track`) | `playout.queue_manage` | |
| `POST /api/playlists/<pk>/play-now/` (`api_playlist_play_now`) | `playout.queue_manage` | found during the 2.5C re-sweep, see below |
| `POST /api/engine/seek/` (`api_engine_seek`) | `playout.control` | |
| `POST /api/engine/deck/<slot>/` (`api_engine_deck_command`) | `playout.control` | |
| `POST /api/engine/manual-mode/` (`api_engine_manual_mode`) | `playout.manual_mode` | |
| `POST /api/engine/remote-dj-gate/` (`api_engine_remote_dj_gate`) | `remote_dj.mic_gate` | usable by operator or the connected DJ |
| `POST /api/engine/mic-ptt/` (`api_engine_mic_ptt`) | `studio.mic_ptt` | operator-only in practice (no Role grants it but Station Administrator) |
| `POST /api/fx/fire/` (`api_fx_fire`) | `fx.fire` | |
| `_can_edit_voicetracks()` (5 call sites: upload/audio/save/delete + `voicetracks_page`) | `voicetrack.record` | not schedule-restricted; unchanged effective behavior |
| `POST /api/schedule/`, `DELETE /api/schedule/<pk>/` | `schedule.edit` | schedule reads remain capability-free |
| all mutation methods on `/api/rotations/...` | `rotations_playlists.edit` | rotation reads remain capability-free |
| all playlist-definition mutation methods on `/api/playlists/...` | `rotations_playlists.edit` | excludes `play-now`, which remains `playout.queue_manage` |
| `api_log_build/update/delete/reorder/item_swap` | `schedule.edit` | log reads and preview/dry-run remain capability-free |
| `POST /monitoring/api/listeners/reset-peak/`, `reset-tlh/` | `monitoring.reset_listener_counters` | Remote Host reachability does not grant reset authority |
| `POST /api/aircheck/start/`, `stop/` | `aircheck.control` | status read remains capability-free |

`GroupAccess` reachability gaps closed alongside the capability mapping
(without either, the intended feature simply doesn't work end to end —
see "Discovered during the re-sweep" below): `remote_dj`'s seeded row now
also includes `/api/engine/queue/set-next/` (`library.migrations.
0085_remote_dj_queue_set_next_access`).

## Remote DJ trust boundary (roadmap 2.5C)

Three enforcement points, all calling the same `authorize()`:

1. **Token issuance** (`api_remote_dj_token`) — `authorize(request.user,
   "remote_dj.connect")`, default policy. Denial returns the real
   `AuthzResult.reason` in the JSON body's `error` field — deliberately
   generic/operator-facing text (e.g. "No talent assignment is currently
   in its effective window..."), never a signing key, another user's
   schedule, or other internal state. The dashboard's `rdjConnect()` now
   surfaces this text directly instead of a generic "Token failed" (see
   "UI capability awareness" below).
2. **Signaling admission** (`library.services.remote_dj_signaling.
   RemoteDJSignalingServer._handler`) — a valid token signature proves
   the token was minted at some point in the last `TOKEN_MAX_AGE_SECONDS`
   (60s); it does **not** prove the authorization that was true at mint
   time still holds. `_handler` re-fetches the User row and re-runs
   `authorize(user, "remote_dj.connect", bypass_cache=True)` before
   admitting, closing every one of: Role/capability removed after
   issuance, account disabled, assignment disabled, scheduled window
   expired between issuance and admission, or enforcement enabled in that
   same window. Denial closes the socket with a new code,
   `CLOSE_CODE_NOT_AUTHORIZED` (4003), distinct from the existing
   `CLOSE_CODE_INVALID_TOKEN` (4001, bad signature) and
   `CLOSE_CODE_SESSION_BUSY` (4002).
   - This check is a genuine cross-thread, cross-process-cache-freshness
     operation: `_handler` is a plain `async def` coroutine (the signaling
     server runs its own dedicated thread + asyncio loop, per this
     module's own docstring), and Django refuses synchronous ORM access
     from a running event loop regardless of which OS thread is running
     it — the check runs via `asgiref.sync.sync_to_async` on a throwaway
     worker thread, which explicitly closes its own DB connection before
     returning (`django.db.connections.close_all()`) since nothing else
     would ever clean it up.
3. **Active-session re-authorization** — see below.

## Active-session reauthorization (roadmap 2.5C)

`PlaybackEngine._remote_dj_authorization_tick` (`library/services/
engine.py`), registered via `GLib.timeout_add_seconds(
REMOTE_DJ_AUTHZ_TICK_SECONDS, ...)` alongside the signaling server itself
(only when `RemoteDJConfig.enabled`) — a no-op tick when no session is
active. While a session **is** active, every tick re-fetches the session's
`user_id` (threaded through from the signaling server's already-verified
token identity at `_remote_dj_session_start`, stored on `RemoteDJSession.
user_id`) and re-runs the identical `authorize(user, "remote_dj.connect",
bypass_cache=True)` — no second schedule calculator exists anywhere in
the engine.

**Cadence / maximum delay**: `REMOTE_DJ_AUTHZ_TICK_SECONDS = 5`. This is
the documented maximum delay between authorization actually becoming
invalid (scheduled effective end reached, `TalentAssignment` disabled/
deleted, Role/capability removed, account disabled, or enforcement
enabled mid-session) and the session being torn down. Because the
scheduled effective end is exclusive (2.5B's half-open interval), the
FIRST tick at or after that instant sees `authorize()` deny and retires
the session — no additional grace is layered on top of the tick cadence
itself.

**Termination** reuses the existing, unmodified `_remote_dj_session_stop()`
path verbatim: gate closes, manual-mode-from-mic folds back to Auto, slot/
signaling teardown all happen exactly as an ordinary disconnect. This is
deliberately **not** treated as a transport failure — no reconnect-grace
window (`RemoteDJConfig.reconnect_grace_seconds`, which exists for a
recoverable WebRTC transport loss, a different concept) is granted; an
authorization revocation is not recoverable by waiting.

## Audit trail (roadmap 2.5C)

Reuses `monitoring.models.SystemEvent`/`emit_event()` (`category="authz"`)
— no parallel logging subsystem. Coalesced via `emit_event`'s own existing
60-second dedupe window (keyed on user+capability+code, or
user+attempt+outcome code),
so a script hammering a denied endpoint produces one row with a rising
`repeat_count`, never a flood. Never logs credentials, signed tokens, or
unnecessary personal data — only identity (username), capability, and
outcome code.

Emitted at:

- Every `authz.evaluator.forbidden_response(result, user=..., capability_slug=...)`
  call — i.e. every denied privileged mutation/control attempt at a real
  view, including Remote DJ token denial (same code path as any other
  endpoint).
- Remote DJ signaling admission denial (`RemoteDJSignalingServer.
  _check_remote_dj_authorization`).
- Remote DJ session terminated because authorization was revoked
  (`PlaybackEngine._remote_dj_authorization_tick`).
- Scheduled enforcement enabled/disabled (`ScheduleAccessConfigAdmin.
  save_model`) — records who flipped it and to which state.

Deliberately **not** emitted for every successful periodic re-check (only
the terminating one) or any GET — avoiding log floods was an explicit
requirement, not an oversight.

## UI capability awareness (roadmap 2.5C)

Server-side enforcement is authoritative regardless of anything below —
this section is presentation only, and deliberately minimal per the
workorder's own instruction not to build an elaborate workspace/
navigation redesign in this phase:

- `dashboard.html`'s `rdjConnect()` now surfaces the real
  `AuthzResult.reason` text from a denied token response (e.g. "No
  talent assignment is currently in its effective window; 'remote_dj.
  connect' requires one.") instead of a generic "Token failed" —
  distinguishes "not permitted at all" from "outside your scheduled
  window" for the DJ without them needing to ask an operator. Falls back
  to the generic message if the response body isn't parseable JSON.
  Every other 2.5C-migrated endpoint already returns the same
  `{"error": "..."}` shape via `forbidden_response`; only the token flow
  had custom error-swallowing JS worth fixing here.
- Every control this phase migrated was already hidden/shown correctly
  by existing `mode`/`IS_REMOTE_DJ` template logic (`remote_dj_page`'s
  own docstring documents exactly which controls remote_dj mode hides) —
  no template changes were needed for those; the server-side capability
  now simply agrees with what the UI already only showed to the right
  audience.
- Not done: a live "outside your window, reconnect at 5:50 PM" indicator
  on the console before the DJ even clicks Connect. This would need the
  effective-window boundaries surfaced to the client ahead of time, which
  is a real feature but not requested by this phase's scope; left as a
  documented follow-up rather than built speculatively.

## Discovered during the 2.5C re-sweep

Re-running the roadmap 2.5 audit's search terms (literal `remote_dj`/
`Contributor` checks, `user_is_library_read_only`/`user_is_contributor`,
mutation endpoints protected only by `GroupAccess`, engine command
endpoints without a capability check) after implementation, per the
workorder's own instruction, found:

- **`api_playlist_play_now`** — same reachability-only shape as 2.5A's
  original two defects. `remote_dj`'s seeded `GroupAccess` regex
  (`^/api/playlists/\d+/play-now/$`) reached this "force the engine to
  play this playlist immediately" endpoint with zero other check.
  `remote_dj_page`'s own docstring names "Play Now" as an intended Remote
  Host feature (same evidentiary standard as the `playout.queue_manage`
  split above) — **closed**, requires `playout.queue_manage`.
- **`api_log_reorder`'s drag-to-reorder** — dashboard.html's remote_dj-
  mode UI also wires up drag-to-reorder against `/api/log/<pk>/reorder/`,
  which is NOT under `/api/engine/` and has no `GroupAccess` grant for
  `remote_dj` at all today — meaning this documented feature has never
  actually worked for a Remote Host in production, independent of
  roadmap 2.5. **Not fixed in 2.5C**: `api_log_reorder` takes an
  arbitrary `PlaylistLog` primary key (any hour, any day, not just "the
  currently active queue"), so closing this correctly needs a
  resource-scope check ("is this the currently active log") that doesn't
  exist yet — beyond a plain capability-slug swap, and outside 2.5C's
  named target list. Tracked as authorization/functionality debt below.
- No other literal `remote_dj`/`Contributor` group-name check remains
  anywhere in non-test, non-migration code. `user_is_contributor`/
  `user_is_library_read_only` remain, intentionally — see "Intentionally
  retained checks" below.

### Intentionally retained checks

- **`library.middleware.user_is_contributor`/`user_is_library_read_only`**
  and their call sites (`api_track_detail`'s own-upload-delete carve-out,
  `api_library_upload`'s category-auto-pin-to-own-username,
  `access_flags()`'s template flags) — these encode **resource-scoped**
  product rules (own upload, own category, not-yet-approved content),
  not a bare capability gate. The roadmap's own instruction is explicit
  that such rules should be preserved, not folded into a broad capability
  that would then imply unintended access to every object. Not migrated;
  not in 2.5C's named target list; 2.5A deliberately left the surrounding
  category/track READ paths these depend on untouched for the same
  reason.
- **Update Center / Web Requests config / Reports staff-only checks** —
  unchanged, proven still exclusive of any talent Role by
  `library.tests.test_authz_administrative_boundary_2_5c`.
- **`schedule.edit`/`rotations_playlists.edit`** — established in 2.5A
  and now enforced by 2.5D at the schedule, PlaylistLog, rotation, and
  playlist-definition mutation boundaries listed above.

### Remaining authorization debt

- `api_log_reorder` now has the primary `schedule.edit` boundary, but it
  still accepts an arbitrary `PlaylistLog` primary key. A future phase
  should decide whether it also needs a current-log/resource-scope rule;
  2.5D deliberately does not redesign PlaylistLog ownership.
- `api_cd_detect`/`api_cd_eject`/`api_cd_rip_start`/`api_cd_rip_status`/
  `api_cd_rip_cancel` remain outside capability authorization. They are
  physical-hardware-adjacent and no seeded non-staff Group can reach them.
  **If CD control is ever exposed to a non-staff workspace/group, add an
  independent server-side capability boundary before widening
  `GroupAccess`.**
- The final 2.5D mutation sweep confirmed three pre-existing library
  write surfaces that do not yet fit the final capability taxonomy:
  `api_library_upload` does not enforce `library.upload` before applying
  its Contributor category-pin rule; `api_track_detail` does not enforce
  `library.manage_tracks` before applying its Contributor own-upload
  deletion/read-only rules; and `api_track_autofill_related_artists`
  relies on `user_is_library_read_only` rather than
  `library.manage_tracks`. The two named seeded non-staff Groups remain
  constrained by those legacy group/resource checks, but these are not
  capability boundaries and a newly created Group with widened
  `GroupAccess` could expose them. They were outside the finite 2.5D
  punch list and were therefore documented rather than silently added to
  scope. Close them before declaring the whole roadmap item complete.

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

## Seeded Roles (migrations `authz.0002`, corrected by `authz.0006` and `authz.0007`)

| Role | Bound Group | Capabilities |
|---|---|---|
| Contributor | `Contributor` | `library.view`, `library.upload` |
| Remote Host | `remote_dj` | `library.view`, `voicetrack.record`, `remote_dj.connect`, `playout.queue_manage`, `playout.manual_mode`, `remote_dj.mic_gate`, `fx.fire` |
| Station Administrator | *(none — not auto-bound to anything)* | every capability in the vocabulary |

`Remote Host` holds `playout.queue_manage`, not `playout.control` — see
the "Capability vocabulary" section's correction note.

`authz.0007` repairs 2.5C's omission of `playout.queue_manage` from
Station Administrator and grants that Role both new 2.5D capabilities,
`monitoring.reset_listener_counters` and `aircheck.control`. Remote Host
receives neither new administrative capability. The resulting Station
Administrator seed contains the complete capability vocabulary.

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

**Roadmap 2.5C**: `ScheduleAccessConfigAdmin` gained the
`scheduled_enforcement_enabled` field plus `ScheduleAccessConfigForm`'s
activation-safety validation (see "Safe activation" above) and a
`save_model` audit hook that records an `authz` `SystemEvent` whenever
the field actually changes value.

**Roadmap 2.5D**: activation validation now requires at least one active
assignment row for every affected active non-staff/non-superuser account;
inactive-only rows block activation, while an active future specific-date
or recurring assignment is sufficient.
