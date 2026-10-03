"""P1 1.17 -- the guarded one-migration apply, against REAL PostgreSQL.

updatecenter_apply_migration_guarded is the ownership boundary of runtime 11:
in ONE transaction on ONE PostgreSQL session it locks django_migrations
(ACCESS EXCLUSIVE), proves the owned prefix and the next migration's
absence, applies exactly that atomic migration with Django's real migration
engine, and inserts a nonce-bound receipt. These tests prove, with real
sessions, threads, barriers and pg_locks -- never mocks of the race itself:

* same session: begin, lock, migration DDL, recorder row and receipt all on
  the one backend pid that holds the lock;
* atomicity: a refusal, a raising migration, a failing receipt insert, a
  crash after the proof, and a backend dying mid-migration leave no M2, no
  recorder row, no receipt (and no receipt table if it was created inside);
* concurrency: Schedule 1 (updater wins, a real competing migrate blocks),
  Schedule 2 (external wins, the updater refuses with no receipt), the
  latest-point Race C insert (blocks, lands after, is never credited), a
  backup-style lock (bounded, retryable timeout), and a deadlock (either
  victim is safe);
* the protected runtime's psql receipt lookup.

The migration under test is ogremote.0003 (an atomic AddField, no
dependents), with ogremote.0002 as the updater-owned prefix.
"""
from __future__ import annotations

import importlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from unittest import mock
import uuid

from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from updatecenter.management.commands import updatecenter_apply_migration_guarded as guarded
from updatecenter.management.commands.updatecenter_apply_migration_guarded import (
    RECEIPT_TABLE, GuardRefusal, apply_guarded,
)

from .phase_b_helpers import (
    PROJECT_ROOT, config_dict, drop_guarded_receipt_table, orm_guarded_receipts, orm_migration_records,
)
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import ExecutionError, Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner

M0 = "ogremote.0001_initial"
M1 = "ogremote.0002_seed_categories"
M2 = "ogremote.0003_ogremoteconfig_poll_interval_minutes"
M2_NAME = "0003_ogremoteconfig_poll_interval_minutes"
M2_TABLE = "ogremote_ogremoteconfig"
M2_COLUMN = "poll_interval_minutes"
WAIT = 20


class _Crash(BaseException):
    """A process death: escapes every except clause."""


class Worker(threading.Thread):
    """Runs `fn` on its OWN database session; records result or exception."""

    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn = fn
        self.result = None
        self.error = None
        self.pid = None
        self.started_session = threading.Event()

    def run(self):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.pid = cursor.fetchone()[0]
            self.started_session.set()
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001 -- reported to the test
            self.error = exc
        finally:
            self.started_session.set()
            connections.close_all()

    def begin(self):
        self.start()
        if not self.started_session.wait(WAIT):
            raise AssertionError("worker session never started")
        return self


