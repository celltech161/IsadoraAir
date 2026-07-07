from django.conf import settings
from django.core.management.base import BaseCommand
from pathlib import Path

from library.models import Category


class Command(BaseCommand):
    help = (
        "Diff LIBRARY_ROOT's top-level folders against Category rows -- "
        "reports any folder with no matching category (import_songs skips "
        "these instead of crashing, since Category.kind can't be guessed "
        "from a folder name) and any category with no matching folder."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Directory to check. Defaults to LIBRARY_ROOT from settings.",
        )

    def handle(self, *args, **options):
        scan_path = options["path"] or getattr(settings, "LIBRARY_ROOT", None)
        if not scan_path:
            self.stderr.write("No path given and LIBRARY_ROOT is not set in settings.")
            return

        root = Path(scan_path)
        if not root.is_dir():
            self.stderr.write(f"Not a directory: {root}")
            return

        disk_folders = {p.name for p in root.iterdir() if p.is_dir()}
        db_codes = set(Category.objects.values_list("code", flat=True))

        orphan_folders = sorted(disk_folders - db_codes)
        orphan_categories = sorted(db_codes - disk_folders)

        if orphan_folders:
            self.stdout.write(self.style.WARNING("Folders with no matching Category (import_songs will skip these):"))
            for name in orphan_folders:
                self.stdout.write(f"  {name}")
        else:
            self.stdout.write(self.style.SUCCESS("Every folder has a matching Category."))

        self.stdout.write("")

        if orphan_categories:
            self.stdout.write(self.style.WARNING("Categories with no matching folder on disk:"))
            for code in orphan_categories:
                self.stdout.write(f"  {code}")
        else:
            self.stdout.write(self.style.SUCCESS("Every Category has a matching folder."))
