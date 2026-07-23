"""Seed the StationTimeConfig singleton with whatever the deploying
site's Django settings.TIME_ZONE is at migration time -- so an
existing deploy takes ZERO behavior change on migrate (the model-
driven active timezone matches the previously-hardcoded one), and a
fresh install lands with its own settings.TIME_ZONE seeded (an admin
can then flip it via /admin/library/stationtimeconfig/ without a
code touch)."""
from django.conf import settings
from django.db import migrations


def seed(apps, schema_editor):
    StationTimeConfig = apps.get_model("library", "StationTimeConfig")
    StationTimeConfig.objects.update_or_create(
        pk=1,
        defaults={"timezone": getattr(settings, "TIME_ZONE", "UTC")},
    )


def unseed(apps, schema_editor):
    StationTimeConfig = apps.get_model("library", "StationTimeConfig")
    StationTimeConfig.objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('library', '0055_station_time_config'),
    ]
    operations = [
        migrations.RunPython(seed, unseed),
    ]
