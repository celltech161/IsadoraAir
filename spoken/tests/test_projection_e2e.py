"""C2B end to end, the five legacy content kinds through the REAL chain:
recording -> validation -> leased worker -> isadoraair-render (C2A) -> exact
validated rendition -> projection (worker pass) -> library Track and file.

    cal_events, usd239min, news_item  -> current_edition (one stable asset/Track)
    3min_ctr, lcl_psa                  -> accumulating (one asset/Track per item)
"""
import io
import unittest
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from library.models import Track
from production.models import ProductionMedia
from production.services import intake, reference_audio
from production.tests.support import ffmpeg_available
from spoken import delivery, transitions, worker
from spoken.models import AssetState, DeliveryMode, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItem

from . import support as audio
from .projection_support import ProjectionFixtures

S = ItemState
KINDS = {
    "cal_events": (DeliveryMode.CURRENT_EDITION, "bed.cal_events", "Announcements"),
    "usd239min": (DeliveryMode.CURRENT_EDITION, "bed.usd239min", "USD239"),
    "news_item": (DeliveryMode.CURRENT_EDITION, "bed.news_intro", "LocalNews"),
    "3min_ctr": (DeliveryMode.ACCUMULATING, "bed.3min_ctr", "ThreeMin"),
    "lcl_psa": (DeliveryMode.ACCUMULATING, None, "PSA"),
}


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class LegacyKindsEndToEndTests(ProjectionFixtures):
    def setUp(self):
        super().setUp()
        for _recipe, (_mode, bed_key, _category) in KINDS.items():
            if bed_key:
                # the beds the accepted C2A real-media matrix uses (spoken.tests.test_recipes)
                data = audio.bed(6.0, amplitude=0.25) if bed_key == "bed.news_intro" else audio.bed(6.0)
                bed = intake.ingest_stream(io.BytesIO(data), kind=ProductionMedia.KIND_BED, validate=True).media
                reference_audio.register(bed_key, bed)

    def submit_and_run(self, ctype, seconds):
        voice = intake.ingest_stream(io.BytesIO(audio.voice(seconds)), kind=ProductionMedia.KIND_RECORDING,
                                     validate=True).media
        item = transitions.create_item(ctype, actor=self.owner, title=f"{ctype.recipe_key} {seconds}")
        item = transitions.record_validation(transitions.submit(item, voice, actor=self.owner))
        outcome = worker.process_one(sleep=lambda s: None)           # the real render (isadoraair-render)
        self.assertEqual(outcome.result, "processed", outcome)
        self.assertEqual([o.result for o in worker.advance_ready()], ["delivered"])
        return SpokenItem.objects.get(pk=item.pk)

    def test_the_five_kinds_project_by_their_mode(self):
        for recipe, (mode, _bed, category) in KINDS.items():
            with self.subTest(recipe=recipe):
                ctype = self.make_type(mode, recipe_key=recipe, category=category, code=recipe.replace("_", "-"))
                a = self.submit_and_run(ctype, 1.0)
                b = self.submit_and_run(ctype, 1.4)
                assets = list(SpokenAsset.objects.filter(content_type=ctype).order_by("created_at"))
                for item in (a, b):
                    self.assertEqual(item.rendition_media.validation_state, "valid")
                if mode == DeliveryMode.CURRENT_EDITION:
                    self.assertEqual(len(assets), 1)
                    asset = assets[0]
                    editions = list(SpokenAssetEdition.objects.filter(asset=asset).order_by("generation"))
                    self.assertEqual([(e.generation, e.state, e.item_id) for e in editions],
                                     [(1, EditionState.SUPERSEDED, a.pk), (2, EditionState.ACTIVE, b.pk)])
                    self.assertEqual((asset.generation, asset.current_item_id), (2, b.pk))
                    self.assertEqual((SpokenItem.objects.get(pk=a.pk).state, b.state), (S.SUPERSEDED, S.DELIVERED))
                    self.assertEqual(self.sha_on_disk(asset.library_relpath), b.rendition_media.sha256)
                    track = Track.objects.get(pk=asset.track_id)
                    self.assertEqual((track.category.code, track.ready2air, track.filepath),
                                     (category, True, str(self.library_file(asset.library_relpath))))
                else:
                    self.assertEqual(len(assets), 2)
                    self.assertEqual([x.item_id for x in assets], [a.pk, b.pk])
                    for asset, item in zip(assets, (a, b)):
                        self.assertEqual(self.sha_on_disk(asset.library_relpath), item.rendition_media.sha256)
                        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.DELIVERED)
                    self.assertEqual(len({x.track_id for x in assets}), 2)
                changes = [f for f in delivery.reconcile() if f.get("action") not in ("none", "skipped")]
                self.assertEqual(changes, [])                                   # a healthy projection

    def test_recall_and_expiry_through_the_real_chain(self):
        news = self.make_type(DeliveryMode.CURRENT_EDITION, recipe_key="news_item", code="news",
                              default_lifetime=timedelta(hours=1))
        a = self.submit_and_run(news, 1.0)
        b = self.submit_and_run(news, 1.2)
        delivery.recall_delivered(a, actor=self.manager, reason="stale recall")         # superseded: no effect
        asset = SpokenAsset.objects.get(content_type=news)
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.LIVE, b.pk))
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=2)):
            self.assertEqual([item.pk for item in delivery.expire_due()], [b.pk])
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.PENDING, None))
        self.assertFalse(Track.objects.get(pk=asset.track_id).ready2air)
        self.assertIsNone(self.sha_on_disk(asset.library_relpath))
        self.assertEqual(SpokenItem.objects.get(pk=a.pk).state, S.RECALLED)              # never resurrected
