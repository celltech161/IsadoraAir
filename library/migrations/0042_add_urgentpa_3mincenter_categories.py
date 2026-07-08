from django.db import migrations

# Both new categories for the OGRemote PA/PSA port:
#   - UrgentPA: zero RotationSlots on purpose, same as WxAlert -- never
#     drawn by the normal rotation walk, only ever populated via the
#     insert_urgent live-queue splice.
#   - 3MinCenter: a real programming category (accumulating meditation
#     episodes) that just hasn't started airing yet -- provisioned now
#     per explicit request, rotation scheduling is a separate decision.


def create_categories(apps, schema_editor):
    CategoryKind = apps.get_model("library", "CategoryKind")
    Category = apps.get_model("library", "Category")
    spot_kind = CategoryKind.objects.get(code="spot")
    talk_kind = CategoryKind.objects.get(code="talk")
    Category.objects.get_or_create(
        code="UrgentPA",
        defaults={"name": "Urgent PA", "kind": spot_kind},
    )
    Category.objects.get_or_create(
        code="3MinCenter",
        defaults={"name": "3 Minutes to Center", "kind": talk_kind},
    )


def reverse_delete(apps, schema_editor):
    Category = apps.get_model("library", "Category")
    Category.objects.filter(code__in=["UrgentPA", "3MinCenter"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0041_add_wxalert_category"),
    ]

    operations = [
        migrations.RunPython(create_categories, reverse_delete),
    ]
