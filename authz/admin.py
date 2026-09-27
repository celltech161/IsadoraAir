from django import forms
from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from authz.models import (
    Capability, GroupRole, Role, RoleCapability,
    ScheduleAccessConfig, TalentAssignment,
)


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


class TalentAssignmentForm(forms.ModelForm):
    class Meta:
        model = TalentAssignment
        fields = ["user", "day_of_week", "specific_date", "start_time", "end_time", "active"]

    def clean(self):
        cleaned = super().clean()
        day_of_week = cleaned.get("day_of_week")
        specific_date = cleaned.get("specific_date")
        if (day_of_week is None) == (specific_date is None):
            raise forms.ValidationError(
                "Set exactly one of Day of week (recurring) or Specific date "
                "(one-off), not both and not neither."
            )
        return cleaned


class TalentAssignmentInline(admin.TabularInline):
    """Inline on the User admin (library.admin.InviteCapableUserAdmin) --
    an operator looking at a talent account sees/edits that person's show
    windows directly. Deliberately does NOT show or imply a Role/capability
    here: an assignment is temporal scope over capabilities the user
    already holds through their Group(s) -- see TalentAssignment's own
    docstring. `user` is excluded from the inline's own fields (it's
    fixed by which User's change page this is rendered on)."""
    model = TalentAssignment
    form = TalentAssignmentForm
    fk_name = "user"
    extra = 0
    fields = ["day_of_week", "specific_date", "start_time", "end_time", "active"]


@admin.register(TalentAssignment)
class TalentAssignmentAdmin(admin.ModelAdmin):
    """Station-wide direct view of every talent assignment across every
    user -- useful for an operator building next week's on-air schedule
    without opening each DJ's User page individually. The per-user inline
    above (on InviteCapableUserAdmin) is the other, complementary way to
    reach the same rows."""
    form = TalentAssignmentForm
    list_display = ["user", "when_display", "start_time", "end_time", "crosses_midnight", "active"]
    list_filter = ["active", "day_of_week"]
    search_fields = ["user__username", "user__first_name", "user__last_name"]
    autocomplete_fields = ["user"]
    fieldsets = (
        (None, {
            "fields": ("user", "active"),
        }),
        ("When (set exactly one)", {
            "fields": ("day_of_week", "specific_date"),
            "description": (
                "Recurring weekly show: set Day of week, leave Specific "
                "date blank. One-off/override for a single calendar date: "
                "set Specific date, leave Day of week blank."
            ),
        }),
        ("Show window", {
            "fields": ("start_time", "end_time"),
            "description": (
                "If End time is less than or equal to Start time, this "
                "assignment is treated as crossing midnight (e.g. 22:00 "
                "-> 02:00 runs through 2 AM the following day). Station-"
                "wide pre/post access allowances (Config > Schedule Access) "
                "apply on top of this window -- they are not set per "
                "assignment."
            ),
        }),
    )

    @admin.display(description="Recurs / date")
    def when_display(self, obj):
        return obj.specific_date if obj.specific_date is not None else obj.get_day_of_week_display()

    @admin.display(description="Overnight", boolean=True)
    def crosses_midnight(self, obj):
        return obj.crosses_midnight


class ScheduleAccessConfigForm(forms.ModelForm):
    class Meta:
        model = ScheduleAccessConfig
        fields = [
            "pre_schedule_allowance_minutes", "post_schedule_allowance_minutes",
            "scheduled_enforcement_enabled",
        ]

    def clean_scheduled_enforcement_enabled(self):
        """Roadmap 2.5C activation safety (docs/AUTHORIZATION.md's "Safe
        activation" section). Refuses to save with this ON while any
        active, non-staff account holds a schedule-restricted capability
        but has zero TalentAssignment rows configured at all -- turning
        enforcement on would otherwise silently lock that account out the
        moment this form saves. Existence, not liveness: a future/
        recurring assignment is enough (see
        users_missing_talent_assignments_for_scheduled_capabilities's own
        docstring) -- this is deliberately not a live authorize() check."""
        enabled = self.cleaned_data["scheduled_enforcement_enabled"]
        if enabled:
            from authz.evaluator import users_missing_talent_assignments_for_scheduled_capabilities
            missing = users_missing_talent_assignments_for_scheduled_capabilities()
            if missing:
                names = ", ".join(sorted(u.username for u in missing))
                raise forms.ValidationError(
                    "Cannot enable scheduled enforcement: the following account(s) "
                    "hold a schedule-restricted capability (e.g. Remote DJ connect) "
                    f"but have NO Talent Assignment configured at all: {names}. "
                    "Add at least one assignment for each (Config > Talent "
                    "Assignments, or that user's own admin page) before enabling."
                )
        return enabled


@admin.register(ScheduleAccessConfig)
class ScheduleAccessConfigAdmin(admin.ModelAdmin):
    """Singleton -- station-wide pre/post schedule access allowances and
    the scheduled-enforcement activation switch, applied to every
    schedule-restricted capability check. Same singleton admin pattern as
    library.admin.StationTimeConfigAdmin: add is blocked once the one row
    exists, delete is blocked entirely, and the changelist redirects
    straight to that row's own change page."""
    form = ScheduleAccessConfigForm
    fieldsets = (
        (None, {
            "fields": ("pre_schedule_allowance_minutes", "post_schedule_allowance_minutes"),
            "description": (
                "How many minutes before/after an assignment's scheduled "
                "start/end its schedule-restricted capabilities (Remote "
                "DJ connect, queue management, manual mode, mic gates, FX "
                "fire) become/remain usable. Both accept 0. Applied "
                "station-wide -- there is no per-user or per-assignment "
                "override."
            ),
        }),
        ("Activation", {
            "fields": ("scheduled_enforcement_enabled",),
            "description": (
                "OFF (default): schedule-restricted capabilities behave "
                "like ordinary ones -- the account must still hold the "
                "capability through a Role, but no Talent Assignment is "
                "required. ON: the account must ALSO have an active "
                "Talent Assignment whose window currently covers the "
                "capability being used. Turning this ON is refused if "
                "any account would be immediately locked out -- see this "
                "field's own help text."
            ),
        }),
    )

    def has_add_permission(self, request):
        return not ScheduleAccessConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = ScheduleAccessConfig.load()
        return HttpResponseRedirect(
            reverse("admin:authz_scheduleaccessconfig_change", args=[obj.pk])
        )

    def save_model(self, request, obj, form, change):
        was_enabled = None
        if change:
            was_enabled = ScheduleAccessConfig.objects.filter(pk=obj.pk).values_list(
                "scheduled_enforcement_enabled", flat=True
            ).first()
        super().save_model(request, obj, form, change)
        if was_enabled is not None and was_enabled != obj.scheduled_enforcement_enabled:
            from monitoring.models import emit_event
            state = "ENABLED" if obj.scheduled_enforcement_enabled else "disabled"
            emit_event(
                category="authz",
                level="warning",
                title=f"Scheduled talent enforcement {state}",
                detail={"by": getattr(request.user, "username", "")},
            )
