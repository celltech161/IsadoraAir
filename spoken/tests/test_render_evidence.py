"""complete_processing() binds a rendition only if it is EXACTLY the render
the item's type authorizes now: the persisted rendition's source, recipe key,
recipe version and canonical provenance digest must equal both the caller's
RenderEvidence and what the type's current configuration and current
reference audio produce. The evidence alone is never trusted.
"""
import io
import unittest

from django.db import transaction

from production.models import ProductionMedia
from production.services import intake, reference_audio, render
from production.tests.support import IsolatedMediaRootMixin, ffmpeg_available
from spoken import transitions
from spoken.models import ItemState, SpokenContentType, SpokenItem, SpokenItemEvent

from . import support as audio
from .test_domain import Fixtures, make_type, media

S = ItemState


def ingest(data, kind):
    return intake.ingest_stream(io.BytesIO(data), kind=kind, validate=True).media


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RenderEvidenceTests(IsolatedMediaRootMixin, Fixtures):
    def setUp(self):
        super().setUp()
        reference_audio.register("bed.3min_ctr", ingest(audio.bed(1.0), ProductionMedia.KIND_BED))
        self.ctype = make_type(code="ctr", recipe_key="3min_ctr")
        self.item, self.token = self.processing()

    def digest_for(self, **params):
        with transaction.atomic():
            return render.expected("3min_ctr", params).digest

    def assert_refused(self, code, evidence):
        before = (SpokenItem.objects.filter(pk=self.item.pk).values().get(),
                  list(SpokenItemEvent.objects.filter(item=self.item).values_list("id", flat=True)))
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            transitions.complete_processing(self.item, lease_token=self.token, evidence=evidence)
        self.assertEqual(raised.exception.code, code, str(raised.exception))
        after = (SpokenItem.objects.filter(pk=self.item.pk).values().get(),
                 list(SpokenItemEvent.objects.filter(item=self.item).values_list("id", flat=True)))
        self.assertEqual(after, before)                                   # nothing bound, nothing recorded
        self.assertEqual((after[0]["state"], after[0]["rendition_media_id"]), (S.PROCESSING, None))

    def test_the_exact_render_under_the_current_lease_completes(self):
        rendition = self.rendition_for(self.item)
        item = self.complete(self.item, self.token, rendition)
        self.assertEqual((item.state, item.rendition_media_id), (S.PROCESSED, rendition.pk))
        event = SpokenItemEvent.objects.filter(item=item, event="processed").get()
        self.assertEqual(event.detail["evidence"], self.evidence(rendition).as_dict())

    def test_another_recipe_version_is_refused(self):
        stale = self.rendition_for(self.item, recipe_version=2)
        self.assert_refused("render_outdated", self.evidence(stale))
        self.assert_refused("rendition_mismatch", self.evidence(stale, recipe_version=1))   # claim != row

    def test_other_parameters_are_refused(self):
        other = self.rendition_for(self.item, recipe_params_digest=self.digest_for(bed_gain_db=-12.0))
        self.assert_refused("render_outdated", self.evidence(other))
        rendered = self.rendition_for(self.item)                          # right when rendered ...
        ctype = SpokenContentType.objects.get(pk=self.ctype.pk)
        ctype.recipe_params = {"bed_gain_db": -12.0}                      # ... reconfigured before completion
        ctype.save()
        self.assert_refused("render_outdated", self.evidence(rendered))

    def test_replaced_or_retired_reference_audio_is_refused(self):
        rendered = self.rendition_for(self.item)
        reference_audio.register("bed.3min_ctr", ingest(audio.bed(1.5), ProductionMedia.KIND_BED))
        self.assert_refused("render_outdated", self.evidence(rendered))
        rendered = self.rendition_for(self.item)                          # against the new bed
        reference_audio.retire("bed.3min_ctr", reason="gone")
        self.assert_refused("render_outdated", self.evidence(rendered))

    def test_forged_evidence_cannot_vouch_for_a_mismatching_rendition(self):
        expected = self.digest_for()
        forged_row = self.rendition_for(self.item, recipe_params_digest="0" * 64)
        self.assert_refused("rendition_mismatch", self.evidence(forged_row, provenance_digest=expected))
        forged_result = render.RenderResult(
            media=forged_row, recipe_key="3min_ctr", recipe_version=1, params={}, provenance={},
            params_digest=expected, toolchain="forged", measured_input={})
        self.assert_refused("rendition_mismatch", forged_result)
        for junk in (None, {"rendition_id": str(forged_row.pk)}, str(forged_row.pk), forged_row):
            with self.subTest(junk=type(junk).__name__):
                self.assert_refused("rendition_mismatch", junk)
        self.assert_refused("rendition_mismatch", self.evidence(forged_row, rendition_id="not-a-uuid"))

    def test_swapped_rendition_identity_is_refused(self):
        right = self.rendition_for(self.item)
        wrong_digest = self.rendition_for(self.item, recipe_params_digest=self.digest_for(bed_gain_db=-20.0))
        self.assert_refused("rendition_mismatch", self.evidence(right, rendition_id=str(wrong_digest.pk)))
        elsewhere = self.rendition_for(self.item, derived_from=media())
        self.assert_refused("rendition_mismatch", self.evidence(right, rendition_id=str(elsewhere.pk)))

    def test_a_rendition_of_another_source_is_refused(self):
        foreign = self.rendition_for(self.item, derived_from=media())
        self.assert_refused("rendition_mismatch", self.evidence(foreign))
        self.assert_refused("rendition_mismatch", self.evidence(foreign, source_id=str(self.item.submission_media_id)))

    def test_an_unvalidated_rendition_is_refused(self):
        pending = self.rendition_for(self.item, validation_state="unvalidated")
        self.assert_refused("media_unusable", self.evidence(pending))

    def test_a_rendition_bound_to_another_item_is_refused(self):
        twin = transitions.submit(self.item_for_twin(), self.item.submission_media, actor=self.owner)
        twin, twin_token = transitions.start_processing(transitions.record_validation(twin), lease_seconds=60)
        shared = self.rendition_for(self.item)
        self.complete(twin, twin_token, shared)
        self.assert_refused("rendition_already_bound", self.evidence(shared))

    def item_for_twin(self):
        return transitions.create_item(self.ctype, actor=self.owner, title="twin")


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RealRenderCompletionTests(IsolatedMediaRootMixin, Fixtures):
    """End to end: the engine's own RenderResult completes; the same render is
    refused once the bed behind its key is replaced."""

    def setUp(self):
        super().setUp()
        reference_audio.register("bed.3min_ctr", ingest(audio.bed(1.0), ProductionMedia.KIND_BED))
        self.ctype = make_type(code="ctr", recipe_key="3min_ctr", recipe_params={"bed_gain_db": -12.0})

    def processing_with_real_voice(self):
        item = transitions.create_item(self.ctype, actor=self.owner)
        item = transitions.submit(item, ingest(audio.voice(1.0), ProductionMedia.KIND_RECORDING), actor=self.owner)
        return transitions.start_processing(transitions.record_validation(item), lease_seconds=300)

    def render(self, item):
        ctype = SpokenContentType.objects.get(pk=item.content_type_id)
        return render.render(item.submission_media, ctype.recipe_key, ctype.recipe_params)

    def test_a_real_render_completes_and_an_outdated_one_does_not(self):
        item, token = self.processing_with_real_voice()
        result = self.render(item)
        item = transitions.complete_processing(item, lease_token=token, evidence=result)
        self.assertEqual((item.state, item.rendition_media_id), (S.PROCESSED, result.media.pk))

        item, token = self.processing_with_real_voice()
        result = self.render(item)
        reference_audio.register("bed.3min_ctr", ingest(audio.bed(2.0), ProductionMedia.KIND_BED))
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            transitions.complete_processing(item, lease_token=token, evidence=result.evidence)
        self.assertEqual(raised.exception.code, "render_outdated")
        result = self.render(item)                                        # render again: now it completes
        item = transitions.complete_processing(item, lease_token=token, evidence=result.evidence)
        self.assertEqual(item.state, S.PROCESSED)
