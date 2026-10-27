"""Roadmap 2.27 Phase D: the off-thread runner, policy gates, gate migration,
operator status and admin surface.

Real rows and media (Phase B/C fixtures) and a bare PlaybackEngine stand-in.
Every simulated GLib callback runs under StrictGLibSpy, which fails the test
on any database query, file open/stat, hash or sleep. Installs are reached
only through the REAL gates: a policy row with dry_run_only=False (possible
because migration 0096 is applied to this development test database) plus
override_settings(HOUR_REFILL_INSTALL_ARMED=True). Nothing patches
install_permitted. Races and lifecycles are ordered with Events/barriers.

Set HOUR_REFILL_PHASE_D_EVIDENCE=<dir> to write representative runner
status documents."""
import builtins
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.models import HourRefillCause, HourRefillInstall, HourRefillPolicy, LogItem, PlayEvent, PlaylistLog
from library.services import hour_refill_evidence as evidence_mod
from library.services import hour_refill_install as inst
from library.services import hour_refill_projection as proj
from library.services import hour_refill_proposal as svc
from library.services import hour_refill_runner as runner_mod
from library.services import hour_refill_status as status_mod
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_hour_refill_install_2_27 import InstallFixtures
from library.tests.test_hour_refill_proposal_2_27 import DAY, _NoQueries, at
from updatecenter.management.commands.updatecenter_probe import _serialize_exact_plan
from webrequests.models import SongRequest

REPO = Path(__file__).resolve().parents[2]
ARMED = override_settings(HOUR_REFILL_INSTALL_ARMED=True)


class _Forbidden:
    def __init__(self, what):
        self.what = what

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"GLib callback performed {self.what}")


class StrictGLibSpy:
    """GLib.idle_add stand-in. Callbacks run at once (or are queued when
    deferred) under blockers for SQL, file open/stat, hashing and sleeping."""

    def __init__(self):
        self.calls, self.deferred, self.queued, self.fail = 0, False, [], False

    def __call__(self, fn, *args):
        self.calls += 1
        if self.fail:
            raise RuntimeError("idle_add failed")
        if self.deferred:
            self.queued.append((fn, args))
            return 0
        self.run(fn, *args)
        return 0

    @staticmethod
    def run(fn, *args):
        with connection.execute_wrapper(_NoQueries()), \
                patch.object(builtins, "open", _Forbidden("file open")), \
                patch.object(os, "stat", _Forbidden("stat")), \
                patch.object(hashlib, "sha256", _Forbidden("hashing")), \
                patch.object(time, "sleep", _Forbidden("sleep")):
            return fn(*args)

    def flush(self):
        queued, self.queued = self.queued, []
        for fn, args in queued:
            self.run(fn, *args)


class RunnerFixtures(InstallFixtures):
    def set_policy(self, *, enabled=True, dry_run=True, request=True, eject=True):
        HourRefillPolicy.objects.all().delete()
        HourRefillPolicy(enabled=enabled, dry_run_only=dry_run, capture_request_substitution=request,
                         capture_operator_eject=eject).save()

    def make_runner(self, engine, *, now=None, **kwargs):
        self.glib = StrictGLibSpy()
        self.now = now or at(14, 40)
        self.t = [1000.0]
        status_dir = tempfile.TemporaryDirectory()
        self.addCleanup(status_dir.cleanup)
        self.status_path = Path(status_dir.name) / "hour_refill_state.json"
        kwargs.setdefault("status_path", self.status_path)
        kwargs.setdefault("clock", lambda: self.t[0])
        return runner_mod.HourRefillRunner(engine, idle_add=self.glib, wall_clock=lambda: self.now, **kwargs)

    def advance(self, seconds):
        self.t[0] += seconds

    def closer_engine(self, psa_seconds=60.0):
        """Phase C's protected-closer hour: PSA + legal ID close the log."""
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        self.row(self.track(290.0, self.recur), 3)
        self.closer_psa = self.row(self.track(psa_seconds, self.psa), 4)
        self.closer_legal = self.row(self.track(10.0, self.legal), 5)
        self.eject_cause(ejected)
        return self.engine(cursor=3, deck_index=2, deck_position=10.0)

    def outcome(self, runner):
        return runner.last_outcome or {}

    def assert_no_writes(self, before):
        self.assertEqual(self.rows(), before)
        self.assertFalse(HourRefillInstall.objects.exists())
        self.assertFalse(LogItem.objects.filter(origin=LogItem.ORIGIN_REFILL_INSTALLED).exists())

    def record_status(self, name, runner):
        target = os.environ.get("HOUR_REFILL_PHASE_D_EVIDENCE")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            (Path(target) / f"{name}.json").write_text(json.dumps(runner.status(), indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# 1. Policy truth table and gates
# ---------------------------------------------------------------------------

class PolicyGateTests(RunnerFixtures, TestCase):
    def test_truth_table(self):
        cases = [
            (None, False, ("off", "no_policy_row")),
            (dict(enabled=False, dry_run=False), True, ("off", "policy_disabled")),
            (dict(enabled=True, dry_run=True), True, ("observation", "dry_run_only")),
            (dict(enabled=True, dry_run=False), False, ("observation", "install_not_armed")),
            (dict(enabled=True, dry_run=False), True, ("install", "install_permitted")),
        ]
        for policy, armed, expected in cases:
            with self.subTest(policy=policy, armed=armed), override_settings(HOUR_REFILL_INSTALL_ARMED=armed):
                HourRefillPolicy.objects.all().delete()
                if policy is not None:
                    self.set_policy(**policy)
                state = runner_mod.read_policy_state()
                self.assertEqual((state.mode, state.reason), expected)
                self.assertEqual(inst.install_permitted(), expected[0] == "install")

    def test_cause_switches_gate_each_type_and_a_mixed_set_as_a_whole(self):
        both = ("operator_eject", "request_substitution")
        with ARMED:
            for request, eject in ((True, True), (True, False), (False, True), (False, False)):
                with self.subTest(request=request, eject=eject):
                    self.set_policy(dry_run=False, request=request, eject=eject)
                    self.assertEqual(inst.install_permitted(cause_types=("request_substitution",)), request)
                    self.assertEqual(inst.install_permitted(cause_types=("operator_eject",)), eject)
                    self.assertEqual(inst.install_permitted(cause_types=both), request and eject)
                    self.assertFalse(inst.install_permitted(cause_types=("unknown_type",)))

    def test_arming_flag_must_be_exactly_true(self):
        self.set_policy(dry_run=False)
        for value in (False, None, "True", 1):
            with self.subTest(value=value), override_settings(HOUR_REFILL_INSTALL_ARMED=value):
                self.assertFalse(inst.install_permitted())

    def test_policy_read_failure_is_off(self):
        self.set_policy(dry_run=False)
        with ARMED, patch.object(HourRefillPolicy.objects, "filter", side_effect=RuntimeError("db down")):
            self.assertEqual(runner_mod.read_policy_state().mode, "off")

    def test_policy_lock_read_is_for_update(self):
        self.set_policy(dry_run=False)
        with ARMED, CaptureQueriesContext(connection) as queries, transaction.atomic():
            self.assertTrue(inst.install_permitted(lock=True))
        self.assertTrue(any("FOR UPDATE" in q["sql"] for q in queries.captured_queries))

    def test_defaults_stay_off_and_dry_run(self):
        HourRefillPolicy().save()
        row = HourRefillPolicy.objects.get()
        self.assertEqual((row.enabled, row.capture_request_substitution, row.capture_operator_eject,
                          row.dry_run_only), (False, False, False, True))
        self.assertEqual(runner_mod.read_policy_state().mode, "off")


# ---------------------------------------------------------------------------
# 2. Gate migration 0096
# ---------------------------------------------------------------------------

class GateMigrationClassificationTests(TestCase):
    def test_0096_is_manual_for_the_update_center(self):
        plan = _serialize_exact_plan(MigrationLoader(connection), ["library.0096_hour_refill_install_gate"])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]],
                         [("RemoveConstraint", "manual"), ("AlterField", "manual")])

    def test_0096_has_no_data_operation_and_enables_nothing(self):
        source = (REPO / "library/migrations/0096_hour_refill_install_gate.py").read_text()
        for forbidden in ("RunPython", "RunSQL", "enabled", "update(", "objects"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, source.split('"""')[0].split("operations = [")[1])
        self.assertFalse(status_mod.dry_run_gate_present())  # applied in this test database


