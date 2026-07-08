from django.db import migrations

# Floydian, Warrior, and 105Live already exist as Categories (from an
# earlier orphan-folder reconciliation, filed under "talk") --
# reclassified to "syndicated" here, matching the earlier shows.


def reclassify(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="syndicated")
    Category.objects.filter(code__in=["Floydian", "Warrior", "105Live"]).update(kind=kind)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0038_reclassify_radiopush_syndicated"),
    ]

    operations = [
        migrations.RunPython(reclassify, reverse_noop),
    ]
