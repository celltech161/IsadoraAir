"""A frozen stinger that becomes unusable before render never loses the
airing: the failed occurrence stays truthful and ONE RECONCILE successor is
built from current authority -- for a due occurrence and for a Replay Now
whose messages are not otherwise due -- while cleared, expired or ineligible
content is never resurrected."""
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from active_messages import services, worker
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent
from production.tests.support import ffmpeg_available

from .d2a_support import RealAudio, stinger
from .support import corrupt_stored_bytes

X = ActiveBroadcastExecution


def reconciled_outcome(execution):
    return ActiveBroadcastMessageEvent.objects.get(execution=execution, event="reconciled").detail


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReconcileTests(RealAudio, TestCase):
    def setUp(self):
        super().setUp()
        self.registration = self.use_stinger()

    def air_once(self):
        """Complete one airing, so the active messages are NOT due again for an interval."""
        execution = services.materialize_due_execution()
        started, token = self.put_on_air(execution)
        services.record_air_completed(started, attempt_token=token, ended_at=started.started_at + timedelta(seconds=1),
                                      confirmed_duration_seconds=Decimal("1"))

    def test_a_due_occurrence_whose_frozen_stinger_fails_is_succeeded_and_rendered(self):
        message = self.real_active(kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        snapshot = frozen.composition_snapshot
        corrupt_stored_bytes(self.registration.media)
        outcome = worker.run_once()
        old, successor = X.objects.get(pk=frozen.pk), X.objects.get(pk=outcome.execution)
        self.assertEqual((old.state, old.failure_code, old.composition_snapshot), ("failed", "stinger_pin_unavailable",
                                                                                  snapshot))
        self.assertEqual((outcome.result, successor.state, successor.reason), ("ready", "ready", "reconcile"))
        self.assertIsNone(successor.composition_snapshot["stinger"])                 # current authority: omitted
        self.assertEqual(successor.composition_snapshot["stinger_unavailable"], "reference_hash_mismatch")
        self.assertEqual(successor.composition_snapshot["components"][0]["message"], str(message.pk))
        self.assertEqual(reconciled_outcome(old), {"outcome": "successor", "successor": str(successor.pk)})
        link = ActiveBroadcastMessageEvent.objects.get(execution=successor, event="reconciles")
        self.assertEqual(link.detail, {"predecessor": str(old.pk), "predecessor_reason": "scheduled"})

    def test_replay_now_while_nothing_is_due_still_airs_after_its_stinger_fails(self):
        message = self.real_active(kind="urgent", seconds=1.0)
        self.air_once()
        self.assertGreater(ActiveBroadcastMessage.objects.get(pk=message.pk).next_due_at, timezone.now())
        self.assertIsNone(services.materialize_due_execution())                    # NOT otherwise due
        token = uuid.uuid4()
        replay = services.request_manual_replay(actor=self.admin, request_token=token)
        corrupt_stored_bytes(self.registration.media)
        outcome = worker.run_once()
        self.assertEqual(X.objects.get(pk=replay.pk).failure_code, "stinger_pin_unavailable")
        successor = X.objects.get(pk=outcome.execution)
        self.assertEqual((outcome.result, successor.reason, successor.state), ("ready", "reconcile", "ready"))
        self.assertEqual(ActiveBroadcastMessageEvent.objects.get(execution=successor, event="reconciles").detail,
                         {"predecessor": str(replay.pk), "predecessor_reason": "manual"})
        # The request token still resolves to the ORIGINAL execution -- never moved.
        self.assertEqual(services.request_manual_replay(actor=self.admin, request_token=token).pk, replay.pk)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(request_token=token).count(), 1)
        self.assertIsNone(successor.events.filter(request_token__isnull=False).first())

    def test_the_successor_uses_the_keys_current_valid_registration(self):
        self.real_active(kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        newer = stinger(self.admin, frequency=2500.0)
        corrupt_stored_bytes(self.registration.media)
        outcome = worker.run_once()
        successor = X.objects.get(pk=outcome.execution)
        self.assertEqual(successor.composition_snapshot["stinger"]["reference"], str(newer.pk))
        self.assertEqual(X.objects.get(pk=frozen.pk).composition_snapshot["stinger"]["reference"],
                         str(self.registration.pk))                          # the predecessor's pin is untouched

    def test_reconciliation_is_idempotent_and_survives_a_crash_before_it(self):
        self.real_active(kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        services.fail_execution(frozen, code="stinger_pin_unavailable")      # the worker died right here
        outcome = worker.run_once()                                            # crash recovery reconciles
        successor = X.objects.get(pk=outcome.execution)
        self.assertEqual((outcome.result, successor.reason), ("ready", "reconcile"))
        self.assertEqual(services.reconcile_stinger_failure(frozen).pk, successor.pk)
        self.assertEqual(services.reconcile_stinger_failure(frozen).pk, successor.pk)
        self.assertEqual(worker.run_once().result, "idle")
        self.assertEqual(X.objects.filter(reason="reconcile").count(), 1)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(execution=frozen, event="reconciled").count(), 1)

    def test_an_unfinished_current_occurrence_absorbs_the_reconciliation(self):
        self.real_active(kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        services.fail_execution(frozen, code="stinger_pin_unavailable")
        replay = services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        self.assertEqual(services.reconcile_stinger_failure(frozen).pk, replay.pk)
        self.assertEqual(reconciled_outcome(frozen)["outcome"], "absorbed")
        self.assertFalse(X.objects.filter(reason="reconcile").exists())

    def test_cleared_content_is_not_resurrected(self):
        keep = self.real_active(self.producer, kind="urgent", seconds=1.0)
        gone = self.real_active(self.producer2, kind="public_address", seconds=1.0)
        frozen = services.materialize_due_execution()
        services.fail_execution(frozen, code="stinger_pin_unavailable")
        services.clear_message(gone, actor=self.admin)
        self.assertIsNone(services.reconcile_stinger_failure(frozen))
        self.assertEqual(reconciled_outcome(frozen), {"outcome": "composition_changed", "successor": None})
        self.assertFalse(X.objects.filter(reason="reconcile").exists())
        self.assertTrue(keep)

    def test_expired_content_is_not_resurrected(self):
        now = timezone.now()
        self.real_active(kind="urgent", seconds=1.0, now=now)
        message = ActiveBroadcastMessage.objects.get()
        services.set_message_expiry(message, now + timedelta(minutes=1), actor=self.admin, now=now)
        frozen = services.materialize_due_execution(now=now)
        services.fail_execution(frozen, code="stinger_pin_unavailable", now=now)
        self.assertIsNone(services.reconcile_stinger_failure(frozen, now=now + timedelta(minutes=2)))
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).state, "expired")
        self.assertEqual(reconciled_outcome(frozen)["outcome"], "composition_changed")
        self.assertFalse(X.objects.filter(reason="reconcile").exists())

    def test_currently_ineligible_content_is_not_resurrected(self):
        self.real_active(self.producer, kind="urgent", seconds=1.0)
        self.real_active(self.producer2, kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        services.fail_execution(frozen, code="stinger_pin_unavailable")
        services.set_limits(actor=self.admin, max_active_messages=1)               # the set no longer fits
        self.assertIsNone(services.reconcile_stinger_failure(frozen))
        self.assertFalse(X.objects.filter(reason="reconcile").exists())
        with self.assertRaises(services.CompositionIneligible):
            services.materialize_due_execution()

    def test_only_a_stinger_failure_is_reconciled(self):
        self.real_active(kind="urgent", seconds=1.0)
        frozen = services.materialize_due_execution()
        with self.assertRaises(services.InvalidRequest):
            services.reconcile_stinger_failure(frozen)                         # still PENDING_RENDER
        services.fail_execution(frozen, code="render_failed")
        with self.assertRaises(services.InvalidRequest):
            services.reconcile_stinger_failure(frozen)
