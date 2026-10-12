"""C2B corrective reverse proofs R18-R20: reserved-path ownership after FK
loss, identity-safe reconciliation, and a literally read-only dry run.

R16/R17 (durable barrier before rename, current deck-open authority) run
through the real engine deck path in test_projection_deck_open.
"""
import hashlib
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from django.db import connection
from django.db.models import QuerySet
from django.utils import timezone

from library.models import Track, TrackWriteRefused
from library.services import engine as engine_module
from monitoring.models import SystemEvent
from spoken import delivery
from spoken.models import SpokenAsset

from .projection_support import FFMPEG, ProjectionFixtures, flac


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
