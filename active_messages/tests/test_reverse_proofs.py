"""Reverse proofs: take one barrier away and show the bad outcome it prevents
really happens. Each test is the mirror of a protective test elsewhere in
this package; if a barrier ever became decorative, its proof here would fail.

Database constraints are dropped inside the test's own transaction (Postgres
DDL is transactional; TestCase rolls it back).
"""
import threading
import uuid
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from active_messages import composition, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent
from production.models import ProductionMedia
from production.services import reconcile, retention

from .support import Domain, media
from .test_concurrency import WAIT, Sessions


def _drop_constraint(model, name):
    with connection.cursor() as cursor:
        # Fire the deferred FK checks of this transaction first: Postgres refuses
        # ALTER TABLE on a table with pending trigger events.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f'ALTER TABLE "{model._meta.db_table}" DROP CONSTRAINT IF EXISTS "{name}"')
        cursor.execute(f'DROP INDEX IF EXISTS "{name}"')


class GenerationReverseProofTests(Domain, TestCase):
    def test_without_stale_suppression_a_cleared_message_keeps_a_live_pending_airing(self):
        message = self.active()
        execution = services.materialize_due_execution()
        with mock.patch("active_messages.services._suppress_stale", return_value=[]):
            services.clear_message(message, actor=self.admin)
        stored = ActiveBroadcastExecution.objects.get(pk=execution.pk)
        self.assertEqual(stored.state, "pending_render")                           # the cleared message would air
        self.assertLess(stored.policy_generation, self.policy().generation)
        # ...and materialization would keep handing that stale airing out:
        with mock.patch("active_messages.services._suppress_stale", return_value=[]):
            self.assertEqual(services.materialize_due_execution().pk, execution.pk)


class UniquenessReverseProofTests(Domain, TestCase):
    def test_without_the_service_check_the_database_still_refuses_a_second_unfinished_airing(self):
        self.active()
        services.materialize_due_execution()
        with mock.patch("active_messages.services._open_execution", return_value=None), \
                self.assertRaises(IntegrityError), transaction.atomic():
            services.materialize_due_execution()

    def test_without_both_barriers_two_due_airings_exist(self):
        self.active()
        services.materialize_due_execution()
        _drop_constraint(ActiveBroadcastExecution, "active_execution_one_unfinished")
        with mock.patch("active_messages.services._open_execution", return_value=None):
            services.materialize_due_execution()
        self.assertEqual(ActiveBroadcastExecution.objects.filter(state="pending_render").count(), 2)

    def test_without_both_token_barriers_one_replay_request_airs_twice(self):
        self.active()
        services.materialize_due_execution()                                # a finished-or-not earlier airing
        token = uuid.uuid4()
        _drop_constraint(ActiveBroadcastMessageEvent, "active_event_one_per_request_token")
        _drop_constraint(ActiveBroadcastExecution, "active_execution_one_unfinished")
        with mock.patch("active_messages.services._open_execution", return_value=None), \
                mock.patch("active_messages.services._prior_request", return_value=None):
            a = services.request_manual_replay(actor=self.admin, request_token=token)
            b = services.request_manual_replay(actor=self.admin, request_token=token)
        self.assertNotEqual(a.pk, b.pk)                                      # one POST retried = two airings
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(request_token=token).count(), 2)

    def test_without_the_service_token_check_the_database_still_refuses_the_retry(self):
        self.active()
        token = uuid.uuid4()
        services.request_manual_replay(actor=self.admin, request_token=token)
        _drop_constraint(ActiveBroadcastExecution, "active_execution_one_unfinished")
        with mock.patch("active_messages.services._open_execution", return_value=None), \
                mock.patch("active_messages.services._prior_request", return_value=None), \
                self.assertRaises(IntegrityError), transaction.atomic():
            services.request_manual_replay(actor=self.admin, request_token=token)

    def test_without_both_active_barriers_one_producer_has_two_active_messages_of_a_type(self):
        self.active(self.producer, kind="urgent")
        new = self.prepared(self.producer, kind="urgent")
        _drop_constraint(ActiveBroadcastMessage, "active_message_one_active_per_producer_type")
        with mock.patch("active_messages.services._current_active_id", return_value=None):
            services.activate_message(new, actor=self.admin)
        self.assertEqual(ActiveBroadcastMessage.objects.filter(
            state="active", kind="urgent", producer_key=f"user:{self.producer.pk}").count(), 2)

    def test_without_the_service_supersession_the_database_refuses_a_second_active_message(self):
        self.active(self.producer, kind="urgent")
        new = self.prepared(self.producer, kind="urgent")
        with mock.patch("active_messages.services._current_active_id", return_value=None), \
                self.assertRaises(IntegrityError), transaction.atomic():
            services.activate_message(new, actor=self.admin)


