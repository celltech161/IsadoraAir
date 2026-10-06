"""production.0001 is additive under the REAL runtime-11 classifier.

Nothing here weakens or re-implements the classifier: it calls the protected
updater's own probe functions (updatecenter_probe) against the actual on-disk
migration, both by exact-plan reconstruction and live over the real graph with
the migration unapplied.
"""
import ast
from pathlib import Path

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from updatecenter.management.commands import updatecenter_probe as probe

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0001_initial.py"
REF = "production.0001_initial"
EXPECTED_CONSTRAINTS = {
    "production_media_sha256_hex", "production_media_storage_key_shape", "production_media_byte_size_positive",
    "production_media_derived_requires_parent", "production_media_retention_consistent",
    "production_media_validation_consistent", "production_media_valid_has_facts",
}


class MigrationStructureTests(SimpleTestCase):
    def test_it_is_the_only_migration_and_contains_exactly_one_createmodel(self):
        names = sorted(path.name for path in MIGRATION.parent.glob("*.py"))
        self.assertEqual(names, ["0001_initial.py", "__init__.py"])
        tree = ast.parse(MIGRATION.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "migrations"]
        self.assertEqual(sorted(call.func.attr for call in calls), ["CreateModel", "swappable_dependency"])

    def test_no_code_no_sql_no_seed_data_anywhere_in_the_migration(self):
        text = MIGRATION.read_text()
        for forbidden in ("RunPython", "RunSQL", "RunSQL", "SeparateDatabaseAndState", "AddConstraint", "AddIndex",
                          "AlterField", "RemoveField", "DeleteModel", "RenameField", "Capability", "get_model"):
            self.assertNotIn(forbidden, text)

    def test_the_essential_integrity_invariants_are_in_0001(self):
        text = MIGRATION.read_text()
        for name in EXPECTED_CONSTRAINTS:
            self.assertIn(f"name='{name}'", text)
        self.assertIn("unique=True", text)                      # storage_key
        self.assertEqual(text.count("CheckConstraint"), len(EXPECTED_CONSTRAINTS))

    def test_it_is_atomic_and_depends_only_on_the_user_model(self):
        loader = MigrationLoader(None)
        migration = loader.disk_migrations[("production", "0001_initial")]
        self.assertTrue(migration.atomic)
        self.assertTrue(migration.initial)
        # swappable_dependency(AUTH_USER_MODEL): the auth app and nothing else.
        self.assertEqual([dependency[0] for dependency in migration.dependencies], ["auth"])
        self.assertEqual([getattr(dependency, "setting", None) for dependency in migration.dependencies],
                         ["auth.User"])


class MigrationDriftTests(TestCase):
    def test_the_model_state_matches_the_migration(self):
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)     # SystemExit(1) on any drift


class StaticClassificationTests(SimpleTestCase):
    def test_every_operation_of_the_exact_plan_is_additive_with_zero_manual_operations(self):
        loader = MigrationLoader(None)
        plan = probe._serialize_exact_plan(loader, [REF])
        self.assertEqual(len(plan), 1)
        self.assertEqual([op["operation"] for op in plan[0]["operations"]], ["CreateModel"])
        self.assertEqual([op["classification"] for op in plan[0]["operations"]], ["additive"])
        self.assertEqual(probe.extract_manual_operations(plan), [])

    def test_no_migration_authorization_companion_mentions_it(self):
        repo = Path(__file__).resolve().parents[2]
        for path in (repo / "deploy" / "migration_authorizations").glob("*.json"):
            self.assertNotIn("production.", path.read_text(), path.name)


class LivePlanClassificationTests(TransactionTestCase):
    """The probe exactly as the protected updater runs it: with the migration
    genuinely pending in a real database."""

    def test_the_pending_plan_is_one_additive_migration_and_no_manual_operations(self):
        call_command("migrate", "production", "zero", verbosity=0)
        try:
            payload = probe.build_probe_payload()
            refs = [item["ref"] for item in payload["plan"]]
            # 2.22B: library.0089 (VoiceTrack.media) depends on production.0001,
            # so unapplying production also unapplies it; production.0001 is
            # still planned first and judged on its own.
            self.assertEqual(refs[0], REF)
            self.assertEqual(set(refs) - {REF}, {"library.0089_voicetrack_media"})
            self.assertEqual(payload["conflicts"], {})
            self.assertEqual(probe.extract_manual_operations(payload["plan"]), [])
            self.assertEqual([op["classification"] for op in payload["plan"][0]["operations"]], ["additive"])
            self.assertTrue(all(dep.startswith("auth.") for dep in payload["plan"][0]["dependencies"]))
        finally:
            call_command("migrate", verbosity=0)           # every app back to its leaf, dependents included
