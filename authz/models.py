"""Roadmap 2.5A -- authorization foundation: capability vocabulary, roles,
and the Group->Role binding.

Deliberately separate from `library.models.GroupAccess`, which remains the
authoritative mechanism for coarse per-group URL reachability (see its own
docstring). These models answer a different question -- not "can this
user's browser reach this URL" but "may this user perform this specific
operation" -- and the two are NOT allowed to collapse into one concept:
reaching a page via GroupAccess must never, by itself, imply a Capability.

See docs/AUTHORIZATION.md (added alongside this app) and PROJECT_NOTES.md's
"Roadmap 2.5" section for the full design/audit history.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class Capability(models.Model):
    """One named, code-referenced operational capability. This is a fixed
    vocabulary -- call sites (views, and eventually the Remote DJ signaling
    server) reference a Capability by its `slug` as a string constant, so
    slugs are a code-level contract. Seeded by a data migration; not meant
    to be freely invented in the admin (nothing stops it, but a slug with
    no call site checking for it does nothing).

    `requires_schedule` is reserved for 2.5B/C: when true, this capability
    will additionally require an active TalentAssignment covering "now"
    (station time) before being granted -- on top of, not instead of, the
    Role->Capability grant below. 2.5A defines the field and seeds its
    value for forward compatibility but does not enforce it anywhere yet
    (no TalentAssignment model exists yet)."""
    slug = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    requires_schedule = models.BooleanField(
        default=False,
        help_text="Reserved for roadmap 2.5B -- will require an active "
                   "scheduled talent assignment in addition to Role "
                   "membership once TalentAssignment exists. Not yet "
                   "enforced by anything in 2.5A.",
    )

    class Meta:
        ordering = ["slug"]
        verbose_name = "Capability"
        verbose_name_plural = "Capabilities"

    def __str__(self):
        return self.slug


class Role(models.Model):
    """A named, admin-assignable bundle of Capabilities. Distinct from
    auth.Group: Group (via GroupAccess) still drives coarse page
    reachability; Role (via RoleCapability, bound to a Group through
    GroupRole) drives operational capability. There is exactly one
    authoritative path from a User to their capability set:

        User -> Group(s) -> GroupRole -> Role -> RoleCapability -> Capability

    A User is never directly assigned a Role -- always through a Group,
    reusing the Group-membership workflow operators already use for
    GroupAccess."""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    capabilities = models.ManyToManyField(
        Capability, through="RoleCapability", related_name="roles",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Role"
        verbose_name_plural = "Roles"

    def __str__(self):
        return self.name


class RoleCapability(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_capabilities")
    capability = models.ForeignKey(Capability, on_delete=models.CASCADE, related_name="capability_roles")

    class Meta:
        unique_together = [("role", "capability")]
        verbose_name = "Role Capability"
        verbose_name_plural = "Role Capabilities"

    def __str__(self):
        return f"{self.role.name} -> {self.capability.slug}"


class GroupRole(models.Model):
    """Binds exactly one Role to an auth.Group. A Group with no GroupRole
    row grants no capabilities at all (its members may still reach
    whatever GroupAccess allows -- reachability and capability are
    independent). Surfaced as an inline on the standard auth.Group admin
    page (library.admin.GroupAdminWithAccess), right alongside the
    existing GroupAccess inline, so an operator can see both "where can
    this group go" and "what can this group do" on one page -- without
    the two ever being the same database row."""
    group = models.OneToOneField(
        "auth.Group", on_delete=models.CASCADE, related_name="role_binding",
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="group_bindings")

    class Meta:
        verbose_name = "Group Role"
        verbose_name_plural = "Group Roles"

    def __str__(self):
        return f"{self.group.name} -> {self.role.name}"


# Same Monday=0..Sunday=6 vocabulary as library.models.ScheduleBlock.DAY_CHOICES
# (matching Python's date.weekday()) -- duplicated here rather than imported
# from library.models. The two models are deliberately NOT the same concept
# (see this module's own docstring and docs/AUTHORIZATION.md's "Roadmap 2.5"
# audit finding: ScheduleBlock is hourly, row-per-hour content scheduling;
# TalentAssignment is a human's show window, independent of how many
# ScheduleBlock rows the automation underneath happens to use), and authz is
# listed before library in INSTALLED_APPS -- importing library.models from
# here would add a fragile, unnecessary app-loading-order dependency for the
# sake of not retyping seven tuples.
DAY_CHOICES = [
    (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
    (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
]


class TalentAssignment(models.Model):
    """A user's scheduled on-air window -- WHEN their already-granted
    schedule-restricted capabilities (Capability.requires_schedule=True)
    may actually be exercised. Deliberately NOT a role-grant mechanism:
    there is no `role` field here, and none should ever be added. A
    TalentAssignment is temporal SCOPE over a capability the user already
    holds through Group -> GroupRole -> Role -> RoleCapability -- it can
    never itself grant a Capability the user lacks. See authorize() in
    authz/evaluator.py, which enforces this by checking capability
    possession FIRST and independently of assignment lookup.

    Exactly one of day_of_week (recurring) or specific_date (one-off
    override) must be set -- same exclusivity pattern as ScheduleBlock,
    for a consistent station-wide vocabulary, but this is its own model:
    ScheduleBlock rows are hourly content-scheduling units and are not an
    appropriate parent for a human show assignment (a two-hour show is
    two ScheduleBlock rows but exactly one TalentAssignment).

    Cross-midnight: if end_time <= start_time, the assignment is
    interpreted as crossing midnight (e.g. Saturday 22:00 -> 02:00 means
    Saturday night through Sunday 02:00). authz.evaluator's window
    computation handles this, plus pre/post allowances crossing a
    calendar-day boundary on either side, uniformly -- see its own
    module docstring rather than duplicating the algorithm description
    here."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="talent_assignments",
    )
    day_of_week = models.PositiveSmallIntegerField(choices=DAY_CHOICES, null=True, blank=True)
    specific_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField(
        help_text="If this is less than or equal to Start time, the "
                   "assignment is interpreted as crossing midnight (e.g. "
                   "22:00 -> 02:00 means through 2 AM the following day).",
    )
    active = models.BooleanField(
        default=True,
        help_text="Uncheck to disable this assignment without deleting "
                   "it (e.g. a host on hiatus). An inactive assignment "
                   "never satisfies a scheduled capability check.",
    )

    class Meta:
        ordering = ["user__username", "specific_date", "day_of_week", "start_time"]
        verbose_name = "Talent Assignment"
        verbose_name_plural = "Talent Assignments"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(day_of_week__isnull=False, specific_date__isnull=True)
                    | Q(day_of_week__isnull=True, specific_date__isnull=False)
                ),
                name="talentassignment_exactly_one_of_day_or_date",
            ),
        ]

    @property
    def crosses_midnight(self):
        return self.end_time <= self.start_time

    def __str__(self):
        when = self.specific_date if self.specific_date is not None else self.get_day_of_week_display()
        span = f"{self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"
        if self.crosses_midnight:
            span += " (overnight)"
        if not self.active:
            span += " [inactive]"
        return f"{self.user}: {when} {span}"


