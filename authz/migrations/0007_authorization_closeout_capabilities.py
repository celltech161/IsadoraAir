"""Roadmap 2.5D authorization closeout seed corrections.

This migration is deliberately append-only on top of the accepted 2.5A-C
lineage.  It does three bounded things:

* repairs 0006's omission of ``playout.queue_manage`` from the Station
  Administrator Role;
* adds a narrow listener-counter-reset capability rather than overloading
  the much broader service-restart capability; and
* adds the requested Aircheck start/stop capability.

Both new capabilities are administrative operational authority and are
seeded only to Station Administrator.  Remote Host receives neither.
"""
from django.db import migrations, models


NEW_CAPABILITIES = (
    (
        "monitoring.reset_listener_counters",
        "Reset listener peak and total-listening-hour counters",
        False,
    ),
    ("aircheck.control", "Start and stop Aircheck recording", False),
)

STATION_ADMINISTRATOR_CAPABILITIES = (
    "playout.queue_manage",
    "monitoring.reset_listener_counters",
    "aircheck.control",
)


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

    # 0006 owns this row.  get_or_create keeps this migration robust on a
    # partially-applied development database while preserving its contract.
    queue_manage, _ = Capability.objects.get_or_create(
        slug="playout.queue_manage",
        defaults={
            "label": "Manage the upcoming queue (insert, force-next)",
            "requires_schedule": True,
        },
    )
    capabilities["playout.queue_manage"] = queue_manage

    station_administrator = Role.objects.filter(name="Station Administrator").first()
    if station_administrator is None:
        return
    for slug in STATION_ADMINISTRATOR_CAPABILITIES:
        RoleCapability.objects.get_or_create(
            role=station_administrator,
            capability=capabilities[slug],
        )


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
    Capability.objects.filter(
        slug__in=[slug for slug, _label, _requires_schedule in NEW_CAPABILITIES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("authz", "0006_correct_remote_host_playout_capability"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name="scheduleaccessconfig",
            name="scheduled_enforcement_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Roadmap 2.5C activation switch. OFF (the default, "
                    "including on every existing/upgraded installation): "
                    "schedule-restricted capabilities behave as ordinary "
                    "capabilities -- the account must still hold the "
                    "capability, but no TalentAssignment is required. ON: "
                    "the account must ALSO have an active TalentAssignment "
                    "whose effective window covers the current moment. "
                    "This never affects ordinary (non-scheduled) "
                    "capabilities. The admin form refuses to turn this ON "
                    "while an active, non-staff account holds a "
                    "schedule-restricted capability but has no active "
                    "Talent Assignment configured -- see "
                    'docs/AUTHORIZATION.md\'s "Safe activation" section.'
                ),
            ),
        ),
    ]
