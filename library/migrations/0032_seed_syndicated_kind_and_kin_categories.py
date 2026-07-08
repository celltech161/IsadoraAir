from django.db import migrations

# "Syndicated" CategoryKind is organizational only (dashboard color-coding,
# matches Music/Imaging/Spot/Talk) -- NOT used to trigger analysis. Fresh
# analysis on file replace is handled by sync_track_file at ingest time
# instead (see library/management/commands/sync_track_file.py); see
# PROJECT_NOTES.md for why deck-load-time analysis was rejected.
#
# KIN21..KIN27 already exist as Categories (created during an earlier
# orphan-folder reconciliation pass, one placeholder Track each, currently
# filed under "talk") -- reclassified here to "syndicated" rather than
# recreated. KIN23 is included even though auto_processor.log shows it's
# not part of the current hourly cron rotation (retired in favor of
# SPKI21/22/24 taking its old hour slots) -- it's still a real KIN category
# with a real library folder, just not actively re-ingested by the pilot's
# systemd timer for now.
KIN_CODES = ["KIN21", "KIN22", "KIN23", "KIN24", "KIN25", "KIN26", "KIN27"]


def seed_kind_and_reclassify_categories(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")

    kind, _ = CategoryKind.objects.get_or_create(
        code="syndicated",
        defaults={"name": "Syndicated", "fill_color": "#0e7490", "sort_order": 4},
    )
    Category.objects.filter(code__in=KIN_CODES).update(kind=kind)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0031_logitem_track_snapshot"),
    ]

    operations = [
        migrations.RunPython(seed_kind_and_reclassify_categories, reverse_noop),
    ]
