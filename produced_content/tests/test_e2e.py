"""D3C end to end through the REAL Phase-C chain:

    producer start (My Content) -> shared recorder take -> Submit for processing
    -> leased spoken render worker -> isadoraair-render with the accepted recipe
    -> exact validated rendition -> spoken projection -> ordinary library Track

for each of the five reused recipes, then the single (current edition) and
accumulating delivery and recall semantics, recall after losing the
assignment, and a missing reference bed failing cleanly."""
import io
import unittest
from unittest import mock

from django.test import TestCase, override_settings

from active_messages import services as active_message_services
from active_messages.models import ActiveBroadcastExecution

from library.models import Track
from produced_content.models import ProducedContentType, ProducedContentTypeAssignment
from production.models import ProductionMedia
from production.services import intake, reference_audio
from production.tests.support import ffmpeg_available
from spoken import worker
from spoken.models import AssetState, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItem
from spoken.tests import support as audio

from .support import PAGE, People, assign, regular_type

S = ItemState
RECIPES = {
    #  recipe       output mode     reference bed       category
    "cal_events": ("single", "bed.cal_events", "PCCAL"),
    "usd239min": ("single", "bed.usd239min", "PCUSD"),
    "news_item": ("single", "bed.news_intro", "PCNEWS"),
    "3min_ctr": ("accumulating", "bed.3min_ctr", "PCCTR"),
    "lcl_psa": ("accumulating", None, "PCPSA"),
}


def register_beds(*keys):
    for key in keys:
        # the beds the accepted Phase-C real-media matrix uses (spoken.tests.test_recipes)
        data = audio.bed(6.0, amplitude=0.25) if key == "bed.news_intro" else audio.bed(6.0)
        bed = intake.ingest_stream(io.BytesIO(data), kind=ProductionMedia.KIND_BED, validate=True).media
        reference_audio.register(key, bed)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
@override_settings(SECURE_SSL_REDIRECT=False)
class EndToEndCase(People, TestCase):
    def produce(self, user, content_type, title, seconds=1.0):
        """Start, record (a deterministic voice), submit -- all over HTTP, as
        the browser does; returns the VALIDATED item."""
        response = self.post(user, f"{PAGE}start/", {"content_type": content_type.key, "title": title})
        self.assertEqual(response.status_code, 302)
        item = SpokenItem.objects.filter(submitted_by=user).order_by("-created_at").first()
        query = f"item={item.pk}"
        take = self.recorded(user, query, data=audio.voice(seconds))
        revision = self.context(user, query).json()["context"]["revision"]
        response = self.commit(user, {"item": str(item.pk)}, take, revision)
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.state, S.VALIDATED)
        return item

    def run_worker(self, item):
        outcome = worker.process_one(sleep=lambda s: None)                 # the real render
        self.assertEqual((outcome.item_id, outcome.result), (str(item.pk), "processed"), outcome)
        self.assertEqual([o.result for o in worker.advance_ready()], ["delivered"])
        return SpokenItem.objects.select_related("rendition_media").get(pk=item.pk)

    def produced(self, user, content_type, title, seconds=1.0):
        return self.run_worker(self.produce(user, content_type, title, seconds))

    def withdraw(self, user, item):
        state = SpokenItem.objects.get(pk=item.pk).state
        return self.post(user, f"{PAGE}{item.pk}/withdraw/", {"expected_state": state})


