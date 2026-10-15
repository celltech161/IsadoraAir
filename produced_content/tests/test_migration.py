"""D3C migrations, as the protected updater's real classifier sees them:

* produced_content.0002 -- purely ADDITIVE: the nullable explicit backend link
  (AddField) and the TEST take model with its constraints folded into the
  CreateModel; no data migration, no station seed;
* produced_content.0003 -- the recipe-key pattern correction (D3A's pattern
  refused the accepted recipe ``3min_ctr``): RemoveConstraint, AlterField
  (validator), AddConstraint -- MANUAL operations a release must authorize.
  Widening only: every existing row satisfies the new constraint.
* produced_content.0004 (review correction) -- the database guard of the
  bridge-only link: a spoken backend implies a regular type (AddConstraint:
  MANUAL). Every existing row satisfies it."""
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from produced_content.models import ProducedContentType
from updatecenter.management.commands import updatecenter_probe as probe

from .support import category

D3C_REFS = ["produced_content.0002_d3c_spoken_backend_and_test_take", "produced_content.0003_recipe_key_leading_digit",
            "produced_content.0004_backend_regular_only"]


class ClassificationTests(SimpleTestCase):
    def test_0002_is_additive_and_0003_0004_are_exactly_four_manual_operations(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), D3C_REFS)
        self.assertEqual([item["ref"] for item in plan], D3C_REFS)
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]],
                         [("AddField", "additive"), ("CreateModel", "additive")])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[1]["operations"]],
                         [("RemoveConstraint", "manual"), ("AlterField", "manual"), ("AddConstraint", "manual")])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[2]["operations"]],
                         [("AddConstraint", "manual")])
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in probe.extract_manual_operations(plan)],
                         [(D3C_REFS[1], 0, "RemoveConstraint"), (D3C_REFS[1], 1, "AlterField"),
                          (D3C_REFS[1], 2, "AddConstraint"), (D3C_REFS[2], 0, "AddConstraint")])

    def test_the_migrations_depend_only_on_what_they_reference(self):
        loader = MigrationLoader(None)
        second = loader.get_migration("produced_content", "0002_d3c_spoken_backend_and_test_take")
        self.assertEqual(sorted(second.dependencies),
                         sorted([("produced_content", "0001_initial"), ("production", "0002_referenceaudio"),
                                 ("spoken", "0001_initial"), ("auth", "__first__")]))
        third = loader.get_migration("produced_content", "0003_recipe_key_leading_digit")
        self.assertEqual(third.dependencies, [("produced_content", "0002_d3c_spoken_backend_and_test_take")])
        fourth = loader.get_migration("produced_content", "0004_backend_regular_only")
        self.assertEqual(fourth.dependencies, [("produced_content", "0003_recipe_key_leading_digit")])

    def test_no_data_migration_and_no_station_seed(self):
        loader = MigrationLoader(None)
        for ref in D3C_REFS:
            migration = loader.get_migration(*ref.split("."))
            self.assertFalse([op for op in migration.operations if type(op).__name__ in ("RunPython", "RunSQL")])


class ConstraintTests(TestCase):
    def test_the_database_accepts_a_leading_digit_and_still_refuses_paths_and_imports(self):
        row = ProducedContentType.objects.create(key="ctr", name="ctr", delivery_mode="regular",
                                                 output_mode="accumulating", target_category=category("PCCTR"),
                                                 recipe_key="lcl_psa")
        ProducedContentType.objects.filter(pk=row.pk).update(recipe_key="3min_ctr")      # the CHECK, not clean()
        for bad in ("3min.ctr", "processors/3min_ctr", "Recipe", "_x"):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                ProducedContentType.objects.filter(pk=row.pk).update(recipe_key=bad)


class UpgradeTests(TransactionTestCase):
    """A D3A-schema database (produced_content at 0001) with station rows:
    0002/0003 apply cleanly and keep every row (no backend link, no take)."""

    serialized_rollback = True

    def test_d3a_rows_survive_the_upgrade_unchanged(self):
        call_command("migrate", "produced_content", "0001", verbosity=0)
        try:
            state = MigrationExecutor(connection).loader.project_state([("produced_content", "0001_initial")])
            Type = state.apps.get_model("produced_content", "ProducedContentType")
            Category = state.apps.get_model("library", "Category")
            Kind = state.apps.get_model("library", "CategoryKind")
            kind = Kind.objects.create(code="pcupg", name="pcupg")
            target = Category.objects.create(code="PCUPG", name="PCUPG", kind=kind)
            Type.objects.create(key="old_news", name="Old News", delivery_mode="regular", output_mode="single",
                                recipe_key="news_item", target_category=target)
            Type.objects.create(key="practice_old", name="Practice", delivery_mode="test")
            call_command("migrate", "produced_content", verbosity=0)
            rows = {row.key: row for row in ProducedContentType.objects.filter(key__in=("old_news", "practice_old"))}
            self.assertEqual({key: (row.delivery_mode, row.recipe_key, row.spoken_content_type_id)
                              for key, row in rows.items()},
                             {"old_news": ("regular", "news_item", None), "practice_old": ("test", "", None)})
            self.assertFalse(rows["practice_old"].test_takes.exists())
        finally:
            call_command("migrate", verbosity=0)
