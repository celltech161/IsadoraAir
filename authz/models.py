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
from django.db import models


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
