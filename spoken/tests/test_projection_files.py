"""C2B -- the DB/filesystem commit model: atomic promotion, the crash-boundary
matrix (every step, then reconcile), compensation of a failure, reconciliation
(healthy = no writes; missing / wrong bytes / drift / missing Track /
relocation / staging / stale copies), and path safety (symlinks)."""
import os
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from django.db import connection
from django.db.models import QuerySet

from library.models import Track
from monitoring.models import SystemEvent
from production.services import layout
from spoken import delivery, library_files
from spoken.models import AssetState, DeliveryMode, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItem

from .projection_support import FFMPEG, ProjectionFixtures, flac
from .test_domain import make_category

S = ItemState

DB_POINTS = ("before_pending", "pending_created", "barrier_committed", "before_stage", "after_publish", "track_written",
             "edition_activated", "before_delivered", "before_commit")
FS_POINTS = ("staged", "verified", "published")


@contextmanager
def crash_at(point):
    """A process death at ``point``: SimulatedCrash is a BaseException, so no
    compensation runs -- only the database's own rollback, as after a crash."""
    def fault(name):
        if name == point:
            raise delivery.SimulatedCrash(point)
    with mock.patch.object(delivery, "_fault", fault), mock.patch.object(library_files, "_fault", fault):
        yield


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class CrashMatrixTests(ProjectionFixtures):
    def setUp(self):
        super().setUp()
        stale = mock.patch.object(delivery, "STALE_PENDING_SECONDS", 0)
        stale.start()
        self.addCleanup(stale.stop)

    def assert_converged(self, asset, *, item, previous_item):
        """One safe state: either the previous edition (or nothing) is
        authoritative and on disk, or the new one is -- never a mixture."""
        asset.refresh_from_db()
        item.refresh_from_db()
        active = list(SpokenAssetEdition.objects.filter(asset=asset, state=EditionState.ACTIVE))
        self.assertLessEqual(len(active), 1)
        self.assertFalse(SpokenAssetEdition.objects.filter(asset=asset, state=EditionState.PENDING).exists())
        staging = [p.name for p in self.library.rglob(".spoken-*")]
        self.assertEqual(staging, [])
        if item.state == S.DELIVERED:
            self.assertEqual((active[0].item_id, asset.current_item_id), (item.pk, item.pk))
            authority = item.rendition_media.sha256
        elif previous_item is not None:
            self.assertEqual(item.state, S.PROCESSED)
            self.assertEqual((active[0].item_id, asset.current_item_id), (previous_item.pk, previous_item.pk))
            authority = previous_item.rendition_media.sha256
        else:
            self.assertEqual((item.state, active, asset.state), (S.PROCESSED, [], AssetState.PENDING))
            authority = None
        self.assertEqual(self.sha_on_disk(asset.library_relpath), authority)
        if asset.track_id is not None:
            track = Track.objects.get(pk=asset.track_id)
            self.assertEqual(track.ready2air, authority is not None)
            self.assertEqual(track.filepath, str(self.library_file(asset.library_relpath)))

    def test_every_boundary_of_a_replacement_converges(self):
        for point in (*DB_POINTS, *FS_POINTS, "committed"):
            with self.subTest(point=point):
                ctype = self.make_type(code=f"crash-{point.replace('_', '-')}")
                previous, first = self.delivered(ctype)
                asset = SpokenAsset.objects.get(pk=first.asset_id)
                item = self.processed(ctype)
                with crash_at(point), self.assertRaises(delivery.SimulatedCrash):
                    delivery.deliver_item(item)
                delivery.reconcile()
                self.assert_converged(asset, item=item, previous_item=previous)
                if SpokenItem.objects.get(pk=item.pk).state == S.PROCESSED:
                    retried = delivery.deliver_item(item)                 # the item can still be delivered
                    self.assertEqual(retried.generation, max(
                        SpokenAssetEdition.objects.filter(asset=asset).values_list("generation", flat=True)))
                    self.assert_converged(asset, item=item, previous_item=previous)

    def test_every_boundary_of_a_first_delivery_converges(self):
        for point in (*DB_POINTS[1:], *FS_POINTS, "committed"):
            with self.subTest(point=point):
                ctype = self.make_type(code=f"first-{point.replace('_', '-')}", category="Fresh")
                item = self.processed(ctype)
                with crash_at(point), self.assertRaises(delivery.SimulatedCrash):
                    delivery.deliver_item(item)
                delivery.reconcile()
                asset = SpokenAsset.objects.get(content_type=ctype)
                self.assert_converged(asset, item=item, previous_item=None)
                self.assertEqual(Track.objects.filter(filepath=str(self.library_file(asset.library_relpath)))
                                 .count(), 1 if SpokenItem.objects.get(pk=item.pk).state == S.DELIVERED else 0)

    def test_a_crash_before_any_pending_edition_leaves_nothing(self):
        ctype = self.make_type()
        item = self.processed(ctype)
        with crash_at("before_pending"), self.assertRaises(delivery.SimulatedCrash):
            delivery.deliver_item(item)
        self.assertEqual((SpokenAssetEdition.objects.count(), list(self.library.rglob("*.flac"))), (0, []))

    def test_a_failure_is_compensated_at_once(self):
        ctype = self.make_type()
        previous, first = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=first.asset_id)
        item = self.processed(ctype)

        def fail(name):
            if name == "track_written":
                raise RuntimeError("disk full")
        with mock.patch.object(delivery, "_fault", fail), self.assertRaises(RuntimeError):
            delivery.deliver_item(item)
        # no reconcile: the failed delivery already restored the previous edition's bytes
        self.assertEqual(self.sha_on_disk(asset.library_relpath), previous.rendition_media.sha256)
        self.assertEqual(SpokenAssetEdition.objects.get(asset=asset, item=item).state, EditionState.ABORTED)
        self.assert_converged(asset, item=item, previous_item=previous)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class AtomicPublicationTests(ProjectionFixtures):
    def test_readers_see_the_old_complete_file_until_the_rename(self):
        ctype = self.make_type()
        previous, first = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=first.asset_id)
        item = self.processed(ctype, data=flac(4321))
        observed = []

        def look(name):
            if name in ("staged", "verified"):
                observed.append((name, self.sha_on_disk(asset.library_relpath)))
        with mock.patch.object(library_files, "_fault", look):
            delivery.deliver_item(item)
        self.assertEqual(observed, [("staged", previous.rendition_media.sha256),
                                    ("verified", previous.rendition_media.sha256)])
        self.assertEqual(self.sha_on_disk(asset.library_relpath), item.rendition_media.sha256)

    def test_a_rendition_whose_bytes_changed_is_never_published(self):
        ctype = self.make_type()
        item = self.processed(ctype)
        path = layout.resolve_storage_path(item.rendition_media.storage_key)
        os.chmod(path, 0o640)
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF
        path.write_bytes(bytes(data))
        with self.assertRaises(library_files.LibraryFileError) as raised:
            delivery.deliver_item(item)
        self.assertEqual(raised.exception.code, "sha_mismatch")
        self.assertEqual(list(self.library.rglob("*.flac")), [])

    def test_symlinks_are_never_followed(self):
        ctype = self.make_type()
        _, result = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        final = self.library_file(asset.library_relpath)
        outside = self.library.parent / "outside.flac"
        outside.write_bytes(b"precious")
        final.unlink()
        final.symlink_to(outside)                                  # a link planted at the stable path
        report = delivery.reconcile()
        self.assertIn("file_wrong_bytes", [f["finding"] for f in report])
        self.assertFalse(final.is_symlink())
        self.assertEqual(outside.read_bytes(), b"precious")        # the link was replaced, not followed
        category = self.library / "Linked"
        category.symlink_to(self.library.parent)                   # a category directory that is a link
        linked = self.make_type(category="Linked")
        with self.assertRaises(library_files.LibraryFileError) as raised:
            delivery.deliver_item(self.processed(linked))
        self.assertEqual(raised.exception.code, "unsafe_library_path")
        self.assertFalse((self.library.parent / f"spoken-{SpokenAsset.objects.get(content_type=linked).pk.hex}.flac")
                         .exists())


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class ReconcileTests(ProjectionFixtures):
    def live(self, mode=DeliveryMode.CURRENT_EDITION, **type_options):
        ctype = self.make_type(mode, **type_options)
        item, result = self.delivered(ctype)
        return ctype, item, SpokenAsset.objects.get(pk=result.asset_id), Track.objects.get(pk=result.track_id)

    def changes(self, report):
        return [f for f in report if f.get("action") not in ("none", "skipped")]

    def test_a_healthy_projection_causes_no_write_at_all(self):
        _, _, asset, track = self.live()
        stamp = (asset.updated_at, track.updated_at, self.library_file(asset.library_relpath).stat().st_mtime_ns)
        with mock.patch.object(library_files, "publish", side_effect=AssertionError("rewrote")), \
                mock.patch.object(library_files, "remove", side_effect=AssertionError("removed")):
            self.assertEqual(self.changes(delivery.reconcile()), [])
            self.assertEqual(self.changes(delivery.reconcile()), [])          # idempotent
        asset.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual((asset.updated_at, track.updated_at,
                          self.library_file(asset.library_relpath).stat().st_mtime_ns), stamp)

    def test_a_missing_file_is_restored_from_the_immutable_rendition(self):
        _, item, asset, track = self.live()
        self.library_file(asset.library_relpath).unlink()
        report = self.changes(delivery.reconcile())
        self.assertEqual([(f["finding"], f["action"]) for f in report], [("file_missing", "restored")])
        self.assertEqual(self.sha_on_disk(asset.library_relpath), item.rendition_media.sha256)
        self.assertEqual(SpokenAsset.objects.get(pk=asset.pk).track_id, track.pk)        # identity kept

    def test_unexplained_bytes_are_replaced_never_adopted(self):
        _, item, asset, _ = self.live()
        self.library_file(asset.library_relpath).write_bytes(flac(999))
        delivery.reconcile()
        self.assertEqual(self.sha_on_disk(asset.library_relpath), item.rendition_media.sha256)
        self.assertEqual(SpokenAssetEdition.objects.get(asset=asset, state="active").library_sha256,
                         item.rendition_media.sha256)

    def test_an_unrestorable_file_fails_closed_and_tells_the_operator(self):
        _, item, asset, track = self.live()
        self.library_file(asset.library_relpath).unlink()
        os.unlink(layout.resolve_storage_path(item.rendition_media.storage_key))     # the bytes are gone too
        report = delivery.reconcile()
        self.assertIn(("file_missing", "restore_failed"), [(f["finding"], f["action"]) for f in report])
        track.refresh_from_db()
        self.assertFalse(track.ready2air)
        self.assertTrue(SystemEvent.objects.filter(category="spoken_projection").exists())

    def test_track_drift_and_a_missing_track_are_repaired_from_authority(self):
        ctype, item, asset, track = self.live(title_policy="item_title")
        QuerySet.update(Track._base_manager.filter(pk=track.pk), title="edited by hand", ready2air=False,
                        category=make_category("Elsewhere"))
        report = self.changes(delivery.reconcile())
        self.assertEqual(report[0]["finding"], "track_drift")
        track.refresh_from_db()
        self.assertEqual((track.title, track.ready2air, track.category.code), ("t", True, "LocalNews"))
        with connection.cursor() as cursor:                          # corruption: the link vanished
            cursor.execute("UPDATE spoken_spokenasset SET track_id = NULL WHERE id = %s", [asset.pk])
        report = self.changes(delivery.reconcile())
        self.assertEqual([(f["finding"], f["action"]) for f in report], [("track_missing", "relinked")])
        asset.refresh_from_db()
        self.assertEqual(asset.track_id, track.pk)                    # the same Track identity
        with connection.cursor() as cursor:                          # corruption: link and Track row vanished
            cursor.execute("UPDATE spoken_spokenasset SET track_id = NULL WHERE id = %s", [asset.pk])
            cursor.execute("DELETE FROM library_track WHERE id = %s", [track.pk])
        report = self.changes(delivery.reconcile())
        self.assertEqual([(f["finding"], f["action"]) for f in report], [])
        self.assertIn("track_identity_missing", [f["finding"] for f in delivery.reconcile()])
        asset.refresh_from_db()
        self.assertIsNone(asset.track_id)                              # identity is not guessed/recreated

    def test_a_category_change_is_a_controlled_relocation(self):
        ctype, item, asset, track = self.live()
        old = asset.library_relpath
        ctype.target_category = make_category("NewsDesk")
        ctype.save()
        with mock.patch.object(library_files, "remove", wraps=library_files.remove) as removing:
            report = self.changes(delivery.reconcile())
        asset.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual(report[0]["finding"], "category_changed")
        self.assertEqual(asset.library_relpath, f"NewsDesk/spoken-{asset.pk.hex}.flac")
        self.assertEqual((track.category.code, track.filepath), ("NewsDesk", str(self.library_file(asset.library_relpath))))
        self.assertEqual(self.sha_on_disk(asset.library_relpath), item.rendition_media.sha256)
        self.assertFalse(self.library_file(old).exists())
        self.assertEqual([call.args[0] for call in removing.call_args_list], [old])   # only AFTER the move

    def test_a_relocation_that_cannot_publish_keeps_the_last_good_file(self):
        ctype, item, asset, track = self.live()
        old = asset.library_relpath
        ctype.target_category = make_category("NewsDesk")
        ctype.save()
        with mock.patch.object(library_files, "publish", side_effect=library_files.LibraryFileError("full")):
            with self.assertRaises(library_files.LibraryFileError):
                delivery.reconcile()
        asset.refresh_from_db()
        self.assertEqual((asset.library_relpath, self.sha_on_disk(old)), (old, item.rendition_media.sha256))

    def test_stale_staging_stale_copies_and_unknown_files(self):
        _, _, asset, _ = self.live()
        category = self.library / "LocalNews"
        stale = category / f".spoken-{asset.pk.hex}.{'0' * 16}.partial"
        fresh = category / f".spoken-{asset.pk.hex}.{'1' * 16}.partial"
        stale.write_bytes(b"x")
        fresh.write_bytes(b"y")
        old = time.time() - delivery.STALE_STAGING_SECONDS - 10
        os.utime(stale, (old, old))
        (self.library / "Elsewhere").mkdir()
        copy = self.library / "Elsewhere" / f"spoken-{asset.pk.hex}.flac"          # an interrupted relocation
        copy.write_bytes(b"old")
        unknown = category / f"spoken-{'f' * 32}.flac"
        unknown.write_bytes(b"?")
        report = delivery.reconcile()
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())                       # an in-flight attempt's staging is left alone
        self.assertFalse(copy.exists())
        self.assertTrue(unknown.exists())                     # reported, never deleted
        self.assertIn("unknown_projected_file", [f["finding"] for f in report])
        self.assertTrue(self.library_file(asset.library_relpath).exists())

    def test_dry_run_changes_nothing(self):
        _, _, asset, _ = self.live()
        self.library_file(asset.library_relpath).unlink()
        report = delivery.reconcile(apply=False)
        self.assertEqual([f["action"] for f in report if f.get("asset") == str(asset.pk)], ["would_restore"])
        self.assertFalse(self.library_file(asset.library_relpath).exists())
