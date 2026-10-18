"""D3E-A: worker lifecycle and recovery hardening.

* A1 -- expiry proceeds while the current composition is permanently blocked
  (the sweep runs in the same serialized transaction that decides "blocked");
* A2 -- every non-progressing pass waits (one definition), progress is prompt
  but bounded, signals stop the loop promptly, database errors close the
  connection and wait;
* A3 -- the crash / interruption boundaries recover as their contracts say;
* A4 -- the worker heartbeat / readiness contract: test_worker_heartbeat;
* A5 -- with native airing closed nothing is published or dispatched.

Render functions are injected (exact rendition rows from d2a_support): no
render service is needed here; test_aggregate_render covers the real one.
"""
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from active_messages import services, worker
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent
from production.services.render.engine import RenderError, RenderResult

from .d2a_support import fake_rendition
from .support import Domain

X = ActiveBroadcastExecution
M = ActiveBroadcastMessage


def result_of(media):
    return RenderResult(media=media, recipe_key=media.recipe_key, recipe_version=media.recipe_version, params={},
                        provenance={}, params_digest=media.recipe_params_digest, toolchain=media.toolchain,
                        measured_input={})


def renders_ready(execution):
    return result_of(fake_rendition(execution.composition_snapshot))


def fails(code="render_failed"):
    return mock.Mock(side_effect=RenderError(code, "permanent"))


def events(name, **filters):
    return ActiveBroadcastMessageEvent.objects.filter(event=name, **filters).count()


