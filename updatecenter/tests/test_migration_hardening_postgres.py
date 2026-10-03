"""P1 1.17 -- protocol-6 safety boundaries proven against REAL PostgreSQL.

* the protected runtime's psql recorder observation (S3 row identity) and
  its read-only session;
* the read-only migration preflight transaction;
* the stage-aware ScheduleBlock preflights (S2), each confirmed against the
  REAL next migration (0087 / 0088) on the same data;
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

        def writes(cursor, pending):
            cursor.execute("UPDATE library_scheduleprofile SET name = 'mutated' WHERE name = 'ro-before'")
            return {"ok": True}

        def advances_sequence(cursor, pending):
            cursor.execute("SELECT nextval(pg_get_serial_sequence('library_scheduleprofile', 'id'))")
            return {"ok": True}

        for check in (writes, advances_sequence):
            with self.subTest(check=check.__name__), \
                    mock.patch.dict(preflight.REGISTRY, {"ro.0001_check": [("ro.check", check)]}, clear=True):
                with self.assertRaisesRegex(Exception, "read-only transaction"):
                    preflight.run_preflights(["ro.0001_check"])
        self.assertTrue(ScheduleProfile.objects.filter(name="ro-before").exists())
        self.assertFalse(ScheduleProfile.objects.filter(name="mutated").exists())


class ScheduleBlockStagePreflightTests(TransactionTestCase):
    """S2: each registered check reports exactly what the immediately pending
    migration will do. Every verdict is confirmed by running the REAL next
    migration on the same data."""

    def setUp(self):
        from library.models import Rotation
        self.rotation = Rotation.objects.create(name=f"preflight-{uuid.uuid4().hex[:8]}").id
        self.leaves, _ = _library_targets("0088_enforce_schedule_profile_integrity")

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM library_scheduleblock")
        MigrationExecutor(connection).migrate(self.leaves)

    def _stage(self, leaf):
        _leaves, targets = _library_targets(leaf)
        MigrationExecutor(connection).migrate(targets)

    def _real_migration_succeeds(self, leaf):
        _leaves, targets = _library_targets(leaf)
        try:
            MigrationExecutor(connection).migrate(targets)
            return True
        except Exception:  # noqa: BLE001 -- 0087's RuntimeError or 0088's IntegrityError
            return False

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

    def _preflight(self, *pending):
        result = preflight.run_preflights(list(pending))
        return result["status"], {item["id"]: item for item in result["checks"]}

    # -- stage 0086: 0087 and 0088 pending ------------------------------------
    def test_0086_cross_profile_duplicate_fails_like_0087_does(self):
        self._stage("0086_scheduleprofile_foundation_schema")
        with connection.cursor() as cursor:
            other = self._profile(cursor, "other")
            unassigned = self._block(cursor, day=2, at=8, profile=None)
            assigned = self._block(cursor, day=2, at=8, profile=other)
        status, checks = self._preflight(preflight.M0087, preflight.M0088)
        self.assertEqual(status, "failed")
        check = checks["library.0087.global_slot_ambiguity"]
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["evidence"]["offending_groups"][0]["scope"], "global")
        self.assertEqual(check["evidence"]["offending_groups"][0]["schedule_block_ids"], sorted([unassigned, assigned]))
        self.assertFalse(self._real_migration_succeeds("0087_backfill_default_schedule_profile"))

    def test_0086_clean_data_passes_and_0087_then_0088_really_succeed(self):
        self._stage("0086_scheduleprofile_foundation_schema")
        with connection.cursor() as cursor:
            self._block(cursor, day=2, at=8, profile=None)
            self._block(cursor, day=2, at=9, profile=None)
        self.assertEqual(self._preflight(preflight.M0087, preflight.M0088)[0], "ok")
        self.assertTrue(self._real_migration_succeeds("0087_backfill_default_schedule_profile"))
        self.assertTrue(self._real_migration_succeeds("0088_enforce_schedule_profile_integrity"))

    def test_before_0086_every_block_shares_one_future_profile(self):
        self._stage("0085_remote_dj_queue_set_next_access")
        with connection.cursor() as cursor:
            ids = [self._block(cursor), self._block(cursor)]
        status, checks = self._preflight(preflight.M0087, preflight.M0088)
        self.assertEqual(status, "failed")
        self.assertEqual(checks["library.0087.global_slot_ambiguity"]["evidence"]["offending_groups"][0]["schedule_block_ids"], ids)

    # -- stage 0087: only 0088 pending ----------------------------------------
    def test_0087_cross_profile_same_time_passes_and_0088_really_succeeds(self):
        self._stage("0087_backfill_default_schedule_profile")
        with connection.cursor() as cursor:
            first, second = self._profile(cursor, "weekday"), self._profile(cursor, "holiday")
            self._block(cursor, profile=first)
            self._block(cursor, profile=second)
        status, checks = self._preflight(preflight.M0088)
        self.assertEqual(status, "ok", checks)
        self.assertNotIn("library.0087.global_slot_ambiguity", checks)
        self.assertTrue(self._real_migration_succeeds("0088_enforce_schedule_profile_integrity"))

    def test_0087_same_profile_duplicate_fails_like_0088_does(self):
        self._stage("0087_backfill_default_schedule_profile")
        with connection.cursor() as cursor:
            ops = self._profile(cursor, "ops")
            ids = [self._block(cursor, profile=ops), self._block(cursor, profile=ops)]
        status, checks = self._preflight(preflight.M0088)
        self.assertEqual(status, "failed")
        group = checks["library.0088.profile_integrity"]["evidence"]["offending_groups"][0]
        self.assertEqual((group["scope"], group["key"], group["schedule_block_ids"]), ({"profile_id": ops}, ["0", "06:00:00"], ids))
        self.assertFalse(self._real_migration_succeeds("0088_enforce_schedule_profile_integrity"))

    def test_0087_null_profile_fails_like_0088_does(self):
        self._stage("0087_backfill_default_schedule_profile")
        with connection.cursor() as cursor:
            orphan = self._block(cursor, profile=None)
        status, checks = self._preflight(preflight.M0088)
        self.assertEqual(status, "failed")
        problems = checks["library.0088.profile_integrity"]["evidence"]["null_profile_problems"]
        self.assertEqual(problems[0]["schedule_block_ids"], [orphan])
        self.assertFalse(self._real_migration_succeeds("0088_enforce_schedule_profile_integrity"))

    # -- stage 0088 applied ----------------------------------------------------
    def test_after_0088_no_historical_check_runs(self):
        status, checks = self._preflight("library.0089_future_unregistered")
        self.assertEqual((status, checks), ("ok", {}))
        status, checks = self._preflight(preflight.M0088)
        self.assertEqual(status, "failed")
        self.assertEqual(list(checks), ["updater.pending_stage_consistency"])

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
