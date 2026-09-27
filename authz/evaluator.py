"""Roadmap 2.5A -- the central authorization evaluator.

`authorize(user, capability_slug, ...)` is the ONE authoritative place
that answers "may this user do this operation." Every view (and, in
2.5C, the Remote DJ signaling server, which runs outside any Django
request/response cycle entirely -- see library/services/remote_dj_signaling.py)
calls this instead of hand-rolling a group/staff check.

Deliberately request-agnostic: takes a Django User object, not an
HttpRequest, and does not read or write anything thread-local or
request-scoped (no django.utils.timezone.get_current_timezone()
dependency, no request.session access). This is required by roadmap 2.5C,
which needs to call authorize() from the engine's GLib thread / the
signaling server's asyncio loop -- neither of which is ever inside a
Django request.

Staff/superuser compatibility (see PROJECT_NOTES.md's "Roadmap 2.5"
section for the full reasoning): both `is_superuser` and `is_staff`
unconditionally satisfy every operational capability, mirroring
library.middleware.GroupBasedAccessMiddleware's existing "staff/superuser
bypass all group checks" convention for GroupAccess. This is NOT the
same thing as a capability granting staff/superuser authority -- the
implication is strictly one-way (staff/superuser -> capability), never
the reverse. Nothing in this module ever sets or checks is_staff/
is_superuser as an OUTPUT of a capability grant. This bypass applies to
schedule-restricted capabilities too (roadmap 2.5B) -- staff/superuser
are never required to hold a TalentAssignment.

Roadmap 2.5B -- scheduled authorization: a Capability with
`requires_schedule=True` additionally requires an active TalentAssignment
whose EFFECTIVE window covers the current moment in STATION-local time
(never browser-local, never naive, never the server process's own local
zone -- see _station_local_now). The effective window is a half-open
interval:

    [effective_start, effective_end)
    effective_start = scheduled_start - pre_schedule_allowance
    effective_end   = scheduled_end   + post_schedule_allowance

so `effective_start` itself is authorized and `effective_end` itself is
NOT (prevents two back-to-back assignments from both claiming the exact
boundary instant). pre/post allowances come from the single station-wide
ScheduleAccessConfig singleton (authz/models.py) -- never hard-coded here.

Cross-midnight and allowance-crosses-midnight handling: rather than
special-casing "is this assignment cross-midnight" separately from "does
an allowance push the window across a calendar-day boundary," every
assignment is checked against THREE candidate calendar-day anchors
(yesterday/today/tomorrow relative to station-local "now") and its
window is computed fresh for whichever anchor(s) it recurs/applies on
(see _assignment_covers). This one mechanism handles all of: a plain
same-day assignment, a recurring OR specific-date assignment that itself
crosses midnight, a pre-allowance pulling a just-after-midnight
assignment's window back into the previous calendar day, and a
post-allowance pushing a late assignment's window into the next day --
without four separate code paths.

DST: local wall-clock times are attached to the station zoneinfo via
plain `datetime.combine(...).replace(tzinfo=...)` and compared as aware
datetimes -- standard zoneinfo/Django behavior, using Python's default
(`fold=0`) interpretation for the rare case where a computed instant
falls in a DST-transition's ambiguous or nonexistent hour. No separate
DST-disambiguation policy is implemented; a station whose scheduled
handoff moment lands exactly inside a DST transition is a known,
accepted edge case, not specially handled.

Roadmap 2.5C -- safe activation (`schedule_policy`): 2.5B's schedule
check must not become authoritative at a real production endpoint the
moment 2.5C's code ships, because every existing Remote Host has zero
TalentAssignment rows (the concept didn't exist before 2.5B) -- see
docs/AUTHORIZATION.md's "Safe activation" section. `ScheduleAccessConfig.
scheduled_enforcement_enabled` (default **False** on every existing and
fresh install) is the station-wide switch an operator flips once
TalentAssignments are actually configured. `authorize()`'s
`schedule_policy` kwarg controls how a call site relates to that switch:

  - `SCHEDULE_POLICY_ENFORCE` (default) -- respect the station switch.
    OFF: a requires_schedule=True capability behaves like an ordinary
    one (compatibility bypass -- capability possession is still
    mandatory, the assignment/window requirement is skipped). ON: full
    schedule evaluation. This is what every real production call site
    uses.
  - `SCHEDULE_POLICY_STRICT` -- always fully evaluate the schedule
    window regardless of the station switch. Used by tests that assert
    the scheduling MECHANISM itself is correct independent of whether
    any particular station has activated it yet, and by the activation-
    safety validation this module also provides.
  - `SCHEDULE_POLICY_IGNORE` -- never evaluate the schedule window;
    capability possession alone is sufficient. Used for page-
    reachability-style checks (e.g. whether to render the Remote DJ
    console at all) where the UX goal is "let a Remote Host see their
    console/status even outside their window" -- the actual privileged
    OPERATION (minting a connect token, firing a control command) is
    where the real schedule_policy="enforce" check bites. See
    library.views.remote_dj_page.

This is NOT the same knob as authorize()'s existing staff/superuser
bypass or ordinary capability check -- `scheduled_enforcement_enabled`
being OFF never means "authorization is off." Capability possession,
`is_active`, and `is_authenticated` are checked unconditionally
regardless of schedule_policy or the station switch.

`bypass_cache=True` skips the process-local Group->capability cache and
reads fresh from the database. Real Django request handlers never need
this (the cache is invalidated correctly within that process via
Django signals). It exists for `library.services.engine.py`'s Remote DJ
periodic re-authorization tick and its signaling-admission check, which
run inside the SEPARATE `manage.py run_engine` process -- a Role/
GroupRole edit made through the web (gunicorn) process's admin fires
Django signals only in THAT process, so the engine process's own cached
copy would otherwise never see the change short of an engine restart.
Since these are rare, low-frequency calls (at most one active Remote DJ
session at a time), the cache's performance benefit is irrelevant there
and correctness/freshness matters far more.
"""
import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

