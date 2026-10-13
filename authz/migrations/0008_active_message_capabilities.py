"""iPortal Phase D1: the two active-message capabilities.

* ``active_message.produce`` -- prepare, record/edit and bind media to an
  urgent or public-address message;
* ``active_message.control`` -- activate, Replay Now, clear, expiry and the
  domain policy.

Both are administrative and seeded only to Station Administrator (Remote
Host and Contributor receive neither); staff/superuser keep authorize()'s
compatibility bypass. Append-only on top of 0007, in the same shape.

This is the only non-additive (RunPython) operation of Phase D1: at release
time it needs a migration-authorization companion or local approval
(docs/UPDATE_CENTER.md, "Trusted companion migration authorization").
active_messages.0001 is CreateModel-only.
"""
from django.db import migrations


NEW_CAPABILITIES = (
    ("active_message.produce", "Prepare urgent and public-address messages (record, edit, bind media)", False),
    ("active_message.control", "Activate, replay, clear and expire urgent and public-address messages", False),
)

STATION_ADMINISTRATOR_CAPABILITIES = ("active_message.produce", "active_message.control")


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

    station_administrator = Role.objects.filter(name="Station Administrator").first()
    if station_administrator is None:
        return
    for slug in STATION_ADMINISTRATOR_CAPABILITIES:
        RoleCapability.objects.get_or_create(role=station_administrator, capability=capabilities[slug])


def backwards(apps, schema_editor):
    Capability = apps.get_model("authz", "Capability")
    Role = apps.get_model("authz", "Role")
    RoleCapability = apps.get_model("authz", "RoleCapability")

    station_administrator = Role.objects.filter(name="Station Administrator").first()
    if station_administrator is not None:
        RoleCapability.objects.filter(
            role=station_administrator,
            capability__slug__in=STATION_ADMINISTRATOR_CAPABILITIES,
        ).delete()
    Capability.objects.filter(slug__in=[slug for slug, _label, _requires_schedule in NEW_CAPABILITIES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("authz", "0007_authorization_closeout_capabilities"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
