"""updatecenter/schema_health.py's own unit tests -- [P0] 1.1 correction.

Uses Django's own MigrationRecorder to deterministically simulate
"a migration is pending" (bookkeeping only, no raw SQL, never a
DB-error-string match) rather than relying on the test database's
incidental applied-state."""
from unittest.mock import patch

from django.db import OperationalError, connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase

from updatecenter import schema_health


def _leaf(app_label):
    """The app's CURRENT leaf migration. check_schema_health() plans to
    the graph's leaf nodes, so only an unapplied leaf (or chain ending in
    one) is "pending"; a hard-coded name silently stops being a leaf the
    moment the app gains a newer migration (webrequests.0010, r0056)."""
    (leaf,) = MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes(app_label)
    return leaf


class SchemaHealthTests(TestCase):
    def test_schema_current_when_fully_migrated(self):
        """`manage.py test` fully migrates the test database by
        default -- this is the normal state for this whole suite."""
        result = schema_health.check_schema_health()
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.SCHEMA_CURRENT)
        self.assertEqual(result.pending_migrations, ())

    def test_unapplied_migration_detected_deterministically(self):
        recorder = MigrationRecorder(connection)
        app, name = _leaf("webrequests")
        recorder.record_unapplied(app, name)
        try:
            result = schema_health.check_schema_health()
            self.assertEqual(result.status, schema_health.SchemaHealthStatus.UNAPPLIED_MIGRATIONS_DETECTED)
            self.assertIn(f"{app}.{name}", result.pending_migrations)
        finally:
            recorder.record_applied(app, name)

    def test_multiple_unapplied_migrations_all_listed(self):
        recorder = MigrationRecorder(connection)
        targets = [_leaf("webrequests"), _leaf("road_conditions")]
        for app, name in targets:
            recorder.record_unapplied(app, name)
        try:
            result = schema_health.check_schema_health()
            self.assertEqual(result.status, schema_health.SchemaHealthStatus.UNAPPLIED_MIGRATIONS_DETECTED)
            for app, name in targets:
                self.assertIn(f"{app}.{name}", result.pending_migrations)
        finally:
            for app, name in targets:
                recorder.record_applied(app, name)

    def test_never_raises_on_internal_error(self):
        """MIGRATION_STATE_INDETERMINATE, not an exception, on failure
        -- /updates/ must render even if this check itself breaks."""
        from unittest.mock import patch
        with patch("updatecenter.schema_health.MigrationExecutor", side_effect=RuntimeError("boom")):
            result = schema_health.check_schema_health()
            self.assertEqual(result.status, schema_health.SchemaHealthStatus.MIGRATION_STATE_INDETERMINATE)
            self.assertEqual(result.pending_migrations, ())

    def test_result_restored_after_reapplying(self):
        """Confirms the recorder round-trip itself is clean -- the
        fixture technique other tests rely on actually works both ways."""
        recorder = MigrationRecorder(connection)
        app, name = _leaf("webrequests")
        recorder.record_unapplied(app, name)
        recorder.record_applied(app, name)
        result = schema_health.check_schema_health()
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.SCHEMA_CURRENT)


def _interior(app_label):
    """A NON-leaf migration of `app_label` whose own dependent (the app's
    current leaf) stays recorded as applied: the parent of the leaf within the
    same app. Marking it unapplied is exactly the inconsistent history
    `migrate` refuses, and exactly what a leaf-only plan cannot see."""
    graph = MigrationLoader(None, ignore_no_migrations=True).graph
    leaf = _leaf(app_label)
    parents = sorted(p.key for p in graph.node_map[leaf].parents if p.key[0] == app_label)
    assert parents, f"{app_label} has no same-app parent of its leaf; pick another app"
    return leaf, parents[0]