from django.db.models.signals import post_delete, post_save
from django.utils import timezone as dj_timezone

from authz.models import Capability, GroupRole, ScheduleAccessConfig, TalentAssignment


# ---------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------

# Stable result codes. New codes may be ADDED (2.5C may add more) but
# existing ones must never change meaning -- callers may branch on `code`.
CODE_ALLOWED = "allowed"
CODE_UNAUTHENTICATED = "unauthenticated"
CODE_DISABLED_ACCOUNT = "disabled_account"
CODE_CAPABILITY_MISSING = "capability_missing"
# Roadmap 2.5B additions -- only ever returned for a
# requires_schedule=True capability the user DOES otherwise possess
# (capability possession is always checked first and independently).
CODE_NO_ASSIGNMENT = "no_assignment"
CODE_OUTSIDE_SCHEDULE_WINDOW = "outside_schedule_window"
CODE_INACTIVE_ASSIGNMENT = "inactive_assignment"

# Roadmap 2.5C schedule_policy values -- see module docstring's "safe
# activation" section for exactly what each one means.
SCHEDULE_POLICY_ENFORCE = "enforce"
SCHEDULE_POLICY_STRICT = "strict"
SCHEDULE_POLICY_IGNORE = "ignore"


@dataclasses.dataclass(frozen=True)
class AuthzResult:
    allowed: bool
    code: str
    reason: str

    def __bool__(self):
        # Lets call sites write `if not authorize(...):` as a shorthand
        # for the common case, while still keeping `.code`/`.reason`
        # available for anything that needs the detail (logging,
        # audit events in 2.5C).
        return self.allowed


def _allowed(reason):
    return AuthzResult(True, CODE_ALLOWED, reason)


def _denied(code, reason):
    return AuthzResult(False, code, reason)


# ---------------------------------------------------------------
# Capability-set cache: group_name -> frozenset of capability slugs,
# via GroupRole -> Role -> RoleCapability -> Capability. Same shape and
# invalidation pattern as library.middleware.get_group_access_map --
# kept as a plain process-local dict rather than the Django cache
# framework so a Role/GroupRole edit lands in a gunicorn worker on its
# very next request with no memcached/redis round-trip.
# ---------------------------------------------------------------

