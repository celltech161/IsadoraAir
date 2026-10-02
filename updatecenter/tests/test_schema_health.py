"""updatecenter/schema_health.py's own unit tests -- [P0] 1.1 correction.

Uses Django's own MigrationRecorder to deterministically simulate
"a migration is pending" (bookkeeping only, no raw SQL, never a
DB-error-string match) rather than relying on the test database's
incidental applied-state."""
from django.db import connection
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
