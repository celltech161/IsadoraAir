"""C2B -- delivery through the projection service: current-edition (stable
asset and Track, increasing generations), accumulating (one asset/Track per
item), retain-only (no projection), review, provenance and policy."""
import hashlib
import unittest
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from library.models import Track
from production.services import reference_audio
from spoken import delivery, transitions
from spoken.models import (
    AssetMode, AssetState, DeliveryMode, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItemEvent,
)

from .projection_support import FFMPEG, ProjectionFixtures, flac

S = ItemState


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class CurrentEditionDeliveryTests(ProjectionFixtures):
    def test_a_then_b_keeps_one_asset_and_track_and_promotes_a_new_generation(self):
        ctype = self.make_type(DeliveryMode.CURRENT_EDITION, title_policy="item_title")
        a, first = self.delivered(ctype, title="Morning news")
        asset = SpokenAsset.objects.get(pk=first.asset_id)
        track = Track.objects.get(pk=first.track_id)
        self.assertEqual((asset.mode, asset.state, asset.generation, asset.current_item_id),
                         (AssetMode.CURRENT_EDITION, AssetState.LIVE, 1, a.pk))
        self.assertEqual(asset.library_relpath, f"LocalNews/spoken-{asset.pk.hex}.flac")
        self.assertEqual(self.sha_on_disk(asset.library_relpath), a.rendition_media.sha256)
        self.assertEqual((track.filepath, track.title, track.artist.name, track.category.code, track.ready2air),
                         (str(self.library_file(asset.library_relpath)), "Morning news", "KOGR News", "LocalNews",
                          True))
        b, second = self.delivered(ctype, title="Evening news")
        self.assertEqual((second.asset_id, second.track_id, second.generation), (first.asset_id, first.track_id, 2))
        asset.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual((asset.generation, asset.current_item_id, asset.current_rendition_id),
                         (2, b.pk, b.rendition_media_id))
        self.assertEqual(self.sha_on_disk(asset.library_relpath), b.rendition_media.sha256)
        self.assertEqual((track.title, track.duration_seconds), ("Evening news", 1.5))
        editions = list(SpokenAssetEdition.objects.filter(asset=asset).order_by("generation"))
        self.assertEqual([(e.generation, e.state, e.item_id) for e in editions],
                         [(1, EditionState.SUPERSEDED, a.pk), (2, EditionState.ACTIVE, b.pk)])
        self.assertEqual(editions[0].superseded_by_id, editions[1].pk)
        self.assertEqual(editions[1].library_sha256, b.rendition_media.sha256)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.state, b.state), (S.SUPERSEDED, S.DELIVERED))
        self.assertEqual(Track.objects.filter(filepath__contains="spoken-").count(), 1)
        events = list(SpokenItemEvent.objects.filter(item=b).values_list("event", "asset_id", "edition_id"))
        self.assertEqual(events[-1], ("delivered", asset.pk, editions[1].pk))
        self.assertEqual(list(SpokenItemEvent.objects.filter(item=a).values_list("event", flat=True))[-1],
                         "superseded")

    def test_idempotent_replay_and_no_bare_delivery(self):
        ctype = self.make_type()
        item, result = self.delivered(ctype)
        again = delivery.deliver_item(item)
        self.assertTrue(again.already)
        self.assertEqual((again.asset_id, again.generation), (result.asset_id, result.generation))
        self.assertEqual(SpokenAssetEdition.objects.count(), 1)
        with self.assertRaises(transitions.ProjectionRequired):
            transitions._apply(self.processed(ctype), S.DELIVERED, actor=self.manager)

    def test_review_is_never_bypassed(self):
        ctype = self.make_type(requires_review=True)
        item = self.processed(ctype)
        with self.assertRaises(transitions.InvalidTransition) as raised:
            delivery.deliver_item(item)
        self.assertEqual(raised.exception.code, "review_required")
        self.assertIsNone(delivery.advance_processed(item))                  # -> awaiting review
        item.refresh_from_db()
        self.assertEqual(item.state, S.AWAITING_REVIEW)
        with self.assertRaises(transitions.NotPermitted):
            delivery.approve_and_deliver(item, actor=self.owner)
        result = delivery.approve_and_deliver(item, actor=self.manager, note="fine")
        item.refresh_from_db()
        self.assertEqual((item.state, item.reviewed_by_id, item.review_note, result.generation),
                         (S.DELIVERED, self.manager.pk, "fine", 1))
        self.assertEqual(SpokenItemEvent.objects.filter(item=item).last().event, "approved_delivered")

    def test_lifetime_and_ready_to_air_policy(self):
        ctype = self.make_type(deliver_ready_to_air=False, default_lifetime=timedelta(hours=2))
        item, result = self.delivered(ctype)
        item.refresh_from_db()
        self.assertEqual(item.expires_at, item.delivered_at + timedelta(hours=2))
        self.assertFalse(Track.objects.get(pk=result.track_id).ready2air)

    def test_title_policies_and_empty_artist_label(self):
        ctype = self.make_type(title_policy="submitter_and_date", artist_label="")
        item, result = self.delivered(ctype)
        track = Track.objects.get(pk=result.track_id)
        local = timezone.localtime(item.submitted_at).strftime("%m/%d/%Y")
        self.assertEqual((track.title, track.artist.name), (f"{self.owner.username} - {local}", ctype.name))

    def test_refusals_leave_the_item_processed_and_the_library_untouched(self):
        register_bed(self, "bed.3min_ctr", 1.0)
        ctype = self.make_type(recipe_key="3min_ctr")
        outdated = self.processed(ctype)
        register_bed(self, "bed.3min_ctr", 1.3)                 # the bed changed after the render
        with self.assertRaises(delivery.ProjectionError) as raised:
            delivery.deliver_item(outdated)
        self.assertEqual(raised.exception.code, "render_outdated")
        outdated.refresh_from_db()
        self.assertEqual(outdated.state, S.PROCESSED)
        self.assertEqual(list(self.library.rglob("*.flac")), [])
        self.assertFalse(SpokenAssetEdition.objects.exists())
        missing = self.make_type(category=None, enabled=False)
        with self.assertRaises(delivery.ProjectionError) as raised:
            delivery._category_code(missing)
        self.assertEqual(raised.exception.code, "target_category_missing")