_GROUP_CAPABILITY_CACHE = None


def _load_group_capability_map():
    result = {}
    for gr in GroupRole.objects.select_related("group").prefetch_related(
        "role__role_capabilities__capability"
    ):
        result[gr.group.name] = frozenset(
            rc.capability.slug for rc in gr.role.role_capabilities.all()
        )
    return result


def get_group_capability_map():
    global _GROUP_CAPABILITY_CACHE
    if _GROUP_CAPABILITY_CACHE is None:
        _GROUP_CAPABILITY_CACHE = _load_group_capability_map()
    return _GROUP_CAPABILITY_CACHE


def _invalidate_group_capability_cache(*_args, **_kwargs):
    global _GROUP_CAPABILITY_CACHE
    _GROUP_CAPABILITY_CACHE = None


def _wire_signals():
    """Mirrors library.middleware._wire_signals / library/apps.py's
    ready()-time wiring exactly -- see authz/apps.py. Note there is
    deliberately no signal on User<->Group membership itself: that
    membership isn't cached anywhere in this module (authorize() reads
    it fresh via user.groups.values_list(...) on every call) -- only the
    group_name -> capability-slugs MAP is cached, and only Group/Role/
    RoleCapability/Capability/GroupRole changes affect that map."""
    from django.contrib.auth.models import Group
    from authz.models import Role, RoleCapability

    for sender in (GroupRole, Role, RoleCapability, Capability, Group):
        post_save.connect(_invalidate_group_capability_cache, sender=sender, weak=False)
        post_delete.connect(_invalidate_group_capability_cache, sender=sender, weak=False)


def _capability_exists(slug):
    return Capability.objects.filter(slug=slug).exists()


# ---------------------------------------------------------------
# Roadmap 2.5B -- scheduled-window evaluation
# ---------------------------------------------------------------

def _station_local_now(now=None):
    """Station-local aware "now", per the station-time authority
    (library.middleware.get_station_timezone / library.models.
    StationTimeConfig) -- NOT django.utils.timezone.get_current_timezone(),
    which depends on a per-request timezone.activate() call that never
    happens outside a Django request (see this module's docstring on why
    that matters for 2.5C). `now`, when given, must already be an aware
    datetime (any timezone) and is converted to station-local; used by
    tests and by any future caller that already has an authoritative
    instant to evaluate against instead of the real wall clock."""
    from library.middleware import get_station_timezone

    instant = now if now is not None else dj_timezone.now()
    return instant.astimezone(ZoneInfo(get_station_timezone()))


def _assignment_covers(assignment, station_now, pre_minutes, post_minutes):
    """True if `assignment`'s effective window -- computed fresh against
    each of the three calendar-day anchors adjacent to station_now's own
    date (yesterday/today/tomorrow) -- covers station_now. See this
    module's docstring for why three anchors, not one, are needed."""
    tz = station_now.tzinfo
    for delta_days in (-1, 0, 1):
        reference_date = station_now.date() + dt.timedelta(days=delta_days)

        if assignment.specific_date is not None:
            if assignment.specific_date != reference_date:
                continue
        elif assignment.day_of_week != reference_date.weekday():
            continue

        start_dt = dt.datetime.combine(reference_date, assignment.start_time, tzinfo=tz)
        end_anchor_date = reference_date + dt.timedelta(days=1) if assignment.crosses_midnight else reference_date
        end_dt = dt.datetime.combine(end_anchor_date, assignment.end_time, tzinfo=tz)

        effective_start = start_dt - dt.timedelta(minutes=pre_minutes)
        effective_end = end_dt + dt.timedelta(minutes=post_minutes)

        if effective_start <= station_now < effective_end:
            return True
    return False


