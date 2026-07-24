"""Grant remote_dj access to /api/voicetrack/ so on-air DJs can record,
preview, and delete voice tracks against the tracks they're playing.

Idempotent (appends only if missing) so a customized GroupAccess row
keeps its other prefixes."""
from django.db import migrations


NEW_PREFIXES = ("/api/voicetrack/",)


def add(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        return
    existing = [l.strip() for l in ga.allowed_prefixes.splitlines() if l.strip()]
    changed = False
    for p in NEW_PREFIXES:
        if p not in existing:
            existing.append(p)
            changed = True
    if changed:
        ga.allowed_prefixes = "\n".join(existing)
        ga.save()


def remove(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        return
    existing = [
        l.strip() for l in ga.allowed_prefixes.splitlines()
        if l.strip() and l.strip() not in NEW_PREFIXES
    ]
    ga.allowed_prefixes = "\n".join(existing)
    ga.save()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0067_voicetrackconfig_voicetrack"),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