class GateMigrationTests(TransactionTestCase):
    """Old schema (0095) vs new schema (0096), rows unchanged, reverse."""

    def tearDown(self):
        call_command("migrate", verbosity=0)

    def test_forward_keeps_rows_and_reverse_needs_dry_run(self):
        call_command("migrate", "library", "0095", verbosity=0)
        self.assertTrue(status_mod.dry_run_gate_present())
        HourRefillPolicy(enabled=True, capture_request_substitution=True, capture_operator_eject=False).save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            HourRefillPolicy.objects.filter(pk=1).update(dry_run_only=False)
        with ARMED:  # new code, old schema: still refuses
            self.assertFalse(inst.install_permitted())
        before = list(HourRefillPolicy.objects.values_list(
            "enabled", "capture_request_substitution", "capture_operator_eject", "dry_run_only"))

        call_command("migrate", "library", "0096", verbosity=0)
        self.assertFalse(status_mod.dry_run_gate_present())
        after = list(HourRefillPolicy.objects.values_list(
            "enabled", "capture_request_substitution", "capture_operator_eject", "dry_run_only"))
        self.assertEqual(after, before)
        self.assertEqual(after[0][3], True)  # nothing auto-enabled
        with ARMED:
            self.assertFalse(inst.install_permitted())  # still observation until an operator acts

        HourRefillPolicy.objects.filter(pk=1).update(dry_run_only=False)
        with self.assertRaises(IntegrityError):
            call_command("migrate", "library", "0095", verbosity=0)
        self.assertFalse(status_mod.dry_run_gate_present())
        HourRefillPolicy.objects.filter(pk=1).update(dry_run_only=True)  # documented rollback order
        call_command("migrate", "library", "0095", verbosity=0)
        self.assertTrue(status_mod.dry_run_gate_present())


# ---------------------------------------------------------------------------
# 3. Off parity and observation
# ---------------------------------------------------------------------------

