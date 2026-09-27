# Authorization — capability architecture (Roadmap 2.5)

This document covers what exists **today**, after Phase 2.5A (Authorization
Foundation). See `PROJECT_NOTES.md`'s "Roadmap 2.5" section for the full
audit history, the operator's binding corrections to the original design,
and the phase-by-phase continuation record. This document is the durable,
git-tracked contract; `PROJECT_NOTES.md` is the working scratchpad.

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
  +-- TalentAssignment(s)  [roadmap 2.5B, not yet implemented]
```

- **`Capability`** (`authz.models.Capability`) — a fixed, code-referenced
  vocabulary (see below). `requires_schedule=True` marks a capability
  that roadmap 2.5B will additionally gate on an active scheduled talent
  assignment; **2.5A does not enforce that field anywhere yet.**
- **`Role`** (`authz.models.Role`) — an admin-named bundle of Capabilities,
  via the `RoleCapability` through table.
- **`GroupRole`** (`authz.models.GroupRole`) — binds exactly one `Role` to
  one `auth.Group` (`OneToOneField` on `group`). A Group with no
  `GroupRole` row grants no capabilities at all, independent of whatever
  `GroupAccess` grants it.
- There is no direct User↔Role relationship. A user's effective
  capability set is always the union, across every Group they belong to,
  of that Group's bound Role's capabilities.

## The evaluator

`authz.evaluator.authorize(user, capability_slug, *, resource=None,
context=None) -> AuthzResult` is the one authoritative place every view
(and, in 2.5C, the Remote DJ signaling server) asks "may this user do
this." `AuthzResult` is `(allowed: bool, code: str, reason: str)` and is
truthy/falsy directly, so `if not authorize(...):` works as a one-line
guard. Stable `code` values today: `allowed`, `unauthenticated`,
`disabled_account`, `capability_missing`. Roadmap 2.5B will add
`no_assignment` / `outside_schedule_window` / `inactive_assignment`
without changing this shape or any existing caller.

`authorize()` takes a plain Django user object, never an `HttpRequest` —
required because roadmap 2.5C calls it from the Remote DJ signaling
server, which runs on its own thread/asyncio loop entirely outside any
Django request/response cycle (see `library/services/remote_dj_signaling.py`).

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
mechanism.

## Capability vocabulary (Phase 2.5A)

| slug | requires_schedule | Enforced at a call site in 2.5A? |
|---|---|---|
| `library.view` | no | no (existing checks unchanged) |
| `library.upload` | no | no (existing checks unchanged) |
| `library.manage_tracks` | no | **yes** — see below |
| `library.manage_categories` | no | **yes** — see below |
| `schedule.edit` | no | no (established, not wired up) |
| `rotations_playlists.edit` | no | no (established, not wired up) |
| `voicetrack.record` | no | no (existing `_can_edit_voicetracks` unchanged) |
| `remote_dj.connect` | yes | no — 2.5B/C |
| `playout.control` | yes | no — 2.5B/C |
| `playout.manual_mode` | yes | no — 2.5B/C |
| `remote_dj.mic_gate` | yes | no — 2.5B/C |
| `studio.mic_ptt` | yes | no — 2.5B/C |
| `fx.fire` | yes | no — 2.5B/C |
| `fx.manage_carts` | no | no (existing staff-only check unchanged) |
| `reports.view` | no | no (existing staff-only check unchanged) |
| `webrequests.administer` | no | no (existing staff-only check unchanged) |
| `monitoring.restart_service` | no | **yes** — see below |
| `system.administer` | no | no (reserved; nothing is gated on it yet) |

Capabilities marked `requires_schedule=True` are seeded as **rows only**
in 2.5A. Wiring them into their real call sites (Remote DJ token mint and
signaling admission, playout control, manual-mode, mic gate, PTT, FX
fire) is explicitly deferred to 2.5B/C: no `TalentAssignment`/schedule-
window mechanism exists yet to authoritatively re-grant them, so
migrating their enforcement now would leave those endpoints *more* open
mid-migration, not less — they keep their existing `GroupAccess`-only
gating untouched until 2.5B lands.

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
