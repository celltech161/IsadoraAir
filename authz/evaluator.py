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

def authorize(user, capability_slug, *, resource=None, context=None, now=None):
    """Return an AuthzResult for whether `user` may exercise
    `capability_slug`.

    `resource` and `context` are accepted now (part of the stable API
    surface roadmap 2.5C will extend) but nothing shipped through 2.5B
    uses either for anything -- no capability is resource-scoped yet.
    They exist so call sites can start passing them (e.g. resource=track)
    without a second migration of every call site later.

    `now`, when given, must be an aware datetime and is used as "the
    current instant" for schedule evaluation instead of the real wall
    clock -- for deterministic tests and any future caller that already
    has an authoritative instant in hand. Irrelevant for a capability
    that doesn't require_schedule.

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

    capability_map = get_group_capability_map()
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

    # Role/Group grants the capability, but it's schedule-restricted:
    # possession alone is not enough -- a TalentAssignment must also be
    # currently in its effective window. A missing/expired/inactive
    # assignment can never be compensated for by capability possession,
    # and capability possession can never be skipped just because an
    # assignment exists (checked above, in that order, on purpose).
    return _check_schedule(user, capability_slug, now=now)


def forbidden_response(result):
    """Convenience for API views: turn a denied AuthzResult into the
    right status code (401 for not-even-authenticated, 403 for every
    other denial) with a JSON body. `result.allowed` must be False --
    callers check that themselves first, same as every other view-side
    permission helper in this codebase (e.g. api_track_detail's own
    HttpResponseForbidden pattern)."""
    from django.http import JsonResponse

    status = 401 if result.code == CODE_UNAUTHENTICATED else 403
    return JsonResponse({"error": result.reason}, status=status)
