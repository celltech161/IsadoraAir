"""[P0] 1.21: migration library/0100 (DST replay marker) is additive and r0130-compatible, applied forward.

Runs Django's migration executor against the disposable test database: at the r0130 schema (0099) rows are written
as r0130 code writes them, then 0100 is applied -- the rows survive untouched with a NULL marker, and r0130-style
inserts (without the new column) keep working.  The schema is always restored to the latest state afterwards.
(The Update Center is forward-only; reversing 0100 is not a supported path -- docs/DST_FALLBACK_CONTINUITY.md.)"""
from datetime import date, datetime, timezone as dt_timezone

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("library", "0099_track_cue_marker_authority")]
AFTER = [("library", "0100_dst_fallback_replay_pass")]
UTC = dt_timezone.utc


class MigrationCompatibilityTests(TransactionTestCase):
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Always back to the latest schema, whatever the test left (the next test modules need it).
        try:
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            self.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDown()

    def test_r0130_rows_and_inserts_survive_the_forward_migration(self):
        old = self.migrate(BEFORE)
        kind = old.get_model("library", "CategoryKind").objects.create(code="mig-kind", name="Migration kind")
        category = old.get_model("library", "Category").objects.create(code="MIGCAT", name="Migration", kind=kind)
        artist = old.get_model("library", "Artist").objects.create(name="Migration Artist")
        track = old.get_model("library", "Track").objects.create(filepath="/tmp/1-21-migration.wav",
                                                                  filename="1-21-migration.wav", title="t",
                                                                  artist=artist, category=category)
        log = old.get_model("library", "PlaylistLog").objects.create(date=date(2026, 11, 1), hour=2, status="approved")
        before = old.get_model("library", "LogItem").objects.create(
            playlist_log=log, position=0, scheduled_time=datetime(2026, 11, 1, 8, tzinfo=UTC), track=track,
            category=category, origin="generated_schedule", played_at=datetime(2026, 11, 1, 7, 5, tzinfo=UTC))

        new = self.migrate(AFTER)
        PlaylistLog = new.get_model("library", "PlaylistLog")
        self.assertIsNone(PlaylistLog.objects.get(pk=log.pk).dst_replay_hour_start)      # nothing backfilled
        row = new.get_model("library", "LogItem").objects.get(pk=before.pk)
        self.assertEqual((row.origin, row.played_at),
                         ("generated_schedule", datetime(2026, 11, 1, 7, 5, tzinfo=UTC)))  # history untouched
        # Code that predates the migration inserts PlaylistLogs without the new column: still valid.
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO library_playlistlog (date, hour, generated_at, status) "
                           "VALUES (%s, 3, %s, 'draft') RETURNING id",
                           [date(2026, 11, 1), datetime(2026, 11, 1, 8, tzinfo=UTC)])
            inserted = cursor.fetchone()[0]
        self.assertIsNone(PlaylistLog.objects.get(pk=inserted).dst_replay_hour_start)