class GuardedApplyTestCase(TransactionTestCase):
    def setUp(self):
        drop_guarded_receipt_table()
        self.leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
        MigrationExecutor(connection).migrate([("ogremote", "0002_seed_categories")])
        self.assertEqual(set(orm_migration_records([M1, M2])), {M1})
        self.assertFalse(self._column_exists())
        self.owned = orm_migration_records([M1])
        self.job_id = str(uuid.uuid4())
        self.nonce = str(uuid.uuid4())

    def tearDown(self):
        # Restore the shared test database: no receipt table, exactly one
        # ogremote.0003 row matching the schema, every app at its leaf.
        drop_guarded_receipt_table()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM django_migrations WHERE app = 'ogremote' AND name = %s", [M2_NAME])
            cursor.execute(
                "DELETE FROM django_migrations a USING django_migrations b "
                "WHERE a.app = 'ogremote' AND b.app = a.app AND b.name = a.name AND a.id > b.id"
            )
        if self._column_exists():
            MigrationRecorder(connection).record_applied("ogremote", M2_NAME)
        MigrationExecutor(connection).migrate(self.leaves)

    # -- helpers -----------------------------------------------------------
    def guard(self, **overrides):
        arguments = dict(job_id=self.job_id, nonce=self.nonce, migration=M2, transition=[M1, M2], owned=self.owned)
        arguments.update(overrides)
        return apply_guarded(**arguments)

    def _column_exists(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = %s AND column_name = %s", [M2_TABLE, M2_COLUMN],
            )
            return cursor.fetchone() is not None

    def _receipt_table_exists(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [RECEIPT_TABLE])
            return cursor.fetchone()[0]

    def _m2_rows(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM django_migrations WHERE app = 'ogremote' AND name = %s ORDER BY id", [M2_NAME])
            return [row[0] for row in cursor.fetchall()]

    def _locks(self, relation="django_migrations"):
        """(pid, mode, granted) on `relation`, read from pg_locks -- never from
        the relation itself, which the guard may hold exclusively."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT l.pid, l.mode, l.granted FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE l.locktype = 'relation' AND c.relname = %s "
                "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())",
                [relation],
            )
            return cursor.fetchall()

    def _wait_for(self, predicate, what):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def _exclusive_holder(self):
        holders = [pid for pid, mode, granted in self._locks() if mode == "AccessExclusiveLock" and granted]
        return holders[0] if holders else None

    def _waiting(self, pid, relation="django_migrations"):
        return any(holder == pid and not granted for holder, _mode, granted in self._locks(relation))

    def assert_nothing_applied(self, *, table_created_inside=True):
        self.assertEqual(self._m2_rows(), [])
        self.assertFalse(self._column_exists())
        if table_created_inside:
            self.assertFalse(self._receipt_table_exists())
        else:
            self.assertEqual(orm_guarded_receipts(self.nonce), [])

    def assert_exact_receipt(self, result):
        [row_id] = self._m2_rows()
        record = orm_migration_records([M2])[M2]
        self.assertEqual(result["row"], record)
        self.assertEqual(record["id"], row_id)
        self.assertEqual(orm_guarded_receipts(self.nonce), [{
            "nonce": self.nonce, "job_id": self.job_id, "migration": M2,
            "django_migration_id": record["id"], "applied_utc": record["applied"],
        }])
        self.assertTrue(self._column_exists())


class GuardedApplySameSessionTests(GuardedApplyTestCase):
    def test_lock_migration_recorder_row_and_receipt_share_one_backend(self):
        statements = []

        def wrapper(execute, sql, params, many, context):
            statements.append((context["connection"].connection.get_backend_pid(), sql))
            return execute(sql, params, many, context)

        observed_locks = []

        def after_proof():
            # Seen from ANOTHER session while the guard is paused.
            worker = Worker(self._locks).begin()
            worker.join(WAIT)
            observed_locks.extend(worker.result)

        with connection.execute_wrapper(wrapper):
            result = self.guard(after_proof=after_proof)
        session = result["session"]
        self.assertEqual(set(session), {"begin", "locked", "after_migrate", "receipt"})
        pid = session["begin"]
        self.assertEqual(set(session.values()), {pid})
        self.assertIn((pid, "AccessExclusiveLock", True), observed_locks)
        # EVERY statement -- including the migration's own DDL and recorder
        # insert -- ran on that one backend.
        self.assertEqual({backend for backend, _sql in statements}, {pid})

        def index(pattern):
            matches = [i for i, (_pid, sql) in enumerate(statements) if re.search(pattern, sql)]
            self.assertTrue(matches, f"no statement matched {pattern!r}")
            return matches[0]
        lock = index(r"^LOCK TABLE django_migrations IN ACCESS EXCLUSIVE MODE$")
        ddl = index(rf'^ALTER TABLE "{M2_TABLE}" ADD COLUMN "{M2_COLUMN}"')
        recorder = index(r'^INSERT INTO "django_migrations"')
        receipt = index(rf"^INSERT INTO {RECEIPT_TABLE} ")
        self.assertLess(index(r"^SET LOCAL lock_timeout = '120s'$"), lock)
        self.assertLess(lock, ddl)
        self.assertLess(ddl, recorder)
        self.assertLess(recorder, receipt)
        self.assert_exact_receipt(result)

    def test_command_output_is_the_bounded_contract(self):
        output = io.StringIO()
        call_command(
            "updatecenter_apply_migration_guarded", "--job-id", self.job_id, "--nonce", self.nonce,
            "--migration", M2, "--transition", M1, "--transition", M2,
            "--owned", f"{M1}={self.owned[M1]['id']},{self.owned[M1]['applied']}", stdout=output,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload, {
            "schema_version": 1, "status": "applied", "migration": M2, "nonce": self.nonce,
            "row": orm_migration_records([M2])[M2],
        })
        refused = io.StringIO()
        call_command(
            "updatecenter_apply_migration_guarded", "--job-id", self.job_id, "--nonce", str(uuid.uuid4()),
            "--migration", M2, "--transition", M1, "--transition", M2,
            "--owned", f"{M1}={self.owned[M1]['id']},{self.owned[M1]['applied']}", stdout=refused,
        )
        self.assertEqual(json.loads(refused.getvalue()),
                         {"schema_version": 1, "status": "refused", "reason": "next_migration_already_applied"})


class GuardedApplyRefusalAndRollbackTests(GuardedApplyTestCase):
    def assert_refused(self, reason, **overrides):
        with self.assertRaises(GuardRefusal) as caught:
            self.guard(**overrides)
        self.assertEqual(caught.exception.reason, reason)

    def test_refusal_after_receipt_table_creation_rolls_the_table_back(self):
        module = importlib.import_module(f"ogremote.migrations.{M2_NAME}")
        with mock.patch.object(module.Migration, "atomic", False):
            self.assert_refused("NON_ATOMIC_UNSUPPORTED")
        self.assert_nothing_applied()            # the receipt table did not commit as a side effect

    def test_preapplied_next_migration_is_refused(self):
        MigrationExecutor(connection).migrate([("ogremote", M2_NAME)])     # E: external M2 first
        self.assert_refused("next_migration_already_applied")
        self.assertEqual(len(self._m2_rows()), 1)
        self.assertFalse(self._receipt_table_exists())

    def test_changed_owned_row_identity_is_refused(self):
        self.assert_refused("owned_recorder_row_identity_changed",
                            owned={M1: {**self.owned[M1], "id": self.owned[M1]["id"] + 1000}})
        self.assert_nothing_applied()

    def test_extra_unowned_applied_transition_row_is_refused(self):
        """G-shaped: the next migration is absent, but another transition
        migration (here M1, listed after it) is applied and not owned."""
        self.assert_refused("applied_transition_differs_from_owned_prefix",
                            transition=[M0, M2, M1], owned=orm_migration_records([M0]), migration=M2)
        self.assert_nothing_applied()

    def test_duplicate_transition_rows_are_refused(self):
        MigrationRecorder(connection).migration_qs.create(app="ogremote", name="0002_seed_categories")
        self.assert_refused("duplicate_transition_recorder_rows")
        self.assert_nothing_applied()

    def test_django_plan_must_be_exactly_the_one_migration(self):
        MigrationExecutor(connection).migrate([("ogremote", "0001_initial")])
        self.assert_refused("django_plan_is_not_exactly_the_requested_migration", transition=[M2], owned={})
        self.assertEqual(set(orm_migration_records([M1, M2])), set())
        self.assertFalse(self._receipt_table_exists())

    def test_input_contract_is_enforced_before_any_session_work(self):
        cases = {
            "job id": dict(job_id="not-a-uuid"),
            "nonce not uuid4": dict(nonce=str(uuid.uuid1())),
            "owned not a prefix": dict(owned={M2: {"id": 1, "applied": "2026-10-03T00:00:00.000000Z"}}),
            "skips an unowned migration": dict(transition=[M0, M1, M2], owned={M0: self.owned[M1]}),
            "duplicate transition": dict(transition=[M1, M1, M2]),
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(GuardRefusal):
                    self.guard(**overrides)
        self.assert_nothing_applied()

    def test_reused_nonce_is_refused(self):
        self.assert_exact_receipt(self.guard())
        MigrationExecutor(connection).migrate([("ogremote", "0002_seed_categories")])   # unapply M2
        self.assert_refused("nonce_already_used")
        self.assertEqual(self._m2_rows(), [])
        self.assertEqual(len(orm_guarded_receipts(self.nonce)), 1)     # the first receipt, unchanged

    def test_b_crash_after_proof_before_mutation_rolls_back(self):
        def crash():
            raise _Crash()
        with self.assertRaises(_Crash):
            self.guard(after_proof=crash)
        self.assert_nothing_applied()

    def test_c_migration_raising_after_its_ddl_rolls_everything_back(self):
        from django.db.migrations.migration import Migration
        original = Migration.apply

        def apply_then_raise(migration, *args, **kwargs):
            state = original(migration, *args, **kwargs)
            raise RuntimeError("migration failed after its DDL")
        with mock.patch.object(Migration, "apply", apply_then_raise):
            with self.assertRaisesRegex(RuntimeError, "after its DDL"):
                self.guard()
        self.assert_nothing_applied()

    def test_c_failure_after_the_recorder_row_rolls_back_ddl_and_row(self):
        original = MigrationRecorder.record_applied

        def record_then_raise(recorder, app, name):
            original(recorder, app, name)
            raise RuntimeError("died after recording")
        with mock.patch.object(MigrationRecorder, "record_applied", record_then_raise):
            with self.assertRaisesRegex(RuntimeError, "after recording"):
                self.guard()
        self.assert_nothing_applied()

    def test_receipt_insert_failure_rolls_back_the_migration_and_its_row(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE {RECEIPT_TABLE} (nonce uuid PRIMARY KEY, job_id uuid NOT NULL, "
                "migration text NOT NULL, django_migration_id bigint NOT NULL UNIQUE, applied_utc text NOT NULL, "
                f"created_at timestamptz NOT NULL DEFAULT now(), CHECK (migration <> '{M2}'))"
            )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            self.guard()
        self.assert_nothing_applied(table_created_inside=False)

    def test_c_backend_dying_mid_migration_leaves_nothing(self):
        """A real backend death after the DDL AND the recorder row, before COMMIT."""
        recorded, release = threading.Event(), threading.Event()
        original = MigrationRecorder.record_applied

        def record_then_wait(recorder, app, name):
            original(recorder, app, name)
            recorded.set()
            release.wait(WAIT)
        with mock.patch.object(MigrationRecorder, "record_applied", record_then_wait):
            worker = Worker(self.guard).begin()
            self.assertTrue(recorded.wait(WAIT))
            self.assertEqual(self._exclusive_holder(), worker.pid)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(%s)", [worker.pid])     # simulated crash
            release.set()
            worker.join(WAIT)
        self.assertIsNotNone(worker.error)
        self.assert_nothing_applied()


class GuardedApplyConcurrencyTests(GuardedApplyTestCase):
    def setUp(self):
        super().setUp()
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
        self.runtime = Executor(config, self.store, CommandRunner())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
        super().tearDown()

    def runtime_claims(self, nonce=None):
        """The protected runtime's own psql observation + receipt rule."""
        observed = self.runtime._observe_migration_records([M1, M2])
        return M2 in observed and self.runtime._receipt_proves(
            nonce or self.nonce, job_id=self.job_id, migration=M2, row=observed[M2],
        )

    def paused_guard(self):
        locked, release = threading.Event(), threading.Event()

        def after_proof():
            locked.set()
            if not release.wait(WAIT):
                raise RuntimeError("the test never released the guard")
        worker = Worker(lambda: self.guard(after_proof=after_proof)).begin()
        self.assertTrue(locked.wait(WAIT), worker.error)
        self.assertEqual(self._exclusive_holder(), worker.pid)
        return worker, release

    # -- runtime receipt lookup ------------------------------------------------
    def test_runtime_receipt_lookup_through_psql(self):
        self.assertEqual(self.runtime._observe_guarded_receipt(self.nonce), [])      # no table: no receipt
        result = self.guard()
        self.assertEqual(self.runtime._observe_guarded_receipt(self.nonce), orm_guarded_receipts(self.nonce))
        self.assertTrue(self.runtime_claims())
        self.assertEqual(self.runtime._observe_guarded_receipt(str(uuid.uuid4())), [])
        for wrong in ({"job_id": str(uuid.uuid4())}, {"migration": M1},
                      {"row": {**result["row"], "id": result["row"]["id"] + 1}}):
            arguments = {"job_id": self.job_id, "migration": M2, "row": result["row"], **wrong}
            with self.subTest(wrong=wrong):
                self.assertFalse(self.runtime._receipt_proves(self.nonce, **arguments))
        with self.assertRaises(ExecutionError):
            self.runtime._observe_guarded_receipt("'; DROP TABLE django_migrations; --")

    # -- Schedule 1: updater wins the lock -------------------------------------
    def test_schedule_1_updater_wins_and_a_real_competing_migrate_blocks(self):
        guard, release = self.paused_guard()
        competitor = Worker(lambda: call_command(
            "migrate", "ogremote", M2_NAME, interactive=False, verbosity=0, stdout=io.StringIO(),
        )).begin()
        self._wait_for(lambda: self._waiting(competitor.pid), "the competing migrate to wait on django_migrations")
        self.assertTrue(competitor.is_alive())
        release.set()
        guard.join(WAIT)
        competitor.join(WAIT)
        self.assertIsNone(guard.error)
        self.assertIsNone(competitor.error)
        self.assert_exact_receipt(guard.result)
        self.assertEqual(set(guard.result["session"].values()), {guard.pid})
        self.assertEqual(len(self._m2_rows()), 1)            # the competitor created nothing
        self.assertTrue(self.runtime_claims())

    # -- Schedule 2: an external migrate wins -----------------------------------
    def test_schedule_2_external_wins_and_the_updater_refuses_without_receipt(self):
        pre_commit, release = threading.Event(), threading.Event()

        def external():
            with transaction.atomic():
                call_command("migrate", "ogremote", M2_NAME, interactive=False, verbosity=0, stdout=io.StringIO())
                pre_commit.set()
                if not release.wait(WAIT):
                    raise RuntimeError("the test never released the external migrate")
        outsider = Worker(external).begin()
        self.assertTrue(pre_commit.wait(WAIT), outsider.error)
        guard = Worker(self.guard).begin()
        self._wait_for(lambda: self._waiting(guard.pid), "the guard to wait for its lock")
        self.assertIn((outsider.pid, "RowExclusiveLock", True), self._locks())
        release.set()
        outsider.join(WAIT)
        guard.join(WAIT)
        self.assertIsNone(outsider.error)
        self.assertIsInstance(guard.error, GuardRefusal)
        self.assertEqual(guard.error.reason, "next_migration_already_applied")
        self.assertEqual(len(self._m2_rows()), 1)            # the outsider's
        self.assertFalse(self._receipt_table_exists())       # no updater receipt
        self.assertFalse(self.runtime_claims())

    # -- the previous Race C boundary, at the latest possible point -------------
    def test_race_c_latest_point_external_m2_blocks_lands_after_and_is_never_credited(self):
        guard, release = self.paused_guard()                  # proof done, mutation not started

        def raw_insert():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO django_migrations (app, name, applied) VALUES ('ogremote', %s, now()) RETURNING id",
                    [M2_NAME],
                )
                return cursor.fetchone()[0]
        outsider = Worker(raw_insert).begin()
        self._wait_for(lambda: self._waiting(outsider.pid), "the external M2 insert to wait on django_migrations")
        release.set()
        guard.join(WAIT)
        outsider.join(WAIT)
        self.assertIsNone(guard.error)
        self.assertIsNone(outsider.error)
        rows = self._m2_rows()
        self.assertEqual(rows, sorted([guard.result["row"]["id"], outsider.result]))
        self.assertLess(guard.result["row"]["id"], outsider.result)    # it could only land AFTER our COMMIT
        # The runtime's observation sees two M2 rows: ambiguous, never credited.
        with self.assertRaises(ExecutionError) as caught:
            self.runtime._observe_migration_records([M1, M2])
        self.assertEqual(caught.exception.classification, "MIGRATION_RECORD_AMBIGUOUS")

    # -- bounded lock wait (e.g. pg_dump's ACCESS SHARE) -----------------------
    def test_backup_style_lock_causes_a_bounded_retryable_timeout_without_receipt(self):
        holding, release = threading.Event(), threading.Event()

        def backup():
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("LOCK TABLE django_migrations IN ACCESS SHARE MODE")     # what pg_dump takes
                holding.set()
                release.wait(WAIT)
        dump = Worker(backup).begin()
        self.assertTrue(holding.wait(WAIT))
        started = time.monotonic()
        try:
            with self.assertRaises(GuardRefusal) as caught:
                self.guard(lock_timeout="300ms")
        finally:
            release.set()
            dump.join(WAIT)
        self.assertEqual(caught.exception.reason, "lock_timeout")
        self.assertLess(time.monotonic() - started, 10)
        self.assert_nothing_applied()
        self.assert_exact_receipt(self.guard())                # retry later succeeds

    def test_lock_timeout_is_validated(self):
        with self.assertRaises(GuardRefusal) as caught:
            self.guard(lock_timeout="1s'; SET x TO 'y")
        self.assertEqual(caught.exception.reason, "invalid_lock_timeout")

    # -- deadlock: either victim is safe ---------------------------------------
    # PostgreSQL's detector runs in the session that has waited longest, so
    # the order of the waits picks the victim deterministically: one schedule
    # per side, plus the invariant "receipt iff the updater's M2 committed".
    def test_deadlock_with_the_updater_as_victim_leaves_nothing(self):
        holding, go = threading.Event(), threading.Event()

        def app_session():
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"LOCK TABLE {M2_TABLE} IN ACCESS EXCLUSIVE MODE")
                    holding.set()
                    if not go.wait(WAIT):
                        raise RuntimeError("never released")
                    cursor.execute("SELECT count(*) FROM django_migrations")
        app = Worker(app_session).begin()
        self.assertTrue(holding.wait(WAIT))
        guard = Worker(self.guard).begin()
        self._wait_for(lambda: self._waiting(guard.pid, M2_TABLE), "the guard's DDL to wait on the app table")
        go.set()
        guard.join(WAIT)
        app.join(WAIT)
        self.assertIsNone(app.error)
        self.assertIsInstance(guard.error, GuardRefusal)
        self.assertEqual(guard.error.reason, "deadlock_victim")     # retryable, nothing changed
        self.assert_nothing_applied()
        self.assertFalse(self.runtime_claims())

    def test_deadlock_with_the_other_session_as_victim_commits_with_receipt(self):
        guard, release = self.paused_guard()                  # updater holds django_migrations
        holding = threading.Event()

        def app_session():
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"LOCK TABLE {M2_TABLE} IN ACCESS EXCLUSIVE MODE")
                    holding.set()
                    cursor.execute("SELECT count(*) FROM django_migrations")      # waits first
        app = Worker(app_session).begin()
        self.assertTrue(holding.wait(WAIT))
        self._wait_for(lambda: self._waiting(app.pid), "the app session to wait on django_migrations")
        release.set()                                          # guard's DDL now waits on the app table
        guard.join(WAIT)
        app.join(WAIT)
        self.assertIsNotNone(app.error)
        self.assertIn("deadlock", str(app.error))
        self.assertIsNone(guard.error)
        self.assert_exact_receipt(guard.result)
        self.assertTrue(self.runtime_claims())


class ReceiptImmutabilityTests(TransactionTestCase):
    def test_runtime_code_never_updates_or_deletes_receipts(self):
        sources = [
            Path(guarded.__file__),
            PROJECT_ROOT / "deploy/updater_runtime/isadoraair_updater/executor.py",
        ]
        for source in sources:
            text = source.read_text(encoding="utf-8")
            with self.subTest(source=source.name):
                self.assertNotRegex(text, rf"(?i)(UPDATE|DELETE\s+FROM|TRUNCATE|DROP\s+TABLE)\s+[^\n]*{RECEIPT_TABLE}")
                self.assertNotRegex(text, rf"(?i)(UPDATE|DELETE\s+FROM|TRUNCATE|DROP\s+TABLE)\s+\{{?RECEIPT_TABLE")
                self.assertNotRegex(text, r"(?i)(UPDATE|DELETE\s+FROM|TRUNCATE|DROP\s+TABLE)\s+\{?GUARDED_RECEIPT_TABLE")
