import re

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.http import HttpResponseForbidden
from django.shortcuts import redirect


# Paths always allowed for any authenticated user, regardless of group
# membership. Auth flow, static assets, the welcome page itself (so the
# redirect target for privilege-less users is reachable), and logout
# (so a stuck-on-welcome user can still sign out).
_ALWAYS_ALLOWED_PREFIXES = (
    "/login/",
    "/logout/",
    "/password-reset/",
    "/static/",
    "/media/",
    "/welcome/",
)


# Module-level cache of the compiled per-group access map, populated
# lazily from the GroupAccess model on first request after startup or
# after any GroupAccess change. Cleared by the post_save/post_delete
# signal receivers below so the very next request sees the update.
# Kept as a plain dict (single-process reload semantics) rather than
# the Django cache framework so a config change lands in this worker
# with no round-trip to memcached/redis; each gunicorn worker refreshes
# independently on next request after signal invalidation.
_GROUP_MAP_CACHE = None


def _load_group_map():
    """Read every GroupAccess row and build a dict of
    group_name -> {prefixes, exact, regex, landing_url, priority}. Called
    lazily; result cached in _GROUP_MAP_CACHE until a GroupAccess
    save/delete invalidates it."""
    from library.models import GroupAccess
    result = {}
    for ga in GroupAccess.objects.select_related("group").all():
        result[ga.group.name] = {
            "prefixes": ga.prefix_list(),
            "exact": ga.exact_set(),
            "regex": ga.regex_list(),
            "landing_url": ga.landing_url,
            "priority": ga.priority,
        }
    return result


def get_group_access_map():
    """Public accessor for the cached group->access map. Used by both
    the middleware and the nav_menu context processor -- keeping the
    lookup in one place means a template's nav filter and the actual
    permission gate can never drift."""
    global _GROUP_MAP_CACHE
    if _GROUP_MAP_CACHE is None:
        _GROUP_MAP_CACHE = _load_group_map()
    return _GROUP_MAP_CACHE


def _invalidate_group_map_cache(*_args, **_kwargs):
    global _GROUP_MAP_CACHE
    _GROUP_MAP_CACHE = None


# Public helper for view code that needs to know whether the current
# user is limited to Contributor privileges. Kept in the module because
# view files import it -- avoid a circular import by not touching the
# GROUP_ACCESS_MAP here, just the group membership.
def user_is_contributor(user):
    return bool(
        user and user.is_authenticated
        and not user.is_staff and not user.is_superuser
        and user.groups.filter(name="Contributor").exists()
    )


class GroupBasedAccessMiddleware:
    """Restricts non-staff/non-superuser authenticated users to the
    union of their recognized groups' allowed paths, as configured in
    the GroupAccess model (admin: Groups -> access inline).

    Rules:
      - Staff and superuser bypass all group checks (they see everything).
      - Anonymous users are handled upstream by LoginRequiredMiddleware.
      - Paths in _ALWAYS_ALLOWED_PREFIXES are open to any authenticated
        user (auth flow, static, welcome, logout).
      - Authenticated + no recognized groups -> redirect to /welcome/
        (or 403 for API/ws). This closes the historical gap where any
        logged-in user had access to everything by default.
      - Authenticated + one or more recognized groups -> allowed to
        reach any path in the union of their groups' access sets.
        Other paths 403 (API/ws) or redirect to whichever group's
        landing_url has the lowest priority number (ties by group
        name alphabetically).

    Runs after AuthenticationMiddleware (needs request.user) and after
    LoginRequiredMiddleware. Kept as an allowlist -- a new page/app
    added later is blocked for restricted roles by default until
    someone deliberately adds it to a group's GroupAccess entry.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return self.get_response(request)

        if user.is_staff or user.is_superuser:
            return self.get_response(request)

        path = request.path

        if any(path.startswith(p) for p in _ALWAYS_ALLOWED_PREFIXES):
            return self.get_response(request)

        access_map = get_group_access_map()
        user_group_names = set(user.groups.values_list("name", flat=True))
        recognized = user_group_names & set(access_map.keys())

        if not recognized:
            if path.startswith("/api/") or path.startswith("/ws/"):
                return HttpResponseForbidden(
                    "You have no assigned privileges. Contact the station."
                )
            return redirect("library:welcome")

        for group_name in recognized:
            access = access_map[group_name]
            if path in access["exact"]:
                return self.get_response(request)
            if any(path.startswith(p) for p in access["prefixes"]):
                return self.get_response(request)
            if any(rx.match(path) for rx in access["regex"]):
                return self.get_response(request)

        if path.startswith("/api/") or path.startswith("/ws/"):
            return HttpResponseForbidden("Not available for this account.")

        # Pick a landing page: lowest-priority-number wins among the
        # user's recognized groups. Ties broken alphabetically by
        # group name (so the choice is deterministic across restarts).
        # Groups without a configured landing_url are skipped; if
        # nobody has one, fall through to /welcome/.
        candidates = sorted(
            (g for g in recognized if access_map[g]["landing_url"]),
            key=lambda g: (access_map[g]["priority"], g),
        )
        if candidates:
            return redirect(access_map[candidates[0]]["landing_url"])
        return redirect("library:welcome")


# Cache invalidation: any change to a GroupAccess row (via admin,
# management command, shell, whatever) flips the module cache back to
# None so the next request rebuilds it. Group name changes also matter
# because our lookup uses the name string.
def _wire_signals():
    from library.models import GroupAccess
    from django.contrib.auth.models import Group
    post_save.connect(_invalidate_group_map_cache, sender=GroupAccess, weak=False)
    post_delete.connect(_invalidate_group_map_cache, sender=GroupAccess, weak=False)
    post_save.connect(_invalidate_group_map_cache, sender=Group, weak=False)
    post_delete.connect(_invalidate_group_map_cache, sender=Group, weak=False)


try:
    _wire_signals()
except Exception:
    # Import ordering: if this module is imported before the app
    # registry is ready (e.g. during initial migrations), the model
    # import inside _wire_signals will fail. The AppConfig.ready() hook
    # in library/apps.py is the safer place; this try/except lets
    # runserver/gunicorn start regardless, and the AppConfig will run
    # _wire_signals again once apps are loaded.
    pass


# Backward-compat alias. Nothing in-tree still imports this, but any
# out-of-tree deploy pinned to the old name keeps working.
RemoteDJRestrictMiddleware = GroupBasedAccessMiddleware
