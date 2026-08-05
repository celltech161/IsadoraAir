"""Tests for the KanDrive library Category: exact code/name, idempotent
creation (both via the real migration path having already run, and by
directly re-invoking the migration's own RunPython functions), and that
an operator's pre-existing manually-created category is reused rather
than duplicated. See library/migrations/0076_add_kandrive_category.py."""
import importlib

from django.apps import apps
from django.test import TestCase

from library.models import Category, CategoryKind


def _load_kandrive_migration():
    # Migration filenames aren't valid Python identifiers (leading
    # digits) so they can't be `import`ed with the normal statement --
    # importlib.import_module works fine with the literal string,
    # exactly how Django's own migration loader does it internally.
    return importlib.import_module("library.migrations.0076_add_kandrive_category")


class KanDriveCategoryMigrationTests(TestCase):
    def test_category_exists_with_exact_code_after_migrations(self):
        """The real migration has already run for this test database
        (Django applies the full migration graph before tests run) --
        this confirms the actual end state, not just the RunPython
        functions in isolation."""
        category = Category.objects.get(code="KanDrive")
        self.assertEqual(category.code, "KanDrive")
        self.assertEqual(category.name, "KanDrive")
        self.assertEqual(category.kind.code, "spot")

    def test_no_case_or_whitespace_variant_duplicate_exists(self):
        codes = list(Category.objects.filter(name__iexact="kandrive").values_list("code", flat=True))
        self.assertEqual(codes, ["KanDrive"])

    def test_create_category_is_idempotent(self):
        module = _load_kandrive_migration()
        # Already created by the real migration -- calling create_category
        # again directly must not raise or create a duplicate row.
        module.create_category(apps, None)
        module.create_category(apps, None)
        self.assertEqual(Category.objects.filter(code="KanDrive").count(), 1)

    def test_create_category_reuses_existing_manual_category(self):
        """Simulates an operator having already created a "KanDrive"
        category by hand before this migration ran -- create_category
        must reuse that existing row (matched by code), not error and
        not create a second row differing only in name/whitespace."""
        Category.objects.filter(code="KanDrive").delete()
        kind = CategoryKind.objects.get(code="spot")
        manual = Category.objects.create(code="KanDrive", name="KanDrive (manually created)", kind=kind)

        module = _load_kandrive_migration()
        module.create_category(apps, None)

        self.assertEqual(Category.objects.filter(code="KanDrive").count(), 1)
        reused = Category.objects.get(code="KanDrive")
        self.assertEqual(reused.id, manual.id)
        # get_or_create's defaults are only applied on CREATE -- an
        # existing row's own name is never overwritten.
        self.assertEqual(reused.name, "KanDrive (manually created)")

    def test_reverse_delete_removes_only_kandrive(self):
        module = _load_kandrive_migration()
        other_count_before = Category.objects.exclude(code="KanDrive").count()

        module.reverse_delete(apps, None)

        self.assertFalse(Category.objects.filter(code="KanDrive").exists())
        self.assertEqual(Category.objects.exclude(code="KanDrive").count(), other_count_before)
