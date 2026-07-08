from django.db import migrations

# WxAlert is a new category, unlike the WxTemp/WxForecast/WxObs family
# (which pre-existed from an earlier orphan-folder reconciliation).
# Deliberately given ZERO RotationSlots -- it must never be drawn by
# the normal rotation walk (log_builder.pick_track). It exists purely
# as a delivery target for severe Watch/Warning audio, inserted
# directly into the live queue via engine.py's insert_urgent command
# (see PROJECT_NOTES.md) rather than played on a schedule.


def create_category(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="spot")
    Category.objects.get_or_create(
        code="WxAlert",
        defaults={"name": "Weather Alert", "kind": kind},
    )


def reverse_delete(apps, schema_editor):
    Category = apps.get_model("library", "Category")
    Category.objects.filter(code="WxAlert").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0040_reclassify_amos_askakansan_gllounge_syndicated"),
    ]

    operations = [
        migrations.RunPython(create_category, reverse_delete),
    ]
