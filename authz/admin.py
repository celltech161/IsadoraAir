from django import forms
from django.contrib import admin

from authz.models import Capability, GroupRole, Role, RoleCapability


@admin.register(Capability)
class CapabilityAdmin(admin.ModelAdmin):
    """Capability rows are seeded by migration 0002 -- this admin page is
    for INSPECTING the vocabulary (what does 'library.manage_categories'
    actually mean, does it require a schedule) and for a future 2.5B/C
    Capability, not for operators freely inventing new slugs (a slug
    with no call site checking for it does nothing)."""
    list_display = ["slug", "label", "requires_schedule"]
    list_filter = ["requires_schedule"]
    search_fields = ["slug", "label"]
    ordering = ["slug"]


class RoleCapabilityInline(admin.TabularInline):
    model = RoleCapability
    extra = 1
    autocomplete_fields = ["capability"]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    """Answers 'what can this Role do' directly -- the inline lists every
    Capability the Role grants. Which Group(s) are bound to a Role is
    shown the other direction, on the Group admin page itself (see
    GroupRoleInline below, attached to library.admin.GroupAdminWithAccess)
    so an operator editing a Group sees reachability (GroupAccess) and
    capability (GroupRole -> Role) on the same page."""
    list_display = ["name", "description", "capability_count"]
    search_fields = ["name"]
    inlines = [RoleCapabilityInline]

    @admin.display(description="Capabilities")
    def capability_count(self, obj):
        return obj.capabilities.count()


class GroupRoleInline(admin.StackedInline):
    """Inline GroupRole on the auth.Group admin page (attached in
    library/admin.py, alongside the existing GroupAccessInline). A Group
    with no GroupRole row grants no capabilities at all -- reachability
    (GroupAccess) and capability (GroupRole) are independent, deliberately
    never merged into one row/model even though they're shown on the same
    admin page."""
    model = GroupRole
    can_delete = True
    max_num = 1
    fields = ["role"]
    autocomplete_fields = ["role"]


# RoleCapability has no standalone admin registration -- it's managed
# only via RoleCapabilityInline on the Role change page above.
