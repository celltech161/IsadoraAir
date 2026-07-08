from django.db import migrations

# DemocracyNow already exists as a Category (from an earlier orphan-folder
# reconciliation, filed under "talk") -- reclassified to "syndicated" here,
# matching the KIN21-KIN27 treatment in 0032.


def reclassify(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="syndicated")
    Category.objects.filter(code="DemocracyNow").update(kind=kind)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0032_seed_syndicated_kind_and_kin_categories"),
    ]

    operations = [
        migrations.RunPython(reclassify, reverse_noop),
    ]
