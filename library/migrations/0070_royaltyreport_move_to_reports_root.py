"""Move RoyaltyReport files from MEDIA_ROOT/royalty_reports/ to
REPORTS_ROOT (default /var/lib/isadoraair/reports/).

Two changes:
  1. Schema: FileField.storage swaps from Django's default (MEDIA_ROOT)
     to library.storage.royalty_report_storage (REPORTS_ROOT), and
     upload_to drops the "royalty_reports/" prefix because the storage
     is now already rooted at the reports directory.
  2. Data: for every existing RoyaltyReport row with a .file, strip
     the "royalty_reports/" prefix from file.name and physically move
     the file from the old MEDIA_ROOT/royalty_reports/<name> path to
     the new REPORTS_ROOT/<name> path.

Idempotent: files already at the new path (or already-stripped names)
are left alone. Missing on-disk files are logged and skipped rather
than aborting the migration -- the DB row is preserved but its file
handle will read as empty.
"""

import os
import shutil

from django.conf import settings
from django.db import migrations, models

import library.storage


_OLD_PREFIX = "royalty_reports/"


def _forward(apps, schema_editor):
    RoyaltyReport = apps.get_model("library", "RoyaltyReport")
    old_root = os.path.join(str(settings.MEDIA_ROOT), "royalty_reports")
    new_root = str(settings.REPORTS_ROOT)
    os.makedirs(new_root, exist_ok=True)

    for rr in RoyaltyReport.objects.exclude(file="").exclude(file__isnull=True):
        old_name = rr.file.name or ""
        if not old_name:
            continue

        new_name = (
            old_name[len(_OLD_PREFIX):]
            if old_name.startswith(_OLD_PREFIX)
            else old_name
        )

        old_path = os.path.join(str(settings.MEDIA_ROOT), old_name)
        new_path = os.path.join(new_root, new_name)

        if os.path.exists(old_path) and not os.path.exists(new_path):
            os.makedirs(os.path.dirname(new_path) or new_root, exist_ok=True)
            shutil.move(old_path, new_path)
            print(f"  moved {old_path} -> {new_path}")
        elif not os.path.exists(old_path) and not os.path.exists(new_path):
            print(f"  WARN: neither old nor new file found for "
                  f"RoyaltyReport id={rr.pk} ({old_name}); DB row kept.")

        if old_name != new_name:
            rr.file.name = new_name
            rr.save(update_fields=["file"])

    # Clean up the (now empty) old directory if it exists.
    if os.path.isdir(old_root) and not os.listdir(old_root):
        try:
            os.rmdir(old_root)
        except OSError:
            pass


def _reverse(apps, schema_editor):
    RoyaltyReport = apps.get_model("library", "RoyaltyReport")
    new_root = str(settings.REPORTS_ROOT)
    old_root = os.path.join(str(settings.MEDIA_ROOT), "royalty_reports")
    os.makedirs(old_root, exist_ok=True)

    for rr in RoyaltyReport.objects.exclude(file="").exclude(file__isnull=True):
        current_name = rr.file.name or ""
        if not current_name:
            continue

        if current_name.startswith(_OLD_PREFIX):
            continue

        old_style_name = _OLD_PREFIX + current_name

        new_path = os.path.join(new_root, current_name)
        old_path = os.path.join(str(settings.MEDIA_ROOT), old_style_name)

        if os.path.exists(new_path) and not os.path.exists(old_path):
            os.makedirs(os.path.dirname(old_path) or old_root, exist_ok=True)
            shutil.move(new_path, old_path)

        rr.file.name = old_style_name
        rr.save(update_fields=["file"])


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0069_voicetracks_page_access_and_nav"),
    ]

    operations = [
        migrations.AlterField(
            model_name="royaltyreport",
            name="file",
            field=models.FileField(
                blank=True,
                null=True,
                storage=library.storage.royalty_report_storage,
                upload_to="",
            ),
        ),
        migrations.RunPython(_forward, _reverse),
    ]