class FiveRecipeTests(EndToEndCase):
    def setUp(self):
        super().setUp()
        register_beds(*(bed for _mode, bed, _category in RECIPES.values() if bed))

    def test_each_recipe_runs_from_producer_submission_to_an_ordinary_ready_track(self):
        self.assertIs(active_message_services.NATIVE_AIRING_AVAILABLE, False)
        no_engine = mock.patch("isadoraair.engine_commands.enqueue_engine_command",
                               side_effect=AssertionError("no engine command (insert_urgent or any other)"))
        no_engine.start()
        self.addCleanup(no_engine.stop)
        for recipe, (mode, _bed, category_code) in RECIPES.items():
            with self.subTest(recipe=recipe):
                content_type = regular_type(f"pc_{recipe}", output_mode=mode, recipe_key=recipe,
                                            category_code=category_code, name=f"Produced {recipe}",
                                            artist_tag="KOGR Community" if recipe != "lcl_psa" else "")
                assign(self.producer, content_type)
                item = self.produced(self.producer, content_type, f"{recipe} segment")
                rendition = item.rendition_media
                self.assertEqual((item.state, rendition.kind, rendition.recipe_key, rendition.recipe_version,
                                  rendition.validation_state, rendition.container),
                                 (S.DELIVERED, ProductionMedia.KIND_RENDITION, recipe, 1, "valid", "flac"))
                self.assertEqual(rendition.derived_from_id, item.submission_media_id)
                self.assertEqual(list(item.events.values_list("event", flat=True)),
                                 ["created", "submitted", "validated", "processing_started", "processed",
                                  "delivered"])
                asset = SpokenAsset.objects.get(current_item=item)
                track = Track.objects.select_related("category", "artist").get(pk=asset.track_id)
                self.assertEqual((track.category.code, track.ready2air, track.format, track.title,
                                  track.artist.name),
                                 (category_code, True, "flac", f"{recipe} segment",
                                  "KOGR Community" if recipe != "lcl_psa" else f"Produced {recipe}"))
                self.assertEqual(track.filepath, str(self.library_file(asset.library_relpath)))
                self.assertEqual(self.sha_on_disk(asset.library_relpath), rendition.sha256)
                self.assertEqual(asset.mode, "current_edition" if mode == "single" else "accumulating")
        self.assertEqual(ActiveBroadcastExecution.objects.count(), 0)          # ordinary playout only


