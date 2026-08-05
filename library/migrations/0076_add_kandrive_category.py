from django.db import migrations

# KanDrive is a new category for road_conditions' generated KDOT road-
# report audio -- same "single reused file" family as WxForecast/WxObs/
# WxTemp (see weather-ingest/wx_forecast.py, lib/delivery.py): exactly
# one Track, filepath-keyed update_or_create on every regeneration
# cycle, no accumulation. Unlike WxAlert/Dedications (zero RotationSlots,
# urgent-splice-only), KanDrive IS meant to be rotation-eligible -- the
# operator adds it to whichever rotations should carry road reports
# manually (see PROJECT_NOTES.md / the road_conditions app's own docs);
# this migration only ever creates the Category itself, never touches
# RotationSlot, Rotation, or any clock/schedule.
#
# Idempotent via get_or_create -- safe to re-run, and safe if an
# operator already manually created a "KanDrive" category by hand
# before this migration ran (reuses it rather than erroring or
# creating a duplicate differing only in case/whitespace).


def create_category(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="spot")
    Category.objects.get_or_create(
        code="KanDrive",
        defaults={"name": "KanDrive", "kind": kind},
    )


def reverse_delete(apps, schema_editor):
    Category = apps.get_model("library", "Category")
    Category.objects.filter(code="KanDrive").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0075_add_dedications_category"),
    ]

    operations = [
        migrations.RunPython(create_category, reverse_delete),
    ]
