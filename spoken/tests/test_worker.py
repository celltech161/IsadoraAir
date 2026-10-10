"""2.22C2A -- the leased spoken render worker, and completion's executor check.

claim -> render (isadoraair-render) -> complete | fail, with the SpokenItem
lease as the only authority: bounded transient retries, one re-render after
render_outdated, deterministic failures at once, attempts bounded, a lost
lease left alone, two workers never on one item, and an end-to-end render
through the real service.
"""
import io
import os
import threading
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone

from monitoring.models import SystemEvent
from production.models import ProductionMedia
from production.services import intake, render
from production.services.render import protocol
from production.tests.support import IsolatedMediaRootMixin, ffmpeg_available
from spoken import transitions, worker
from spoken.models import ItemState, SpokenItem, SpokenItemEvent

from . import support as audio
from .test_domain import Fixtures, make_type, media

S = ItemState
User = get_user_model()


class FakeRenders:
    """A render function: each call pops the next behaviour -- an exception,
    or "ok"/"stale" (a rendition the CURRENT/an outdated config would make)."""

    def __init__(self, testcase, *behaviours):
        self.testcase, self.behaviours, self.calls = testcase, list(behaviours), []

    def __call__(self, source, recipe_key, recipe_params, **kwargs):
        self.calls.append(kwargs)
        behaviour = self.behaviours.pop(0)
        if isinstance(behaviour, Exception):
            raise behaviour
        if callable(behaviour):
            return behaviour()
        item = SpokenItem.objects.get(pk=kwargs["item"])
        overrides = {"recipe_params_digest": "0" * 64} if behaviour == "stale" else {}
        rendition = self.testcase.rendition_for(item, **overrides)
        return SimpleNamespace(evidence=self.testcase.evidence(rendition), media=rendition)


def transient(code="busy"):
    return render.RenderError(code, "transient", retryable=True)


class WorkerTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.sleeps = []

    def run_once(self, renders):
        return worker.process_one(render_fn=renders, sleep=self.sleeps.append)

    def test_nothing_claimable(self):
        self.assertIsNone(self.run_once(FakeRenders(self)))

    def test_a_validated_item_is_rendered_and_completed_under_its_lease(self):
        item = self.validated()
        renders = FakeRenders(self, "ok")
        outcome = self.run_once(renders)
        self.assertEqual((outcome.item_id, outcome.result), (str(item.pk), "processed"))
        item.refresh_from_db()
        self.assertEqual((item.state, item.lease_token), (S.PROCESSED, None))
        self.assertEqual(renders.calls[0]["item"], item.pk)
        self.assertIsNotNone(renders.calls[0]["lease"])                       # the lease travels as correlation

    def test_transient_failures_are_retried_within_the_lease_then_succeed(self):
        self.validated()
        outcome = self.run_once(FakeRenders(self, transient(), transient("service_unavailable"), "ok"))
        self.assertEqual(outcome.result, "processed")
        self.assertEqual(self.sleeps, list(worker.BACKOFF_SECONDS))

    def test_transient_failures_are_bounded_and_surface_to_the_operator(self):
        item = self.validated()
        outcome = self.run_once(FakeRenders(self, *(transient() for _ in range(worker.TRANSIENT_RETRIES + 1))))
        self.assertEqual((outcome.result, outcome.code), ("failed", "busy"))
        item.refresh_from_db()
        self.assertEqual((item.state, item.failure_code), (S.FAILED, "busy"))
        self.assertTrue(SystemEvent.objects.filter(category="spoken_render", title__contains="busy").exists())

    def test_deterministic_failures_fail_at_once(self):
        self.validated()
        renders = FakeRenders(self, render.RenderError("reference_missing", "no bed"))
        outcome = self.run_once(renders)
        self.assertEqual((outcome.result, outcome.code, len(renders.calls), self.sleeps),
                         ("failed", "reference_missing", 1, []))
        self.assertFalse(SystemEvent.objects.filter(category="spoken_render").exists())

    def test_an_outdated_render_is_redone_once_against_the_current_configuration(self):
        self.validated()
        outcome = self.run_once(FakeRenders(self, "stale", "ok"))
        self.assertEqual(outcome.result, "processed")
        self.validated()
        outcome = self.run_once(FakeRenders(self, "stale", "stale"))
        self.assertEqual((outcome.result, outcome.code), ("failed", "render_outdated"))

    def test_a_lease_lost_to_expiry_is_left_to_its_new_holder(self):
        item = self.validated()

        def reclaimed_elsewhere():
            later = timezone.now() + timedelta(hours=2)
            with mock.patch("django.utils.timezone.now", return_value=later):
                transitions.start_processing(item.pk, lease_seconds=60)            # another worker
            return FakeRenders(self, "ok")(None, None, None, item=item.pk)
        outcome = self.run_once(FakeRenders(self, reclaimed_elsewhere))
        self.assertEqual((outcome.result, outcome.code), ("lost_lease", "stale_lease"))
        item.refresh_from_db()
        self.assertEqual((item.state, item.processing_attempts), (S.PROCESSING, 2))   # still the other's

    def test_attempts_are_bounded(self):
        item = self.validated()
        later = timezone.now()
        for _ in range(worker.MAX_ATTEMPTS + 1):
            later += timedelta(hours=2)
            with mock.patch("django.utils.timezone.now", return_value=later):
                transitions.start_processing(item.pk, lease_seconds=60)        # crashed workers' claims
        with mock.patch("django.utils.timezone.now", return_value=later + timedelta(hours=2)):
            outcome = self.run_once(FakeRenders(self))
        self.assertEqual((outcome.result, outcome.code), ("failed", "attempts_exhausted"))

    def test_the_command_runs_once(self):
        self.validated()
        out = io.StringIO()
        with mock.patch.object(worker, "process_one",
                               return_value=worker.Outcome("x", "processed")) as process:
            call_command("spoken_render_worker", "--once", stdout=out)
        self.assertEqual((process.call_count, out.getvalue().strip()), (1, "x: processed"))

    def test_completion_refuses_a_rendition_the_executor_did_not_make(self):
        item, token = self.processing()
        forged = self.rendition_for(item, toolchain="ffmpeg version 8; render-engine 1")    # no executor identity
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            self.complete(item, token, forged)
        self.assertEqual(raised.exception.code, "rendition_mismatch")
        copied = self.rendition_for(self.validated_processing_item(), toolchain=f"x; {protocol.EXECUTOR_MARKER}")
        with self.assertRaises(transitions.EvidenceRefused):
            self.complete(item, token, copied)                                 # another item's rendition

    def validated_processing_item(self):
        other, _ = transitions.start_processing(self.validated(), lease_seconds=60)
        return other