class BlockedExpiryTests(Domain, TestCase):
    """A1: a permanently failed composition blocks only ITSELF -- never the
    expiry sweep that changes it."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.keep = self.active(kind="urgent", seconds="10.000000", now=self.now)
        self.going = self.active(seconds="10.000000", now=self.now)
        services.set_message_expiry(self.going, self.now + timedelta(seconds=60), actor=self.admin, now=self.now)
        first = worker.run_once(now=self.now, render_fn=fails())
        self.assertEqual((first.result, first.code), ("failed", "render_failed"))
        self.failed = X.objects.get(pk=first.execution)
        self.assertEqual(worker.run_once(now=self.now + timedelta(seconds=5), render_fn=fails()).result, "blocked")
        self.generation = services.current_policy().generation

    def test_expiry_proceeds_while_the_composition_is_blocked(self):
        later = self.now + timedelta(minutes=2)
        render = mock.Mock(side_effect=renders_ready)
        outcome = worker.run_once(now=later, render_fn=render)
        self.assertEqual(M.objects.get(pk=self.going.pk).state, "expired")
        self.assertEqual(events("expired", message=self.going), 1)
        self.assertEqual(services.current_policy().generation, self.generation + 1)
        # The changed composition is reassessed at once: only the remaining message, rendered READY.
        self.assertEqual(outcome.result, "ready")
        fresh = X.objects.get(pk=outcome.execution)
        self.assertEqual([c["message"] for c in fresh.composition_snapshot["components"]], [str(self.keep.pk)])
        self.assertEqual(fresh.policy_generation, self.generation + 1)
        # The failed occurrence is untouched and never re-rendered.
        self.assertEqual(X.objects.get(pk=self.failed.pk).state, "failed")
        self.assertEqual(render.call_count, 1)
        self.assertEqual(render.call_args[0][0].pk, fresh.pk)
        self.assertEqual(events("expired"), 1)                                   # no duplicate on later passes
        worker.run_once(now=later + timedelta(seconds=5), render_fn=render)
        self.assertEqual(events("expired"), 1)


class CommandCadenceReproductionTests(Domain, TestCase):
    """A2: the management command's loop must wait after a pass that made no
    progress -- including every playout (in-flight) result."""

    def state_file(self):
        import tempfile
        from pathlib import Path
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "worker.json"

    def test_the_command_waits_after_playout_passes(self):
        passes = [worker.Outcome("playout", code=code) for code in ("in_flight", "accepted", "publication_backoff")]
        with mock.patch.object(worker, "run_once", side_effect=passes + [SystemExit(0)]), \
                mock.patch("time.sleep") as sleep, mock.patch.object(worker, "close_old_connections"), \
                mock.patch.object(worker.heartbeat, "state_path", return_value=self.state_file()):
            with self.assertRaises(SystemExit):
                call_command("active_message_worker", stdout=mock.Mock())
        self.assertGreaterEqual(sleep.call_count, len(passes))


class BlockedCompositionTests(BlockedExpiryTests):
    """A1, continued (same blocked set-up)."""

    test_expiry_proceeds_while_the_composition_is_blocked = None

    def test_a_blocked_pass_with_nothing_to_expire_changes_nothing(self):
        before = (X.objects.count(), ActiveBroadcastMessageEvent.objects.count())
        render = mock.Mock()
        with mock.patch.object(services, "snapshot_active_composition", side_effect=AssertionError("no extra lock")):
            outcome = worker.run_once(now=self.now + timedelta(seconds=30), render_fn=render)
        self.assertEqual((outcome.result, outcome.code), ("blocked", "render_failed"))
        self.assertEqual((X.objects.count(), ActiveBroadcastMessageEvent.objects.count()), before)
        self.assertEqual(services.current_policy().generation, self.generation)
        render.assert_not_called()                              # the identical composition is never re-rendered

    def test_a_clear_while_blocked_is_not_resurrected_and_the_new_set_is_assessed(self):
        services.clear_message(self.keep, actor=self.admin)
        outcome = worker.run_once(now=self.now + timedelta(seconds=30), render_fn=renders_ready)
        self.assertEqual(outcome.result, "ready")
        fresh = X.objects.get(pk=outcome.execution)
        self.assertEqual([c["message"] for c in fresh.composition_snapshot["components"]], [str(self.going.pk)])
        self.assertEqual(M.objects.get(pk=self.keep.pk).state, "cleared")

    def test_expiring_the_whole_set_while_blocked_leaves_nothing_to_air(self):
        services.set_message_expiry(self.keep, self.now + timedelta(seconds=90), actor=self.admin, now=self.now)
        outcome = worker.run_once(now=self.now + timedelta(minutes=5), render_fn=mock.Mock())
        self.assertEqual(outcome.result, "idle")
        self.assertEqual(set(M.objects.filter(pk__in=[self.keep.pk, self.going.pk]).values_list("state", flat=True)),
                         {"expired"})
        self.assertEqual(events("expired"), 2)
        self.assertEqual(services.current_policy().generation, self.generation + 1)    # one sweep, one step
        self.assertFalse(X.objects.filter(state__in=("pending_render", "ready")).exists())

    def test_the_service_contract_without_blocking_codes_is_unchanged(self):
        execution = services.materialize_due_execution(now=self.now + timedelta(seconds=30))
        self.assertEqual(execution.state, "pending_render")                 # old callers: no blocking check
        self.assertEqual(execution.composition_digest, self.failed.composition_digest)


class LoopCadenceTests(TestCase):
    """A2: the one cadence rule, driven with a scripted pass sequence and a
    fake sleep -- deterministic, no wall clock."""

    def drive(self, results, *, stop_after=None, poll=5.0):
        script = [worker.Outcome(result, code=code) for result, code in results]
        calls, slept, stopped = [], [], []

        def run():
            calls.append(True)
            outcome = script[len(calls) - 1]
            if len(calls) == len(script):
                stopped.append(True)
            return outcome
        worker.run_forever(poll_seconds=poll, stop=lambda: bool(stopped), sleep=slept.append, run=run,
                           refresh=mock.Mock(), close=mock.Mock())
        return slept

    def test_every_result_that_made_no_progress_waits_a_full_poll(self):
        waiting = [("idle", ""), ("idle", "gated"), ("busy", ""), ("blocked", "render_failed"), ("backoff", ""),
                   ("retry", "render_unavailable"), ("error", "OperationalError")]
        waiting += [("playout", code) for code in ("in_flight", "dispatched", "resent", "accepted", "claimed",
                                                   "started", "publication_backoff", "publication_retry", "failed")]
        for result, code in waiting:
            with self.subTest(result=result, code=code):
                slept = self.drive([(result, code), ("idle", "")])
                self.assertEqual(sum(slept[:10]), 5.0)          # the first pass's wait, in slices
                self.assertTrue(worker.waits_after(worker.Outcome(result, code=code)))

    def test_progress_continues_at_once_but_is_bounded(self):
        for result in ("ready", "reused", "failed", "superseded"):
            self.assertFalse(worker.waits_after(worker.Outcome(result)))
        # Exactly MAX_PROGRESS_PASSES progress passes run back-to-back, then a wait.
        slept = self.drive([("ready", "")] * worker.MAX_PROGRESS_PASSES + [("idle", "")])
        self.assertEqual(sum(slept), 5.0)                      # after the 10th (the idle pass is followed by stop)
        self.assertEqual(self.drive([("ready", "")] * (worker.MAX_PROGRESS_PASSES - 1) + [("idle", "")]), [])
        passes, waits_after_pass = [], []

        def run():
            passes.append(True)
            return worker.Outcome("ready")

        def sleep(seconds):
            waits_after_pass.append(len(passes))
        stop = lambda: len(set(waits_after_pass)) >= 3  # noqa: E731
        worker.run_forever(stop=stop, sleep=sleep, run=run, refresh=mock.Mock(), close=mock.Mock())
        self.assertEqual(sorted(set(waits_after_pass)),
                         [worker.MAX_PROGRESS_PASSES * n for n in (1, 2, 3)])   # every 10th pass, never the 11th

    def test_a_stop_during_a_wait_returns_within_one_slice(self):
        stopped, slept = [], []

        def sleep(seconds):
            slept.append(seconds)
            stopped.append(True)                                # SIGTERM arrives during the first slice
        recorder = mock.Mock()
        worker.run_forever(poll_seconds=300, stop=lambda: bool(stopped), sleep=sleep,
                           run=lambda: worker.Outcome("idle"), recorder=recorder, refresh=mock.Mock(),
                           close=mock.Mock())
        self.assertEqual(slept, [worker.PAUSE_SLICE_SECONDS])
        recorder.stopped.assert_called_once_with()

    def test_a_stop_while_working_finishes_that_pass_then_stops(self):
        stopped, passes = [], []

        def run():
            passes.append(True)
            stopped.append(True)                                # arrives mid-pass
            return worker.Outcome("ready")
        close = mock.Mock()
        worker.run_forever(stop=lambda: bool(stopped), sleep=mock.Mock(), run=run, refresh=mock.Mock(), close=close)
        self.assertEqual(len(passes), 1)
        close.assert_called_once_with()                         # the connection is closed on the way out

    def test_a_database_failure_closes_the_connection_and_waits(self):
        from django.db import OperationalError
        outcomes, stopped, close, slept = [], [], mock.Mock(), []

        def run():
            if not outcomes:
                outcomes.append(True)
                raise OperationalError("server closed the connection unexpectedly")
            stopped.append(True)
            return worker.Outcome("idle")
        recorder = mock.Mock()
        worker.run_forever(stop=lambda: bool(stopped), sleep=slept.append, run=run, recorder=recorder,
                           refresh=mock.Mock(), close=close)
        first = recorder.cycle.call_args_list[0][0][0]
        self.assertEqual((first.result, first.code), ("error", "OperationalError"))
        self.assertEqual(close.call_count, 2)                    # after the failure, and on the way out
        self.assertEqual(sum(slept[:10]), worker.POLL_SECONDS)

    def test_any_other_exception_ends_the_loop_without_claiming_a_clean_stop(self):
        recorder = mock.Mock()
        with self.assertRaises(RuntimeError):
            worker.run_forever(stop=lambda: False, sleep=mock.Mock(), run=mock.Mock(side_effect=RuntimeError("bug")),
                               recorder=recorder, refresh=mock.Mock(), close=mock.Mock())
        recorder.stopped.assert_not_called()


class CommandLoopTests(Domain, TestCase):
    """A2: the real management command -- SIGTERM, heartbeat, connection."""

    def setUp(self):
        super().setUp()
        import signal
        import tempfile
        from pathlib import Path
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "worker.json"
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))

    def test_sigterm_while_idle_stops_promptly_records_stopped_and_closes_the_connection(self):
        import json
        import os
        import signal
        slices = []

        def sleep(seconds):
            slices.append(seconds)
            if len(slices) == 3:
                os.kill(os.getpid(), signal.SIGTERM)            # the real signal, handled by the command
        with mock.patch.object(worker, "run_once", return_value=worker.Outcome("idle")), \
                mock.patch("time.sleep", side_effect=sleep), \
                mock.patch.object(worker, "close_old_connections"), \
                mock.patch.object(worker, "connection") as connection, \
                mock.patch.object(worker.heartbeat, "state_path", return_value=self.state):
            call_command("active_message_worker", stdout=mock.Mock())
        self.assertEqual(len(slices), 3)                         # stopped inside the wait, not after it
        connection.close.assert_called_once_with()
        state = json.loads(self.state.read_text())
        self.assertEqual((state["phase"], state["result"], state["cycle"]), ("stopped", "idle", 1))


class CrashBoundaryTests(Domain, TestCase):
    """A3: the crash / interruption boundaries D3E-A adds tests for. (Reuse
    after a crash between render and bind, stinger reconciliation surviving a
    crash, render retry/backoff, publication retry and the engine-side
    recovery rules already have their own tests -- test_aggregate_binding,
    test_aggregate_reconcile, test_playout_publication, test_playout_recovery.)"""

    def setUp(self):
        super().setUp()
        self.message = self.active(kind="urgent", seconds="10.000000")

    def test_a_crash_before_materialization_commits_leaves_nothing_behind(self):
        with mock.patch.object(services, "_create_execution", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                worker.run_once(render_fn=renders_ready)
        self.assertFalse(X.objects.exists())
        self.assertEqual(events("execution_materialized"), 0)
        self.assertEqual(worker.run_once(render_fn=renders_ready).result, "ready")      # the next pass creates it
        self.assertEqual(X.objects.count(), 1)

    def test_a_crash_after_creation_renders_that_same_execution(self):
        created = services.materialize_due_execution()
        outcome = worker.run_once(render_fn=renders_ready)
        self.assertEqual((outcome.result, outcome.execution), ("ready", str(created.pk)))
        self.assertEqual(X.objects.count(), 1)

    def test_a_ready_execution_is_never_rendered_again_and_waits_behind_the_gate(self):
        worker.run_once(render_fn=renders_ready)
        render = mock.Mock()
        for _ in range(3):
            outcome = worker.run_once(render_fn=render)
            self.assertEqual((outcome.result, outcome.code), ("idle", "gated"))
        render.assert_not_called()
        self.assertEqual(X.objects.get().state, "ready")

    def test_lock_contention_skips_the_pass_without_touching_anything(self):
        with mock.patch.object(worker, "worker_lock") as lock:
            lock.return_value.__enter__.return_value = False
            self.assertEqual(worker.run_once(render_fn=mock.Mock()).result, "busy")
        self.assertFalse(X.objects.exists())


class ClosedGateTests(Domain, TestCase):
    """A5: with native airing closed the worker renders and binds, and nothing
    is published, dispatched or recorded as air."""

    def test_nothing_is_published_dispatched_or_aired(self):
        from library.models import LogItem, PlayEvent, Track

        from active_messages.models import ActiveBroadcastInsertion, ActiveBroadcastPublication
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
        self.active(kind="urgent", seconds="10.000000")
        self.active(seconds="10.000000")
        before = (Track.objects.count(), LogItem.objects.count(), PlayEvent.objects.count())
        with mock.patch("isadoraair.engine_commands.enqueue_engine_command",
                        side_effect=AssertionError("no engine command")):
            outcomes = [worker.run_once(render_fn=renders_ready) for _ in range(4)]
        self.assertEqual([o.result for o in outcomes], ["ready", "idle", "idle", "idle"])
        self.assertTrue(all(o.code == "gated" for o in outcomes[1:]))
        self.assertEqual(X.objects.get().state, "ready")
        self.assertFalse(X.objects.filter(state__in=("accepted", "claimed", "started")).exists())
        self.assertFalse(ActiveBroadcastPublication.objects.exists())
        self.assertFalse(ActiveBroadcastInsertion.objects.exists())
        self.assertEqual((Track.objects.count(), LogItem.objects.count(), PlayEvent.objects.count()), before)
