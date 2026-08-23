"""updatecenter/schema_health.py's own unit tests -- [P0] 1.1 correction.

Uses Django's own MigrationRecorder to deterministically simulate
"a migration is pending" (bookkeeping only, no raw SQL, never a
DB-error-string match) rather than relying on the test database's
incidental applied-state."""
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase

from updatecenter import schema_health


class SchemaHealthTests(TestCase):
    def test_schema_current_when_fully_migrated(self):
        """`manage.py test` fully migrates the test database by
        default -- this is the normal state for this whole suite."""
        result = schema_health.check_schema_health()
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.SCHEMA_CURRENT)
        self.assertEqual(result.pending_migrations, ())

    def test_unapplied_migration_detected_deterministically(self):
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied("webrequests", "0008_webrequestconfig_dedication_tts")
        try:
            result = schema_health.check_schema_health()
            self.assertEqual(result.status, schema_health.SchemaHealthStatus.UNAPPLIED_MIGRATIONS_DETECTED)
            self.assertIn("webrequests.0008_webrequestconfig_dedication_tts", result.pending_migrations)
        finally:
            recorder.record_applied("webrequests", "0008_webrequestconfig_dedication_tts")

    def test_multiple_unapplied_migrations_all_listed(self):
        recorder = MigrationRecorder(connection)
        targets = [
            ("webrequests", "0008_webrequestconfig_dedication_tts"),
            ("road_conditions", "0010_roadconditionsconfiguration_tts"),
        ]
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
        recorder.record_unapplied("webrequests", "0008_webrequestconfig_dedication_tts")
        recorder.record_applied("webrequests", "0008_webrequestconfig_dedication_tts")
        result = schema_health.check_schema_health()
        self.assertEqual(result.status, schema_health.SchemaHealthStatus.SCHEMA_CURRENT)
