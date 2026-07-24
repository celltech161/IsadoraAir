"""Grant remote_dj access to /api/fx/ so on-air DJs can fire hot-keys.

Idempotent -- appends only what's missing to the existing allowed_prefixes
so an admin who's customized the row keeps everything else."""
from django.db import migrations


NEW_PREFIXES = ("/api/fx/",)


def add_fx(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        return

    existing = [
        line.strip() for line in ga.allowed_prefixes.splitlines()
        if line.strip()
    ]
    changed = False
    for p in NEW_PREFIXES:
        if p not in existing:
            existing.append(p)
            changed = True
    if changed:
        ga.allowed_prefixes = "\n".join(existing)
        ga.save()


def remove_fx(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        return
    existing = [
        line.strip() for line in ga.allowed_prefixes.splitlines()
        if line.strip() and line.strip() not in NEW_PREFIXES
    ]
    ga.allowed_prefixes = "\n".join(existing)
    ga.save()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0064_fxbusconfig_fxcart"),
    ]

    operations = [
        migrations.RunPython(add_fx, remove_fx),
    ]
