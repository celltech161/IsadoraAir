"""D2B publication: a READY aggregate becomes ordinary UrgentPA library content
(exact bytes, atomic and never overwritten, normal Track, analysis and
waveform) -- idempotently, and only through the native-airing gate."""
import hashlib
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from active_messages import playout, services
from active_messages.models import (
    ActiveBroadcastExecution, ActiveBroadcastMessageEvent, ActiveBroadcastPublication, ActorKind,
)
from library.models import Track
from production.tests.support import ffmpeg_available

from .d2b_support import Playout
from .support import corrupt_stored_bytes


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class PublicationTests(Playout, TestCase):
    def test_a_ready_aggregate_is_published_as_an_ordinary_urgentpa_track(self):
        execution, publication = self.published()
        path = self.library_root / "UrgentPA" / f"am-{execution.pk.hex}.flac"
        self.assertEqual(publication.library_relpath, f"UrgentPA/am-{execution.pk.hex}.flac")
        self.assertTrue(path.is_file())
        self.assertEqual(sha(path), execution.mix_media.sha256)                # exact D2A bytes
        self.assertEqual((publication.media_id, publication.sha256), (execution.mix_media_id,
                                                                      execution.mix_media.sha256))
        track = Track.objects.get(pk=publication.track_id)
        self.assertEqual((track.filepath, track.category.code, track.title, track.artist.name, track.ready2air),
                         (str(path), "UrgentPA", "Urgent PA", "Urgent PA", False))
        self.assertAlmostEqual(track.duration_seconds, float(execution.mix_media.decoded_duration_seconds), delta=0.1)
        self.assertIsNotNone(track.next_start_seconds)                         # normal analysis ran
        self.assertEqual(track.waveform_path, str(self.waveforms / f"{track.pk}.json"))
        self.assertTrue(Path(track.waveform_path).is_file())                    # normal waveform, real dir
        self.assertFalse(any(self.library_root.glob("UrgentPA/.am-publish-*")))  # no scratch left behind
        self.assertIn("published", list(ActiveBroadcastMessageEvent.objects.filter(execution=execution)
                                        .values_list("event", flat=True)))
        self.assertTrue(playout.is_candidate(track))

    def test_publication_is_idempotent(self):
        execution, first = self.published()
        again = playout.publish(execution)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(Track.objects.filter(category__code="UrgentPA").count(), 1)
        self.assertEqual(len(list((self.library_root / "UrgentPA").glob("am-*.flac"))), 1)

    def test_the_gate_closed_publishes_nothing(self):
        execution = self.ready_execution()
        services.set_native_airing(False, actor=self.admin)
        with self.assertRaises(playout.PlayoutError) as caught:
            playout.publish(execution)
        self.assertEqual(caught.exception.code, "native_airing_closed")
        self.assertFalse((self.library_root / "UrgentPA").exists() and any((self.library_root / "UrgentPA").iterdir()))
        with mock.patch("active_messages.services.NATIVE_AIRING_AVAILABLE", False):
            self.assertFalse(playout.gate_open())

    def test_corrupt_aggregate_bytes_are_refused_and_fail_the_execution(self):
        execution = self.ready_execution()
        corrupt_stored_bytes(execution.mix_media)
        with self.assertRaises(playout.PlayoutError) as caught:
            playout.publish(execution)
        self.assertEqual(caught.exception.code, "aggregate_unavailable")
        self.assertEqual(playout.advance(), "publication_failed")              # a permanent integrity failure
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.failure_code), ("failed", "aggregate_unavailable"))
        self.assertFalse(Track.objects.filter(category__code="UrgentPA").exists())
        self.assertFalse(list(self.library_root.glob("UrgentPA/am-*.flac")))

    def test_only_a_current_ready_execution_is_published(self):
        execution = self.ready_execution()
        services.set_limits(actor=self.admin, max_active_messages=10)          # new generation: suppressed
        with self.assertRaises(playout.PlayoutError) as caught:
            playout.publish(execution)
        self.assertEqual(caught.exception.code, "not_ready")

    def test_other_bytes_at_the_reserved_path_are_never_overwritten(self):
        execution = self.ready_execution()
        dest = self.library_root / playout.relpath_for(execution.pk)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not this aggregate")
        with self.assertRaises(playout.PlayoutError) as caught:
            playout.publish(execution)
        self.assertEqual(caught.exception.code, "destination_conflict")
        self.assertEqual(dest.read_bytes(), b"not this aggregate")
        self.assertFalse(ActiveBroadcastPublication.objects.exists())
        self.assertEqual(playout.advance(), "publication_failed")              # permanent, with its own code
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.failure_code), ("failed", "destination_conflict"))
        self.assertEqual(dest.read_bytes(), b"not this aggregate")

    def test_a_publish_interrupted_after_placing_the_file_is_completed_by_reusing_it(self):
        execution = self.ready_execution()
        dest = self.library_root / playout.relpath_for(execution.pk)
        dest.parent.mkdir(parents=True, exist_ok=True)
        from production.services import media_io
        with media_io.open_media(execution.mix_media) as opened:
            dest.write_bytes(opened.file.read())                                # the crash left exactly this
        publication = playout.publish(execution)
        self.assertEqual(sha(dest), execution.mix_media.sha256)
        self.assertEqual(Track.objects.get(pk=publication.track_id).filepath, str(dest))

    def test_a_failed_analysis_rolls_everything_back(self):
        execution = self.ready_execution()
        with mock.patch("library.management.commands.sync_track_file.analyze_one_track", return_value=False):
            with self.assertRaises(playout.PlayoutError) as caught:
                playout.publish(execution)
        self.assertEqual(caught.exception.code, "analysis_failed")
        self.assertIn(caught.exception.code, playout.PUBLICATION_RETRYABLE)
        self.assertFalse(ActiveBroadcastPublication.objects.exists())
        self.assertFalse(Track.objects.filter(category__code="UrgentPA").exists())
        self.assertFalse(list(self.library_root.glob("UrgentPA/am-*.flac")))
        self.assertFalse(list(self.waveforms.glob("*.json")))

    def test_each_execution_has_its_own_file_and_an_earlier_one_is_never_replaced(self):
        first, first_publication = self.published()
        first_path = self.library_root / first_publication.library_relpath
        inode, digest = first_path.stat().st_ino, sha(first_path)
        services.set_limits(actor=self.admin, max_active_messages=10)          # a new occurrence, same aggregate
        execution = services.materialize_due_execution()
        from active_messages import worker
        self.assertEqual(worker.process(execution).result, "reused")
        second = playout.publish(ActiveBroadcastExecution.objects.get(pk=execution.pk))
        self.assertNotEqual(second.library_relpath, first_publication.library_relpath)
        self.assertEqual((first_path.stat().st_ino, sha(first_path)), (inode, digest))
        self.assertNotEqual(second.track_id, first_publication.track_id)


