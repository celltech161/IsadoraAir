"""Seed the two default GroupAccess rows -- remote_dj and Contributor --
matching the previously-hardcoded GROUP_ACCESS_MAP in
library/middleware.py byte-for-byte. Existing deploys take zero
behavior change from the model-driven refactor; a fresh deploy from
GitHub gets a working default configuration out of the gate that an
admin can inspect and edit via the Group admin page."""
from django.db import migrations


REMOTE_DJ_PREFIXES = """
/remote-dj/
/monitoring/
/api/remote-dj/
/api/engine/status/
/api/engine/manual-mode/
/api/engine/remote-dj-gate/
/api/engine/levels/
/api/waveform/
/api/albumart/
""".strip()

REMOTE_DJ_EXACT = """
/api/tracks/
/api/engine/queue/insert/
/rbds/api/status/
""".strip()

REMOTE_DJ_REGEX = r"^/api/playlists/\d+/play-now/$"

CONTRIBUTOR_PREFIXES = """
/library/
/track/
/api/tracks/
/api/library/upload/
/api/categories/
/api/waveform/
/api/albumart/
""".strip()


def seed(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    GroupAccess = apps.get_model("library", "GroupAccess")

    dj, _ = Group.objects.get_or_create(name="remote_dj")
    GroupAccess.objects.update_or_create(
        group=dj,
        defaults={
            "allowed_prefixes": REMOTE_DJ_PREFIXES,
            "allowed_exact": REMOTE_DJ_EXACT,
            "allowed_regex": REMOTE_DJ_REGEX,
            "landing_url": "/remote-dj/",
            "priority": 50,
        },
    )

    contrib, _ = Group.objects.get_or_create(name="Contributor")
    GroupAccess.objects.update_or_create(
        group=contrib,
        defaults={
            "allowed_prefixes": CONTRIBUTOR_PREFIXES,
            "allowed_exact": "",
            "allowed_regex": "",
            "landing_url": "/library/",
            "priority": 40,
        },
    )


def unseed(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    GroupAccess.objects.filter(group__name__in=["remote_dj", "Contributor"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0053_group_access'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
