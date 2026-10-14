"""D2B publication: a READY aggregate becomes ordinary UrgentPA library content
(exact bytes, atomic and never overwritten, normal Track, analysis and
waveform) -- idempotently, and only through the native-airing gate."""
import hashlib
import unittest
from pathlib import Path
from unittest import mock

from django.test import TestCase

from active_messages import playout, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessageEvent, ActiveBroadcastPublication
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
        self.assertEqual(playout.advance(), "publication_failed")
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.failure_code), ("failed", "publication_failed"))
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
            with self.assertRaises(playout.PlayoutError):
                playout.publish(execution)
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
