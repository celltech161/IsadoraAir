from django.db import migrations

# LocalPSA and U239MU exist as real folders under LIBRARY_ROOT with no
# matching Category row -- Category.kind is required, so import_songs can't
# auto-create these (it skips+warns instead, see import_songs.py). Created
# here as Spot kind per user decision (2026-07-06).
SEED_CATEGORIES = [
    ("LocalPSA", "LocalPSA"),
    ("U239MU", "U239MU"),
]


def seed_categories(apps, schema_editor):
    Category = apps.get_model("library", "Category")
    CategoryKind = apps.get_model("library", "CategoryKind")
    spot_kind = CategoryKind.objects.get(code="spot")
    for code, name in SEED_CATEGORIES:
        Category.objects.get_or_create(
            code=code,
            defaults={"name": name, "kind": spot_kind},
        )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0023_rotation_slot_track_insert"),
    ]

    operations = [
        migrations.RunPython(seed_categories, reverse_noop),
    ]
