from django.db import migrations

# AMOS, AskAKansan, and GLLounge already exist as Categories (from an
# earlier orphan-folder reconciliation, filed under "talk") --
# reclassified to "syndicated" here, matching the earlier shows.


def reclassify(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="syndicated")
    Category.objects.filter(code__in=["AMOS", "AskAKansan", "GLLounge"]).update(kind=kind)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0039_reclassify_floyd_warrior_105live_syndicated"),
    ]

    operations = [
        migrations.RunPython(reclassify, reverse_noop),
    ]
