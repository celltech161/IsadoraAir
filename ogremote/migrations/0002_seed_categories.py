from django.db import migrations

# Seeds the fixed set of category_key -> target Category mappings the
# ingest scripts know how to process. Editable afterward in admin (that's
# the whole point of this table -- see OGRemoteCategory's docstring) but
# needs to exist for the pipeline to function at all, so it's seeded here
# rather than left to a manual one-time shell setup.

CATEGORIES = [
    # category_key, name, target_category_code, output_mode, recallable, artist_tag
    ("cal_events", "Calendar of Events", "Announcements", "single", True, "Calendar of Events"),
    ("usd239min", "USD 239 Minute Update", "U239MU", "single", True, "USD 239 Minute Update"),
    ("news_item", "News Item", "LocalNews", "single", True, "Oak Grove Radio News"),
    ("3min_ctr", "3 Minutes to Center", "3MinCenter", "accumulating", True, "3 Minutes to Center"),
    ("lcl_psa", "Local PSA", "LocalPSA", "accumulating", True, "PSA"),
]


def seed(apps, schema_editor):
    Category = apps.get_model("library", "Category")
    OGRemoteCategory = apps.get_model("ogremote", "OGRemoteCategory")
    for category_key, name, target_code, output_mode, recallable, artist_tag in CATEGORIES:
        target = Category.objects.get(code=target_code)
        OGRemoteCategory.objects.get_or_create(
            category_key=category_key,
            defaults={
                "name": name,
                "target_category": target,
                "output_mode": output_mode,
                "recallable": recallable,
                "artist_tag": artist_tag,
                "enabled": True,
            },
        )


def reverse_delete(apps, schema_editor):
    OGRemoteCategory = apps.get_model("ogremote", "OGRemoteCategory")
    OGRemoteCategory.objects.filter(
        category_key__in=[c[0] for c in CATEGORIES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ogremote", "0001_initial"),
        ("library", "0042_add_urgentpa_3mincenter_categories"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_delete),
    ]
