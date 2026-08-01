from django.db import migrations

# Dedications is a new category, same family as WxAlert (0041) and
# UrgentPA (0042) -- deliberately given ZERO RotationSlots, so it must
# never be drawn by the normal rotation walk (log_builder.pick_track).
# It exists purely as a delivery target for one-off synthesized spoken
# request-dedication clips, spliced directly into the live queue via
# engine.py's dedication-splice machinery (_maybe_insert_dedication_
# intro and friends), never played on a schedule. Unlike WxAlert's
# single-reused-file pattern, this is accumulating -- one Track per
# rendered dedication, same shape as UrgentPA. Unlike 0041/0042's
# reverse (a plain delete, safe there since WxAlert/UrgentPA only ever
# have one reused Track), reversing THIS migration is a no-op --
# Track.category is SET_NULL, so deleting the category on a rollback
# would silently strip it off every accumulated historical dedication
# Track rather than failing loudly. Doesn't affect forward deployment.


def create_category(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="spot")
    Category.objects.get_or_create(
        code="Dedications",
        defaults={"name": "Dedications", "kind": kind},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0074_add_web_requests_nav_item"),
    ]

    operations = [
        migrations.RunPython(create_category, migrations.RunPython.noop),
    ]
