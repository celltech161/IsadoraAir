"""Seed the top-level iPortal navigation entry idempotently."""
from django.db import migrations


URL_NAME = "iportal-index"
EXTRA_ACTIVE_VIEW_NAMES = ",".join(
    (
        "active_messages:my-messages",
        "active_messages:iportal-active-message-page",
        "library:voicetracks",
        "library:iportal-evergreen-voicetrack-page",
    )
)
SORT_ORDER = 120


def seed_iportal_nav(apps, schema_editor):
    NavMenuItem = apps.get_model("library", "NavMenuItem")
    NavMenuItem.objects.get_or_create(
        url_name=URL_NAME,
        defaults={
            "label": "iPortal",
            "parent": None,
            "custom_url": "",
            "extra_active_view_names": EXTRA_ACTIVE_VIEW_NAMES,
            "sort_order": SORT_ORDER,
            "enabled": True,
            "open_in_new_tab": False,
        },
    )


def unseed_iportal_nav(apps, schema_editor):
    NavMenuItem = apps.get_model("library", "NavMenuItem")
    # Leave a pre-existing or subsequently customized row alone. These exact
    # defaults identify the product row this migration is responsible for.
    NavMenuItem.objects.filter(
        url_name=URL_NAME,
        label="iPortal",
        parent__isnull=True,
        custom_url="",
        extra_active_view_names=EXTRA_ACTIVE_VIEW_NAMES,
        sort_order=SORT_ORDER,
        enabled=True,
        open_in_new_tab=False,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0089_voicetrack_media"),
    ]

    operations = [
        migrations.RunPython(seed_iportal_nav, unseed_iportal_nav),
    ]
