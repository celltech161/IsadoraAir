"""spoken.0001 and production.0002 are additive under the REAL runtime-11
classifier (the protected updater's own probe functions), with zero manual
operations -- statically and live with the migrations genuinely pending.

The classifier allows only CreateModel and safe AddField; a standalone
AddConstraint/AddIndex is MANUAL. So every constraint and index is folded
into its CreateModel, and the models are created in dependency order so no
foreign key needs a later AddField.
"""
import ast
from pathlib import Path

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TransactionTestCase

from updatecenter.management.commands import updatecenter_probe as probe

REPO = Path(__file__).resolve().parents[2]
SPOKEN = REPO / "spoken" / "migrations" / "0001_initial.py"
PRODUCTION = REPO / "production" / "migrations" / "0002_referenceaudio.py"
REFS = ["production.0002_referenceaudio", "spoken.0001_initial"]
SPOKEN_CONSTRAINTS = {
    "spoken_type_code_shape", "spoken_type_recipe_required", "spoken_type_min_duration_positive",
    "spoken_type_max_duration_positive", "spoken_type_duration_order", "spoken_type_max_bytes_positive",
    "spoken_item_lease_pair", "spoken_item_import_identity", "spoken_item_import_provenance_consistent",
    "spoken_item_attempts_bounded",
    "spoken_asset_one_current_edition_per_slot", "spoken_asset_one_accumulating_per_item",
    "spoken_asset_item_matches_mode", "spoken_asset_retired_consistent", "spoken_asset_slot_key_shape",
    "spoken_asset_library_relpath_shape",
    "spoken_edition_one_active_per_asset", "spoken_edition_unique_generation", "spoken_edition_library_sha256_hex",
    "spoken_edition_active_promoted", "spoken_edition_superseded_dated",
}
SPOKEN_INDEXES = {"spoken_item_state_lease", "spoken_item_type_state", "spoken_item_submitter",
                  "spoken_asset_type_state", "spoken_event_item_time"}


def _migration_calls(path):
    tree = ast.parse(path.read_text())
    return sorted(node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "migrations")


class StructureTests(SimpleTestCase):
    def test_only_createmodel_operations(self):
        self.assertEqual(_migration_calls(SPOKEN), ["CreateModel"] * 5 + ["swappable_dependency"])
        self.assertEqual(_migration_calls(PRODUCTION), ["CreateModel", "swappable_dependency"])
        for path in (SPOKEN, PRODUCTION):
            text = path.read_text()
            for forbidden in ("RunPython", "RunSQL", "SeparateDatabaseAndState", "AddConstraint", "AddIndex",
                              "AddField", "AlterField", "Capability", "get_model"):
                self.assertNotIn(forbidden, text, path.name)

    def test_every_permanent_invariant_is_in_the_initial_migration(self):
        text = SPOKEN.read_text()
        for name in SPOKEN_CONSTRAINTS | SPOKEN_INDEXES:
            self.assertIn(f"name='{name}'", text)
        self.assertEqual(text.count("CheckConstraint") + text.count("UniqueConstraint"), len(SPOKEN_CONSTRAINTS))
        self.assertIn("production_reference_one_current_per_key", PRODUCTION.read_text())

    def test_atomic_and_spoken_has_exactly_one_migration(self):
        loader = MigrationLoader(None)
        for app, name in (("spoken", "0001_initial"), ("production", "0002_referenceaudio")):
            self.assertTrue(loader.disk_migrations[(app, name)].atomic)
        self.assertEqual(sorted(p.name for p in SPOKEN.parent.glob("*.py")), ["0001_initial.py", "__init__.py"])


class StaticClassificationTests(SimpleTestCase):
    def test_every_operation_is_additive_with_zero_manual_operations(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), REFS)
        self.assertEqual([item["ref"] for item in plan], REFS)
        for item in plan:
            self.assertEqual({op["classification"] for op in item["operations"]}, {"additive"}, item["ref"])
        self.assertEqual(probe.extract_manual_operations(plan), [])

    def test_no_migration_authorization_companion_mentions_them(self):
        for path in (REPO / "deploy" / "migration_authorizations").glob("*.json"):
            text = path.read_text()
            self.assertNotIn("spoken.", text, path.name)
            self.assertNotIn("0002_referenceaudio", text, path.name)


class LivePlanClassificationTests(TransactionTestCase):
    """As the protected updater sees an r0108 station: both genuinely pending."""

    def test_the_pending_plan_has_zero_manual_operations(self):
        call_command("migrate", "spoken", "zero", verbosity=0)
        call_command("migrate", "production", "0001", verbosity=0)
        try:
            payload = probe.build_probe_payload()
            # 2.22D1: active_messages.0001 depends on production.0002, so unapplying
            # production.0002 unapplies it too (it has its own classifier test).
            # 2.22D2B: and active_messages.0002 after it.
            # 2.22D3A: and active_messages.0003/0004 -- whose backfill/identity
            # operations are D3A's own manual ones (active_messages has its own
            # classifier test); spoken and production stay purely additive.
            # 2.22D3C: and produced_content.0002 (the spoken backend link, on
            # spoken.0001 / production.0002) with 0003 -- the recipe-key pattern
            # correction, and 0004 -- backend implies regular -- D3C's own manual
            # operations (produced_content's test).
            self.assertEqual([item["ref"] for item in payload["plan"]],
                             ["production.0002_referenceaudio", "active_messages.0001_initial",
                              "active_messages.0002_playout_correlation", "active_messages.0003_message_content_type",
                              "active_messages.0004_message_content_type_identity", "spoken.0001_initial",
                              "produced_content.0002_d3c_spoken_backend_and_test_take",
                              "produced_content.0003_recipe_key_leading_digit",
                              "produced_content.0004_backend_regular_only"])
            self.assertEqual(payload["conflicts"], {})
            self.assertEqual([(entry["ref"], entry["operation"])
                              for entry in probe.extract_manual_operations(payload["plan"])],
                             [("active_messages.0003_message_content_type", "RunPython"),
                              ("active_messages.0004_message_content_type_identity", "AlterField"),
                              ("active_messages.0004_message_content_type_identity", "RemoveConstraint"),
                              ("active_messages.0004_message_content_type_identity", "AddConstraint"),
                              ("produced_content.0003_recipe_key_leading_digit", "RemoveConstraint"),
                              ("produced_content.0003_recipe_key_leading_digit", "AlterField"),
                              ("produced_content.0003_recipe_key_leading_digit", "AddConstraint"),
                              ("produced_content.0004_backend_regular_only", "AddConstraint")])
            for item in payload["plan"]:
                if not item["ref"].startswith(("active_messages.0003", "active_messages.0004",
                                               "produced_content.0003", "produced_content.0004")):
                    self.assertEqual({op["classification"] for op in item["operations"]}, {"additive"}, item["ref"])
        finally:
            call_command("migrate", verbosity=0)