class OffParityTests(RunnerFixtures, TestCase):
    def setUp(self):
        self.build()

    def test_off_is_one_policy_read_no_glib_no_writes(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        for policy in (None, dict(enabled=False, dry_run=False), dict(enabled=False)):
            with self.subTest(policy=policy), ARMED:
                HourRefillPolicy.objects.all().delete()
                if policy is not None:
                    self.set_policy(**policy)
                runner = self.make_runner(engine)
                with CaptureQueriesContext(connection) as queries:
                    self.assertTrue(runner.run_once())
                self.assertEqual(len(queries.captured_queries), 1, [q["sql"] for q in queries.captured_queries])
                self.assertEqual(self.glib.calls, 0)
                self.assertEqual(runner.counters["skipped_policy_off"], 1)
                self.assertEqual(runner.status()["policy"]["mode"], "off")
                self.assertIsNone(engine._refill_coordinator.refill_owner)
                self.assertFalse(engine._refill_coordinator.blocks_legacy(self.log.id, engine.log_items))
        self.assert_no_writes(before)

    def test_observation_reports_an_advisory_proposal_and_writes_nothing(self):
        engine = self.eject_engine()
        a, b = self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=True)
        runner = self.make_runner(engine)
        with ARMED:
            runner.run_once()
        outcome = self.outcome(runner)
        self.record_status("01-observation-advisory", runner)
        self.assertEqual((outcome["kind"], outcome["status"], outcome["mode"]), ("advisory", "proposed", "observation"))
        self.assertEqual(sorted(outcome["track_ids"]), sorted([a.id, b.id]))
        self.assertEqual(outcome["safety_budget_seconds"], 7.0)
        self.assertEqual(outcome["net_shortage_seconds"], 389.25)
        self.assertEqual(self.glib.calls, 1)  # one primitive capture
        self.assert_no_writes(before)

    def test_one_journal_line_per_evaluation_and_none_otherwise(self):
        import io
        from contextlib import redirect_stdout

        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        runner = self.make_runner(engine)
        out = io.StringIO()
        with redirect_stdout(out):
            runner.run_once()  # Off: silent
            self.set_policy()
            runner.run_once()  # one evaluation
            runner.run_once()  # backoff: silent
        lines = [line for line in out.getvalue().splitlines() if "Hour-refill evaluation" in line]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0].split("Hour-refill evaluation ", 1)[1])
        self.assertEqual((record["status"], record["committed_runway_seconds"]), ("proposed", 810.75))
        self.assertIn("captured_at", record)

    def test_a_failing_evaluation_journals_its_own_error_not_the_previous_outcome(self):
        # F1: success, then an exception inside the next evaluation.
        import io
        from contextlib import redirect_stdout

        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        self.set_policy()
        runner = self.make_runner(engine)
        out = io.StringIO()
        with redirect_stdout(out):
            runner.run_once()
            self.assertEqual(self.outcome(runner)["status"], "proposed")
            self.advance(runner_mod.RETRY_SECONDS)
            with patch.object(runner_mod, "project_refill", side_effect=RuntimeError("injected")):
                self.assertFalse(runner.run_once())
            # Errors outside an evaluation are counted, never journaled as one.
            self.advance(runner_mod.RETRY_SECONDS)
            with patch.object(runner_mod.HourRefillRunner, "_cause_ids", side_effect=RuntimeError("db")):
                self.assertFalse(runner.run_once())
        records = [json.loads(line.split("Hour-refill evaluation ", 1)[1])
                   for line in out.getvalue().splitlines() if "Hour-refill evaluation " in line]
        self.assertEqual([r["status"] for r in records], ["proposed", "error"])
        failed = records[1]
        self.assertEqual((failed["log_id"], failed["cause_ids"]),
                         (self.log.id, [HourRefillCause.objects.get().id]))
        for key in ("net_shortage_seconds", "track_ids", "committed_runway_seconds", "captured_at"):
            self.assertNotIn(key, failed)  # nothing carried over from the successful evaluation
        self.assertEqual((self.outcome(runner)["status"], runner.counters["errors"],
                          runner.counters["journal_lines"]), ("error", 2, 2))

    def test_install_requested_but_not_armed_stays_observation(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        runner.run_once()  # HOUR_REFILL_INSTALL_ARMED unset (False) in tests
        self.assertEqual((runner.policy.mode, runner.policy.reason), ("observation", "install_not_armed"))
        self.assertEqual(self.outcome(runner)["kind"], "advisory")
        self.assert_no_writes(before)

    def test_disabled_cause_type_refuses_the_whole_set(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), ARMED:
                self.set_policy(dry_run=dry_run, eject=False)
                runner = self.make_runner(engine)
                runner.run_once()
                outcome = self.outcome(runner)
                self.assertEqual((outcome["status"], outcome["reasons"]),
                                 ("cause_type_disabled", ["switched_off:operator_eject"]))
        self.assert_no_writes(before)

    def test_no_cause_evidence_means_no_snapshot(self):
        engine = self.eject_engine()
        HourRefillCause.objects.all().delete()
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        with ARMED, CaptureQueriesContext(connection) as queries:
            runner.run_once()
        self.assertEqual(self.outcome(runner)["status"], "no_new_cause")
        self.assertEqual(self.glib.calls, 0)
        self.assertLessEqual(len(queries.captured_queries), 3)

    def test_policy_changes_take_effect_at_the_next_check(self):
        # Latency is one check: CHECK_INTERVAL_SECONDS (15 s), or sooner on a wake.
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        runner = self.make_runner(engine)
        runner.run_once()
        self.assertEqual((runner.policy.mode, self.glib.calls), ("off", 0))
        self.set_policy(dry_run=True)
        runner.run_once()
        self.assertEqual((runner.policy.mode, self.outcome(runner)["kind"]), ("observation", "advisory"))
        HourRefillPolicy.objects.filter(pk=1).update(enabled=False)
        calls = self.glib.calls
        runner.run_once()
        self.assertEqual((runner.policy.mode, self.glib.calls), ("off", calls))
        self.assertEqual(runner_mod.CHECK_INTERVAL_SECONDS, 15.0)

    def test_no_active_log_abstains(self):
        engine = self.eject_engine()
        engine.current_log = None
        self.set_policy()
        runner = self.make_runner(engine)
        runner.run_once()
        self.assertEqual((self.outcome(runner)["status"], self.glib.calls), ("no_active_log", 0))


# ---------------------------------------------------------------------------
# 4. Explicit development-only install path
# ---------------------------------------------------------------------------

@ARMED
class InstallPathTests(RunnerFixtures, TestCase):
    def setUp(self):
        self.build()

    def test_eject_end_to_end_installs_once_and_reports_committed_not_aired(self):
        engine = self.eject_engine()
        a, b = self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        runner.run_once()
        outcome = self.outcome(runner)
        self.record_status("02-install-eject-committed", runner)
        self.assertEqual((outcome["kind"], outcome["status"]), ("committed", "installed"), outcome)
        self.assertEqual(sorted(outcome["track_ids"]), sorted([a.id, b.id]))
        self.assertEqual(outcome["placement_mode"], "tail_append")
        self.assertEqual(outcome["cause_ids"], [HourRefillCause.objects.get().id])
        self.assertEqual(outcome["category_ids"], [self.gold.id, self.gold.id])
        ledger = HourRefillInstall.objects.get()
        self.assertEqual(outcome["installed_log_item_ids"], ledger.installed_log_item_ids)
        self.assertLessEqual(len(ledger.track_ids), 3)
        self.assertEqual(self.rows()[:len(before)], before)
        self.assertEqual(runner.last_install["adoption"], "pending")
        self.assertEqual(self.glib.calls, 3)  # runner pre-check capture + installer S1, S2
        self.assertFalse(PlayEvent.objects.exists())
        self.assertFalse(LogItem.objects.filter(id__in=ledger.installed_log_item_ids)
                         .exclude(played_at=None, playback_claimed_at=None).exists())

        # The unchanged reload adopts the rows; the runner observes it later.
        self.reload(engine)
        runner.run_once()
        self.assertEqual(runner.last_install["adoption"], "observed")
        self.assertEqual(self.outcome(runner)["status"], "no_new_cause")  # cause set now covered
        self.assertEqual(HourRefillInstall.objects.count(), 1)

        # Committed is never aired; only an existing PlayEvent says so.
        installs = status_mod.recent_installs()
        self.assertEqual([i["aired_at"] for i in installs[0]["items"]], [None, None])
        first = ledger.installed_log_item_ids[0]
        PlayEvent.objects.create(track_id=ledger.track_ids[0], log_item_id_snapshot=first, started_at=at(14, 45))
        installs = status_mod.recent_installs()
        self.assertEqual([i["aired_at"] is not None for i in installs[0]["items"]], [True, False])

    def test_a_fresh_runner_after_restart_never_installs_the_same_set_again(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0), self.track(170.0), self.track(160.0)
        self.set_policy(dry_run=False)
        self.make_runner(engine).run_once()
        self.reload(engine)
        restarted = self.make_runner(engine)
        restarted.run_once()
        self.assertEqual(self.outcome(restarted)["status"], "no_new_cause")
        self.assertEqual(HourRefillInstall.objects.count(), 1)

    def test_request_cause_from_another_process_is_found_by_polling(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        self.row(self.track(240.0), 1, played=True)
        original = self.track(300.0)
        swapped = self.row(self.track(210.0), 2)
        self.row(self.track(290.0), 3)
        request = SongRequest.objects.create(external_request_id="d-req", track=swapped.track,
                                             submitted_at=at(14, 1), status="scheduled", log_item=swapped,
                                             requester_name="Private Person", dedication_message="Hidden")
        engine = self.engine(cursor=2, deck_index=1, deck_position=10.0)
        self.track(240.0), self.track(215.0)
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        runner.run_once()
        self.assertEqual(self.outcome(runner)["status"], "no_new_cause")
        self.request_cause(swapped, original)  # as the reconciler's own transaction would
        runner.run_once()
        self.record_status("03-install-request-committed", runner)
        self.assertEqual(self.outcome(runner)["status"], "installed", self.outcome(runner))
        request.refresh_from_db()
        self.assertEqual((request.status, request.log_item_id, request.fulfilled_at), ("scheduled", swapped.id, None))
        text = json.dumps(runner.status()) + self.status_path.read_text()
        for secret in ("Private Person", "Hidden", "d-req"):
            self.assertNotIn(secret, text)

    def test_engine_path_evidence_wakes_the_runner(self):
        runner = self.make_runner(self.eject_engine())
        recorder = evidence_mod.HourRefillEvidenceRecorder(read_flags=lambda: (True, True), insert=lambda e: None,
                                                            on_recorded=runner.wake)

        class Evidence:
            @staticmethod
            def cause_fields():
                return {"cause_type": "request_substitution"}

        with patch.object(evidence_mod, "close_old_connections"):
            recorder._write(Evidence())
        self.assertTrue(runner._wake.is_set())
        self.assertEqual(runner.counters["wakes"], 1)
        # A failing insert or a disabled trigger never wakes it.
        runner._wake.clear()
        recorder._read_flags = lambda: (False, False)
        with patch.object(evidence_mod, "close_old_connections"):
            recorder._write(Evidence())
        self.assertFalse(runner._wake.is_set())

    def test_mixed_causes_use_the_strict_threshold(self):
        # Net 45 s: enough for an Eject-only set (>30 s), not for a mixed set (>60 s).
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        original = self.track(600.0)
        swapped = self.row(self.track(470.0), 3)
        self.row(self.track(454.25), 4)
        self.eject_cause(ejected, minute=20)
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        self.set_policy(dry_run=True)
        runner = self.make_runner(engine)
        runner.run_once()
        self.assertEqual((self.outcome(runner)["net_shortage_seconds"], self.outcome(runner)["threshold_seconds"]),
                         (45.0, 30.0))
        self.request_cause(swapped, original)
        runner.run_once()
        self.assertEqual((self.outcome(runner)["status"], self.outcome(runner)["threshold_seconds"]),
                         ("below_threshold", 60.0))

    def test_exact_thresholds_do_not_qualify(self):
        # Eject-only: window 1200 s; runway 230 + f1 + f2 + 0.75.
        for future, net, below in (((470.0, 469.25), 30.0, True), ((470.0, 469.0), 30.25, False)):
            with self.subTest(net=net):
                LogItem.objects.all().delete()
                HourRefillCause.objects.all().delete()
                PlaylistLog.objects.exclude(pk=self.log.pk).delete()
                self.eject_hour(future)
                engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
                self.set_policy(dry_run=False)
                runner = self.make_runner(engine)
                runner.run_once()
                outcome = self.outcome(runner)
                self.assertEqual(outcome["net_shortage_seconds"], net)
                self.assertEqual(outcome["status"] == "below_threshold", below, outcome)
                self.assertFalse(HourRefillInstall.objects.exists())  # 30.25 s cannot hold a 30 s track + 6 s

    def test_exact_sixty_for_a_request_does_not_qualify(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        self.row(self.track(240.0), 1, played=True)
        original = self.track(600.0)
        swapped = self.row(self.track(470.0), 2)  # on deck at 10 s: 460 left
        self.row(self.track(679.5), 3)
        self.request_cause(swapped, original)
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        runner.run_once()
        outcome = self.outcome(runner)
        self.assertEqual((outcome["net_shortage_seconds"], outcome["status"]), (60.0, "below_threshold"))

    def test_long_deck_false_deficit_regression(self):
        # The historic Grateful Dead case: one very long live deck.
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(1620.0), 2, played=True)
        self.eject_cause(ejected)
        engine = self.engine(cursor=3, deck_index=2, deck_position=30.0)
        self.track(200.0)
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine, now=at(14, 33, 30))
        runner.run_once()
        self.assertEqual(self.outcome(runner)["status"], proj.OUTCOME_ALREADY_SATISFIED)
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_protected_closer_insert_before(self):
        engine = self.closer_engine()
        self.track(300.0, self.recur), self.track(290.0, self.recur)
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        runner.run_once()
        outcome = self.outcome(runner)
        self.assertEqual((outcome["status"], outcome["placement_mode"]), ("installed", "insert_before"), outcome)
        ids = [r[0] for r in self.rows()]
        self.assertEqual(ids[-2:], [self.closer_psa.id, self.closer_legal.id])

    def test_transport_and_ownership_blocks_spend_no_install_attempt(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)
        for attribute, value, reason in (
                ("manual_mode", True, "manual_mode"), ("remote_dj_session", object(), "remote_dj"),
                ("_next_triggered", True, "transition_pending"), ("_start_next_active", object(), "start_next"),
                ("_vt", {"phase": "outro"}, "voice_track"), ("_live_fill_in_progress", True, "live_fill_pending")):
            with self.subTest(reason):
                saved = getattr(engine, attribute)
                setattr(engine, attribute, value)
                try:
                    runner = self.make_runner(engine)
                    runner.run_once()
                finally:
                    setattr(engine, attribute, saved)
                outcome = self.outcome(runner)
                self.assertEqual((outcome["status"], outcome["reasons"]), ("blocked_transport", [reason]))
                self.assertEqual(engine._refill_coordinator.attempts, {})
        engine._any_mic_live = lambda: True
        runner = self.make_runner(engine)
        runner.run_once()
        self.assertEqual(self.outcome(runner)["reasons"], ["mic_live"])
        self.assertEqual(runner.counters["live_fill_conflicts"], 0)
        self.assert_no_writes(before)

    def test_stale_queue_between_runner_and_installer_snapshots(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)

        def reorder():
            engine.log_items[3], engine.log_items[4] = engine.log_items[4], engine.log_items[3]

        runner = self.make_runner(engine, installer_hooks={"before_second_snapshot": reorder})
        runner.run_once()
        self.assertEqual((self.outcome(runner)["status"], self.outcome(runner)["reasons"]),
                         ("stale_discarded", ["queue_or_deck_changed"]))
        self.assert_no_writes(before)

    def test_policy_turned_off_inside_the_install_transaction_writes_nothing(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)

        def disable():
            HourRefillPolicy.objects.filter(pk=1).update(dry_run_only=True)

        runner = self.make_runner(engine, installer_hooks={"locked": disable})
        runner.run_once()
        self.assertEqual((self.outcome(runner)["status"], self.outcome(runner)["reasons"]),
                         ("policy_refused", ["policy_changed_before_commit"]))
        self.assert_no_writes(before)

    def test_cause_switch_turned_off_between_snapshots_refuses(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)

        def switch_off():
            HourRefillPolicy.objects.filter(pk=1).update(capture_operator_eject=False)

        runner = self.make_runner(engine, installer_hooks={"before_second_snapshot": switch_off})
        runner.run_once()
        self.assertEqual((self.outcome(runner)["status"], self.outcome(runner)["reasons"]),
                         ("policy_refused", ["not_permitted_for_cause_types"]))
        self.assert_no_writes(before)


# ---------------------------------------------------------------------------
# 5. Backoff, coalescing and audio isolation
# ---------------------------------------------------------------------------

class BackoffAndIsolationTests(RunnerFixtures, TestCase):
    def setUp(self):
        self.build()

    def test_backoff_and_per_cause_set_cap(self):
        engine = self.eject_engine()
        engine.manual_mode = True  # a transient block
        self.set_policy()
        runner = self.make_runner(engine)
        for _ in range(3):
            runner.run_once()  # second and third are inside the 30 s backoff
        self.assertEqual((runner.counters["evaluations"], runner.counters["deferred_backoff"]), (1, 2))
        for _ in range(10):
            self.advance(runner_mod.RETRY_SECONDS)
            runner.run_once()
        self.assertEqual(runner.counters["evaluations"], runner_mod.MAX_EVALUATIONS_PER_CAUSE_SET)
        self.assertEqual(runner.abstentions["evaluation_limit"], 1)
        self.assertEqual(self.glib.calls, runner_mod.MAX_EVALUATIONS_PER_CAUSE_SET)
        # A new cause is a new set and is evaluated again at once.
        self.eject_cause(self.played, minute=10)
        runner.run_once()
        self.assertEqual(runner.counters["evaluations"], runner_mod.MAX_EVALUATIONS_PER_CAUSE_SET + 1)

    def test_one_outstanding_capture_and_snapshot_timeout(self):
        engine = self.eject_engine()
        self.set_policy()
        runner = self.make_runner(engine)
        self.glib.deferred = True
        with patch.object(inst.request_snapshot, "__kwdefaults__", {"timeout": 0.05, "wall_clock": None}):
            runner.run_once()
            self.assertEqual(self.outcome(runner)["status"], "snapshot_unavailable")
            self.advance(runner_mod.RETRY_SECONDS)
            runner.run_once()  # the first capture is still queued: not scheduled again
        self.assertEqual((self.glib.calls, len(self.glib.queued)), (1, 1))
        self.assertEqual(runner.counters["capture_coalesced"], 1)
        self.glib.flush()  # the late callback lands in an abandoned mailbox
        self.assertFalse(runner._capture_outstanding)

    def test_idle_add_failure_is_contained(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        self.glib.fail = True
        with ARMED:
            self.assertFalse(runner.run_once())
        self.assertEqual((self.outcome(runner)["status"], runner.counters["errors"]), ("error", 1))
        self.assertFalse(runner._capture_outstanding)
        self.assertIsNone(engine._refill_coordinator.refill_owner)
        self.assert_no_writes(before)

    def test_database_failure_is_contained(self):
        engine = self.eject_engine()
        self.set_policy()
        runner = self.make_runner(engine)
        with patch.object(runner_mod.HourRefillRunner, "_cause_ids", side_effect=RuntimeError("db down")):
            self.assertFalse(runner.run_once())
        self.assertEqual(self.outcome(runner)["status"], "error")
        self.assertEqual(self.glib.calls, 0)

    def test_installer_failure_releases_the_coordinator(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        self.set_policy(dry_run=False)

        def boom(*args):
            raise RuntimeError("fault after reserve")

        runner = self.make_runner(engine, installer_hooks={"before_transaction": boom})
        with ARMED:
            self.assertFalse(runner.run_once())
        self.assertIsNone(engine._refill_coordinator.refill_owner)
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_live_fill_in_flight_is_counted_as_ownership_conflict(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        self.set_policy(dry_run=False)
        runner = self.make_runner(engine)
        # Legacy callback scheduled but not run: the installer must refuse.
        engine._refill_coordinator.mark_legacy_pending(7)
        with ARMED:
            runner.run_once()
        self.assertEqual(self.outcome(runner)["status"], inst.STATUS_LIVE_FILL_PENDING)
        self.assertEqual(runner.counters["live_fill_conflicts"], 1)
        self.assert_no_writes(before)

    def test_runner_source_has_no_glib_or_timer_surface(self):
        import ast

        tree = ast.parse((REPO / "library/services/hour_refill_runner.py").read_text())
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                used.add(node.module or "")
            elif isinstance(node, ast.Import):
                used.update(alias.name for alias in node.names)
        for forbidden in ("GLib", "Gst", "gi", "gi.repository", "timeout_add", "idle_add_full", "sleep",
                          "_poll_position", "_start_next_track", "_reload_queue_if_changed", "decks"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, used)
        engine_source = (REPO / "library/services/engine.py").read_text()
        self.assertIn("HourRefillRunner(self, idle_add=GLib.idle_add)", engine_source)
        self.assertNotIn("HourRefillInstaller", engine_source)
        self.assertNotIn(".attempt(", engine_source)
        self.assertNotIn("run_once", engine_source)


# ---------------------------------------------------------------------------
# 6. Status document and PSA input
# ---------------------------------------------------------------------------

class StatusAndPsaTests(RunnerFixtures, TestCase):
    def setUp(self):
        self.build()

    def test_status_file_is_atomic_bounded_and_private(self):
        engine = self.eject_engine()
        HourRefillCause.objects.all().delete()
        self.set_policy()
        runner = self.make_runner(engine)
        runner.run_once()
        runner.run_once()
        self.assertEqual(runner.counters["status_writes"], 1)  # unchanged: no second write
        self.advance(runner_mod.HEARTBEAT_SECONDS)
        runner.run_once()
        self.assertEqual(runner.counters["status_writes"], 2)  # heartbeat
        self.assertEqual(oct(self.status_path.stat().st_mode & 0o777), "0o644")
        self.assertEqual(list(self.status_path.parent.iterdir()), [self.status_path])  # no temp left behind
        doc, freshness = status_mod.read_runner_status(self.status_path)
        self.assertEqual((freshness, doc["policy"]["mode"], doc["schema"]), ("fresh", "observation", 1))
        self.assertIn("not proof of air", " ".join(doc["notes"]))

    def test_stale_or_missing_status_is_unknown(self):
        missing = Path(tempfile.mkdtemp()) / "none.json"
        self.assertEqual(status_mod.read_runner_status(missing), (None, "missing"))
        engine = self.eject_engine()
        runner = self.make_runner(engine)
        runner.run_once()
        doc, freshness = status_mod.read_runner_status(
            self.status_path, now=at(14, 40) + __import__("datetime").timedelta(days=9999))
        self.assertEqual(freshness, "stale")
        self.status_path.write_text("{not json")
        self.assertEqual(status_mod.read_runner_status(self.status_path), (None, "unreadable"))

    def test_psa_input_is_off_unless_explicitly_configured(self):
        self.assertEqual(runner_mod.psa_policy_from_settings(), svc.PsaResidualPolicy())
        with override_settings(HOUR_REFILL_PSA_CATEGORY_ID="x"):
            self.assertEqual(runner_mod.psa_policy_from_settings(), svc.PsaResidualPolicy(True, None))
        with override_settings(HOUR_REFILL_PSA_CATEGORY_ID=str(self.psa.id)):
            self.assertEqual(runner_mod.psa_policy_from_settings(), svc.PsaResidualPolicy(True, self.psa.id))

    def test_psa_follows_a_music_fit_and_never_runs_alone(self):
        engine = self.closer_engine()
        music = self.track(300.0, self.recur)
        self.track(260.0, self.recur)
        psa = self.track(40.0, self.psa)
        self.set_policy()
        cases = ((None, "psa_disabled"), ("bogus", "psa_not_configured"),
                 (str(self.recur.id), "psa_category_not_psa_kind"), (str(self.psa.id), "psa_proposed"))
        for setting, expected in cases:
            with self.subTest(setting), override_settings(HOUR_REFILL_PSA_CATEGORY_ID=setting or ""):
                runner = self.make_runner(engine)
                runner.run_once()
                outcome = self.outcome(runner)
                self.assertEqual(outcome["psa_status"], expected, outcome)
                if expected == "psa_proposed":
                    self.assertEqual(outcome["track_ids"][-1], psa.id)
                    self.assertIn(music.id, outcome["track_ids"])
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_subthreshold_gap_gets_no_psa(self):
        self.eject_hour((470.0, 469.25))  # net exactly 30 s
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        self.track(25.0, self.psa)
        self.set_policy()
        with override_settings(HOUR_REFILL_PSA_CATEGORY_ID=str(self.psa.id)):
            runner = self.make_runner(engine)
            runner.run_once()
        outcome = self.outcome(runner)
        self.assertEqual(outcome["status"], "below_threshold")
        self.assertNotIn("psa_status", outcome)


# ---------------------------------------------------------------------------
# 7. Thread lifecycle (real threads, own connections)
# ---------------------------------------------------------------------------

class LifecycleTests(RunnerFixtures, TransactionTestCase):
    def setUp(self):
        ensure_schedule_profile_state()
        self.build()

    def tearDown(self):
        connections.close_all()

    def _runner(self, **kwargs):
        engine = self.eject_engine()
        runner = self.make_runner(engine, check_interval=60.0, **kwargs)
        checked = threading.Semaphore(0)
        original = runner.run_once

        def counted(generation=None):
            try:
                return original(generation)
            finally:
                checked.release()

        runner.run_once = counted
        return runner, checked

    def test_repeated_start_makes_one_thread_and_stop_is_bounded(self):
        runner, checked = self._runner()
        self.assertTrue(runner.start())
        self.assertFalse(runner.start())
        self.assertTrue(checked.acquire(timeout=10))
        names = [t.name for t in threading.enumerate() if t.name == "hour-refill-runner"]
        self.assertEqual(names, ["hour-refill-runner"])
        started = time.monotonic()
        self.assertTrue(runner.stop())
        self.assertLess(time.monotonic() - started, runner_mod.STOP_JOIN_SECONDS + 0.5)
        self.assertFalse(runner.alive)
        self.assertEqual([t for t in threading.enumerate() if t.name == "hour-refill-runner"], [])

    def test_wake_triggers_a_prompt_coalesced_check(self):
        runner, checked = self._runner()
        runner.start()
        self.assertTrue(checked.acquire(timeout=10))  # the immediate first check
        for _ in range(5):
            runner.wake()
        self.assertTrue(checked.acquire(timeout=10))  # a prompt check, well before the 60 s interval
        checked.acquire(timeout=0.3)  # at most one more if a wake landed mid-check
        runner.stop()
        self.assertLessEqual(runner.counters["checks"], 3)  # coalesced, never one per wake

    def test_stop_during_a_blocked_check_never_waits_or_doubles_the_thread(self):
        entered, release = threading.Event(), threading.Event()
        runner, checked = self._runner()

        def slow_policy():
            entered.set()
            release.wait(10)
            return runner_mod.PolicyState("off", "test")

        with patch.object(runner_mod, "read_policy_state", side_effect=slow_policy):
            runner.start()
            self.assertTrue(entered.wait(10))
            started = time.monotonic()
            self.assertFalse(runner.stop(join_seconds=0.1))  # in flight: not cancelled, not waited for
            self.assertLess(time.monotonic() - started, 1.0)
            writes = runner.counters["status_writes"]
            runner.start()  # refused while the old thread is still alive
            self.assertEqual(sum(t.name == "hour-refill-runner" for t in threading.enumerate()), 1)
            release.set()
            runner._thread.join(10)
        self.assertFalse(runner._thread.is_alive())
        self.assertEqual(runner.counters["status_writes"], writes + 1)  # its final, still-current publish

    def test_a_stale_generation_never_publishes(self):
        runner, _ = self._runner()
        runner._generation = 5
        runner._publish(4)
        self.assertFalse(self.status_path.exists())

    def test_policy_lock_held_by_an_operator_save_makes_install_wait_or_abstain(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        self.set_policy(dry_run=False)
        held, done = threading.Event(), threading.Event()

        def operator_save():
            try:
                with transaction.atomic():
                    HourRefillPolicy.objects.select_for_update().get(pk=1)
                    held.set()
                    done.wait(10)
            finally:
                connections.close_all()

        t = threading.Thread(target=operator_save)
        t.start()
        self.assertTrue(held.wait(10))
        runner = self.make_runner(engine)
        with ARMED:
            runner.run_once()
        done.set()
        t.join(10)
        self.assertEqual((self.outcome(runner)["status"], self.outcome(runner)["reasons"]),
                         ("lock_contended", ["lock_timeout_or_deadlock"]))
        self.assertFalse(HourRefillInstall.objects.exists())


# ---------------------------------------------------------------------------
# 8. Admin surface
# ---------------------------------------------------------------------------

@override_settings(SECURE_SSL_REDIRECT=False)
class AdminSurfaceTests(RunnerFixtures, TestCase):
    def setUp(self):
        self.url = reverse("admin:library_hourrefillpolicy_changelist")
        self.admin = User.objects.create_superuser("radmin", "r@example.invalid", "pw")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.status_file = Path(tmp.name) / "state.json"
        patcher = patch.object(status_mod, "STATUS_PATH", self.status_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_access_requires_admin_permissions(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)  # anonymous -> login
        dj = User.objects.create_user("dj", password="pw")
        self.client.force_login(dj)
        self.assertEqual(self.client.get(self.url).status_code, 302)  # not staff
        staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)  # no model permission
        HourRefillPolicy().save()
        change = reverse("admin:library_hourrefillpolicy_change", args=[1])
        self.assertEqual(self.client.get(change).status_code, 403)
        staff.user_permissions.add(Permission.objects.get(codename="view_hourrefillpolicy"))
        staff = User.objects.get(pk=staff.pk)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(change).status_code, 200)
        self.assertEqual(self.client.post(change, {"enabled": "on", "dry_run_only": "on"}).status_code, 403)
        self.assertFalse(HourRefillPolicy.objects.get().enabled)

    def test_get_never_creates_the_policy_row(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("admin:library_hourrefillpolicy_add"), fetch_redirect_response=False)
        self.client.get(reverse("admin:library_hourrefillpolicy_add"))
        self.assertFalse(HourRefillPolicy.objects.exists())

    def test_page_shows_enabled_and_dry_run_separately_and_unknown_runner(self):
        self.client.force_login(self.admin)
        HourRefillPolicy(enabled=True, capture_operator_eject=True).save()
        page = self.client.get(reverse("admin:library_hourrefillpolicy_change", args=[1])).content.decode()
        for text in ("Observation (dry run): advisory proposals only", "Master switch", "Observation only (dry run)",
                     "removed by reviewed migration 0096", "Host armed (this web process)", "unknown (missing status)",
                     "greater than 60 s", "greater than 30 s", "provisional engineering margin, not measured",
                     "Only an existing play record shows that a row aired"):
            self.assertIn(text, page)
        self.assertNotIn(">Enabled<", page)

    def test_form_refuses_install_while_the_database_gate_is_present(self):
        self.client.force_login(self.admin)
        HourRefillPolicy().save()
        change = reverse("admin:library_hourrefillpolicy_change", args=[1])
        with patch.object(status_mod, "dry_run_gate_present", return_value=True):
            response = self.client.post(change, {"enabled": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("gate migration (library 0096) has not been applied", response.content.decode())
        self.assertTrue(HourRefillPolicy.objects.get().dry_run_only)

    def test_save_is_audited_and_install_mode_needs_the_host(self):
        self.client.force_login(self.admin)
        HourRefillPolicy().save()
        change = reverse("admin:library_hourrefillpolicy_change", args=[1])
        with patch("library.admin.emit_config_change_event") as audit, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(change, {"enabled": "on", "capture_operator_eject": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(audit.call_args.kwargs["changed_fields"], ["enabled", "capture_operator_eject", "dry_run_only"])
        self.assertEqual(audit.call_args.kwargs["apply_modes"]["dry_run_only"],
                         "next_hour_refill_runner_check_and_every_install_transaction")
        row = HourRefillPolicy.objects.get()
        self.assertEqual((row.enabled, row.dry_run_only), (True, False))  # unchecked box clears dry run
        page = self.client.get(change).content.decode()
        self.assertIn("Install requested, but this host is not armed", page)
        with ARMED:
            page = self.client.get(change).content.decode()
        self.assertIn("Install permitted", page)

    def test_status_panel_reads_runner_status_without_requester_data(self):
        self.client.force_login(self.admin)
        HourRefillPolicy(enabled=True).save()
        from django.utils import timezone

        self.status_file.write_text(json.dumps({
            "schema": 1, "written_at": timezone.now().isoformat(),
            "runner": {"running": True, "last_check_at": "2027-06-01T14:40:00-05:00"},
            "policy": {"mode": "observation", "host_armed": False}, "psa_residual": "off",
            "last_outcome": {"at": "2027-06-01T14:40:00-05:00", "kind": "advisory", "status": "proposed",
                             "track_ids": [11, 12], "net_shortage_seconds": 389.25},
            "counters": {"advisory_proposals": 1, "live_fill_conflicts": 2}, "abstentions": {"below_threshold": 3},
            "evidence_counters": {"recorded": 4, "dropped_full": 1}, "live_fill": {"legacy_discards": 0},
        }))
        page = self.client.get(reverse("admin:library_hourrefillpolicy_change", args=[1])).content.decode()
        for text in ("running", "advisory", "proposed", "[11, 12]", "389.25", "4 / 1"):
            self.assertIn(text, page)
