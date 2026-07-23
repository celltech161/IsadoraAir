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


def _is_playlist_play_now(path):
    """/api/playlists/<id>/play-now/ -- a specific playlist action a
    remote_dj is allowed to trigger. Not modeled as an exact-path
    entry since <id> varies. Deliberately narrow: only this one
    action, not the rest of /api/playlists/ (no create/edit/reorder/
    delete for this role)."""
    parts = path.strip("/").split("/")
    return (
        len(parts) == 4
        and parts[0] == "api" and parts[1] == "playlists"
        and parts[2].isdigit() and parts[3] == "play-now"
    )


# Per-group access map. A user's total allowed set is the UNION of the
# entries for every recognized group they belong to -- so a user in
# both `remote_dj` and (future) `Contributor` gets the sum of both
# groups' privileges, matching Django's additive-permissions paradigm.
# Staff and superuser bypass this map entirely; they see everything.
#
# Each entry:
#   "prefixes" -- tuple of path prefixes; any path startswith one of
#                 these is allowed
#   "exact"    -- frozenset of exact paths that must match literally
#   "extras"   -- tuple of callables (path -> bool) for parameterized
#                 patterns that can't be modeled as exact or prefix
#
# Adding a new group here is the sole edit needed to grant it access
# to a curated URL set. The nav_menu context processor consults this
# same map to filter the top-level menu, so a new group's menu items
# appear automatically once its prefixes are declared.
GROUP_ACCESS_MAP = {
    "remote_dj": {
        "prefixes": (
            "/remote-dj/",
            "/monitoring/",
            "/api/remote-dj/",
            "/api/engine/status/",
            "/api/engine/manual-mode/",
            "/api/engine/remote-dj-gate/",
            "/api/engine/levels/",  # 100ms VU-meter poll on /remote-dj/'s dashboard render
            "/api/waveform/",
            "/api/albumart/",
        ),
        "exact": frozenset({
            "/api/tracks/",
            "/api/engine/queue/insert/",
            # The Monitoring dashboard's embedded RBDS widget polls
            # this one rbds API route directly (see monitoring/
            # templates/monitoring/dashboard.html), even though the
            # rest of /rbds/ is off-limits.
            "/rbds/api/status/",
        }),
        "extras": (_is_playlist_play_now,),
    },
    "Contributor": {
        # Contributors get read-only access to the WHOLE library
        # (including their own not-yet-approved uploads) plus the
        # upload flow scoped to their own username-matched category
        # (that scoping is enforced VIEW-side; the middleware just
        # opens the URL). The specific write endpoints they need are
        # /api/library/upload/ (POST -- creates their track) and the
        # per-track detail endpoint (view-side gates PUT/DELETE to
        # their own not-yet-approved rows). Read endpoints for the
        # browser player, waveform display, album art, and category
        # filter are all in.
        "prefixes": (
            "/library/",
            "/track/",
            "/api/tracks/",
            "/api/library/upload/",
            "/api/categories/",
            "/api/waveform/",
            "/api/albumart/",
        ),
        "exact": frozenset(),
        "extras": (),
    },
}


class GroupBasedAccessMiddleware:
    """Restricts non-staff/non-superuser authenticated users to the
    union of their recognized groups' allowed paths in
    GROUP_ACCESS_MAP.

    Rules:
      - Staff and superuser bypass all group checks entirely -- the
        studio-operator role sees everything.
      - Anonymous users are handled upstream by LoginRequiredMiddleware,
        so this middleware only ever sees authenticated requests.
      - Paths in _ALWAYS_ALLOWED_PREFIXES are open to any authenticated
        user (auth flow, static, welcome, logout).
      - An authenticated user with NO recognized groups is redirected
        to /welcome/ for page-shaped requests, or 403'd for API/ws
        requests. This closes the historical gap where any logged-in
        user had access to everything by default.
      - An authenticated user with one or more recognized groups is
        allowed to reach any path in the union of their groups'
        access sets. Other paths are 403'd (API/ws) or redirected
        to a group-appropriate landing page (page-shaped).

    Runs after AuthenticationMiddleware (needs request.user) and after
    LoginRequiredMiddleware. Kept as an allowlist rather than a
    denylist: a new page/app added later is blocked for restricted
    roles by default until someone deliberately adds it to
    GROUP_ACCESS_MAP.
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

        user_group_names = set(user.groups.values_list("name", flat=True))
        recognized = user_group_names & set(GROUP_ACCESS_MAP.keys())

        if not recognized:
            # No recognized groups -- send them to /welcome/. API- or
            # websocket-shaped requests get 403 instead of a redirect
            # (a JS caller would rather see 403 than a 302 to HTML).
            if path.startswith("/api/") or path.startswith("/ws/"):
                return HttpResponseForbidden(
                    "You have no assigned privileges. Contact the station."
                )
            return redirect("library:welcome")

        for group_name in recognized:
            access = GROUP_ACCESS_MAP[group_name]
            if path in access["exact"]:
                return self.get_response(request)
            if any(path.startswith(p) for p in access["prefixes"]):
                return self.get_response(request)
            if any(check(path) for check in access.get("extras", ())):
                return self.get_response(request)

        # Path not in any of their groups' allowed sets.
        if path.startswith("/api/") or path.startswith("/ws/"):
            return HttpResponseForbidden("Not available for this account.")

        # Page-shaped -- redirect to a landing page appropriate for
        # whichever group they belong to. Future groups can add their
        # own landing here; unmatched fall to /welcome/.
        if "Contributor" in recognized:
            return redirect("library:library")
        if "remote_dj" in recognized:
            return redirect("library:remote-dj")
        return redirect("library:welcome")


# Public helper for view code that needs to know whether the current
# user is limited to Contributor privileges (i.e., is a Contributor
# and NOT also staff/superuser). Kept next to GROUP_ACCESS_MAP so a
# view importing it doesn't need to duplicate the "not staff, not
# superuser, is in group X" boilerplate.
def user_is_contributor(user):
    return bool(
        user and user.is_authenticated
        and not user.is_staff and not user.is_superuser
        and user.groups.filter(name="Contributor").exists()
    )


# Backward-compat alias. settings.py's MIDDLEWARE list still references
# the old name; getting updated in the same commit so this alias is
# effectively vestigial the moment the config change lands, but kept
# here for a release cycle in case any out-of-tree deploy pins to the
# old name.
RemoteDJRestrictMiddleware = GroupBasedAccessMiddleware
