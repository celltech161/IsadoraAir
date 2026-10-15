"""The message lifecycle: prepare, bind, activate (incl. replacement), clear,
expiry, authorization, idempotency and the generation contract."""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from active_messages import services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent
from production.models import ProductionMedia
from production.services import retention

from .support import Domain, corrupt_stored_bytes, media, registered_stinger

M = ActiveBroadcastMessage


class AuthorizationTests(Domain, TestCase):
    def test_capabilities_are_seeded_to_station_administrator_only(self):
        from authz.models import Role
        admin_role = set(Role.objects.get(name="Station Administrator").capabilities.values_list("slug", flat=True))
        self.assertLessEqual({services.PRODUCE, services.CONTROL}, admin_role)
        for role in Role.objects.exclude(name__in=("Station Administrator", "Test Producer", "Test Controller")):
            with self.subTest(role=role.name):
                self.assertFalse(role.capabilities.filter(slug__startswith="active_message.").exists())

    def test_who_may_do_what(self):
        self.assertTrue(services.can(self.admin, services.CONTROL))
        self.assertTrue(services.can(self.producer, services.PRODUCE))
        self.assertFalse(services.can(self.producer, services.CONTROL))
        for user in (self.host, self.nobody):
            with self.subTest(user=user.username):
                with self.assertRaises(services.NotPermitted):
                    services.prepare_message(actor=user, content_type="urgent_pa")
        message = self.prepared(self.producer)
        for action in (lambda: services.activate_message(message, actor=self.producer),
                       lambda: services.request_manual_replay(actor=self.producer, request_token="0" * 32),
                       lambda: services.set_stinger_key("stinger", actor=self.producer),
                       lambda: services.set_default_replay_interval(600, actor=self.producer)):
            with self.assertRaises(services.NotPermitted):
                action()
        with self.assertRaises(services.NotPermitted):
            services.prepare_message(actor=None, content_type="urgent_pa")

    def test_staff_keep_the_compatibility_bypass(self):
        from django.contrib.auth import get_user_model
        staff = get_user_model().objects.create_user("staff", is_staff=True)
        message = self.prepared(staff)
        self.assertEqual(services.activate_message(message, actor=staff).state, "active")

    def test_a_producer_works_only_on_their_own_prepared_messages(self):
        mine = services.prepare_message(actor=self.producer, content_type="urgent_pa")
        with self.assertRaises(services.NotPermitted):
            services.bind_message_media(mine, media(self.producer2), actor=self.producer2)
        with self.assertRaises(services.NotPermitted):
            services.clear_message(mine, actor=self.producer2)
        with self.assertRaises(services.MediaRefused) as caught:
            services.bind_message_media(mine, media(self.producer2), actor=self.producer)
        self.assertEqual(caught.exception.code, "not_your_media")
        self.assertEqual(services.clear_message(mine, actor=self.producer).state, "cleared")       # discard own draft

    def test_media_binding_has_independent_produce_and_control_paths(self):
        mine = services.prepare_message(actor=self.producer, content_type="urgent_pa")
        own = media(self.producer)
        self.assertEqual(services.bind_message_media(mine, own, actor=self.producer).message_media_id, own.pk)

        other = services.prepare_message(actor=self.producer2, content_type="public_address")
        controlled = media(self.producer)
        self.assertFalse(services.can(self.controller, services.PRODUCE))
        self.assertTrue(services.can(self.controller, services.CONTROL))
        self.assertEqual(services.bind_message_media(other, controlled, actor=self.controller).message_media_id,
                         controlled.pk)

        unbound = services.prepare_message(actor=self.producer, content_type="public_address")
        with self.assertRaises(services.NotPermitted):
            services.bind_message_media(unbound, media(self.nobody), actor=self.nobody)
        self.assertIsNone(M.objects.get(pk=unbound.pk).message_media_id)

    def test_a_producer_cannot_clear_an_active_message(self):
        message = self.active(self.producer)
        with self.assertRaises(services.NotPermitted):
            services.clear_message(message, actor=self.producer)


