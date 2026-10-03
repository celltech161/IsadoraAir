"""P1 1.17 -- protocol-6 safety boundaries proven against REAL PostgreSQL.

* the protected runtime's psql recorder observation (S3 row identity) and
  its read-only session;
* the read-only migration preflight transaction;
* library.schedule_block_duplicate_times at every schedule-schema stage
  (S2): pre-0086, 0086 (profile-less rows), 0087 (no uniqueness yet), and
  the current schema;
* exact-plan digest reconstruction on the real migration graph.

All run inside the isolated `test_*` database via `manage.py test`.
"""
from datetime import time
import os
from pathlib import Path
import tempfile
from unittest import mock
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from updatecenter.management.commands import updatecenter_migration_preflight as preflight
from updatecenter.management.commands.updatecenter_probe import build_probe_payload

from .phase_b_helpers import config_dict, orm_migration_records
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import ExecutionError, Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner

CHECK = "library.schedule_block_duplicate_times"


def _library_targets(leaf):
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    return leaves, [("library", leaf) if app == "library" else (app, name) for app, name in leaves]


class RecorderObservationTests(TransactionTestCase):
    """Executor._observe_migration_records, through the real psql binary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = connection.settings_dict
        pgpass = root / "pgpass"
        fd = os.open(pgpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(":".join(
                str(value).replace("\\", "\\\\").replace(":", "\\:")
                for value in (settings["HOST"], settings["PORT"], settings["NAME"], settings["USER"], settings["PASSWORD"])
            ) + "\n")
        raw = config_dict(root, str(root / "upstream.git"))
        raw["database"] = {
            "name": settings["NAME"], "user": settings["USER"], "host": settings["HOST"],
            "port": int(settings["PORT"]), "pgpass_file": str(pgpass),
        }
        config = validate_config_dict(raw, allow_local_repository=True)
        self.store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
        self.executor = Executor(config, self.store, CommandRunner())
        self.refs = [
            "ogremote.0001_initial", "ogremote.0003_ogremoteconfig_poll_interval_minutes",
            "library.0088_enforce_schedule_profile_integrity", "nonexistent.0001_never",
        ]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_psql_observation_equals_the_real_recorder_rows(self):
        observed = self.executor._observe_migration_records(self.refs)
        self.assertEqual(observed, orm_migration_records(self.refs))
        self.assertEqual(set(observed), set(self.refs[:3]))

    def test_unapply_then_reapply_changes_the_observed_row_identity(self):
        ref = "ogremote.0003_ogremoteconfig_poll_interval_minutes"
        before = self.executor._observe_migration_records([ref])[ref]
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied("ogremote", "0003_ogremoteconfig_poll_interval_minutes")
        self.assertEqual(self.executor._observe_migration_records([ref]), {})
        recorder.record_applied("ogremote", "0003_ogremoteconfig_poll_interval_minutes")
        after = self.executor._observe_migration_records([ref])[ref]
        self.assertNotEqual(after["id"], before["id"])
        self.assertNotEqual(after, before)

    def test_observation_session_is_read_only(self):
        original_run = CommandRunner.run

        def write_instead(runner, argv, **kwargs):
            argv = list(argv)
            index = argv.index("--command") + 1
            argv[index] = "UPDATE django_migrations SET name = name WHERE false"
            result = original_run(runner, argv, **kwargs)
            self.stderr = result.stderr
            return result

        with mock.patch.object(CommandRunner, "run", write_instead):
            with self.assertRaises(ExecutionError) as caught:
                self.executor._observe_migration_records(self.refs)
        self.assertEqual(caught.exception.classification, "MIGRATION_OBSERVATION_FAILED")
        self.assertIn(b"read-only transaction", self.stderr)


class ReadOnlyPreflightTests(TransactionTestCase):
    def test_writes_and_sequence_advances_are_refused_and_nothing_changes(self):
        from library.models import ScheduleProfile
        ScheduleProfile.objects.create(name="ro-before")

        def writes(cursor):
            cursor.execute("UPDATE library_scheduleprofile SET name = 'mutated' WHERE name = 'ro-before'")
            return {"ok": True}

        def advances_sequence(cursor):
            cursor.execute("SELECT nextval(pg_get_serial_sequence('library_scheduleprofile', 'id'))")
            return {"ok": True}

        for check in (writes, advances_sequence):
            with self.subTest(check=check.__name__), mock.patch.dict(preflight.CHECKS, {"ro.check": check}, clear=True):
                with self.assertRaisesRegex(Exception, "read-only transaction"):
                    preflight.run_preflights(["ro.check"])
        self.assertTrue(ScheduleProfile.objects.filter(name="ro-before").exists())
        self.assertFalse(ScheduleProfile.objects.filter(name="mutated").exists())


class ScheduleBlockPreflightTests(TransactionTestCase):
    """S2: the check follows library.0088's per-profile uniqueness at every stage."""

    def setUp(self):
        from library.models import Rotation
        self.rotation = Rotation.objects.create(name=f"preflight-{uuid.uuid4().hex[:8]}").id
        self.leaves, _ = _library_targets("0088_enforce_schedule_profile_integrity")

    def _migrate_library(self, leaf):
        _leaves, targets = _library_targets(leaf)
        MigrationExecutor(connection).migrate(targets)

    def _restore(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM library_scheduleblock")
        MigrationExecutor(connection).migrate(self.leaves)

    def _profile(self, cursor, name):
        cursor.execute(
            "INSERT INTO library_scheduleprofile (uuid, name, description, sort_order, is_archived, created_at, updated_at) "
            "VALUES (%s, %s, '', 0, false, now(), now()) RETURNING id",
            [str(uuid.uuid4()), name],
        )
        return cursor.fetchone()[0]

    def _block(self, cursor, *, day=0, at=6, profile="omit"):
        columns = ["day_of_week", "start_time", "end_time", "rotation_id"]
        values = [day, time(at), time(at + 1), self.rotation]
        if profile != "omit":
            columns.append("profile_id")
            values.append(profile)
        cursor.execute(
            f"INSERT INTO library_scheduleblock ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id",
            values,
        )
        return cursor.fetchone()[0]

    def _run(self):
        result = preflight.run_preflights([CHECK])
        return result["status"], result["checks"][0]["evidence"]

    def test_same_time_in_two_profiles_passes_on_the_current_schema(self):
        from library.models import ScheduleBlock, ScheduleProfile
        for name in ("weekday-ops", "holiday-ops"):
            ScheduleBlock.objects.create(
                profile=ScheduleProfile.objects.create(name=name), day_of_week=0,
                start_time=time(6), end_time=time(7), rotation_id=self.rotation,
            )
        status, evidence = self._run()
        self.assertEqual(status, "ok", evidence)
        self.assertTrue(evidence["profile_scoped"])

    def test_duplicate_within_one_profile_fails_before_0088_enforces_it(self):
        try:
            self._migrate_library("0087_backfill_default_schedule_profile")
            with connection.cursor() as cursor:
                ops = self._profile(cursor, "ops")
                other = self._profile(cursor, "other")
                first = self._block(cursor, profile=ops)
                second = self._block(cursor, profile=ops)
                self._block(cursor, profile=other)            # same time, different profile: valid
            status, evidence = self._run()
            self.assertEqual(status, "failed")
            self.assertEqual(evidence["offending_group_count"], 1)
            group = evidence["offending_groups"][0]
            self.assertEqual(group["profile"], ops)
            self.assertEqual(group["identity"], ["0", "06:00:00"])
            self.assertEqual(group["schedule_block_ids"], [first, second])
        finally:
            self._restore()

    def test_profile_less_rows_join_the_default_profile_like_0087_will(self):
        try:
            self._migrate_library("0086_scheduleprofile_foundation_schema")
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM library_scheduleprofile WHERE name = %s", [preflight.DEFAULT_PROFILE_NAME])
                row = cursor.fetchone()
                default = row[0] if row else self._profile(cursor, preflight.DEFAULT_PROFILE_NAME)
                other = self._profile(cursor, "other")
                existing = self._block(cursor, day=1, at=7, profile=default)
                unassigned = self._block(cursor, day=1, at=7, profile=None)   # -> Default Schedule
                self._block(cursor, day=2, at=8, profile=None)
                self._block(cursor, day=2, at=8, profile=other)             # different profile: valid
            status, evidence = self._run()
            self.assertEqual(status, "failed")
            self.assertEqual(evidence["offending_group_count"], 1)
            self.assertEqual(evidence["offending_groups"][0]["profile"], default)
            self.assertEqual(evidence["offending_groups"][0]["schedule_block_ids"], sorted([existing, unassigned]))
        finally:
            self._restore()

    def test_before_profiles_exist_every_block_shares_the_future_default(self):
        try:
            self._migrate_library("0085_remote_dj_queue_set_next_access")
            with connection.cursor() as cursor:
                ids = [self._block(cursor), self._block(cursor)]
                self._block(cursor, day=3)
            status, evidence = self._run()
            self.assertEqual(status, "failed")
            self.assertFalse(evidence["profile_scoped"])
            self.assertEqual(evidence["offending_groups"][0]["schedule_block_ids"], ids)
        finally:
            self._restore()

    def test_default_profile_name_matches_migration_0087(self):
        import importlib
        backfill = importlib.import_module("library.migrations.0087_backfill_default_schedule_profile")
        self.assertEqual(preflight.DEFAULT_PROFILE_NAME, backfill.INITIAL_PROFILE_NAME)


class RealGraphReconstructionTests(TransactionTestCase):
    """The reconstructed full-plan digest after M1 equals the original
    pre-M1 digest on the real migration graph, while the ordinary forward
    (suffix) digest does not. 0088 contains AlterField operations, the case
    whose classification depends on project state."""

    def test_original_digest_is_reconstructed_after_the_first_migration(self):
        leaves, before = _library_targets("0086_scheduleprofile_foundation_schema")
        _leaves, after_m1 = _library_targets("0087_backfill_default_schedule_profile")
        context = dict(release_id="r0104", target_commit="b" * 40)
        try:
            MigrationExecutor(connection).migrate(before)
            original = build_probe_payload(**context)
            refs = [item["ref"] for item in original["plan"]]
            self.assertEqual(refs, [
                "library.0087_backfill_default_schedule_profile", "library.0088_enforce_schedule_profile_integrity",
            ])
            MigrationExecutor(connection).migrate(after_m1)
            resumed = build_probe_payload(**context, recovery_plan_refs=refs)
            self.assertNotEqual(resumed["migration_plan_digest"], original["migration_plan_digest"])
            self.assertEqual(resumed["recovery_migration_plan_digest"], original["migration_plan_digest"])
            self.assertEqual(resumed["recovery_plan"], original["plan"])
            self.assertEqual(resumed["recovery_manual_operations"], original["manual_operations"])
        finally:
            MigrationExecutor(connection).migrate(leaves)

    def test_ordinary_probe_never_emits_recovery_keys(self):
        payload = build_probe_payload(release_id="r0104", target_commit="b" * 40)
        self.assertNotIn("recovery_plan", payload)
        self.assertEqual(len(payload), 13)
