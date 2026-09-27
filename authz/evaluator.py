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
is_superuser as an OUTPUT of a capability grant.
"""
import dataclasses

from django.db.models.signals import post_delete, post_save

from authz.models import Capability, GroupRole


# ---------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------

# Stable result codes. New codes may be ADDED (2.5B plans to add
# "no_assignment", "outside_schedule_window", "inactive_assignment") but
# existing ones must never change meaning -- callers may branch on `code`.
CODE_ALLOWED = "allowed"
CODE_UNAUTHENTICATED = "unauthenticated"
CODE_DISABLED_ACCOUNT = "disabled_account"
CODE_CAPABILITY_MISSING = "capability_missing"


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
# The evaluator
# ---------------------------------------------------------------

def authorize(user, capability_slug, *, resource=None, context=None):
    """Return an AuthzResult for whether `user` may exercise
    `capability_slug`.

    `resource` and `context` are accepted now (part of the stable API
    surface roadmap 2.5B/C will extend) but 2.5A does not yet use either
    for anything -- no capability shipped in 2.5A is resource-scoped.
    They exist so call sites can start passing them (e.g. resource=track)
    without a second migration of every call site later.

    Never raises for an ordinary authorization outcome. DOES raise
    ValueError for a capability_slug that isn't a real, seeded Capability
    -- that is a programming error (a typo'd slug), not a runtime
    authorization outcome, and should fail loudly in tests/dev rather
    than silently denying in production.
    """
    if not _capability_exists(capability_slug):
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
    # generally won't yet) be a member of a Group bound to a Role.
    if user.is_superuser:
        return _allowed("Authorized: superuser break-glass authority.")
    if user.is_staff:
        return _allowed("Authorized: staff compatibility bypass (see PROJECT_NOTES.md).")

    capability_map = get_group_capability_map()
    user_group_names = set(user.groups.values_list("name", flat=True))
    granted = frozenset().union(
        *(capability_map.get(name, frozenset()) for name in user_group_names)
    ) if user_group_names else frozenset()

    if capability_slug in granted:
        return _allowed(f"Authorized via Role-granted capability {capability_slug!r}.")

    return _denied(
        CODE_CAPABILITY_MISSING,
        f"Account lacks the {capability_slug!r} capability.",
    )


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