class PrepareAndBindTests(Domain, TestCase):
    def test_prepare_records_the_durable_producer_identity(self):
        message = services.prepare_message(actor=self.producer, content_type="urgent_pa", title="Road closed")
        self.assertEqual((message.state, message.kind, message.producer_key, message.producer_username, message.title),
                         ("prepared", "urgent", f"user:{self.producer.pk}", "producer", "Road closed"))
        self.assertEqual(self.events(message), ["prepared"])

    def test_an_unknown_or_malformed_message_id_is_refused_cleanly(self):
        for message in ("not-a-uuid", "00000000-0000-0000-0000-000000000000", None):
            for action in (lambda: services.activate_message(message, actor=self.admin),
                           lambda: services.clear_message(message, actor=self.admin),
                           lambda: services.bind_message_media(message, media(self.admin), actor=self.admin)):
                with self.subTest(message=message), self.assertRaises(services.InvalidRequest) as caught:
                    action()
                self.assertEqual(caught.exception.code, "unknown_message")

    def test_prepare_validates_its_input(self):
        for kwargs in (dict(content_type="emergency"), dict(content_type="urgent_pa", title="x" * 201),
                       dict(content_type="urgent_pa", expires_at=timezone.now() - timedelta(seconds=1))):
            with self.subTest(kwargs), self.assertRaises(services.InvalidRequest):
                services.prepare_message(actor=self.admin, **kwargs)

    def test_binding_takes_only_valid_present_message_media(self):
        # (A valid media always has a measured duration: production_media_valid_has_facts.)
        message = services.prepare_message(actor=self.admin, content_type="urgent_pa")
        purged = media(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_media(purged)
        cases = {
            "media_not_valid": media(self.admin, validation_state="unvalidated"),
            "media_purged": purged,
            "bed_not_message": media(self.admin, kind=ProductionMedia.KIND_BED),
            "message_too_long": media(self.admin, seconds="300.001000"),
        }
        for code, candidate in cases.items():
            with self.subTest(code):
                with self.assertRaises((services.MediaRefused, services.LimitExceeded)) as caught:
                    services.bind_message_media(message, candidate, actor=self.admin)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(services.MediaRefused) as caught:
            services.bind_message_media(message, "00000000-0000-0000-0000-000000000000", actor=self.admin)
        self.assertEqual(caught.exception.code, "unknown_media")
        self.assertIsNone(M.objects.get(pk=message.pk).message_media_id)

    def test_prepared_media_can_be_replaced_and_rebinding_is_a_no_op(self):
        message = services.prepare_message(actor=self.admin, content_type="urgent_pa")
        first, second = media(self.admin), media(self.admin)
        services.bind_message_media(message, first, actor=self.admin)
        services.bind_message_media(message, first, actor=self.admin)
        message = services.bind_message_media(message, second, actor=self.admin)
        self.assertEqual(message.message_media_id, second.pk)
        bound = ActiveBroadcastMessageEvent.objects.filter(message=message, event="media_bound").order_by("id")
        self.assertEqual([event.detail["media"] for event in bound], [str(first.pk), str(second.pk)])
        self.assertEqual(bound[1].detail["replaced"], str(first.pk))
        # The replaced media is not pinned by history: its bytes may be purged.
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(retention.purge_media(first).retention_state, "purged")

    def test_active_media_is_immutable(self):
        message = self.active()
        with self.assertRaises(services.InvalidTransition) as caught:
            services.bind_message_media(message, media(self.admin), actor=self.admin)
        self.assertEqual(caught.exception.code, "media_immutable")


class ActivationTests(Domain, TestCase):
    def test_activation_makes_the_message_due_now_and_moves_the_generation(self):
        before = self.policy().generation
        now = timezone.now()
        message = self.active(now=now)
        self.assertEqual((message.state, message.activated_at, message.next_due_at, message.replay_interval_seconds),
                         ("active", now, now, 1200))
        self.assertEqual(message.activation_generation, before + 1)
        self.assertEqual(self.policy().generation, before + 1)
        self.assertEqual(self.events(message), ["prepared", "media_bound", "activated"])

    def test_activation_is_idempotent(self):
        message = self.active()
        generation = self.policy().generation
        again = services.activate_message(message, actor=self.admin)
        self.assertEqual((again.state, again.activated_at), ("active", message.activated_at))
        self.assertEqual(self.policy().generation, generation)
        self.assertEqual(self.events(message).count("activated"), 1)

    def test_activation_needs_media_and_a_future_expiry(self):
        bare = services.prepare_message(actor=self.admin, content_type="urgent_pa")
        with self.assertRaises(services.InvalidTransition) as caught:
            services.activate_message(bare, actor=self.admin)
        self.assertEqual(caught.exception.code, "media_required")
        soon = timezone.now() + timedelta(seconds=5)
        message = self.prepared(expires_at=soon)
        with self.assertRaises(services.InvalidTransition) as caught:
            services.activate_message(message, actor=self.admin, now=soon + timedelta(seconds=1))
        self.assertEqual(caught.exception.code, "already_expired")

    def test_a_purged_or_invalidated_media_cannot_go_active(self):
        message = self.prepared()
        from production import transitions
        transitions.mark_purged(message.message_media_id, timezone.now())
        with self.assertRaises(services.MediaRefused) as caught:
            services.activate_message(message, actor=self.admin)
        self.assertEqual(caught.exception.code, "media_purged")

    def test_several_producers_are_active_together(self):
        one = self.active(self.producer, kind="urgent")
        two = self.active(self.producer2, kind="urgent")
        three = self.active(self.producer, kind="public_address")
        self.assertEqual(set(M.objects.filter(state="active").values_list("id", flat=True)), {one.pk, two.pk, three.pk})

    def test_replacement_supersedes_the_same_producers_message_of_the_same_kind_atomically(self):
        old = self.active(self.producer, kind="urgent")
        other = self.active(self.producer2, kind="urgent")
        generation = self.policy().generation
        new = self.active(self.producer, kind="urgent")
        old = M.objects.get(pk=old.pk)
        self.assertEqual((old.state, old.superseded_by_id, old.terminal_reason, old.next_due_at),
                         ("cleared", new.pk, "superseded", None))
        self.assertEqual(M.objects.get(pk=other.pk).state, "active")
        self.assertEqual(self.policy().generation, generation + 1)          # one composition change, one step
        self.assertEqual(new.activation_generation, generation + 1)
        self.assertEqual(self.events(old)[-1], "superseded")


class ClearAndExpiryTests(Domain, TestCase):
    def test_clear_is_exact_idempotent_and_moves_the_generation(self):
        message = self.active()
        generation = self.policy().generation
        cleared = services.clear_message(message, actor=self.admin, reason="recalled")
        self.assertEqual((cleared.state, cleared.terminal_reason, cleared.next_due_at), ("cleared", "recalled", None))
        self.assertEqual(self.policy().generation, generation + 1)
        services.clear_message(message, actor=self.admin)
        self.assertEqual(self.policy().generation, generation + 1)
        self.assertEqual(self.events(message).count("cleared"), 1)

    def test_clearing_an_old_lifecycle_cannot_touch_its_replacement(self):
        old = self.active(self.producer, kind="urgent")
        new = self.active(self.producer, kind="urgent")
        generation = self.policy().generation
        services.clear_message(old, actor=self.admin)                       # a stale "clear" from an old screen
        self.assertEqual(M.objects.get(pk=new.pk).state, "active")
        self.assertEqual(self.policy().generation, generation)

    def test_terminal_messages_never_come_back(self):
        message = self.active()
        services.clear_message(message, actor=self.admin)
        with self.assertRaises(services.InvalidTransition):
            services.activate_message(message, actor=self.admin)
        with self.assertRaises(services.InvalidTransition):
            services.bind_message_media(message, media(self.admin), actor=self.admin)
        with self.assertRaises(services.InvalidTransition):
            services.set_message_expiry(message, timezone.now() + timedelta(days=1), actor=self.admin)

    def test_discarding_a_prepared_message_does_not_change_the_composition(self):
        message = self.prepared()
        generation = self.policy().generation
        self.assertEqual(services.clear_message(message, actor=self.admin).state, "cleared")
        self.assertEqual(self.policy().generation, generation)
        self.assertEqual(self.events(message)[-1], "discarded")

    def test_expiry_sweep_expires_only_due_messages_in_one_generation_step(self):
        now = timezone.now()
        soon = self.active(self.producer, expires_at=now + timedelta(minutes=5), now=now)
        also = self.active(self.producer2, expires_at=now + timedelta(minutes=6), now=now)
        later = self.active(self.producer, kind="urgent", expires_at=now + timedelta(hours=1), now=now)
        never = self.active(self.producer2, kind="urgent", now=now)
        generation = self.policy().generation
        self.assertEqual(services.expire_due_messages(now=now + timedelta(minutes=1)), [])
        self.assertEqual(self.policy().generation, generation)
        expired = services.expire_due_messages(now=now + timedelta(minutes=10))
        self.assertEqual({row.pk for row in expired}, {soon.pk, also.pk})
        self.assertEqual(self.policy().generation, generation + 1)
        states = dict(M.objects.values_list("id", "state"))
        self.assertEqual((states[later.pk], states[never.pk]), ("active", "active"))
        self.assertEqual(M.objects.get(pk=soon.pk).next_due_at, None)

    def test_there_is_no_default_expiry(self):
        self.assertIsNone(self.active().expires_at)

    def test_expiry_can_be_set_on_an_active_message_without_a_composition_change(self):
        message = self.active()
        generation = self.policy().generation
        when = timezone.now() + timedelta(hours=2)
        message = services.set_message_expiry(message, when, actor=self.admin)
        self.assertEqual(message.expires_at, when)
        self.assertEqual(self.policy().generation, generation)
        with self.assertRaises(services.NotPermitted):
            services.set_message_expiry(self.active(self.producer), when, actor=self.producer)


class PolicyTests(Domain, TestCase):
    def test_the_dormant_gate_is_closed_and_cannot_be_opened_in_d1(self):
        self.assertFalse(self.policy().native_airing_enabled)
        with self.assertRaises(services.NativeAiringUnavailable):
            services.set_native_airing(True, actor=self.admin)
        self.assertFalse(self.policy().native_airing_enabled)
        generation = self.policy().generation
        services.set_native_airing(False, actor=self.admin)
        self.assertEqual(self.policy().generation, generation)

    def test_interval_changes_affect_only_future_activations(self):
        before = self.active(self.producer)
        generation = self.policy().generation
        services.set_default_replay_interval(600, actor=self.admin)
        self.assertEqual(self.policy().generation, generation)
        after = self.active(self.producer2)
        self.assertEqual((M.objects.get(pk=before.pk).replay_interval_seconds, after.replay_interval_seconds),
                         (1200, 600))
        for bad in (59, 86401, "600", True):
            with self.subTest(bad), self.assertRaises(services.InvalidRequest):
                services.set_default_replay_interval(bad, actor=self.admin)

    def test_stinger_changes_are_output_affecting(self):
        registered_stinger(self.admin)
        generation = self.policy().generation
        self.assertEqual(services.set_stinger_key("stinger", actor=self.admin).generation, generation + 1)
        self.assertEqual(services.set_stinger_key("stinger", actor=self.admin).generation, generation + 1)
        self.assertEqual(services.set_stinger_key("", actor=self.admin).generation, generation + 2)
        self.assertEqual(services.set_stinger_key("", actor=self.admin).generation, generation + 2)
        with self.assertRaises(services.InvalidRequest) as missing:
            services.set_stinger_key("missing-stinger", actor=self.admin)
        self.assertEqual(missing.exception.code, "reference_missing")
        with self.assertRaises(services.InvalidRequest):
            services.set_stinger_key("../stinger.mp3", actor=self.admin)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(event="policy_stinger").count(), 2)

    def test_stinger_configuration_rejects_a_registered_key_with_corrupt_stored_bytes(self):
        bed = registered_stinger(self.admin)
        corrupt_stored_bytes(bed)
        with self.assertRaises(services.InvalidRequest) as caught:
            services.set_stinger_key("stinger", actor=self.admin)
        self.assertEqual(caught.exception.code, "reference_hash_mismatch")
        self.assertEqual((self.policy().stinger_key, self.policy().generation), ("", 0))

    def test_a_stinger_change_suppresses_old_pending_work_in_one_generation_step(self):
        registered_stinger(self.admin, key="first")
        registered_stinger(self.admin, key="second")
        services.set_stinger_key("first", actor=self.admin)
        self.active(kind="urgent")
        pending = services.materialize_due_execution()
        generation = self.policy().generation
        changed = services.set_stinger_key("second", actor=self.admin)
        self.assertEqual(changed.generation, generation + 1)
        stored = ActiveBroadcastExecution.objects.get(pk=pending.pk)
        self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"))

    def test_generation_never_moves_backward(self):
        seen = [self.policy().generation]
        message = self.active(self.producer)
        seen.append(self.policy().generation)
        self.active(self.producer2)
        seen.append(self.policy().generation)
        services.clear_message(message, actor=self.admin)
        seen.append(self.policy().generation)
        services.materialize_due_execution()
        seen.append(self.policy().generation)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], seen[-2])                              # airing itself never moves it
        generations = list(ActiveBroadcastMessageEvent.objects.order_by("id").values_list("generation", flat=True))
        self.assertEqual(generations, sorted(generations))


