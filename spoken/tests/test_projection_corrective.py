"""C2B corrective reverse proofs R16-R20.

These tests pin the rejection boundaries: durable barrier before rename,
current deck-open authority, reserved-path ownership after FK loss,
identity-safe reconciliation, and a literally read-only dry run.
"""
import hashlib
import os
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from library.models import Track, TrackWriteRefused
from library.services import engine as engine_module
from monitoring.models import SystemEvent
from spoken import delivery, library_files, playout
from spoken.models import AssetState, SpokenAsset

from .projection_support import FFMPEG, ProjectionFixtures, flac


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class DeckBarrierTests(ProjectionFixtures):
    def occurrence(self, track):
        return SimpleNamespace(id=991, track=track)

    def attempt_deck(self, occurrence):
        engine = engine_module.PlaybackEngine.__new__(engine_module.PlaybackEngine)
        with mock.patch.object(
            engine,
            "_claim_playback_occurrence",
            side_effect=AssertionError("deck construction reached"),
        ) as constructor:
            result = engine._create_deck("A", occurrence)
        return result, constructor.called

    def test_r16_rename_before_final_commit_is_never_deck_open_authority(self):
        ctype = self.make_type()
        old, first = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=first.asset_id)
        cached = Track.objects.get(pk=first.track_id)
        occurrence = self.occurrence(cached)
        new = self.processed(ctype, data=flac(2345))
        observed = []

        def inspect(point):
            if point != "after_publish":
                return
            current_asset = SpokenAsset.objects.get(pk=asset.pk)
            current_track = Track.objects.get(pk=cached.pk)
            observed.append((current_asset.state, current_track.ready2air,
                             self.sha_on_disk(asset.library_relpath),
                             engine_module._log_item_playable(occurrence), self.attempt_deck(occurrence)))
            raise delivery.SimulatedCrash(point)

        with mock.patch.object(delivery, "_fault", inspect), self.assertRaises(delivery.SimulatedCrash):
            delivery.deliver_item(new)
        self.assertEqual(observed[0][:3], (AssetState.RETIRING, False, new.rendition_media.sha256))
        self.assertFalse(observed[0][3][0])
        self.assertEqual(observed[0][4], (None, False))
        delivery.reconcile()
        asset.refresh_from_db()
        self.assertEqual((asset.state, self.sha_on_disk(asset.library_relpath)),
                         (AssetState.LIVE, old.rendition_media.sha256))
        self.assertTrue(Track.objects.get(pk=cached.pk).ready2air)

    def test_r17_cached_occurrence_refuses_recall_when_remove_fails(self):
        ctype = self.make_type()
        item, result = self.delivered(ctype)
        occurrence = self.occurrence(Track.objects.get(pk=result.track_id))  # caches ready=True
        with mock.patch.object(library_files, "remove", side_effect=OSError("held open")):
            delivery.recall_delivered(item, actor=self.manager)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        self.assertTrue(self.library_file(asset.library_relpath).exists())
        self.assertEqual(self.attempt_deck(occurrence), (None, False))

    def test_r17_cached_occurrence_refuses_expiry_when_remove_fails(self):
        ctype = self.make_type(default_lifetime=timedelta(seconds=1))
        item, result = self.delivered(ctype)
        occurrence = self.occurrence(Track.objects.get(pk=result.track_id))
        with mock.patch("spoken.delivery.timezone.now", return_value=timezone.now() + timedelta(minutes=1)), \
                mock.patch.object(library_files, "remove", side_effect=OSError("held open")):
            delivery.expire_item(item)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        self.assertTrue(self.library_file(asset.library_relpath).exists())
        self.assertEqual(self.attempt_deck(occurrence), (None, False))

    def test_r17_current_ready_state_and_detach_override_cached_occurrences(self):
        ctype = self.make_type()
        _item, result = self.delivered(ctype)
        cached = Track.objects.get(pk=result.track_id)
        occurrence = self.occurrence(cached)

        # Ordinary not-ready policy is re-read by identity, not taken from the
        # cached instance retained by a previously built log occurrence.
        QuerySet.update(Track._base_manager.filter(pk=cached.pk), ready2air=False)
        self.assertEqual(self.attempt_deck(occurrence), (None, False))
        QuerySet.update(Track._base_manager.filter(pk=cached.pk), ready2air=True)

        # Detach/delete likewise invalidates that same cached occurrence.
        Track.objects.get(pk=cached.pk).delete()
        self.assertEqual(self.attempt_deck(occurrence), (None, False))

    def test_projection_link_with_corrupt_path_is_not_treated_as_ordinary(self):
        ctype = self.make_type()
        _item, result = self.delivered(ctype)
        cached = Track.objects.get(pk=result.track_id)
        QuerySet.update(Track._base_manager.filter(pk=cached.pk), filepath="/tmp/not-the-projection.flac")
        cached.filepath = "/tmp/not-the-projection.flac"
        self.assertEqual(self.attempt_deck(self.occurrence(cached)), (None, False))

    def test_projected_playability_lookup_is_bounded(self):
        ctype = self.make_type()
        _item, result = self.delivered(ctype)
        with CaptureQueriesContext(connection) as queries:
            authority = playout.inspect(result.track_id)
        self.assertTrue(authority.allowed)
        self.assertLessEqual(len(queries), 3)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class ReservedIdentityTests(ProjectionFixtures):
    def test_r18_lost_fk_does_not_open_track_mutation_or_reserved_creation(self):
        ctype = self.make_type()
        _item, result = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        track = Track.objects.get(pk=result.track_id)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE spoken_spokenasset SET track_id = NULL WHERE id = %s", [asset.pk])
        track.title = "ordinary edit"
        with self.assertRaises(TrackWriteRefused):
            track.save(update_fields=["title"])
        with self.assertRaises(TrackWriteRefused):
            Track.objects.create(filepath=track.filepath, filename=track.filename, title="hijack",
                                 artist=track.artist, ready2air=True)
        with self.assertRaises(TrackWriteRefused):
            Track.objects.bulk_create([Track(filepath=track.filepath, filename=track.filename, title="bulk hijack",
                                             artist=track.artist, ready2air=True)])

    def test_r19_reconcile_never_adopts_unrelated_track_at_reserved_path(self):
        ctype = self.make_type()
        _item, result = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        original = Track.objects.get(pk=result.track_id)
        expected = original.filepath
        with connection.cursor() as cursor:
            cursor.execute("UPDATE spoken_spokenasset SET track_id = NULL WHERE id = %s", [asset.pk])
        # Explicit corruption bypass: move the durably witnessed Track away,
        # then inject an unrelated row at the reserved path without guards.
        QuerySet.update(Track._base_manager.filter(pk=original.pk), filepath="/tmp/corrupt-original.flac")
        unrelated = Track(filepath=expected, filename=Path(expected).name, title="not the projection",
                          artist=original.artist, ready2air=True)
        QuerySet.bulk_create(Track._base_manager.all(), [unrelated])
        report = delivery.reconcile()
        asset.refresh_from_db()
        self.assertIsNone(asset.track_id)
        self.assertNotEqual(unrelated.pk, original.pk)
        self.assertIn("reserved_path_occupied", [finding["finding"] for finding in report])
        self.assertFalse(engine_module._log_item_playable(SimpleNamespace(id=992, track=unrelated))[0])


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class DryRunZeroWriteTests(ProjectionFixtures):
    def disk_snapshot(self):
        return {str(path.relative_to(self.library)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                                      path.stat().st_mtime_ns)
                for path in sorted(self.library.rglob("*")) if path.is_file()}

    def row_snapshot(self):
        return {
            "assets": list(SpokenAsset.objects.order_by("pk").values()),
            "tracks": list(Track.objects.order_by("pk").values()),
            "events": SystemEvent.objects.count(),
        }

    def test_r20_dry_run_has_zero_database_or_filesystem_side_effects(self):
        missing_type = self.make_type(code="dry-missing")
        _missing_item, missing_result = self.delivered(missing_type)
        missing_asset = SpokenAsset.objects.get(pk=missing_result.asset_id)
        self.library_file(missing_asset.library_relpath).unlink()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE spoken_spokenasset SET track_id = NULL WHERE id = %s", [missing_asset.pk])

        wrong_type = self.make_type(code="dry-wrong", category="PSA")
        _wrong_item, wrong_result = self.delivered(wrong_type)
        wrong_asset = SpokenAsset.objects.get(pk=wrong_result.asset_id)
        self.library_file(wrong_asset.library_relpath).write_bytes(b"wrong")

        unknown = self.library / "PSA" / f"spoken-{'f' * 32}.flac"
        unknown.write_bytes(b"unknown")
        staging = self.library / "PSA" / f".spoken-{wrong_asset.pk.hex}.{'0' * 16}.partial"
        staging.write_bytes(b"stale")
        old = timezone.now().timestamp() - delivery.STALE_STAGING_SECONDS - 30
        os.utime(staging, (old, old))

        before_rows, before_disk = self.row_snapshot(), self.disk_snapshot()
        report = delivery.reconcile(apply=False)
        self.assertEqual(self.row_snapshot(), before_rows)
        self.assertEqual(self.disk_snapshot(), before_disk)
        self.assertIn("unknown_projected_file", [finding["finding"] for finding in report])
        self.assertIn("file_missing", [finding["finding"] for finding in report])
        self.assertIn("file_wrong_bytes", [finding["finding"] for finding in report])
