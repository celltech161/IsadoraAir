"""active_messages.0001 is additive under the REAL runtime-11 classifier (the
protected updater's own probe functions): CreateModel only, every constraint
and index folded in. The capability seed (authz.0008, RunPython) is Phase
D1's ONE manual operation -- it needs a companion or local approval at release
time -- and is isolated in its own migration.
"""
import ast
from pathlib import Path

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from active_messages import models as domain
from authz.models import Capability, Role
from updatecenter.management.commands import updatecenter_probe as probe

REPO = Path(__file__).resolve().parents[2]
INITIAL = REPO / "active_messages" / "migrations" / "0001_initial.py"
SEED = REPO / "authz" / "migrations" / "0008_active_message_capabilities.py"
REFS = ["active_messages.0001_initial"]
SEED_REF = "authz.0008_active_message_capabilities"


def _migration_calls(path):
    tree = ast.parse(path.read_text())
    return sorted(node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "migrations")


def _declared(model, kind):
    return {item.name for item in getattr(model._meta, kind)}


class StructureTests(SimpleTestCase):
    def test_the_initial_migration_is_createmodel_only(self):
        self.assertEqual(_migration_calls(INITIAL), ["CreateModel"] * 4 + ["swappable_dependency"])
        text = INITIAL.read_text()
        for forbidden in ("RunPython", "RunSQL", "SeparateDatabaseAndState", "AddConstraint", "AddIndex",
                          "AddField", "AlterField", "Capability", "get_model"):
            self.assertNotIn(forbidden, text)

    def test_every_declared_constraint_and_index_is_in_the_initial_migration(self):
        text = INITIAL.read_text()
        names = set()
        for model in (domain.ActiveBroadcastPolicy, domain.ActiveBroadcastMessage, domain.ActiveBroadcastExecution,
                      domain.ActiveBroadcastMessageEvent):
            names |= _declared(model, "constraints") | _declared(model, "indexes")
        self.assertGreaterEqual(len(names), 40)
        for name in names:
            self.assertIn(f"name='{name}'", text)

    def test_one_atomic_migration_each(self):
        loader = MigrationLoader(None)
        self.assertTrue(loader.disk_migrations[("active_messages", "0001_initial")].atomic)
        self.assertTrue(loader.disk_migrations[("authz", "0008_active_message_capabilities")].atomic)
        self.assertEqual(sorted(p.name for p in INITIAL.parent.glob("*.py")), ["0001_initial.py", "__init__.py"])
        self.assertEqual(_migration_calls(SEED), ["RunPython"])


class StaticClassificationTests(SimpleTestCase):
    def test_the_domain_schema_is_additive_with_zero_manual_operations(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), REFS)
        self.assertEqual([item["ref"] for item in plan], REFS)
        self.assertEqual({op["classification"] for op in plan[0]["operations"]}, {"additive"})
        self.assertEqual(probe.extract_manual_operations(plan), [])

    def test_the_capability_seed_is_the_one_manual_operation(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), [SEED_REF])
        manual = probe.extract_manual_operations(plan)
        self.assertEqual([(entry["ref"], entry["operation"]) for entry in manual], [(SEED_REF, "RunPython")])


class SeedTests(TestCase):
    def test_station_administrator_holds_both_capabilities_and_nobody_else_does(self):
        for slug in ("active_message.produce", "active_message.control"):
            capability = Capability.objects.get(slug=slug)
            self.assertFalse(capability.requires_schedule)
            self.assertEqual(list(Role.objects.filter(capabilities=capability).values_list("name", flat=True)),
                             ["Station Administrator"])


class LivePlanClassificationTests(TransactionTestCase):
    """As the protected updater sees an r0109 station: both genuinely pending."""

    serialized_rollback = True

    def test_the_pending_plan_is_additive_except_the_seed(self):
        call_command("migrate", "active_messages", "zero", verbosity=0)
        call_command("migrate", "authz", "0007", verbosity=0)
        try:
            payload = probe.build_probe_payload()
            self.assertEqual(sorted(item["ref"] for item in payload["plan"]), sorted(REFS + [SEED_REF]))
            self.assertEqual(payload["conflicts"], {})
            manual = probe.extract_manual_operations(payload["plan"])
            self.assertEqual([(entry["ref"], entry["operation"]) for entry in manual], [(SEED_REF, "RunPython")])
        finally:
            call_command("migrate", verbosity=0)
        self.assertEqual(Capability.objects.filter(slug__startswith="active_message.").count(), 2)
