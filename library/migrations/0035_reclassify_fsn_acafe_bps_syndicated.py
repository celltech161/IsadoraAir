from django.db import migrations

# "FSN Weekend", ACafe, and BPS already exist as Categories (from an
# earlier orphan-folder reconciliation, filed under "talk") --
# reclassified to "syndicated" here, matching the earlier shows. Hourly
# Drops/Local Drops/XMAS Drop are deliberately left untouched -- they're
# promo/imaging drops, not syndicated show content, and are already
# correctly classified (spot/imaging).


def reclassify(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="syndicated")
    Category.objects.filter(code__in=["FSN Weekend", "ACafe", "BPS"]).update(kind=kind)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0034_reclassify_gdead_kns_syndicated"),
    ]

    operations = [
        migrations.RunPython(reclassify, reverse_noop),
    ]