class ConcurrentClaimTests(TransactionTestCase):
    def test_two_workers_never_claim_the_same_item(self):
        from production.tests.support import VALID_FACTS, make_row
        owner = User.objects.create_user("owner")
        item = transitions.create_item(make_type(), actor=owner)
        item = transitions.record_validation(transitions.submit(
            item, make_row(kind="recording", validated_at=timezone.now(), **VALID_FACTS), actor=owner))
        barrier, results = threading.Barrier(2), []

        def claim():
            from django.db import connections
            try:
                barrier.wait(30)
                results.append(worker.claim())
            finally:
                connections.close_all()
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).processing_attempts, 1)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class EndToEndWorkerTests(IsolatedMediaRootMixin, Fixtures):
    def test_the_worker_renders_through_the_real_service(self):
        voice = intake.ingest_stream(io.BytesIO(audio.voice(1.0)), kind=ProductionMedia.KIND_RECORDING,
                                     validate=True).media
        item = transitions.record_validation(transitions.submit(self.item(), voice, actor=self.owner))
        outcome = worker.process_one(sleep=lambda seconds: None)
        self.assertEqual(outcome.result, "processed", outcome)
        item.refresh_from_db()
        rendition = item.rendition_media
        self.assertEqual((item.state, rendition.kind, rendition.validation_state), (S.PROCESSED, "rendition", "valid"))
        self.assertTrue(rendition.toolchain.endswith(protocol.EXECUTOR_MARKER))
        self.assertEqual(SpokenItemEvent.objects.filter(item=item, event="processed").get().detail["evidence"]
                         ["provenance_digest"], rendition.recipe_params_digest)
        self.assertIsNone(worker.process_one(sleep=lambda seconds: None))     # nothing left


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class CombinedBoundaryTests(IsolatedMediaRootMixin, Fixtures):
    """C2A corrective: claim -> sealed input snapshots -> render -> the exact
    UNVALIDATED rendition row -> its verdict -> completion under a FRESH
    lease; then each boundary broken on its own -- the source rewritten after
    its snapshot, a rendition column tampered with below the model, the lease
    expiring before completion, the validator down -- without weakening the
    others."""

    def setUp(self):
        super().setUp()
        voice = intake.ingest_stream(io.BytesIO(audio.voice(1.0)), kind=ProductionMedia.KIND_RECORDING,
                                     validate=True).media
        self.voice = voice
        self.item_ = transitions.record_validation(transitions.submit(self.item(), voice, actor=self.owner))
        self.addCleanup(mock.patch.stopall)

    def renditions(self):
        return ProductionMedia.objects.filter(kind=ProductionMedia.KIND_RENDITION)

    def test_the_whole_chain_with_every_boundary_intact(self):
        outcome = worker.process_one(sleep=lambda seconds: None)
        self.assertEqual(outcome.result, "processed", outcome)
        item = SpokenItem.objects.get(pk=self.item_.pk)
        rendition = item.rendition_media
        self.assertEqual((rendition.validation_state, rendition.codec, rendition.sample_rate, rendition.channels),
                         ("valid", "flac", 48000, 2))
        self.assertEqual((item.state, item.lease_token), (S.PROCESSED, None))

    def test_a_source_rewritten_after_its_snapshot_changes_nothing(self):
        from production.services.render import client
        from production.tests.test_render_service import FFMPEG, in_process_render_service, layout_path
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            baseline = client.request_render(self.voice, "lcl_psa")
            expected_sha = baseline.sha256
            baseline.close()
            path = layout_path(self.voice)
            real_step, rewritten = service.run_step, []

            def first_step_rewrites_the_source(argv, pass_fds, timeout, cancelled=None):
                if not rewritten:
                    os.chmod(path, 0o600)
                    data = path.read_bytes()
                    with open(path, "r+b") as handle:                     # same inode, same size, silence
                        handle.write(data[:44] + bytes(len(data) - 44))
                    rewritten.append(True)
                return real_step(argv, pass_fds, timeout, cancelled)
            with mock.patch.object(service, "run_step", first_step_rewrites_the_source):
                outcome = worker.process_one(sleep=lambda seconds: None)
        self.assertEqual((outcome.result, rewritten), ("processed", [True]), outcome)
        self.assertEqual(SpokenItem.objects.get(pk=self.item_.pk).rendition_media.sha256, expected_sha)

    def test_a_rendition_column_tampered_below_the_model_stores_nothing(self):
        from production.tests.test_render_service import _tampering
        with _tampering({"codec": "forged"}):
            outcome = worker.process_one(sleep=lambda seconds: None)
        self.assertEqual((outcome.result, outcome.code), ("failed", "rendition_authority"))
        self.assertFalse(self.renditions().exists())
        self.assertEqual(SpokenItem.objects.get(pk=self.item_.pk).state, S.FAILED)

    def test_a_lease_expiring_before_completion_binds_nothing(self):
        def render_then_expire(*args, **kwargs):
            result = render.render(*args, **kwargs)
            moment = SpokenItem.objects.get(pk=self.item_.pk).lease_expires_at + timedelta(seconds=1)
            mock.patch("django.utils.timezone.now", return_value=moment).start()
            return result
        outcome = worker.process_one(render_fn=render_then_expire, sleep=lambda seconds: None)
        self.assertEqual((outcome.result, outcome.code), ("lost_lease", "lease_expired"))
        item = SpokenItem.objects.get(pk=self.item_.pk)
        self.assertEqual((item.state, item.rendition_media_id), (S.PROCESSING, None))
        self.assertEqual(self.renditions().get().validation_state, "valid")     # an unbound, valid orphan

    def test_a_validator_down_orphan_cannot_complete_until_validated(self):
        from production.services import validation
        item, token = transitions.start_processing(self.item_, lease_seconds=600)
        unavailable = validation.ValidationOutcome(validation.STATUS_INFRASTRUCTURE, "validation_unavailable", {})
        with mock.patch.object(validation, "_analyze_path", return_value=unavailable), \
                self.assertRaises(render.RenderError) as raised:
            render.render(self.voice, self.ctype.recipe_key, self.ctype.recipe_params, item=item.pk, lease=token)
        orphan = ProductionMedia.objects.get(pk=raised.exception.media.pk)
        self.assertEqual((orphan.validation_state, orphan.validated_at, orphan.codec, orphan.sample_rate,
                          orphan.channels, orphan.decoded_duration_seconds), ("unvalidated", None, "", None, None, None))
        with self.assertRaises(transitions.EvidenceRefused) as refused:
            self.complete(item, token, orphan)
        self.assertEqual(refused.exception.code, "media_unusable")
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.PROCESSING)
        validation.validate_media(orphan)                                # the ordinary pipeline, later
        done = self.complete(item, token, ProductionMedia.objects.get(pk=orphan.pk))
        self.assertEqual((done.state, done.rendition_media_id), (S.PROCESSED, orphan.pk))