def _check_schedule(user, capability_slug, *, now=None):
    """Assumes the caller already confirmed `user` holds `capability_slug`
    through the ordinary Role/Group path -- this only answers whether a
    TalentAssignment currently authorizes EXERCISING it."""
    station_now = _station_local_now(now)
    cfg = ScheduleAccessConfig.load()

    assignments = list(TalentAssignment.objects.filter(user=user))
    if not assignments:
        return _denied(
            CODE_NO_ASSIGNMENT,
            f"No talent assignment exists for this account; {capability_slug!r} requires one.",
        )

    pre, post = cfg.pre_schedule_allowance_minutes, cfg.post_schedule_allowance_minutes

    if any(a.active and _assignment_covers(a, station_now, pre, post) for a in assignments):
        return _allowed(
            f"Authorized via Role-granted capability {capability_slug!r}, "
            "confirmed by an active talent assignment covering the current time."
        )

    if any((not a.active) and _assignment_covers(a, station_now, pre, post) for a in assignments):
        return _denied(
            CODE_INACTIVE_ASSIGNMENT,
            f"A talent assignment covers the current time but is marked "
            f"inactive; {capability_slug!r} requires an active one.",
        )

    return _denied(
        CODE_OUTSIDE_SCHEDULE_WINDOW,
        f"No talent assignment is currently in its effective window; "
        f"{capability_slug!r} requires one.",
    )


# ---------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------

def authorize(
    user, capability_slug, *, resource=None, context=None, now=None,
    schedule_policy=SCHEDULE_POLICY_ENFORCE, bypass_cache=False,
):
    """Return an AuthzResult for whether `user` may exercise
    `capability_slug`.

    `resource` and `context` are accepted now (part of the stable API
    surface roadmap 2.5C will extend) but nothing shipped yet uses
    either for anything -- no capability is resource-scoped yet. They
    exist so call sites can start passing them (e.g. resource=track)
    without a second migration of every call site later.

    `now`, when given, must be an aware datetime and is used as "the
    current instant" for schedule evaluation instead of the real wall
    clock. `schedule_policy` and `bypass_cache` are documented in this
    module's docstring ("Roadmap 2.5C -- safe activation"). Both are
    irrelevant for a capability that doesn't requires_schedule.

    Never raises for an ordinary authorization outcome. DOES raise
    ValueError for a capability_slug that isn't a real, seeded Capability
    -- that is a programming error (a typo'd slug), not a runtime
    authorization outcome, and should fail loudly in tests/dev rather
    than silently denying in production.
    """
    capability = Capability.objects.filter(slug=capability_slug).first()
    if capability is None:
        raise ValueError(f"authorize() called with unknown capability slug {capability_slug!r}")

    if user is None or not getattr(user, "is_authenticated", False):
        return _denied(CODE_UNAUTHENTICATED, "Not authenticated.")

    if not user.is_active:
        return _denied(CODE_DISABLED_ACCOUNT, "This account is disabled.")

    # Staff/superuser compatibility (see module docstring + PROJECT_NOTES.md
    # "Roadmap 2.5" section): unconditional operational authority, mirroring
    # GroupBasedAccessMiddleware's existing bypass. This is intentionally
    # checked BEFORE looking at the user's Role-derived capability set --
    # a staff/superuser account need not (and, per the transition plan,
    # generally won't yet) be a member of a Group bound to a Role, and
    # (2.5B) is never required to hold a TalentAssignment either.
    if user.is_superuser:
        return _allowed("Authorized: superuser break-glass authority.")
    if user.is_staff:
        return _allowed("Authorized: staff compatibility bypass (see PROJECT_NOTES.md).")

    capability_map = _load_group_capability_map() if bypass_cache else get_group_capability_map()
    user_group_names = set(user.groups.values_list("name", flat=True))
    granted = frozenset().union(
        *(capability_map.get(name, frozenset()) for name in user_group_names)
    ) if user_group_names else frozenset()

    if capability_slug not in granted:
        return _denied(
            CODE_CAPABILITY_MISSING,
            f"Account lacks the {capability_slug!r} capability.",
        )

    if not capability.requires_schedule:
        return _allowed(f"Authorized via Role-granted capability {capability_slug!r}.")

    if schedule_policy == SCHEDULE_POLICY_IGNORE:
        return _allowed(
            f"Authorized via Role-granted capability {capability_slug!r} "
            "(schedule check explicitly skipped by the caller)."
        )

    if schedule_policy == SCHEDULE_POLICY_ENFORCE and not ScheduleAccessConfig.load().scheduled_enforcement_enabled:
        return _allowed(
            f"Authorized via Role-granted capability {capability_slug!r} "
            "(scheduled enforcement is currently disabled station-wide -- "
            "compatibility policy)."
        )

    # Either schedule_policy=="strict", or "enforce" with the station
    # switch ON: possession alone is not enough -- a TalentAssignment
    # must also be currently in its effective window. A missing/expired/
    # inactive assignment can never be compensated for by capability
    # possession, and capability possession can never be skipped just
    # because an assignment exists (checked above, in that order, on
    # purpose).
    return _check_schedule(user, capability_slug, now=now)


