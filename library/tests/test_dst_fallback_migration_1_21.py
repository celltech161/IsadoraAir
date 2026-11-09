"""[P0] 1.21 corrections: migration library/0100 (DST replay provenance) is additive and r0130-compatible.

Runs Django's migration executor against the disposable test database: back to 0099, rows written as r0130 code
writes them, forward to 0100, and back again -- the r0130 rows survive untouched with NULL provenance, r0130-style
inserts keep working after the migration, and the new constraints hold.  The schema is always restored to the
latest state afterwards."""
from datetime import date, datetime, timezone as dt_timezone

from django.db import IntegrityError, connection, transaction
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
            self.migrate(executor_latest())
        finally:
            super().tearDown()

    def test_r0130_rows_and_inserts_survive_the_migration_and_its_reversal(self):
        old = self.migrate(BEFORE)
        kind = old.get_model("library", "CategoryKind").objects.create(code="mig-kind", name="Migration kind")
        category = old.get_model("library", "Category").objects.create(code="MIGCAT", name="Migration", kind=kind)
        artist = old.get_model("library", "Artist").objects.create(name="Migration Artist")
        track = old.get_model("library", "Track").objects.create(filepath="/tmp/1-21-migration.wav",
                                                                  filename="1-21-migration.wav", title="t",
                                                                  artist=artist, category=category)
        log = old.get_model("library", "PlaylistLog").objects.create(date=date(2026, 11, 1), hour=2, status="approved")
        OldLogItem = old.get_model("library", "LogItem")
        before = OldLogItem.objects.create(playlist_log=log, position=0, scheduled_time=datetime(2026, 11, 1, 8, tzinfo=UTC),
                                           track=track, category=category, origin="generated_schedule",
                                           played_at=datetime(2026, 11, 1, 7, 5, tzinfo=UTC))

        new = self.migrate(AFTER)
        LogItem = new.get_model("library", "LogItem")
        Pass = new.get_model("library", "DstFallbackReplayPass")
        row = LogItem.objects.get(pk=before.pk)
        self.assertEqual((row.origin, row.replay_pass_id, row.replay_source_id_snapshot),
                         ("generated_schedule", None, None))
        self.assertEqual(row.played_at, datetime(2026, 11, 1, 7, 5, tzinfo=UTC))     # history untouched
        # Code that predates the migration inserts without the new columns: still valid.
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO library_logitem (playlist_log_id, position, scheduled_time, track_id, track_title, "
                "track_artist, category_id, origin) VALUES (%s, 1, %s, %s, '', '', %s, 'engine_inserted')",
                [log.pk, datetime(2026, 11, 1, 8, 5, tzinfo=UTC), track.pk, category.pk])
        self.assertEqual(LogItem.objects.filter(playlist_log_id=log.pk).count(), 2)
        replay_pass = Pass.objects.create(playlist_log_id=log.pk, successor_hour_start=datetime(2026, 11, 1, 8, tzinfo=UTC),
                                          transition_at=datetime(2026, 11, 1, 7, tzinfo=UTC), time_zone="America/Chicago",
                                          authorized_at=datetime(2026, 11, 1, 7, 59, 40, tzinfo=UTC),
                                          source_log_item_ids=[before.pk], replay_count=1)
        LogItem.objects.create(playlist_log_id=log.pk, position=2, scheduled_time=row.scheduled_time, track_id=track.pk,
                               category_id=category.pk, origin="dst_replay", replay_pass=replay_pass,
                               replay_source_id_snapshot=before.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LogItem.objects.create(playlist_log_id=log.pk, position=3, scheduled_time=row.scheduled_time, track_id=track.pk,
                                   category_id=category.pk, origin="dst_replay", replay_pass=replay_pass,
                                   replay_source_id_snapshot=before.pk)       # one replay per original per pass
        with self.assertRaises(IntegrityError), transaction.atomic():
            Pass.objects.create(playlist_log_id=log.pk, successor_hour_start=datetime(2026, 11, 1, 8, tzinfo=UTC),
                                transition_at=datetime(2026, 11, 1, 7, tzinfo=UTC), time_zone="America/Chicago",
                                authorized_at=datetime(2026, 11, 1, 8, tzinfo=UTC), replay_count=0)

        # Reversal drops the new table and columns; every row, including the replay, remains an ordinary row ...
        old_again = self.migrate(BEFORE)
        rows = old_again.get_model("library", "LogItem").objects.filter(playlist_log_id=log.pk).order_by("position")
        self.assertEqual([r.origin for r in rows], ["generated_schedule", "engine_inserted", "dst_replay"])
        # ... and the migration re-applies over it (a rollback followed by a re-upgrade).
        again = self.migrate(AFTER)
        row = again.get_model("library", "LogItem").objects.get(playlist_log_id=log.pk, origin="dst_replay")
        self.assertEqual((row.replay_pass_id, row.replay_source_id_snapshot), (None, None))
        self.assertFalse(again.get_model("library", "DstFallbackReplayPass").objects.exists())


def executor_latest():
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return executor.loader.graph.leaf_nodes()
