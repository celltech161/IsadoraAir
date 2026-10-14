"""D2A end to end with the REAL render and validation services: the frozen
aggregate is assembled in order, ingested as a validated rendition with exact
provenance, and bound PENDING_RENDER -> READY. Nothing airs."""
import unittest

from django.test import TestCase

from active_messages import rendering, services, worker
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessageEvent
from production.models import ProductionMedia
from production.services import layout
from production.tests.support import ffmpeg_available
from spoken.tests import support as audio

from .d2a_support import PA_HZ, STINGER_HZ, URGENT2_HZ, URGENT_HZ, RealAudio


def windows(media, frequency):
    return audio.energy_profile(layout.resolve_storage_path(media.storage_key), frequency)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class AggregateRenderTests(RealAudio, TestCase):
    def test_the_frozen_aggregate_is_assembled_in_order_and_bound_ready(self):
        self.use_stinger(seconds=1.0)
        pa = self.real_active(self.producer, kind="public_address", seconds=1.0, frequency=PA_HZ)
        urgent = self.real_active(self.producer2, kind="urgent", seconds=1.0, frequency=URGENT_HZ)
        outcome = worker.run_once()
        self.assertEqual(outcome.result, "ready", outcome)
        execution = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertEqual(execution.state, "ready")
        mix = execution.mix_media
        self.assertEqual((mix.kind, mix.validation_state, mix.recipe_key, mix.recipe_version),
                         ("rendition", "valid", "active_message_mix", 1))
        self.assertEqual(mix.recipe_params_digest, rendering.expected_digest(execution.composition_snapshot))
        self.assertEqual(mix.derived_from_id, urgent.message_media_id)            # first message in frozen order
        self.assertEqual((mix.sample_rate, mix.channels, mix.codec), (48000, 2, "flac"))
        self.assertAlmostEqual(float(mix.decoded_duration_seconds), 3.0, delta=0.05)
        # stinger [0,1), urgent [1,2), public address [2,3)
        threshold = 0.02
        stinger, first, last = windows(mix, STINGER_HZ), windows(mix, URGENT_HZ), windows(mix, PA_HZ)
        self.assertTrue(audio.present(stinger, 0.0, 1.0, threshold=threshold))
        self.assertTrue(audio.absent(stinger, 1.1, 3.0, threshold=threshold))
        self.assertTrue(audio.present(first, 1.0, 2.0, threshold=threshold))
        self.assertTrue(audio.absent(first, 0.0, 0.9, threshold=threshold))
        self.assertTrue(audio.present(last, 2.0, 3.0, threshold=threshold))
        self.assertTrue(audio.absent(last, 0.0, 1.9, threshold=threshold))
        self.assertIn("mix_bound", list(ActiveBroadcastMessageEvent.objects.filter(execution=execution)
                                        .values_list("event", flat=True)))
        self.assertTrue(pa)

    def test_public_address_only_has_no_stinger(self):
        self.use_stinger()
        self.real_active(self.producer, kind="public_address", seconds=1.0)
        outcome = worker.run_once()
        execution = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertIsNone(execution.composition_snapshot["stinger"])
        self.assertAlmostEqual(float(execution.mix_media.decoded_duration_seconds), 1.0, delta=0.05)
        self.assertTrue(audio.absent(windows(execution.mix_media, STINGER_HZ), 0.0, 1.0, threshold=0.02))

    def test_urgent_order_is_oldest_activation_first_and_the_stinger_plays_once(self):
        from datetime import timedelta

        from django.utils import timezone
        now = timezone.now()
        self.use_stinger(seconds=0.5)
        self.real_active(self.producer, kind="urgent", seconds=1.0, frequency=URGENT2_HZ, now=now + timedelta(seconds=1))
        self.real_active(self.producer2, kind="urgent", seconds=1.0, frequency=URGENT_HZ, now=now)
        outcome = worker.run_once(now=now + timedelta(seconds=2))
        mix = ActiveBroadcastExecution.objects.get(pk=outcome.execution).mix_media
        self.assertAlmostEqual(float(mix.decoded_duration_seconds), 2.5, delta=0.05)
        stinger = windows(mix, STINGER_HZ)
        self.assertTrue(audio.present(stinger, 0.0, 0.5, threshold=0.02))
        self.assertTrue(audio.absent(stinger, 0.75, 2.5, threshold=0.02))                 # once
        self.assertTrue(audio.present(windows(mix, URGENT_HZ), 0.5, 1.5, threshold=0.02))  # older first
        self.assertTrue(audio.present(windows(mix, URGENT2_HZ), 1.5, 2.5, threshold=0.02))

    def test_the_maximum_assembly_of_24_messages_plus_a_stinger_renders(self):
        from django.contrib.auth import get_user_model
        self.use_stinger(seconds=0.25)
        services.set_limits(actor=self.admin, max_active_messages=24)
        for index in range(24):
            producer = get_user_model().objects.create_user(f"m{index}", is_staff=True)
            self.real_active(producer, kind="urgent" if index < 12 else "public_address", seconds=0.25,
                             frequency=300.0 + 20 * index)
        outcome = worker.run_once()
        execution = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertEqual((outcome.result, execution.component_count), ("ready", 24), (outcome, execution.failure_detail))
        self.assertAlmostEqual(float(execution.mix_media.decoded_duration_seconds), 6.25, delta=0.1)

    def test_nothing_airs_and_no_foreign_rows_or_engine_commands_are_made(self):
        from unittest import mock

        from library.models import LogItem, PlayEvent, Track
        from spoken.models import SpokenAsset, SpokenItem
        models = (Track, LogItem, PlayEvent, SpokenItem, SpokenAsset)
        before = {model: model._base_manager.count() for model in models}
        self.real_active(kind="urgent")
        with mock.patch("isadoraair.engine_commands.enqueue_engine_command") as enqueue:
            self.assertEqual(worker.run_once().result, "ready")
            self.assertEqual(worker.run_once().result, "idle")               # READY waits for D2B
        enqueue.assert_not_called()
        self.assertEqual({model: model._base_manager.count() for model in models}, before)
        self.assertFalse(services.current_policy().native_airing_enabled)
        self.assertFalse(services.NATIVE_AIRING_AVAILABLE)
        self.assertEqual(set(ActiveBroadcastExecution.objects.values_list("state", flat=True)), {"ready"})


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class FrozenStingerTests(RealAudio, TestCase):
    """The execution renders its FROZEN stinger pin -- never the key's current
    registration -- and an unusable frozen pin never strands urgent content."""

    def test_a_degraded_stinger_at_materialization_renders_the_messages_without_it(self):
        from monitoring.models import SystemEvent

        from .support import corrupt_stored_bytes
        registration = self.use_stinger()
        corrupt_stored_bytes(registration.media)
        self.real_active(kind="urgent", seconds=1.0)
        with self.captureOnCommitCallbacks(execute=True):
            outcome = worker.run_once()
        execution = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertEqual(outcome.result, "ready")
        self.assertIsNone(execution.composition_snapshot["stinger"])
        self.assertEqual(execution.composition_snapshot["stinger_unavailable"], "reference_hash_mismatch")
        self.assertAlmostEqual(float(execution.mix_media.decoded_duration_seconds), 1.0, delta=0.05)
        self.assertTrue(SystemEvent.objects.filter(category="active_messages", level="warning").exists())

    def test_a_replaced_registration_after_materialization_does_not_change_the_render(self):
        old = self.use_stinger(frequency=STINGER_HZ)
        self.real_active(kind="urgent", seconds=1.0)
        execution = services.materialize_due_execution()
        self.assertEqual(execution.composition_snapshot["stinger"]["reference"], str(old.pk))
        from .d2a_support import stinger
        newer = stinger(self.admin, frequency=2500.0)                     # same key, new registration
        self.assertNotEqual(newer.pk, old.pk)
        outcome = worker.run_once()
        self.assertEqual((outcome.result, outcome.execution), ("ready", str(execution.pk)))
        mix = ActiveBroadcastExecution.objects.get(pk=execution.pk).mix_media
        self.assertEqual(mix.recipe_params_digest, rendering.expected_digest(execution.composition_snapshot))
        self.assertTrue(audio.present(windows(mix, STINGER_HZ), 0.0, 1.0, threshold=0.02))    # the FROZEN pin
        self.assertTrue(audio.absent(windows(mix, 2500.0), 0.0, 2.0, threshold=0.02))

    def test_an_unusable_frozen_pin_fails_that_occurrence_and_a_fresh_one_airs_the_messages(self):
        from monitoring.models import SystemEvent

        from .support import corrupt_stored_bytes
        registration = self.use_stinger()
        message = self.real_active(kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        snapshot_before = frozen.composition_snapshot
        corrupt_stored_bytes(registration.media)                          # after materialization, before render
        with self.captureOnCommitCallbacks(execute=True):
            outcome = worker.run_once()
        old = ActiveBroadcastExecution.objects.get(pk=frozen.pk)
        self.assertEqual((old.state, old.failure_code, old.mix_media_id), ("failed", "stinger_pin_unavailable", None))
        self.assertEqual(old.composition_snapshot, snapshot_before)        # never edited to pretend
        fresh = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertNotEqual(fresh.pk, old.pk)
        self.assertEqual((outcome.result, fresh.state), ("ready", "ready"))
        self.assertIsNone(fresh.composition_snapshot["stinger"])
        self.assertEqual(fresh.composition_snapshot["components"][0]["message"], str(message.pk))
        self.assertAlmostEqual(float(fresh.mix_media.decoded_duration_seconds), 1.0, delta=0.05)
        self.assertTrue(SystemEvent.objects.filter(category="active_messages",
                                                   title__contains="stinger_pin_unavailable").exists())
        self.assertEqual(ProductionMedia.objects.filter(kind="rendition", derived_from=message.message_media).count(), 1)

    def test_an_unusable_frozen_pin_with_a_good_newer_registration_uses_the_newer_one_only_anew(self):
        from .d2a_support import stinger
        from .support import corrupt_stored_bytes
        old = self.use_stinger(frequency=STINGER_HZ)
        self.real_active(kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        newer = stinger(self.admin, frequency=2500.0)
        corrupt_stored_bytes(old.media)
        outcome = worker.run_once()
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=frozen.pk).failure_code, "stinger_pin_unavailable")
        fresh = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertEqual(fresh.composition_snapshot["stinger"]["reference"], str(newer.pk))
        self.assertTrue(audio.present(windows(fresh.mix_media, 2500.0), 0.0, 1.0, threshold=0.02))


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class DeterminismTests(RealAudio, TestCase):
    def test_the_same_frozen_inputs_render_byte_identical_aggregates(self):
        self.use_stinger(seconds=0.5)
        self.real_active(self.producer, kind="urgent", seconds=1.0, frequency=URGENT_HZ)
        self.real_active(self.producer2, kind="public_address", seconds=1.0, frequency=PA_HZ)
        execution = services.materialize_due_execution()
        first, second = rendering.render(execution), rendering.render(execution)
        self.assertNotEqual(first.media.pk, second.media.pk)
        self.assertEqual((first.media.sha256, first.media.recipe_params_digest),
                         (second.media.sha256, second.media.recipe_params_digest))
        self.assertTrue(rendering.proves(first.media, execution.composition_snapshot))


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class CachedAggregateIntegrityTests(RealAudio, TestCase):
    def test_a_same_size_corrupted_cached_aggregate_is_skipped_and_a_fresh_one_is_bound(self):
        from .support import corrupt_stored_bytes
        self.real_active(kind="urgent", seconds=1.0)
        first = ActiveBroadcastExecution.objects.get(pk=worker.run_once().execution)
        cached = first.mix_media
        services.set_limits(actor=self.admin, max_aggregate_seconds=500)        # new generation: re-materialized
        size = cached.byte_size
        corrupt_stored_bytes(cached)                                           # same size, wrong bytes
        self.assertEqual(layout.resolve_storage_path(cached.storage_key).stat().st_size, size)
        outcome = worker.run_once()
        second = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertEqual((outcome.result, second.state), ("ready", "ready"))  # rendered, not "reused"
        self.assertNotEqual(second.mix_media_id, cached.pk)
        self.assertEqual(second.mix_media.recipe_params_digest, cached.recipe_params_digest)
        from production.services import media_io
        media_io.verify_integrity(second.mix_media)                            # the bound one verifies