class InconsistentMigrationHistoryTests(TestCase):
    """Roadmap 1.22: a missing recorder row for a migration whose dependent is
    still recorded as applied is NOT healthy and NOT "pending". The check
    consults Django's own `check_consistent_history`; nothing here repairs the
    recorder, and every manipulation is bookkeeping in this test transaction."""

    def _unapply(self, app_label, name):
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied(app_label, name)
        return recorder

    def test_status_value_is_stable(self):
        self.assertEqual(schema_health.SchemaHealthStatus.INCONSISTENT_MIGRATION_HISTORY, "inconsistent_migration_history")

    def test_missing_library_leaf_remains_a_legitimate_pending_migration(self):
        recorder = MigrationRecorder(connection)
        app, name = _leaf("library")
        recorder.record_unapplied(app, name)
        try:
            result = schema_health.check_schema_health()
            self.assertEqual(result.status, schema_health.SchemaHealthStatus.UNAPPLIED_MIGRATIONS_DETECTED)
            self.assertEqual(result.pending_migrations, (f"{app}.{name}",))
        finally:
            recorder.record_applied(app, name)

    def test_consistent_unapplied_prefix_remains_pending_not_inconsistent(self):
        """Leaf AND its parent both unapplied is a consistent applied prefix
        (nothing applied depends on a missing record): it is ordinary pending
        work, listed in plan order."""
        (leaf_app, leaf_name), (_, parent_name) = _interior("library")
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied(leaf_app, leaf_name)
        recorder.record_unapplied(leaf_app, parent_name)
        try:
            result = schema_health.check_schema_health()
            self.assertEqual(result.status, schema_health.SchemaHealthStatus.UNAPPLIED_MIGRATIONS_DETECTED)
            self.assertEqual(result.pending_migrations, (f"{leaf_app}.{parent_name}", f"{leaf_app}.{leaf_name}"))
        finally:
            recorder.record_applied(leaf_app, parent_name)
            recorder.record_applied(leaf_app, leaf_name)

    def _assert_inconsistent(self, app_label):
        (leaf_app, leaf_name), (_, parent_name) = _interior(app_label)
        recorder = self._unapply(leaf_app, parent_name)
        try:
            result = schema_health.check_schema_health()
        finally:
            recorder.record_applied(leaf_app, parent_name)
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.INCONSISTENT_MIGRATION_HISTORY)
        # Not a legitimate pending migration: the recorder row is missing, nothing is "to be applied".
        self.assertEqual(result.pending_migrations, ())
        # Django's own description names the applied dependent and the missing prerequisite.
        self.assertIn(f"{leaf_app}.{leaf_name}", result.detail)
        self.assertIn(f"{leaf_app}.{parent_name}", result.detail)
        self.assertIn("applied before its dependency", result.detail)
        self.assertNotEqual(result.status, schema_health.SchemaHealthStatus.SCHEMA_CURRENT)

    def test_missing_nonleaf_library_migration_with_applied_descendant_is_inconsistent(self):
        self._assert_inconsistent("library")

    def test_missing_nonleaf_migration_in_another_app_is_inconsistent(self):
        self._assert_inconsistent("webrequests")

    def test_recorder_round_trip_restores_current(self):
        (leaf_app, _), (_, parent_name) = _interior("library")
        recorder = self._unapply(leaf_app, parent_name)
        recorder.record_applied(leaf_app, parent_name)
        self.assertEqual(schema_health.check_schema_health().status, schema_health.SchemaHealthStatus.SCHEMA_CURRENT)

    def test_unexpected_consistency_check_failure_is_indeterminate_not_current(self):
        with patch("django.db.migrations.loader.MigrationLoader.check_consistent_history", side_effect=RuntimeError("boom")):
            result = schema_health.check_schema_health()
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.MIGRATION_STATE_INDETERMINATE)
        self.assertEqual(result.pending_migrations, ())

    def test_database_failure_remains_indeterminate(self):
        with patch("updatecenter.schema_health.MigrationExecutor", side_effect=OperationalError("database unavailable")):
            result = schema_health.check_schema_health()
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.MIGRATION_STATE_INDETERMINATE)
        self.assertEqual(result.pending_migrations, ())

    def test_broken_migration_graph_remains_indeterminate(self):
        with patch("django.db.migrations.loader.MigrationLoader.build_graph", side_effect=RuntimeError("graph is broken")):
            result = schema_health.check_schema_health()
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.MIGRATION_STATE_INDETERMINATE)

    def test_never_modifies_the_recorder(self):
        """check_schema_health is read-only: the recorder rows are identical before and after, for both a healthy and an inconsistent history."""
        recorder = MigrationRecorder(connection)
        (leaf_app, _), (_, parent_name) = _interior("library")
        before = set(recorder.applied_migrations())
        schema_health.check_schema_health()
        recorder.record_unapplied(leaf_app, parent_name)
        degraded = set(recorder.applied_migrations())
        schema_health.check_schema_health()
        self.assertEqual(set(recorder.applied_migrations()), degraded)
        recorder.record_applied(leaf_app, parent_name)
        self.assertEqual(set(recorder.applied_migrations()), before)
