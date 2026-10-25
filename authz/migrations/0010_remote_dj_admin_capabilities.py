"""Roadmap 2.17A: Station Administrator emergency controls for a connected
Remote DJ.

* ``remote_dj.emergency_mute`` -- force the connected Remote DJ's microphone
  off and latch it off for that session (and release that latch, which never
  opens the microphone);
* ``remote_dj.force_disconnect`` -- forcibly end the connected Remote DJ's
  session (Kick).

Both are granted to Station Administrator only; Remote Host and Contributor
receive neither, and no GroupAccess path is added for the remote_dj group.
Not schedule-restricted: an emergency control must be available whenever an
administrator needs it. Ordinary ``remote_dj.mic_gate`` stays owner-bound for
everyone, administrators included.

RunPython (data) migration, append-only on top of 0009 in the same shape: at
release time it needs a migration-authorization companion or local approval
(docs/UPDATE_CENTER.md, "Trusted companion migration authorization").
"""
from django.db import migrations


NEW_CAPABILITIES = (
    ("remote_dj.emergency_mute", "Emergency-mute a connected Remote DJ's microphone", False),
    ("remote_dj.force_disconnect", "Forcibly disconnect a connected Remote DJ (Kick)", False),
)

ROLE_CAPABILITIES = {
    "Station Administrator": ("remote_dj.emergency_mute", "remote_dj.force_disconnect"),
}


def forwards(apps, schema_editor):
    Capability = apps.get_model("authz", "Capability")
    Role = apps.get_model("authz", "Role")
    RoleCapability = apps.get_model("authz", "RoleCapability")

    capabilities = {}
    for slug, label, requires_schedule in NEW_CAPABILITIES:
        capability, _ = Capability.objects.update_or_create(
            slug=slug,
            defaults={"label": label, "requires_schedule": requires_schedule},
        )
        capabilities[slug] = capability

    for role_name, slugs in ROLE_CAPABILITIES.items():
        role = Role.objects.filter(name=role_name).first()
        if role is None:
            continue
        for slug in slugs:
            RoleCapability.objects.get_or_create(role=role, capability=capabilities[slug])


def backwards(apps, schema_editor):
    Capability = apps.get_model("authz", "Capability")
    Role = apps.get_model("authz", "Role")
    RoleCapability = apps.get_model("authz", "RoleCapability")

    for role_name, slugs in ROLE_CAPABILITIES.items():
        role = Role.objects.filter(name=role_name).first()
        if role is not None:
            RoleCapability.objects.filter(role=role, capability__slug__in=slugs).delete()
    Capability.objects.filter(slug__in=[slug for slug, _label, _requires_schedule in NEW_CAPABILITIES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("authz", "0009_playout_start_next_capability"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
