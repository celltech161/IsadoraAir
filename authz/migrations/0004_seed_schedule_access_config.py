"""Seed the ScheduleAccessConfig singleton with its defaults (10 minutes
pre-schedule allowance, 15 minutes post-schedule allowance) -- same
reasoning/pattern as library.migrations.0056_seed_station_time_config:
a fresh install shows a populated, admin-editable row immediately rather
than the row only springing into existence on first `.load()` call from
whatever code path happens to touch it first."""
from django.db import migrations


def seed(apps, schema_editor):
    ScheduleAccessConfig = apps.get_model("authz", "ScheduleAccessConfig")
    ScheduleAccessConfig.objects.update_or_create(
        pk=1,
        defaults={
            "pre_schedule_allowance_minutes": 10,
            "post_schedule_allowance_minutes": 15,
        },
    )


def unseed(apps, schema_editor):
    ScheduleAccessConfig = apps.get_model("authz", "ScheduleAccessConfig")
    ScheduleAccessConfig.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("authz", "0003_scheduleaccessconfig_talentassignment"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