class DeliverySemanticsTests(EndToEndCase):
    def test_single_replaces_the_current_edition_and_recall_never_resurrects_the_old_one(self):
        news = regular_type("pc_psa_single", output_mode="single", recipe_key="lcl_psa", name="Daily Note")
        assign(self.producer, news)
        first = self.produced(self.producer, news, "Monday", 1.0)
        pending = self.produce(self.producer, news, "Tuesday", 1.3)
        # Starting, recording and submitting the next edition never removes the current one.
        live = SpokenAsset.objects.get(content_type__produced_content_type=news)
        self.assertEqual((live.state, live.current_item_id), (AssetState.LIVE, first.pk))
        self.assertTrue(Track.objects.get(pk=live.track_id).ready2air)
        second = self.run_worker(pending)
        assets = list(SpokenAsset.objects.filter(content_type__produced_content_type=news))
        self.assertEqual(len(assets), 1)                                     # one stable asset ...
        asset = assets[0]
        self.assertEqual(Track.objects.filter(pk=asset.track_id).count(), 1)  # ... and one Track
        self.assertEqual((asset.generation, asset.current_item_id), (2, second.pk))
        self.assertEqual([(e.generation, e.state, e.item_id) for e in
                          SpokenAssetEdition.objects.filter(asset=asset).order_by("generation")],
                         [(1, EditionState.SUPERSEDED, first.pk), (2, EditionState.ACTIVE, second.pk)])
        self.assertEqual(SpokenItem.objects.get(pk=first.pk).state, S.SUPERSEDED)
        page = self.client_for(self.producer).get(PAGE)
        self.assertContains(page, "CURRENT EDITION")
        self.assertContains(page, "Tuesday")
        # A stale recall of the superseded edition never touches the current one.
        self.withdraw(self.producer, first)
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.LIVE, second.pk))
        self.assertTrue(Track.objects.get(pk=asset.track_id).ready2air)
        # Recalling the current edition: unavailable, bytes removed, nothing restored.
        self.withdraw(self.producer, second)
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.PENDING, None))
        self.assertFalse(Track.objects.get(pk=asset.track_id).ready2air)
        self.assertIsNone(self.sha_on_disk(asset.library_relpath))
        self.assertEqual({SpokenItem.objects.get(pk=first.pk).state, SpokenItem.objects.get(pk=second.pk).state},
                         {S.RECALLED})
        self.assertFalse(SpokenAssetEdition.objects.filter(asset=asset, state=EditionState.ACTIVE).exists())

    def test_accumulating_adds_items_and_a_recall_removes_only_that_exact_item(self):
        psa = regular_type("pc_psa_acc", output_mode="accumulating", recipe_key="lcl_psa", name="PSA")
        assign(self.producer, psa)
        first = self.produced(self.producer, psa, "Food drive", 1.0)
        second = self.produced(self.producer, psa, "Blood drive", 1.2)
        assets = {asset.item_id: asset for asset in SpokenAsset.objects.filter(content_type__produced_content_type=psa)}
        self.assertEqual(set(assets), {first.pk, second.pk})
        self.assertEqual(len({asset.track_id for asset in assets.values()}), 2)
        self.withdraw(self.producer, first)
        gone, kept = SpokenAsset.objects.get(pk=assets[first.pk].pk), SpokenAsset.objects.get(pk=assets[second.pk].pk)
        self.assertEqual((gone.state, kept.state), (AssetState.RETIRED, AssetState.LIVE))
        self.assertFalse(Track.objects.get(pk=gone.track_id).ready2air)
        self.assertIsNone(self.sha_on_disk(gone.library_relpath))
        self.assertTrue(Track.objects.get(pk=kept.track_id).ready2air)
        self.assertEqual(self.sha_on_disk(kept.library_relpath), second.rendition_media.sha256)

    def test_losing_the_assignment_or_disabling_the_type_never_strands_delivered_material(self):
        psa = regular_type("pc_psa_strand", output_mode="accumulating", recipe_key="lcl_psa", name="PSA")
        assign(self.producer, psa)
        kept = self.produced(self.producer, psa, "kept", 1.0)
        later = self.produced(self.producer, psa, "later", 1.1)
        ProducedContentTypeAssignment.objects.filter(user=self.producer, content_type=psa).delete()
        page = self.client_for(self.producer).get(PAGE)
        self.assertContains(page, f'data-item="{kept.pk}"')                 # under "Your other items"
        self.assertEqual(self.post(self.producer, f"{PAGE}start/", {"content_type": psa.key}).status_code, 302)
        self.assertEqual(SpokenItem.objects.filter(submitted_by=self.producer).count(), 2)   # no new production
        self.withdraw(self.producer, kept)
        self.assertEqual(SpokenItem.objects.get(pk=kept.pk).state, S.RECALLED)
        assign(self.producer, psa)
        disabled = ProducedContentType.objects.get(pk=psa.pk)
        disabled.enabled = False
        disabled.save()
        self.withdraw(self.producer, later)
        self.assertEqual(SpokenItem.objects.get(pk=later.pk).state, S.RECALLED)

    def test_a_missing_reference_bed_fails_cleanly_with_an_actionable_code(self):
        calendar = regular_type("pc_cal_nobed", output_mode="single", recipe_key="cal_events", name="Calendar")
        assign(self.producer, calendar)
        item = self.produce(self.producer, calendar, "no bed")
        outcome = worker.process_one(sleep=lambda s: None)
        self.assertEqual(outcome.result, "failed")
        item.refresh_from_db()
        self.assertEqual(item.state, S.FAILED)
        self.assertEqual(item.failure_code, "reference_missing")
        self.assertIn("bed.cal_events", item.failure_detail)
        self.assertFalse(SpokenAsset.objects.filter(content_type__produced_content_type=calendar).exists())
        self.assertContains(self.client_for(self.producer).get(PAGE), f"problem: {item.failure_code}")