class ScheduleAccessConfig(models.Model):
    """Singleton -- station-wide pre/post schedule allowances applied to
    every TalentAssignment's effective authorization window (see
    authz.evaluator's module docstring for the exact half-open-interval
    formula). Deliberately station-wide, not per-user/per-assignment, per
    the roadmap 2.5B scope: introducing per-assignment overrides is not
    justified by any repository finding and would make the first
    implementation harder for a small-station operator to reason about.

    Defaults (10 minutes pre, 15 minutes post) match the exact example
    numbers used throughout the roadmap 2.5 audit and workorder
    discussion -- not arbitrary, but also not load-bearing; an operator
    is expected to tune these for their own station's actual on-air
    handoff habits."""
    pre_schedule_allowance_minutes = models.PositiveIntegerField(
        default=10,
        help_text="How many minutes before an assignment's scheduled "
                   "start its schedule-restricted capabilities become "
                   "usable. 0 is valid (no early access).",
    )
    post_schedule_allowance_minutes = models.PositiveIntegerField(
        default=15,
        help_text="How many minutes after an assignment's scheduled end "
                   "its schedule-restricted capabilities remain usable. "
                   "0 is valid (access ends exactly at the scheduled end "
                   "time).",
    )

    class Meta:
        verbose_name = "Schedule Access Config"
        verbose_name_plural = "Schedule Access Config"

    def __str__(self):
        return (
            f"Schedule Access (pre={self.pre_schedule_allowance_minutes}min, "
            f"post={self.post_schedule_allowance_minutes}min)"
        )

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "pre_schedule_allowance_minutes": 10,
                "post_schedule_allowance_minutes": 15,
            },
        )
        return obj
