"""active_messages.0001 (D1) and 0002 (D2B playout correlation) are additive
under the REAL runtime-11 classifier (the protected updater's own probe
functions): CreateModel only, every constraint and index folded in. The capability seed (authz.0008, RunPython) is Phase
D1's ONE manual operation -- it needs a companion or local approval at release
time -- and is isolated in its own migration.

D3A (exact produced-content identity): produced_content.0001 is CreateModel
only (additive); active_messages.0003 (AddField + the compatibility-type
backfill, RunPython) and 0004 (NOT NULL, the uniqueness swap) are D3A's
manual operations -- exactly those, nothing else -- and the backfill is
proven against a D2B-era schema with real rows.
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
PLAYOUT = REPO / "active_messages" / "migrations" / "0002_playout_correlation.py"
SEED = REPO / "authz" / "migrations" / "0008_active_message_capabilities.py"
BACKFILL = REPO / "active_messages" / "migrations" / "0003_message_content_type.py"
IDENTITY = REPO / "active_messages" / "migrations" / "0004_message_content_type_identity.py"
PRODUCED = REPO / "produced_content" / "migrations" / "0001_initial.py"
REFS = ["active_messages.0001_initial", "active_messages.0002_playout_correlation"]
D3A_REFS = ["active_messages.0003_message_content_type", "active_messages.0004_message_content_type_identity"]
D3C_REFS = ["produced_content.0002_d3c_spoken_backend_and_test_take", "produced_content.0003_recipe_key_leading_digit",
            "produced_content.0004_backend_regular_only"]
D3C_MANUAL = [("produced_content.0003_recipe_key_leading_digit", "RemoveConstraint"),
              ("produced_content.0003_recipe_key_leading_digit", "AlterField"),
              ("produced_content.0003_recipe_key_leading_digit", "AddConstraint"),
              ("produced_content.0004_backend_regular_only", "AddConstraint")]
PRODUCED_REF = "produced_content.0001_initial"
SEED_REF = "authz.0008_active_message_capabilities"
D3A_MANUAL = [("active_messages.0003_message_content_type", "RunPython"),
              ("active_messages.0004_message_content_type_identity", "AlterField"),
              ("active_messages.0004_message_content_type_identity", "RemoveConstraint"),
              ("active_messages.0004_message_content_type_identity", "AddConstraint")]
# D3A replaced the D1 uniqueness; the new one is declared in 0004.
D3A_CONSTRAINTS = {"active_message_one_active_per_producer_type"}


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

    def test_the_playout_migration_is_createmodel_only(self):
        self.assertEqual(_migration_calls(PLAYOUT), ["CreateModel"] * 2)
        text = PLAYOUT.read_text()
        for forbidden in ("RunPython", "RunSQL", "SeparateDatabaseAndState", "AddConstraint", "AddIndex",
                          "AddField", "AlterField", "get_model"):
            self.assertNotIn(forbidden, text)
        for model in (domain.ActiveBroadcastPublication, domain.ActiveBroadcastInsertion):
            for name in _declared(model, "constraints") | _declared(model, "indexes"):
                self.assertIn(f"name='{name}'", text)

    def test_every_declared_constraint_and_index_is_in_the_initial_migration(self):
        text = INITIAL.read_text()
        names = set()
        for model in (domain.ActiveBroadcastPolicy, domain.ActiveBroadcastMessage, domain.ActiveBroadcastExecution,
                      domain.ActiveBroadcastMessageEvent):
            names |= _declared(model, "constraints") | _declared(model, "indexes")
        self.assertGreaterEqual(len(names), 40)
        for name in names - D3A_CONSTRAINTS:
            self.assertIn(f"name='{name}'", text)
        for name in D3A_CONSTRAINTS:
            self.assertIn(f'name="{name}"', IDENTITY.read_text())
        self.assertIn("name='active_message_one_active_per_producer_kind'", text)       # removed by 0004
        self.assertIn('name="active_message_one_active_per_producer_kind"', IDENTITY.read_text())

    def test_the_d3a_migrations_have_exactly_their_declared_operations(self):
        from produced_content import models as produced
        self.assertEqual(_migration_calls(PRODUCED), ["CreateModel"] * 2 + ["swappable_dependency"])
        # 2.22D3C added one constraint later, in its own migration (produced_content.0004).
        d3c = {"produced_type_backend_regular_only": PRODUCED.parent / "0004_backend_regular_only.py"}
        for name in (_declared(produced.ProducedContentType, "constraints")
                     | _declared(produced.ProducedContentTypeAssignment, "constraints")):
            self.assertIn(f"name='{name}'", d3c.get(name, PRODUCED).read_text())
        self.assertNotIn("produced_type_backend_regular_only", PRODUCED.read_text())
        self.assertEqual(_migration_calls(BACKFILL), ["AddField", "RunPython"])
        self.assertEqual(_migration_calls(IDENTITY), ["AddConstraint", "AlterField", "RemoveConstraint"])
        for forbidden in ("ogremote", "RunSQL", "SeparateDatabaseAndState"):
            self.assertNotIn(forbidden, BACKFILL.read_text().replace("legacy OGRemote", ""))

    def test_one_atomic_migration_each(self):
        loader = MigrationLoader(None)
        self.assertTrue(loader.disk_migrations[("active_messages", "0001_initial")].atomic)
        self.assertTrue(loader.disk_migrations[("authz", "0008_active_message_capabilities")].atomic)
        self.assertTrue(loader.disk_migrations[("active_messages", "0002_playout_correlation")].atomic)
        self.assertEqual(sorted(p.name for p in INITIAL.parent.glob("*.py")),
                         ["0001_initial.py", "0002_playout_correlation.py", "0003_message_content_type.py",
                          "0004_message_content_type_identity.py", "__init__.py"])
        self.assertEqual(_migration_calls(SEED), ["RunPython"])
        for ref in D3A_REFS + [PRODUCED_REF]:
            app, name = ref.split(".")
            self.assertTrue(loader.disk_migrations[(app, name)].atomic, ref)


class StaticClassificationTests(SimpleTestCase):
    def test_the_domain_schema_is_additive_with_zero_manual_operations(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), REFS)
        self.assertEqual([item["ref"] for item in plan], REFS)
        for item in plan:
            self.assertEqual({op["classification"] for op in item["operations"]}, {"additive"}, item["ref"])
        self.assertEqual(probe.extract_manual_operations(plan), [])

    def test_the_capability_seed_is_the_one_manual_operation(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), [SEED_REF])
        manual = probe.extract_manual_operations(plan)
        self.assertEqual([(entry["ref"], entry["operation"]) for entry in manual], [(SEED_REF, "RunPython")])

    def test_d3a_is_additive_except_its_backfill_and_identity_tightening(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), [PRODUCED_REF] + D3A_REFS)
        self.assertEqual([item["ref"] for item in plan], [PRODUCED_REF] + D3A_REFS)
        self.assertEqual({op["classification"] for op in plan[0]["operations"]}, {"additive"})
        manual = probe.extract_manual_operations(plan)
        self.assertEqual([(entry["ref"], entry["operation"]) for entry in manual], D3A_MANUAL)


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

    def test_the_pending_plan_is_additive_except_the_seed_and_the_d3a_identity(self):
        call_command("migrate", "active_messages", "zero", verbosity=0)
        call_command("migrate", "produced_content", "zero", verbosity=0)
        call_command("migrate", "authz", "0007", verbosity=0)
        try:
            payload = probe.build_probe_payload()
            # 2.22D3C: produced_content's later migrations (its own classifier test).
            self.assertEqual(sorted(item["ref"] for item in payload["plan"]),
                             sorted(REFS + D3A_REFS + [PRODUCED_REF, SEED_REF] + D3C_REFS))
            self.assertEqual(payload["conflicts"], {})
            manual = probe.extract_manual_operations(payload["plan"])
            self.assertEqual(sorted((entry["ref"], entry["operation"]) for entry in manual),
                             sorted([(SEED_REF, "RunPython")] + D3A_MANUAL + D3C_MANUAL))
        finally:
            call_command("migrate", verbosity=0)
        self.assertEqual(Capability.objects.filter(slug__startswith="active_message.").count(), 2)


class CompatibilityTypeTests(TestCase):
    """A fresh database (the test database is built by the migrations)."""

    def test_the_compatibility_types_exist_with_d1_authority(self):
        from produced_content.models import ProducedContentType
        rows = {row.key: row for row in ProducedContentType.objects.filter(key__in=("urgent_pa", "public_address"))}
        self.assertEqual({key: (row.delivery_mode, row.enabled, row.producer_self_publish, row.recallable,
                                row.replay_interval_seconds, row.target_category_id)
                          for key, row in rows.items()},
                         {"urgent_pa": ("urgent", True, False, False, None, None),
                          "public_address": ("public_address", True, False, False, None, None)})


class BackfillTests(TransactionTestCase):
    """An upgraded D1-D2B database: real rows written against the 0002 schema
    are backfilled to their exact compatibility type, deterministically, and
    keep everything else."""

    serialized_rollback = True

    def test_d1_d2b_rows_backfill_to_their_compatibility_types(self):
        import uuid

        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from django.utils import timezone

        from production.tests.support import VALID_FACTS, make_row
        call_command("migrate", "active_messages", "0002", verbosity=0)
        call_command("migrate", "produced_content", "zero", verbosity=0)
        try:
            state = MigrationExecutor(connection).loader.project_state(
                [("active_messages", "0002_playout_correlation")])
            Message = state.apps.get_model("active_messages", "ActiveBroadcastMessage")
            now = timezone.now()
            media = make_row(kind="recording", **{**VALID_FACTS, "decoded_duration_seconds": "5.000000",
                                                   "validated_at": now})
            base = dict(producer_key="user:7", producer_username="seven", state="prepared")
            active = dict(state="active", message_media_id=media.pk, activation_generation=1, activated_at=now,
                          replay_interval_seconds=1200, next_due_at=now)
            rows = {
                "urgent-prepared": dict(base, kind="urgent"),
                "urgent-active": dict(base, kind="urgent", **active),
                "pa-active": dict(base, kind="public_address", **active),
                "pa-cleared": dict(base, kind="public_address", state="cleared", terminal_at=now),
            }
            ids = {}
            for label, values in rows.items():
                ids[label] = uuid.uuid4()
                Message.objects.create(id=ids[label], **values)
            call_command("migrate", verbosity=0)
            from active_messages.models import ActiveBroadcastMessage
            got = {label: ActiveBroadcastMessage.objects.select_related("content_type").get(pk=pk)
                   for label, pk in ids.items()}
            self.assertEqual({label: (row.kind, row.content_type.key, row.state) for label, row in got.items()},
                             {"urgent-prepared": ("urgent", "urgent_pa", "prepared"),
                              "urgent-active": ("urgent", "urgent_pa", "active"),
                              "pa-active": ("public_address", "public_address", "active"),
                              "pa-cleared": ("public_address", "public_address", "cleared")})
            self.assertEqual(got["urgent-active"].message_media_id, media.pk)
            # The new producer slot is in force: the same producer/type cannot have two ACTIVE rows.
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(cursor, ActiveBroadcastMessage._meta.db_table)
            self.assertIn("active_message_one_active_per_producer_type", constraints)
            self.assertNotIn("active_message_one_active_per_producer_kind", constraints)
            column = ActiveBroadcastMessage._meta.get_field("content_type").column
            with connection.cursor() as cursor:
                cursor.execute("SELECT is_nullable FROM information_schema.columns "
                               "WHERE table_name = %s AND column_name = %s",
                               [ActiveBroadcastMessage._meta.db_table, column])
                self.assertEqual(cursor.fetchone()[0], "NO")
        finally:
            call_command("migrate", verbosity=0)