class NoOtherAuthorityTests(Domain, TestCase):
    """Phase D owns its own authority: it creates no library, playlist,
    spoken or music-accounting rows, and SystemEvent is never its history."""

    def test_the_whole_lifecycle_creates_no_foreign_rows(self):
        from library.models import LogItem, PlayEvent, Track
        from monitoring.models import SystemEvent
        from spoken.models import SpokenAsset, SpokenItem
        models = (Track, LogItem, PlayEvent, SpokenItem, SpokenAsset, SystemEvent)
        before = {model: model._base_manager.count() for model in models}
        message = self.active(kind="urgent")
        self.active(self.producer, kind="public_address")
        execution = services.materialize_due_execution()
        services.request_manual_replay(actor=self.admin, request_token="9" * 32)
        services.clear_message(message, actor=self.admin)
        services.expire_due_messages()
        self.assertIsNotNone(execution)
        self.assertEqual({model: model._base_manager.count() for model in models}, before)

    def test_history_is_in_domain_events_not_system_events(self):
        from monitoring.models import SystemEvent
        message = self.active()
        self.assertFalse(SystemEvent.objects.filter(category="active_messages").exists())
        self.assertIn("activated", self.events(message))

    def test_no_engine_command_is_ever_queued(self):
        from unittest import mock
        with mock.patch("isadoraair.engine_commands.enqueue_engine_command") as enqueue:
            self.active(kind="urgent")
            services.materialize_due_execution()
            services.request_manual_replay(actor=self.admin, request_token="8" * 32)
        enqueue.assert_not_called()
        self.assertEqual(set(ActiveBroadcastExecution.objects.values_list("state", flat=True)), {"pending_render"})
