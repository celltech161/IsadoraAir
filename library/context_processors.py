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
    {% block nav_library %}class="active"{% endblock %} overrides did."""
    current_view = request.resolver_match.view_name if request.resolver_match else None

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
    for item in items:
        item.children_enabled = [c for c in item.children.all() if c.enabled]
        item.children_enabled.sort(key=lambda c: c.sort_order)
        child_active = any(is_active(c) for c in item.children_enabled)
        item.is_active = is_active(item) or child_active
        for c in item.children_enabled:
            c.is_active = is_active(c)

    return {"nav_menu_items": items}
