"""D2A binding authority, reuse and worker outcomes -- exercised with exact
rendition ROWS and injected render functions (the real render service is
covered by test_aggregate_render)."""
import uuid
from unittest import mock

from django.test import TestCase

from active_messages import guard, rendering, services, worker
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessageEvent
from production.models import ProductionMedia
from production.services import retention
from production.services.render.engine import RenderError, RenderResult

from .d2a_support import fake_rendition
from .support import Domain, raw


def result_of(media):
    return RenderResult(media=media, recipe_key=media.recipe_key, recipe_version=media.recipe_version, params={},
                        provenance={}, params_digest=media.recipe_params_digest, toolchain=media.toolchain,
                        measured_input={})


class BindingTests(Domain, TestCase):
    def setUp(self):
        super().setUp()
        self.message = self.active(kind="urgent", seconds="10.000000")
        self.execution = services.materialize_due_execution()
        self.snapshot = self.execution.composition_snapshot

    def test_an_exactly_proven_rendition_binds_ready_with_its_event(self):
        mix = fake_rendition(self.snapshot)
        row = services.bind_execution_mix(self.execution, mix)
        self.assertEqual((row.state, row.mix_media_id, row.open_slot), ("ready", mix.pk, 1))
        self.assertIsNotNone(row.ready_at)
        event = ActiveBroadcastMessageEvent.objects.get(execution=row, event="mix_bound")
        self.assertEqual((event.detail["media"], event.detail["provenance_digest"]), (str(mix.pk),
                                                                                       mix.recipe_params_digest))
        self.assertEqual(services.bind_execution_mix(row, mix).state, "ready")              # idempotent
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(event="mix_bound").count(), 1)

    def test_provenance_that_does_not_prove_the_frozen_aggregate_is_refused(self):
        other = ProductionMedia.objects.get(pk=self.message.message_media_id)
        cases = {
            "another digest": dict(digest="e" * 64),
            "another parent": dict(derived_from=self.make_other_media()),
            "another recipe version": dict(recipe_version=2),
            "another recipe": dict(recipe_key="lcl_psa"),
            "no executor identity": dict(toolchain="ffmpeg; somebody else"),
            "not a rendition": dict(kind=ProductionMedia.KIND_EDIT),
        }
        for label, overrides in cases.items():
            with self.subTest(label), self.assertRaises(services.MixRefused) as caught:
                services.bind_execution_mix(self.execution, fake_rendition(self.snapshot, **overrides))
            self.assertEqual(caught.exception.code, "provenance_mismatch")
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).state, "pending_render")
        self.assertTrue(other)

    def make_other_media(self):
        from .support import media
        return media(self.admin)

    def test_a_digest_that_omits_or_reorders_an_input_is_refused(self):
        from production.services.render import provenance
        inputs = rendering.provenance_inputs(self.snapshot)
        recipe = rendering.assembly()
        for label, changed in {"without the message": [],
                               "with an extra input": inputs + [dict(inputs[0], message=str(uuid.uuid4()))]}.items():
            digest = provenance.digest(provenance.assembly_document(recipe, changed))
            with self.subTest(label), self.assertRaises(services.MixRefused) as caught:
                services.bind_execution_mix(self.execution, fake_rendition(self.snapshot, digest=digest))
            self.assertEqual(caught.exception.code, "provenance_mismatch")

    def test_an_unusable_rendition_is_refused(self):
        for label, overrides in {"unvalidated": dict(validation_state="unvalidated", validated_at=None,
                                                     validation_code="", decoded_duration_seconds=None,
                                                     container="", codec="", sample_rate=None, channels=None),
                                 "invalid": dict(validation_state="invalid", validation_code="corrupt",
                                                 decoded_duration_seconds=None)}.items():
            with self.subTest(label), self.assertRaises(services.MixRefused) as caught:
                services.bind_execution_mix(self.execution, fake_rendition(self.snapshot, **overrides))
            self.assertEqual(caught.exception.code, "rendition_unavailable")
        purged = fake_rendition(self.snapshot)
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_media(purged)
        with self.assertRaises(services.MixRefused) as caught:
            services.bind_execution_mix(self.execution, purged)
        self.assertEqual(caught.exception.code, "rendition_unavailable")

    def test_the_measured_duration_is_checked_against_the_current_aggregate_limit(self):
        long = fake_rendition(self.snapshot, seconds="600.001000")
        with self.assertRaises(services.MixRefused) as caught:
            services.bind_execution_mix(self.execution, long)
        self.assertEqual(caught.exception.code, "aggregate_too_long")
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).state, "pending_render")
        self.assertEqual(services.bind_execution_mix(self.execution, fake_rendition(self.snapshot, seconds="600")).state,
                         "ready")

    def test_a_stale_or_departed_execution_takes_no_aggregate(self):
        mix = fake_rendition(self.snapshot)
        services.clear_message(self.message, actor=self.admin)               # suppresses it (D1)
        with self.assertRaises(services.MixRefused) as caught:
            services.bind_execution_mix(self.execution, mix)
        self.assertEqual(caught.exception.code, "execution_not_pending")
        self.assertIsNone(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).mix_media_id)

    def test_a_pending_execution_of_an_old_generation_is_suppressed_not_bound(self):
        # Defense in depth: every generation change already suppresses pending
        # executions; fabricate the impossible state and prove bind still refuses.
        mix = fake_rendition(self.snapshot)
        raw(type(services.current_policy())).filter(pk=1).update(generation=self.execution.policy_generation + 1)
        with self.assertRaises(services.MixRefused) as caught:
            services.bind_execution_mix(self.execution, mix)
        self.assertEqual(caught.exception.code, "stale_generation")
        stored = ActiveBroadcastExecution.objects.get(pk=self.execution.pk)
        self.assertEqual((stored.state, stored.failure_code, stored.mix_media_id), ("suppressed", "stale_generation", None))

    def test_a_tampered_snapshot_is_refused(self):
        tampered = dict(self.snapshot, components=[dict(self.snapshot["components"][0], sha256="f" * 64)])
        raw(ActiveBroadcastExecution).filter(pk=self.execution.pk).update(composition_snapshot=tampered)
        with self.assertRaises(services.MixRefused) as caught:
            services.bind_execution_mix(self.execution, fake_rendition(tampered))
        self.assertEqual(caught.exception.code, "provenance_mismatch")

    def test_binding_takes_the_binding_lock_and_bound_media_cannot_be_purged(self):
        mix = fake_rendition(self.snapshot)
        with mock.patch("active_messages.services.retention.lock_for_binding",
                        wraps=retention.lock_for_binding) as locked:
            services.bind_execution_mix(self.execution, mix)
        locked.assert_called_once()
        from production.errors import PurgeRefused
        with self.assertRaises(PurgeRefused):
            retention.purge_media(mix)

    def test_failure_codes_are_stable_and_detail_is_bounded_and_path_free(self):
        with self.assertRaises(services.InvalidRequest):
            services.fail_execution(self.execution, code="something_new")
        row = services.fail_execution(self.execution, code="render_failed",
                                      detail="ffmpeg said /srv/isadoraair/production-media/x " + "y" * 900)
        self.assertEqual((row.state, row.failure_code, row.open_slot), ("failed", "render_failed", None))
        self.assertNotIn("/", row.failure_detail)
        self.assertLessEqual(len(row.failure_detail), services.FAILURE_DETAIL_LIMIT)
        self.assertEqual(services.fail_execution(row, code="input_unavailable").failure_code, "render_failed")

    def test_the_orm_still_cannot_move_an_execution(self):
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastExecution.objects.filter(pk=self.execution.pk).update(state="ready")
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastExecution.objects.filter(pk=self.execution.pk).update(mix_media=fake_rendition(self.snapshot))


