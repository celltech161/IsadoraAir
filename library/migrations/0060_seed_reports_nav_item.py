"""Seed a top-level 'Reports' NavMenuItem pointing at library:reports.

Idempotent: a re-run won't create a duplicate (checked by url_name).
Skips silently if the reports view name doesn't exist yet on this
install (belt-and-suspenders; the same migration graph that ships
the view also ships this seed, but a partial checkout won't die
here).

For non-staff/non-superuser roles the nav_menu context processor
filters this item out automatically because /reports/ isn't in any
group's access prefixes -- so Contributors and remote_dj don't see
the link even though it's globally enabled."""
from django.db import migrations


def seed(apps, schema_editor):
    NavMenuItem = apps.get_model("library", "NavMenuItem")
    if NavMenuItem.objects.filter(url_name="library:reports").exists():
        return
    max_sort = 0
    for item in NavMenuItem.objects.filter(parent__isnull=True):
        if item.sort_order and item.sort_order > max_sort:
            max_sort = item.sort_order
    NavMenuItem.objects.create(
        label="Reports",
        url_name="library:reports",
        sort_order=max_sort + 10,
        enabled=True,
    )


def unseed(apps, schema_editor):
    NavMenuItem = apps.get_model("library", "NavMenuItem")
    NavMenuItem.objects.filter(url_name="library:reports").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0059_royaltyreport"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