def forbidden_response(result, *, user=None, capability_slug=None):
    """Convenience for API views: turn a denied AuthzResult into the
    right status code (401 for not-even-authenticated, 403 for every
    other denial) with a JSON body. `result.allowed` must be False --
    callers check that themselves first, same as every other view-side
    permission helper in this codebase (e.g. api_track_detail's own
    HttpResponseForbidden pattern).

    Roadmap 2.5C: when `user` and `capability_slug` are given, also
    records a lightweight audit event (see _emit_denial_event) -- every
    real call site added in 2.5C passes both; 2.5A's original two call
    sites keep working exactly as before if a future edit ever omits
    them (audit is additive, never load-bearing for the response
    itself)."""
    from django.http import JsonResponse

    if capability_slug is not None:
        _emit_denial_event(user, capability_slug, result)

    status = 401 if result.code == CODE_UNAUTHENTICATED else 403
    return JsonResponse({"error": result.reason}, status=status)


def _emit_denial_event(user, capability_slug, result):
    """Roadmap 2.5C audit trail. Reuses monitoring.models.SystemEvent --
    the existing operator-facing event log -- rather than a parallel
    logging subsystem. Coalesces via SystemEvent's own existing 60-second
    dedupe window (keyed on user+capability+code) so a script hammering
    a denied endpoint produces one row with a rising repeat_count, not a
    flood. Never raises (emit_event's own contract) and never includes
    the request body, a token, or any secret -- only identity/capability/
    outcome."""
    from monitoring.models import emit_event

    username = getattr(user, "username", None) or "anonymous"
    emit_event(
        category="authz",
        level="warning",
        title=f"Authorization denied: {capability_slug}",
        detail={"user": username, "capability": capability_slug, "code": result.code},
        dedupe_key=f"authz|denied|{username}|{capability_slug}|{result.code}",
    )


def users_missing_talent_assignments_for_scheduled_capabilities():
    """Roadmap 2.5C activation safety (docs/AUTHORIZATION.md's "Safe
    activation" section). Returns the list of active, non-staff,
    non-superuser Users who hold at least one requires_schedule=True
    capability through their ordinary Group->Role chain but have ZERO
    TalentAssignment rows at all (active or not -- existence, not
    liveness: a future/recurring assignment is enough, per the operator's
    own instruction; this deliberately does NOT evaluate any assignment's
    live window). Used by authz.admin.ScheduleAccessConfigForm to refuse
    turning ScheduleAccessConfig.scheduled_enforcement_enabled ON while
    such an account exists -- the simplest understandable safe rule:
    don't allow enabling enforcement while a Remote Host has no talent
    schedule configured at all."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    scheduled_slugs = frozenset(
        Capability.objects.filter(requires_schedule=True).values_list("slug", flat=True)
    )
    if not scheduled_slugs:
        return []

    capability_map = _load_group_capability_map()
    users_with_any_assignment = frozenset(
        TalentAssignment.objects.values_list("user_id", flat=True)
    )

    missing = []
    candidates = User.objects.filter(is_active=True, is_staff=False, is_superuser=False).prefetch_related("groups")
    for user in candidates:
        user_group_names = {g.name for g in user.groups.all()}
        granted = frozenset().union(
            *(capability_map.get(name, frozenset()) for name in user_group_names)
        ) if user_group_names else frozenset()
        if granted & scheduled_slugs and user.id not in users_with_any_assignment:
            missing.append(user)
    return missing