class ReuseTests(Domain, TestCase):
    def setUp(self):
        super().setUp()
        self.active(kind="public_address", seconds="10.000000")
        self.execution = services.materialize_due_execution()
        self.snapshot = self.execution.composition_snapshot

    def test_a_proven_rendition_is_reused_instead_of_rendering(self):
        mix = fake_rendition(self.snapshot, stored=b"aggregate-bytes")
        render = mock.Mock(side_effect=AssertionError("must not render"))
        outcome = worker.run_once(render_fn=render)
        self.assertEqual((outcome.result, outcome.execution), ("reused", str(self.execution.pk)))
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).mix_media_id, mix.pk)
        render.assert_not_called()

    def test_a_cached_aggregate_whose_stored_bytes_no_longer_verify_is_not_reused(self):
        from production.services import layout
        cases = {"missing": lambda path: path.unlink(),
                 "size-inconsistent": lambda path: path.write_bytes(b"short"),
                 "same-size corrupt": lambda path: path.write_bytes(b"aggregate-bytez")}
        for label, damage in cases.items():
            with self.subTest(label):
                cached = fake_rendition(self.snapshot, stored=b"aggregate-bytes")
                path = layout.resolve_storage_path(cached.storage_key)
                path.chmod(0o640)
                damage(path)
                self.assertIsNone(rendering.find_reusable(self.snapshot))
                # The cached row itself is untouched authority: still VALID and PRESENT.
                self.assertEqual((ProductionMedia.objects.get(pk=cached.pk).validation_state,
                                  ProductionMedia.objects.get(pk=cached.pk).retention_state), ("valid", "present"))
        fresh = []
        outcome = worker.run_once(render_fn=lambda execution: result_of(
            fresh.append(fake_rendition(execution.composition_snapshot, stored=b"fresh-aggregate")) or fresh[-1]))
        self.assertEqual(outcome.result, "ready")                          # rendered anew, not "reused"
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).mix_media_id, fresh[0].pk)

    def test_reuse_needs_full_proof_not_a_matching_composition_digest(self):
        fake_rendition(self.snapshot, digest=self.execution.composition_digest)    # the composition digest string
        fake_rendition(self.snapshot, validation_state="invalid", validation_code="corrupt",
                       decoded_duration_seconds=None)
        purged = fake_rendition(self.snapshot)
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_media(purged)
        self.assertIsNone(rendering.find_reusable(self.snapshot))
        outcome = worker.run_once(render_fn=lambda execution: result_of(fake_rendition(execution.composition_snapshot)))
        self.assertEqual(outcome.result, "ready")                          # rendered anew, not "reused"

    def test_a_crash_between_render_and_bind_is_recovered_by_reuse(self):
        made = []

        def render_then_crash(execution):
            made.append(fake_rendition(execution.composition_snapshot, stored=b"aggregate-bytes"))
            raise KeyboardInterrupt("worker killed after the render, before binding")
        with self.assertRaises(KeyboardInterrupt):
            worker.run_once(render_fn=render_then_crash)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).state, "pending_render")
        outcome = worker.run_once(render_fn=mock.Mock(side_effect=AssertionError("must reuse")))
        self.assertEqual(outcome.result, "reused")
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=self.execution.pk).mix_media_id, made[0].pk)


