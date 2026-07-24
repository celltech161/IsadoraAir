"""Grant remote_dj access to /voicetracks/ AND seed a top-level nav
item so operators can reach the index without knowing the URL.

Idempotent -- both the prefix append and the NavMenuItem lookup
skip if already present."""
from django.db import migrations


NAV_LABEL = "Voice Tracks"
NAV_URL_NAME = "library:voicetracks"
NEW_PREFIXES = ("/voicetracks/",)


def apply_forward(apps, schema_editor):
    # 1. GroupAccess prefix append for remote_dj
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
        existing = [l.strip() for l in ga.allowed_prefixes.splitlines() if l.strip()]
        changed = False
        for p in NEW_PREFIXES:
            if p not in existing:
                existing.append(p)
                changed = True
        if changed:
            ga.allowed_prefixes = "\n".join(existing)
            ga.save()
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        pass

    # 2. Nav menu item seed. Same pattern as the Reports seed from
    # migration 0060 -- sort_order = max(top-level) + 10, idempotent
    # by url_name.
    NavMenuItem = apps.get_model("library", "NavMenuItem")
    if not NavMenuItem.objects.filter(url_name=NAV_URL_NAME).exists():
        max_sort = 0
        for item in NavMenuItem.objects.filter(parent__isnull=True):
            if item.sort_order and item.sort_order > max_sort:
                max_sort = item.sort_order
        NavMenuItem.objects.create(
            label=NAV_LABEL,
            url_name=NAV_URL_NAME,
            sort_order=max_sort + 10,
            enabled=True,
        )


def apply_reverse(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
        existing = [
            l.strip() for l in ga.allowed_prefixes.splitlines()
            if l.strip() and l.strip() not in NEW_PREFIXES
        ]
        ga.allowed_prefixes = "\n".join(existing)
        ga.save()
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        pass

    NavMenuItem = apps.get_model("library", "NavMenuItem")
    NavMenuItem.objects.filter(url_name=NAV_URL_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0068_remote_dj_voicetrack_access"),
    ]

    operations = [
        migrations.RunPython(apply_forward, apply_reverse),
    ]
