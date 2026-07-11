from django.http import HttpResponseForbidden
from django.shortcuts import redirect


# Exact paths and path prefixes a remote_dj-group, non-staff user may
# reach. Everything else 403s (API-shaped paths) or redirects to
# /remote-dj/ (page-shaped paths). Kept as a real allowlist rather than
# a denylist -- a new page/app added later is blocked by default for
# this role until someone deliberately opens it up here, not silently
# exposed.
_ALLOWED_PREFIXES = (
    "/remote-dj/",
    "/monitoring/",
    "/login/",
    "/logout/",
    "/password-reset/",
    "/static/",
    "/media/",
    "/api/remote-dj/",
    "/api/engine/status/",
    "/api/engine/manual-mode/",
    "/api/engine/remote-dj-gate/",
    "/api/waveform/",
    "/api/albumart/",
)
# Exact-only, not prefixes: /remote-dj/'s search-to-add and Play Now
# controls hit these. Deliberately NOT a prefix match -- "/api/tracks/"
# as a prefix would also open up /api/tracks/<pk>/write-metadata/,
# /reanalyze/, /blocked-slots/toggle*/ etc., which are real library-
# editing endpoints, not search. This role gets to search for and
# queue a track or start a playlist, nothing more.
_ALLOWED_EXACT = {
    "/api/tracks/",
    "/api/engine/queue/insert/",
    # The Monitoring dashboard's embedded RBDS widget polls this one
    # rbds API route directly (see monitoring/templates/monitoring/
    # dashboard.html), even though the rest of /rbds/ is off-limits.
    "/rbds/api/status/",
}


def _is_playlist_play_now(path):
    # /api/playlists/<id>/play-now/ -- can't be an exact-path entry
    # since <id> varies. Still deliberately narrow: only this one
    # playlist action, not the rest of /api/playlists/ (no create/
    # edit/reorder/delete for this role).
    parts = path.strip("/").split("/")
    return (
        len(parts) == 4
        and parts[0] == "api" and parts[1] == "playlists"
        and parts[2].isdigit() and parts[3] == "play-now"
    )


class RemoteDJRestrictMiddleware:
    """Confines remote_dj-group members who aren't also staff/superusers
    to the pages they actually need: their own console (/remote-dj/),
    the read-only Monitoring dashboard, and auth. Staff/superuser
    accounts (the studio operator role) are never affected, regardless
    of group membership -- this only tightens the NON-staff remote-DJ
    role, it doesn't loosen anything for anyone else.

    Runs after AuthenticationMiddleware (needs request.user) and after
    LoginRequiredMiddleware (anonymous users are already bounced to
    /login/ by then, so this only ever sees authenticated requests)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not user.is_staff
            and not user.is_superuser
            and user.groups.filter(name="remote_dj").exists()
        ):
            path = request.path
            allowed = (
                path in _ALLOWED_EXACT
                or any(path.startswith(p) for p in _ALLOWED_PREFIXES)
                or _is_playlist_play_now(path)
            )
            if not allowed:
                if path.startswith("/api/") or path.startswith("/ws/"):
                    return HttpResponseForbidden("Not available for this account.")
                return redirect("library:remote-dj")
        return self.get_response(request)
