from django.db import migrations

# Dedications is a new category, same family as WxAlert (0041) and
# UrgentPA (0042) -- deliberately given ZERO RotationSlots, so it must
# never be drawn by the normal rotation walk (log_builder.pick_track).
# It exists purely as a delivery target for one-off synthesized spoken
# request-dedication clips, spliced directly into the live queue via
# engine.py's dedication-splice machinery (_maybe_insert_dedication_
# intro and friends), never played on a schedule. Unlike WxAlert's
# single-reused-file pattern, this is accumulating -- one Track per
# rendered dedication, same shape as UrgentPA.


def create_category(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    kind = CategoryKind.objects.get(code="spot")
    Category.objects.get_or_create(
        code="Dedications",
        defaults={"name": "Dedications", "kind": kind},
    )


def reverse_delete(apps, schema_editor):
    Category = apps.get_model("library", "Category")
    Category.objects.filter(code="Dedications").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0074_add_web_requests_nav_item"),
    ]

    operations = [
        migrations.RunPython(create_category, reverse_delete),
    ]
