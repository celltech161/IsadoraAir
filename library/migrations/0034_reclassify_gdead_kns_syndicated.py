from django.db import migrations

# GDead and KNS already exist as Categories (from an earlier orphan-folder
# reconciliation, filed under "talk") -- reclassified to "syndicated" here,
# matching the KIN21-KIN27/DemocracyNow treatment in 0032/0033.


def reclassify(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="syndicated")
    Category.objects.filter(code__in=["GDead", "KNS"]).update(kind=kind)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0033_reclassify_democracynow_syndicated"),
    ]

    operations = [
        migrations.RunPython(reclassify, reverse_noop),
    ]
