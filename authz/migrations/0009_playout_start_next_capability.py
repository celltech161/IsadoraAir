"""Roadmap 2.23 (D3): the dedicated ``playout.start_next`` capability.

Start Next is a soft transport operation affecting on-air playback, so it is
independently assignable rather than riding on ``playout.queue_manage``. It
implies nothing else: Eject/Pause/Resume and seek stay ``playout.control``,
Studio Mic stays ``studio.mic_ptt``.

Seeded to the two Roles whose holders operate the on-air Console:
Station Administrator and Remote Host (the seeded vocabulary has no separate
"on-air operator" Role; staff/superuser keep authorize()'s compatibility
bypass). Contributor receives nothing. Schedule-restricted like every other
playout capability. Append-only on top of 0008, in the same shape; the
matching GroupAccess path is library.0092.

RunPython (data) migration: at release time it needs a migration-
authorization companion or local approval (docs/UPDATE_CENTER.md,
"Trusted companion migration authorization").
"""
from django.db import migrations


NEW_CAPABILITIES = (
    ("playout.start_next", "Start the next item with a soft transition (Start Next)", True),
)

ROLE_CAPABILITIES = {
    "Station Administrator": ("playout.start_next",),
    "Remote Host": ("playout.start_next",),
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
        ("authz", "0008_active_message_capabilities"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