def register_bed(testcase, key, seconds):
    import io

    from production.models import ProductionMedia
    from production.services import intake

    from . import support as audio
    bed = intake.ingest_stream(io.BytesIO(audio.bed(seconds)), kind=ProductionMedia.KIND_BED, validate=True).media
    return reference_audio.register(key, bed)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class AccumulatingAndRetainOnlyTests(ProjectionFixtures):
    def test_each_item_gets_its_own_asset_and_track(self):
        ctype = self.make_type(DeliveryMode.ACCUMULATING, category="PSA")
        a, first = self.delivered(ctype)
        before = self.sha_on_disk(SpokenAsset.objects.get(pk=first.asset_id).library_relpath)
        b, second = self.delivered(ctype)
        self.assertNotEqual((first.asset_id, first.track_id), (second.asset_id, second.track_id))
        assets = {asset.item_id: asset for asset in SpokenAsset.objects.all()}
        self.assertEqual(set(assets), {a.pk, b.pk})
        self.assertEqual(self.sha_on_disk(assets[a.pk].library_relpath), before)        # A unchanged
        self.assertEqual(self.sha_on_disk(assets[b.pk].library_relpath), b.rendition_media.sha256)
        a.refresh_from_db()
        self.assertEqual(a.state, S.DELIVERED)                                         # never superseded
        self.assertEqual(Track.objects.filter(pk__in=[first.track_id, second.track_id]).count(), 2)

    def test_retain_only_delivers_without_any_projection(self):
        ctype = self.make_type(DeliveryMode.RETAIN_ONLY, category=None)
        item, result = self.delivered(ctype)
        item.refresh_from_db()
        self.assertEqual((item.state, result.asset_id, result.track_id), (S.DELIVERED, None, None))
        self.assertEqual((SpokenAsset.objects.count(), list(self.library.rglob("*"))), (0, []))
        recalled = delivery.recall_delivered(item, actor=self.manager, reason="x")
        self.assertEqual(recalled.state, S.RECALLED)

    def test_the_library_bytes_are_exactly_the_rendition(self):
        ctype = self.make_type(DeliveryMode.ACCUMULATING, category="PSA")
        data = flac(1234)
        item, result = self.delivered(ctype, data=data)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        edition = SpokenAssetEdition.objects.get(pk=result.edition_id)
        on_disk = self.library_file(asset.library_relpath).read_bytes()
        self.assertEqual(on_disk, data)
        self.assertEqual({hashlib.sha256(data).hexdigest(), edition.library_sha256, item.rendition_media.sha256},
                         {hashlib.sha256(on_disk).hexdigest()})
        self.assertEqual(oct(self.library_file(asset.library_relpath).stat().st_mode & 0o777), "0o644")
        self.assertEqual([p.name for p in self.library_file(asset.library_relpath).parent.iterdir()],
                         [f"spoken-{asset.pk.hex}.flac"])                                # no staging left


class MultiReferenceLockOrderTests(ProjectionFixtures):
    def test_keys_are_locked_once_each_in_lock_id_order(self):
        keys = ["bed.zeta", "bed.alpha", "bed.mid", "bed.alpha"]
        with transaction.atomic():
            order = reference_audio.lock_keys(keys, shared=True)
        self.assertEqual(order, sorted(set(keys), key=lambda key: (reference_audio.key_lock_id(key), key)))
        with transaction.atomic():
            self.assertEqual(reference_audio.lock_keys(list(reversed(keys))), order)     # input order irrelevant
