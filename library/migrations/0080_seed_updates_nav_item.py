"""Seed the shared product Update Center navigation entry idempotently."""
from django.db import migrations


URL_NAME = "updatecenter:dashboard"
# Repeating the primary view as an extra active view is harmless to rendering
# and gives reversal a conservative marker for a row this migration created.
PRODUCT_MARKER = URL_NAME


def seed_updates_nav(apps, schema_editor):
    NavMenuItem = apps.get_model("library", "NavMenuItem")
    if NavMenuItem.objects.filter(url_name=URL_NAME).exists():
        return
    max_sort = 0
    for item in NavMenuItem.objects.filter(parent__isnull=True):
        max_sort = max(max_sort, item.sort_order or 0)
    NavMenuItem.objects.create(
        label="Updates",
        url_name=URL_NAME,
        extra_active_view_names=PRODUCT_MARKER,
        sort_order=max_sort + 10,
        enabled=True,
    )


def unseed_updates_nav(apps, schema_editor):
    NavMenuItem = apps.get_model("library", "NavMenuItem")
    # Never remove a pre-existing/customized row. Only the exact product
    # identity marker and untouched product defaults are safe to reverse.
    NavMenuItem.objects.filter(
        url_name=URL_NAME,
        label="Updates",
        parent__isnull=True,
        custom_url="",
        extra_active_view_names=PRODUCT_MARKER,
        enabled=True,
        open_in_new_tab=False,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0079_mediaplaybackincident"),
    ]

    operations = [
        migrations.RunPython(seed_updates_nav, unseed_updates_nav),
    ]
