"""D2A races with real concurrent PostgreSQL sessions (see test_concurrency:
one session pauses inside a service transaction after taking its locks; the
competitor must BLOCK, then act on the committed state)."""
import threading

from django.db import connections

from active_messages import services, worker
from active_messages.models import ActiveBroadcastExecution
from production.errors import PurgeRefused
from production.services import retention

from .d2a_support import fake_rendition
from .test_aggregate_binding import result_of
from .test_concurrency import WAIT, Sessions


class WorkerRaceTests(Sessions):
    def test_two_workers_render_one_execution_once(self):
        self.active(kind="urgent", seconds="10.000000")
        rendering_started, release = threading.Event(), threading.Event()
        renders = []

        def slow_render(execution):
            renders.append(execution.pk)
            rendering_started.set()
            assert release.wait(WAIT)
            return result_of(fake_rendition(execution.composition_snapshot))

        first, box = self.run_thread(lambda: worker.run_once(render_fn=slow_render))
        self.assertTrue(rendering_started.wait(WAIT))
        self.assertEqual(worker.run_once(render_fn=slow_render).result, "busy")     # the second worker skips
        release.set()
        first.join(WAIT)
        connections.close_all()
        self.assertIsNone(box["error"])
        self.assertEqual(box["result"].result, "ready")
        self.assertEqual(len(renders), 1)
        self.assertEqual(ActiveBroadcastExecution.objects.filter(state="ready").count(), 1)
        self.assertEqual(worker.run_once(render_fn=slow_render).result, "idle")     # the lock was released


class BindingRaceTests(Sessions):
    def setUp(self):
        super().setUp()
        self.message = self.active(kind="urgent", seconds="10.000000")
        self.execution = services.materialize_due_execution()
        self.mix = fake_rendition(self.execution.composition_snapshot)

    def test_a_clear_waiting_behind_a_binding_suppresses_the_ready_airing(self):
        bound, cleared = self.race(lambda: services.bind_execution_mix(self.execution, self.mix),
                                   lambda: services.clear_message(self.message, actor=self.admin),
                                   "production.services.retention.lock_for_binding")
        self.assertIsNone(bound["error"])
        self.assertIsNone(cleared["error"])
        stored = ActiveBroadcastExecution.objects.get(pk=self.execution.pk)
        self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"))

    def test_a_binding_waiting_behind_a_clear_binds_nothing(self):
        cleared, bound = self.race(lambda: services.clear_message(self.message, actor=self.admin),
                                   lambda: services.bind_execution_mix(self.execution, self.mix),
                                   "active_messages.services._suppress_stale")
        self.assertIsNone(cleared["error"])
        self.assertIsInstance(bound["error"], services.MixRefused)
        self.assertEqual(bound["error"].code, "execution_not_pending")
        stored = ActiveBroadcastExecution.objects.get(pk=self.execution.pk)
        self.assertEqual((stored.state, stored.mix_media_id), ("suppressed", None))

    def test_a_purge_waits_for_a_binding_and_then_refuses(self):
        bound, purged = self.race(lambda: services.bind_execution_mix(self.execution, self.mix),
                                  lambda: retention.purge_media(self.mix),
                                  "production.services.retention.lock_for_binding")
        self.assertIsNone(bound["error"])
        self.assertIsInstance(purged["error"], PurgeRefused)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).mix_media_id, self.mix.pk)

    def test_two_bindings_of_different_renditions_leave_exactly_one_aggregate(self):
        other = fake_rendition(self.execution.composition_snapshot)
        first, second = self.race(lambda: services.bind_execution_mix(self.execution, self.mix),
                                  lambda: services.bind_execution_mix(self.execution, other),
                                  "active_messages.services._lock_execution")
        self.assertIsNone(first["error"])
        self.assertIsInstance(second["error"], services.MixRefused)
        self.assertEqual(second["error"].code, "execution_not_pending")
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).mix_media_id, self.mix.pk)
