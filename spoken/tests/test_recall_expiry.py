"""C2B -- leaving delivery: recall, expiry and operator Track deletion.

An item that is not the current, live content of its asset (superseded) has
NO projection authority: its recall/expiry changes its own lifecycle only. The
current content's removal makes the Track unavailable first, then removes the
file; a current-edition asset keeps its identity (and Track) and waits for a
new deliberate delivery -- nothing is ever re-promoted automatically. Deleting
a projected Track detaches its asset; nothing recreates it silently."""
import unittest
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from library.models import Track
from spoken import delivery, transitions
from spoken.models import (
    AssetState, DeliveryMode, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItem, SpokenItemEvent,
)

from .projection_support import FFMPEG, ProjectionFixtures

S = ItemState
NOW = "django.utils.timezone.now"


def state(item):
    return SpokenItem.objects.get(pk=item.pk).state


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class RecallTests(ProjectionFixtures):
    def test_a_stale_recall_never_touches_the_newer_edition(self):
        ctype = self.make_type()
        a, first = self.delivered(ctype)
        b, second = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=second.asset_id)
        before = (asset.generation, asset.current_item_id, self.sha_on_disk(asset.library_relpath),
                  Track.objects.get(pk=asset.track_id).ready2air)
        recalled = transitions.recall(a, actor=self.manager, reason="wrong date")      # A is superseded
        self.assertEqual(recalled.state, S.RECALLED)
        asset.refresh_from_db()
        self.assertEqual((asset.generation, asset.current_item_id, self.sha_on_disk(asset.library_relpath),
                          Track.objects.get(pk=asset.track_id).ready2air), before)
        self.assertEqual(SpokenAssetEdition.objects.get(pk=second.edition_id).state, EditionState.ACTIVE)
        self.assertEqual(SpokenItemEvent.objects.filter(item=a).last().detail["projection"], "none")

    def test_recalling_the_current_item_empties_the_slot_and_resurrects_nothing(self):
        ctype = self.make_type()
        a, _ = self.delivered(ctype)
        b, second = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=second.asset_id)
        delivery.recall_delivered(b, actor=self.manager, reason="pulled")
        asset.refresh_from_db()
        track = Track.objects.get(pk=second.track_id)
        self.assertEqual((asset.state, asset.current_item_id, asset.current_rendition_id, asset.generation),
                         (AssetState.PENDING, None, None, 2))
        self.assertFalse(track.ready2air)
        self.assertFalse(self.library_file(asset.library_relpath).exists())
        self.assertEqual((state(a), state(b)), (S.SUPERSEDED, S.RECALLED))           # A NOT resurrected
        self.assertFalse(SpokenAssetEdition.objects.filter(asset=asset, state=EditionState.ACTIVE).exists())
        # the next deliberate delivery reuses the SAME asset and Track
        c, third = self.delivered(ctype)
        self.assertEqual((third.asset_id, third.track_id, third.generation), (second.asset_id, second.track_id, 3))
        self.assertTrue(Track.objects.get(pk=third.track_id).ready2air)
        self.assertEqual(self.sha_on_disk(asset.library_relpath), c.rendition_media.sha256)

    def test_accumulating_recall_affects_only_its_own_asset(self):
        ctype = self.make_type(DeliveryMode.ACCUMULATING, category="PSA")
        a, first = self.delivered(ctype)
        b, second = self.delivered(ctype)
        b_asset = SpokenAsset.objects.get(pk=second.asset_id)
        b_before = (b_asset.state, self.sha_on_disk(b_asset.library_relpath))
        delivery.recall_delivered(a, actor=self.manager)
        a_asset = SpokenAsset.objects.get(pk=first.asset_id)
        self.assertEqual((a_asset.state, a_asset.retired_reason), (AssetState.RETIRED, "item recalled"))
        self.assertFalse(Track.objects.get(pk=first.track_id).ready2air)
        self.assertFalse(self.library_file(a_asset.library_relpath).exists())
        b_asset.refresh_from_db()
        self.assertEqual((b_asset.state, self.sha_on_disk(b_asset.library_relpath)), b_before)
        self.assertTrue(Track.objects.get(pk=second.track_id).ready2air)

    def test_recall_policy_for_delivered_content(self):
        ctype = self.make_type()
        item, _ = self.delivered(ctype)
        with self.assertRaises(transitions.NotPermitted):
            transitions.recall(item, actor=self.owner)                # owner_until_delivered
        ctype.recall_policy = "owner_anytime"
        ctype.save()
        self.assertEqual(transitions.recall(item, actor=self.owner).state, S.RECALLED)

    def test_ready_to_air_falls_before_the_file_is_removed(self):
        ctype = self.make_type()
        item, result = self.delivered(ctype)
        seen = []

        def removing(relpath):
            seen.append(Track.objects.get(pk=result.track_id).ready2air)      # committed state at removal
            return True
        with mock.patch.object(delivery.library_files, "remove", side_effect=removing):
            delivery.recall_delivered(item, actor=self.manager)
        self.assertEqual(seen, [False])


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class ExpiryTests(ProjectionFixtures):
    def test_a_stale_expiry_never_retires_the_newer_edition_and_nothing_comes_back(self):
        ctype = self.make_type(default_lifetime=timedelta(minutes=5))
        a, _ = self.delivered(ctype)
        with mock.patch(NOW, return_value=timezone.now() + timedelta(minutes=2)):
            b, second = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=second.asset_id)
        with mock.patch(NOW, return_value=timezone.now() + timedelta(minutes=6)):      # only A is due
            expired = delivery.expire_due()
        self.assertEqual([item.pk for item in expired], [a.pk])
        self.assertEqual((state(a), state(b)), (S.EXPIRED, S.DELIVERED))
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.LIVE, b.pk))
        self.assertEqual(self.sha_on_disk(asset.library_relpath), b.rendition_media.sha256)
        with mock.patch(NOW, return_value=timezone.now() + timedelta(minutes=8)):      # now B
            self.assertEqual([item.pk for item in delivery.expire_due()], [b.pk])
            self.assertEqual(delivery.expire_due(), [])                                  # idempotent
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.PENDING, None))
        self.assertFalse(Track.objects.get(pk=second.track_id).ready2air)
        self.assertFalse(self.library_file(asset.library_relpath).exists())
        self.assertEqual(state(a), S.EXPIRED)                                            # no resurrection

    def test_not_due_and_accumulating_expiry(self):
        ctype = self.make_type(DeliveryMode.ACCUMULATING, category="PSA", default_lifetime=timedelta(hours=1))
        a, first = self.delivered(ctype)
        b, second = self.delivered(ctype)
        with self.assertRaises(transitions.InvalidTransition) as raised:
            delivery.expire_item(a)
        self.assertEqual(raised.exception.code, "not_due")
        with mock.patch(NOW, return_value=timezone.now() + timedelta(hours=2)):
            delivery.expire_item(a)
        self.assertEqual(SpokenAsset.objects.get(pk=first.asset_id).state, AssetState.RETIRED)
        self.assertEqual(SpokenAsset.objects.get(pk=second.asset_id).state, AssetState.LIVE)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class DetachTests(ProjectionFixtures):
    def test_deleting_a_projected_track_detaches_its_asset_and_nothing_recreates_it(self):
        ctype = self.make_type()
        item, result = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        with self.captureOnCommitCallbacks(execute=True):              # the file goes after commit
            Track.objects.get(pk=result.track_id).delete()              # an operator's deliberate deletion
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.track_id, asset.current_item_id), (AssetState.DETACHED, None, item.pk))
        self.assertEqual(SpokenAssetEdition.objects.get(pk=result.edition_id).state, EditionState.SUPERSEDED)
        self.assertFalse(self.library_file(asset.library_relpath).exists())
        self.assertEqual(state(item), S.DELIVERED)                       # history preserved
        report = delivery.reconcile()
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.track_id), (AssetState.DETACHED, None))     # not recreated
        self.assertFalse([f for f in report if f.get("asset") == str(asset.pk) and f["action"] != "none"])
        newer = self.processed(ctype)
        with self.assertRaises(delivery.ProjectionError) as raised:
            delivery.deliver_item(newer)
        self.assertEqual(raised.exception.code, "asset_detached")
        with self.assertRaises(transitions.NotPermitted):
            delivery.reattach_asset(asset, actor=self.owner)
        delivery.reattach_asset(asset, actor=self.manager)
        result = delivery.deliver_item(newer)
        self.assertEqual(result.asset_id, str(asset.pk))
        self.assertNotEqual(result.track_id, None)
        self.assertTrue(self.library_file(asset.library_relpath).exists())
        self.assertEqual(delivery.recall_delivered(item, actor=self.manager).state, S.RECALLED)  # no effect

    def test_queryset_deletion_also_detaches(self):
        ctype = self.make_type(DeliveryMode.ACCUMULATING, category="PSA")
        _, result = self.delivered(ctype)
        Track.objects.filter(pk=result.track_id).delete()
        self.assertEqual(SpokenAsset.objects.get(pk=result.asset_id).state, AssetState.DETACHED)