ANALYSIS_DOWN = "library.management.commands.sync_track_file.analyze_one_track"


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class PublicationRetryTests(Playout, TestCase):
    """A transient local-library problem never throws away a valid READY
    aggregate: the SAME execution retries publication with bounded backoff."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("monitoring.models.emit_event")
        self.emitted = patcher.start()
        self.addCleanup(patcher.stop)

    def retries(self, execution):
        return ActiveBroadcastMessageEvent.objects.filter(execution=execution, event="publication_retry").count()

    def attention_codes(self):
        return [call.kwargs.get("detail", {}).get("code") for call in self.emitted.call_args_list]

    def test_a_transient_analysis_failure_keeps_the_same_ready_execution(self):
        execution = self.ready_execution()
        mix = execution.mix_media_id
        with mock.patch(ANALYSIS_DOWN, return_value=False):
            self.assertEqual(playout.advance(), "publication_retry")
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.mix_media_id, stored.failure_code), ("ready", mix, ""))
        self.assertEqual(ActiveBroadcastExecution.objects.count(), 1)
        self.assertEqual(self.retries(execution), 1)
        self.assertFalse(ActiveBroadcastPublication.objects.exists())
        self.assertFalse(list(self.library_root.glob("UrgentPA/am-*.flac")))
        # Recovered: the exact execution publishes and dispatches.
        later = timezone.now() + timedelta(seconds=playout.PUBLICATION_BACKOFF_BASE_SECONDS + 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(playout.advance(now=later), "dispatched")
        publication = ActiveBroadcastPublication.objects.get()
        self.assertEqual((publication.execution_id, publication.media_id), (execution.pk, mix))
        self.assertEqual(self.commands[-1]["active_execution"], str(execution.pk))

    def test_publication_retry_backs_off_exponentially_within_a_bound_and_raises_attention(self):
        execution = self.ready_execution()
        start = timezone.now()
        with mock.patch(ANALYSIS_DOWN, return_value=False):
            self.assertEqual(playout.advance(now=start), "publication_retry")
            self.assertEqual(playout.advance(now=start + timedelta(seconds=5)), "publication_backoff")
            self.assertNotIn("publication_unavailable", self.attention_codes())
            self.assertEqual(playout.advance(now=start + timedelta(seconds=16)), "publication_retry")
            self.assertEqual(playout.advance(now=start + timedelta(seconds=25)), "publication_backoff")
            self.assertEqual(playout.advance(now=start + timedelta(seconds=31)), "publication_retry")
        self.assertEqual(self.retries(execution), playout.PUBLICATION_ATTENTION_AFTER)
        self.assertIn("publication_unavailable", self.attention_codes())         # deduplicated by key
        keys = {call.kwargs.get("dedupe_key") for call in self.emitted.call_args_list
                if call.kwargs.get("detail", {}).get("code") == "publication_unavailable"}
        self.assertEqual(keys, {"active_messages|playout|publication_unavailable"})
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "ready")
        # The wait is capped however long the condition lasts.
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        for _ in range(20):
            services._event("publication_retry", execution=stored, generation=stored.policy_generation,
                            actor_kind=ActorKind.WORKER, detail={"code": "analysis_failed"})
        self.assertLessEqual(playout._publication_backoff_remaining(stored, timezone.now()),
                             playout.PUBLICATION_BACKOFF_MAX_SECONDS)

    def test_library_io_failure_is_retryable_and_leaves_nothing_behind(self):
        execution = self.ready_execution()
        with mock.patch("active_messages.playout.os.link", side_effect=OSError("read-only file system")):
            with self.assertRaises(playout.PlayoutError) as caught:
                playout.publish(execution)
            self.assertEqual(caught.exception.code, "library_unavailable")
            self.assertEqual(playout.advance(), "publication_retry")
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "ready")
        self.assertFalse(list(self.library_root.glob("UrgentPA/am-*.flac")))

    def test_a_missing_category_is_retryable(self):
        execution = self.ready_execution()
        with mock.patch("active_messages.playout.CATEGORY_CODE", "NoSuchCategory"):
            self.assertEqual(playout.advance(), "publication_retry")
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(
            execution=execution, event="publication_retry").values_list("detail__code", flat=True).get(),
            "category_missing")
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "ready")

    def test_the_worker_keeps_the_ready_execution_in_flight_and_materializes_nothing_else(self):
        from active_messages import worker
        execution = self.ready_execution()
        with mock.patch(ANALYSIS_DOWN, return_value=False):
            outcome = worker.run_once(render_fn=mock.Mock(side_effect=AssertionError("no rerender")))
            self.assertEqual((outcome.result, outcome.code), ("playout", "publication_retry"))
            with mock.patch("active_messages.worker._render_pass", side_effect=AssertionError("no fall-through")):
                outcome = worker.run_once(now=timezone.now() + timedelta(seconds=1))
            self.assertEqual((outcome.result, outcome.code), ("playout", "publication_backoff"))
        self.assertEqual(list(ActiveBroadcastExecution.objects.values_list("pk", "state")), [(execution.pk, "ready")])