class IdentityReverseProofTests(Domain, TestCase):
    def test_a_digest_without_exact_media_identity_cannot_tell_two_compositions_apart(self):
        self.active(self.producer, kind="urgent")
        snapshot = services.snapshot_active_composition()["snapshot"]
        other = dict(snapshot, components=[dict(snapshot["components"][0], media=str(uuid.uuid4()), sha256="d" * 64)])
        self.assertNotEqual(composition.digest(snapshot), composition.digest(other))          # the real digest
        careless = lambda s: {"v": s["v"], "components": [{"message": c["message"]} for c in s["components"]],  # noqa: E731
                              "stinger": s["stinger"]}
        with mock.patch("active_messages.composition.identity", careless):
            self.assertEqual(composition.digest(snapshot), composition.digest(other))         # collision

    def test_a_composition_that_followed_mutable_state_would_change_under_an_existing_airing(self):
        now_message = self.prepared(self.producer, kind="urgent")
        active = services.activate_message(now_message, actor=self.admin)
        execution = services.materialize_due_execution()
        # The frozen snapshot is the authority; re-deriving from the CURRENT rows is
        # what a careless implementation would do. After a replacement they differ.
        self.active(self.producer, kind="urgent")
        rebuilt = services.snapshot_active_composition()
        self.assertNotEqual(rebuilt["digest"], execution.composition_digest)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).composition_digest,
                         execution.composition_digest)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "suppressed")
        self.assertTrue(active)


class BindingLockReverseProofTests(Sessions):
    def test_without_lock_for_binding_a_purge_slips_past_an_uncommitted_bind(self):
        message = services.prepare_message(actor=self.admin, content_type="urgent_pa")
        candidate = media(self.admin)
        bound, release = threading.Event(), threading.Event()
        careless = lambda media_id: ProductionMedia.objects.get(pk=getattr(media_id, "pk", media_id))  # noqa: E731
        real_write = services._store.write

        def pausing_write(*args, **kwargs):
            row = real_write(*args, **kwargs)
            bound.set()
            assert release.wait(WAIT)
            return row

        with mock.patch("active_messages.services._lock_media", careless), \
                mock.patch("active_messages.services._store.write", pausing_write):
            binder, box = self.run_thread(lambda: services.bind_message_media(message, candidate, actor=self.admin))
            self.assertTrue(bound.wait(WAIT))
            purged = retention.purge_media(candidate)                       # NOT blocked
            self.assertEqual(purged.retention_state, "purged")
            release.set()
            binder.join(WAIT)
        self.assertIsNone(box["error"])
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).message_media_id, candidate.pk)
        self.assertIn((str(candidate.pk), "active_messages.ActiveBroadcastMessage", "message_media", 1),
                      reconcile.find_purged_media_still_referenced())


class LimitReverseProofTests(Domain, TestCase):
    def test_without_the_eligibility_check_a_set_over_the_policy_limits_goes_active_and_airs(self):
        services.set_limits(actor=self.admin, max_active_messages=1)
        self.active(self.producer)
        with mock.patch("active_messages.services._check_eligible", return_value=None):
            self.active(self.producer2)                                    # over the configured count
            execution = services.materialize_due_execution()
        self.assertEqual(ActiveBroadcastMessage.objects.filter(state="active").count(), 2)
        self.assertEqual(execution.component_count, 2)                     # it would air
        # The real check reports the same set as ineligible:
        self.assertEqual(services.snapshot_active_composition()["ineligible"]["code"], "too_many_active")