class WorkerOutcomeTests(Domain, TestCase):
    def setUp(self):
        super().setUp()
        self.message = self.active(kind="urgent", seconds="10.000000")

    def test_retryable_infrastructure_failures_keep_the_execution_and_back_off(self):
        from monitoring.models import SystemEvent
        down = mock.Mock(side_effect=RenderError("service_unavailable", "down", retryable=True))
        first = worker.run_once(render_fn=down)
        self.assertEqual((first.result, first.code), ("retry", "service_unavailable"))
        self.assertEqual(worker.run_once(render_fn=down).result, "backoff")
        execution = ActiveBroadcastExecution.objects.get(pk=first.execution)
        self.assertEqual(execution.state, "pending_render")
        from datetime import timedelta

        from django.utils import timezone
        later = timezone.now()
        for _ in range(2):
            later += timedelta(seconds=worker.BACKOFF_MAX_SECONDS + 1)
            worker.run_once(now=later, render_fn=down)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(execution=execution, event="render_retry").count(), 3)
        self.assertTrue(SystemEvent.objects.filter(category="active_messages", title__contains="render_unavailable")
                        .exists())
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "pending_render")

    def test_permanent_failures_are_coded_and_block_the_same_composition(self):
        for code, raised in (("render_failed", RenderError("render_failed", "x")),
                             ("provenance_mismatch", RenderError("provenance_mismatch", "x")),
                             ("rendition_invalid", RenderError("rendition_invalid", "x"))):
            with self.subTest(code):
                outcome = worker.run_once(render_fn=mock.Mock(side_effect=raised))
                self.assertEqual((outcome.result, outcome.code), ("failed", code))
                self.assertEqual(worker.run_once(render_fn=mock.Mock()).result, "blocked")
                # an operator change (a new generation) unblocks it
                services.set_limits(actor=self.admin, max_active_messages=services.current_policy()
                                    .max_active_messages - 1)

    def test_an_aggregate_measured_over_the_limit_fails_closed_without_truncation(self):
        outcome = worker.run_once(render_fn=lambda execution: result_of(
            fake_rendition(execution.composition_snapshot, seconds="600.500000")))
        self.assertEqual((outcome.result, outcome.code), ("failed", "aggregate_too_long"))
        execution = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertIsNone(execution.mix_media_id)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).composition_snapshot["components"][0]
                         ["message"], str(self.message.pk))
        self.assertEqual(self.message.__class__.objects.get(pk=self.message.pk).state, "active")    # untouched

    def test_a_rendition_lost_before_binding_is_retried(self):
        def render_then_lose(execution):
            mix = fake_rendition(execution.composition_snapshot)
            with self.captureOnCommitCallbacks(execute=True):
                retention.purge_media(mix)
            return result_of(ProductionMedia.objects.get(pk=mix.pk))
        outcome = worker.run_once(render_fn=render_then_lose)
        self.assertEqual((outcome.result, outcome.code), ("retry", "rendition_unavailable"))
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=outcome.execution).state, "pending_render")

    def moved_on_during_render(self, change):
        made = []

        def render_while_changing(execution):
            made.append(fake_rendition(execution.composition_snapshot))
            change()
            return result_of(made[-1])
        outcome = worker.run_once(render_fn=render_while_changing)
        self.assertEqual(outcome.result, "superseded")
        stored = ActiveBroadcastExecution.objects.get(pk=outcome.execution)
        self.assertEqual((stored.state, stored.mix_media_id), ("suppressed", None))
        self.assertEqual(ProductionMedia.objects.get(pk=made[-1].pk).retention_state, "present")   # orphan kept
        return stored

    def test_a_clear_during_the_render_binds_nothing(self):
        self.moved_on_during_render(lambda: services.clear_message(self.message, actor=self.admin))

    def test_a_limit_change_during_the_render_binds_nothing(self):
        self.moved_on_during_render(lambda: services.set_limits(actor=self.admin, max_aggregate_seconds=500))

    def test_a_replacement_during_the_render_binds_nothing(self):
        self.moved_on_during_render(lambda: self.active(kind="urgent", seconds="5.000000"))

    def test_an_expiry_during_the_render_binds_nothing(self):
        from datetime import timedelta

        from django.utils import timezone
        services.set_message_expiry(self.message, timezone.now() + timedelta(seconds=30), actor=self.admin)
        self.moved_on_during_render(lambda: services.expire_due_messages(now=timezone.now() + timedelta(minutes=1)))

    def test_an_input_unavailable_failure_blocks_the_same_composition(self):
        from production.services.render import client
        outcome = worker.run_once(render_fn=mock.Mock(side_effect=RenderError(client.INPUT_UNAVAILABLE, "x")))
        self.assertEqual((outcome.result, outcome.code), ("failed", "input_unavailable"))
        self.assertEqual(worker.run_once().result, "blocked")

    def test_a_service_descriptor_mismatch_is_attributed_to_the_unusable_input(self):
        execution = services.materialize_due_execution()
        with mock.patch("active_messages.rendering.attribute_input_failure", return_value="stinger_pin_unavailable"):
            outcome = worker.process(execution, render_fn=mock.Mock(
                side_effect=RenderError("descriptor_mismatch", "bytes changed")))
        self.assertEqual((outcome.result, outcome.code), ("failed", "stinger_pin_unavailable"))

    def test_attribution_names_the_unusable_frozen_input(self):
        # These fixture rows have no stored bytes at all: every message input is unusable.
        execution = services.materialize_due_execution()
        self.assertEqual(rendering.attribute_input_failure(execution.composition_snapshot), "input_unavailable")
        pinned = dict(execution.composition_snapshot, stinger={"key": "stinger", "reference": str(uuid.uuid4()),
                                                                "media": str(uuid.uuid4()), "sha256": "a" * 64})
        self.assertEqual(rendering.attribute_input_failure(pinned), "stinger_pin_unavailable")
