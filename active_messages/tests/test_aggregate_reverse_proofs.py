"""D2A reverse proofs: remove one barrier and show the outcome it prevents."""
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

from django.db import connections
from django.test import TestCase

from active_messages import services, worker
from active_messages.models import ActiveBroadcastExecution
from production.services import reference_audio
from production.tests.support import ffmpeg_available

from .d2a_support import RealAudio, fake_rendition, stinger
from .test_aggregate_binding import result_of
from .test_concurrency import WAIT, Sessions


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class FrozenPinReverseProofTests(RealAudio, TestCase):
    def test_a_client_that_re_resolved_the_key_could_never_make_the_execution_ready(self):
        """Barrier 1 removed (open the key's CURRENT registration instead of the
        frozen pin): the render uses the newer bytes -- and barrier 2, the
        binding's provenance proof against the frozen snapshot, refuses it."""
        self.use_stinger()
        self.real_active(kind="urgent", seconds=1.0)
        execution = services.materialize_due_execution()
        stinger(self.admin, frequency=2500.0)                                # a newer registration, same key

        from production.services.render import client
        exact = client._open_pinned_stinger

        def current_not_pinned(pin):
            resolved = reference_audio.resolve(pin["key"])
            resolved.close()
            return exact({**pin, "reference": resolved.reference_id, "media": str(resolved.media.pk),
                          "sha256": resolved.sha256})
        with mock.patch("production.services.render.client._open_pinned_stinger", current_not_pinned):
            outcome = worker.run_once()
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((outcome.result, outcome.code), ("failed", "provenance_mismatch"))
        self.assertEqual((stored.state, stored.mix_media_id), ("failed", None))


class ProvenanceReverseProofTests(RealAudio, TestCase):
    def test_without_the_provenance_proof_a_wrong_aggregate_would_go_ready(self):
        self.active(kind="urgent", seconds="10.000000")
        execution = services.materialize_due_execution()
        wrong = fake_rendition(execution.composition_snapshot, digest="e" * 64)
        with self.assertRaises(services.MixRefused):
            services.bind_execution_mix(execution, wrong)                    # the real proof refuses
        with mock.patch("active_messages.rendering.proves", return_value=True):
            self.assertEqual(services.bind_execution_mix(execution, wrong).state, "ready")


class WorkerLockReverseProofTests(Sessions):
    def test_without_the_worker_lock_two_workers_render_twice_yet_bind_once(self):
        self.active(kind="urgent", seconds="10.000000")
        both_rendering, release = threading.Barrier(2, timeout=WAIT), threading.Event()
        renders = []

        def slow_render(execution):
            renders.append(execution.pk)
            both_rendering.wait()
            assert release.wait(WAIT)
            return result_of(fake_rendition(execution.composition_snapshot))

        @contextmanager
        def no_lock():
            yield True
        with mock.patch("active_messages.worker.worker_lock", no_lock):
            one, one_box = self.run_thread(lambda: worker.run_once(render_fn=slow_render))
            two, two_box = self.run_thread(lambda: worker.run_once(render_fn=slow_render))
            release.set()
            one.join(WAIT)
            two.join(WAIT)
        connections.close_all()
        self.assertIsNone(one_box["error"])
        self.assertIsNone(two_box["error"])
        self.assertEqual(len(renders), 2)                                     # duplicate work: what the lock prevents
        self.assertEqual(sorted([one_box["result"].result, two_box["result"].result]), ["ready", "superseded"])
        self.assertEqual(ActiveBroadcastExecution.objects.filter(state="ready").count(), 1)   # authority still holds
