from .middleware import GROUP_ACCESS_MAP
from .models import NavMenuItem, UITheme


def ui_theme(request):
    return {"ui_theme": UITheme.load()}


def nav_menu(request):
    """Site-wide nav, admin-editable via NavMenuItem (Config > Nav Menu).
    Computes is_active here (not in the template) since it depends on the
    live request -- resolver_match.view_name is compared against each
    item's own url_name plus its extra_active_view_names, so a section's
    sub-pages (e.g. library:library-import) still highlight their parent
    nav item exactly like the old per-template
    {% block nav_library %}class="active"{% endblock %} overrides did.

    Group-based filtering: for a non-staff/non-superuser user, we filter
    the visible nav items to only those whose resolved_url falls inside
    the union of allowed prefixes across their recognized groups
    (GROUP_ACCESS_MAP in middleware.py). This mirrors the middleware's
    access check so a user never sees a menu item that would bounce
    them back on click. Staff/superuser see the whole admin-configured
    menu unfiltered."""
    current_view = request.resolver_match.view_name if request.resolver_match else None
    user = getattr(request, "user", None)

    def is_active(item):
        if not current_view:
            return False
        if item.url_name == current_view:
            return True
        extra = [n.strip() for n in item.extra_active_view_names.split(",") if n.strip()]
        return current_view in extra

    items = list(
        NavMenuItem.objects.filter(parent__isnull=True, enabled=True)
        .prefetch_related("children")
        .order_by("sort_order")
    )

    # Filter items by the user's group-access union, unless they're
    # staff/superuser (who see everything). Anonymous users (nav is
    # rendered on the login page too via base.html) also see nothing --
    # LoginRequiredMiddleware bounces them away before they can click
    # anything anyway.
    if user and user.is_authenticated and not user.is_staff and not user.is_superuser:
        recognized = set(user.groups.values_list("name", flat=True)) & set(GROUP_ACCESS_MAP.keys())
        if recognized:
            allowed_prefixes = tuple(
                prefix
                for group_name in recognized
                for prefix in GROUP_ACCESS_MAP[group_name]["prefixes"]
            )
            items = [i for i in items if i.resolved_url and i.resolved_url.startswith(allowed_prefixes)]
        else:
            # No recognized groups -- they'll be redirected to /welcome/
            # anyway, so hide the entire nav.
            items = []

    for item in items:
        item.children_enabled = [c for c in item.children.all() if c.enabled]
        item.children_enabled.sort(key=lambda c: c.sort_order)
        child_active = any(is_active(c) for c in item.children_enabled)
        item.is_active = is_active(item) or child_active
        for c in item.children_enabled:
            c.is_active = is_active(c)

    return {"nav_menu_items": items}
