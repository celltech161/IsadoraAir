"""Active messages keyed by the EXACT produced-content type (iPortal D3A):

* identity -- one ACTIVE message per (producer, content type): a producer may
  have several public-address types, several urgent types, or both, active
  together; a replacement supersedes only the same exact type;
* publishing -- CONTROL activates anything; an assigned producer activates
  their own message only when the type allows self-publish;
* recall -- CONTROL clears anything; a producer withdraws their own ACTIVE
  message when the type is recallable (no assignment or self-publish needed);
* scope -- assignment to the exact enabled type, checked server side;
* isolation -- regular and test types never become active messages;
* history -- the frozen kind drives composition, the snapshot names the
  exact type, and the audio digest is unchanged by it."""
from django.db import IntegrityError, transaction
from django.test import TestCase

from active_messages import composition, services
from active_messages.models import ActiveBroadcastMessage, ActiveBroadcastMessageEvent
from library.models import Category, CategoryKind
from produced_content.models import ProducedContentTypeAssignment

from .support import Domain, assign, content_type, media

M = ActiveBroadcastMessage


class ContentTypeCase(Domain, TestCase):
    def setUp(self):
        super().setUp()
        # pa_a: self-publish on (default), producer recall explicitly off; pa_b: the defaults (both on).
        self.pa_a = content_type("town_notice", "public_address", recallable=False)
        self.pa_b = content_type("usd239sc", "public_address")
        self.urgent_a = content_type("school_urgent", "urgent", recallable=True)
        self.reviewed = content_type("reviewed_pa", "public_address", producer_self_publish=False,
                                     recallable=True)
        assign(self.producer, self.pa_a, self.pa_b, self.urgent_a, self.reviewed)

    def draft(self, actor, content_type_, seconds="10.000000"):
        message = services.prepare_message(actor=actor, content_type=content_type_)
        return services.bind_message_media(message, media(actor, seconds=seconds), actor=actor)

    def live(self, actor, content_type_):
        return services.activate_message(self.draft(actor, content_type_), actor=actor)

    def active_types(self, producer):
        return set(M.objects.filter(state="active", producer_key=f"user:{producer.pk}")
                   .values_list("content_type__key", flat=True))


class IdentityTests(ContentTypeCase):
    def test_one_producer_has_several_exact_types_active_together(self):
        self.live(self.producer, self.pa_a)
        self.live(self.producer, self.pa_b)
        self.live(self.producer, self.urgent_a)
        self.assertEqual(self.active_types(self.producer), {"town_notice", "usd239sc", "school_urgent"})

    def test_a_replacement_supersedes_only_the_same_exact_type(self):
        first_a = self.live(self.producer, self.pa_a)
        b = self.live(self.producer, self.pa_b)
        urgent = self.live(self.producer, self.urgent_a)
        second_a = self.live(self.producer, self.pa_a)
        rows = {row.pk: row for row in M.objects.filter(pk__in=[first_a.pk, b.pk, urgent.pk, second_a.pk])}
        self.assertEqual((rows[first_a.pk].state, rows[first_a.pk].superseded_by_id), ("cleared", second_a.pk))
        self.assertEqual({rows[b.pk].state, rows[urgent.pk].state, rows[second_a.pk].state}, {"active"})
        self.assertEqual(self.active_types(self.producer), {"town_notice", "usd239sc", "school_urgent"})
        detail = ActiveBroadcastMessageEvent.objects.get(message=second_a, event="activated").detail
        self.assertEqual((detail["content_type"], detail["supersedes"]), ("town_notice", str(first_a.pk)))

    def test_several_producers_each_have_the_same_type_active(self):
        assign(self.producer2, self.pa_a)
        self.live(self.producer, self.pa_a)
        self.live(self.producer2, self.pa_a)
        self.assertEqual(M.objects.filter(state="active", content_type=self.pa_a).count(), 2)

    def test_the_frozen_kind_comes_from_the_type(self):
        self.assertEqual((self.draft(self.producer, self.pa_b).kind, self.draft(self.producer, self.urgent_a).kind),
                         ("public_address", "urgent"))
        message = services.prepare_message(actor=self.producer, content_type="usd239sc")      # by durable key
        self.assertEqual((message.content_type_id, message.kind), (self.pa_b.pk, "public_address"))
        self.assertEqual(ActiveBroadcastMessageEvent.objects.get(message=message).detail["content_type"],
                         "usd239sc")

    def test_the_snapshot_names_the_exact_type_and_the_digest_ignores_it(self):
        a = self.live(self.producer, self.pa_a)
        b = self.live(self.producer, self.pa_b)
        composed = services.snapshot_active_composition()
        components = {component["message"]: component for component in composed["snapshot"]["components"]}
        self.assertEqual({(components[str(row.pk)]["kind"], components[str(row.pk)]["content_type_key"],
                           components[str(row.pk)]["content_type"]) for row in (a, b)},
                         {("public_address", "town_notice", self.pa_a.pk), ("public_address", "usd239sc", self.pa_b.pk)})
        stripped = {**composed["snapshot"], "components": [
            {key: value for key, value in component.items() if not key.startswith("content_type")}
            for component in composed["snapshot"]["components"]]}
        self.assertEqual(composition.digest(stripped), composed["digest"])        # rendition reuse unaffected

    def test_the_delivery_mode_of_a_referenced_type_is_frozen(self):
        from django.core.exceptions import ValidationError
        self.draft(self.producer, self.pa_a)
        self.pa_a.delivery_mode = "urgent"
        with self.assertRaises(ValidationError):
            self.pa_a.full_clean()
        with self.assertRaises(ValidationError):
            self.pa_a.save()


class PublishingTests(ContentTypeCase):
    def test_an_assigned_producer_self_publishes_their_own_message(self):
        message = self.live(self.producer, self.pa_a)
        self.assertEqual((message.state, message.activated_by_id), ("active", self.producer.pk))
        self.assertTrue(ActiveBroadcastMessageEvent.objects.get(message=message, event="activated")
                        .detail["self_published"])

    def test_self_publish_off_needs_control(self):
        message = self.draft(self.producer, self.reviewed)
        with self.assertRaises(services.NotPermitted) as caught:
            services.activate_message(message, actor=self.producer)
        self.assertEqual(caught.exception.code, "self_publish_disabled")
        self.assertEqual(M.objects.get(pk=message.pk).state, "prepared")         # awaiting publication
        self.assertEqual(services.activate_message(message, actor=self.controller).state, "active")

    def test_control_activates_any_type_without_an_assignment(self):
        self.assertFalse(ProducedContentTypeAssignment.objects.filter(user=self.controller).exists())
        message = self.draft(self.controller, self.urgent_a)
        self.assertEqual(services.activate_message(message, actor=self.controller).state, "active")
        self.assertEqual(services.activate_message(self.draft(self.producer, self.pa_b), actor=self.admin).state,
                         "active")

    def test_an_unassigned_producer_cannot_prepare_or_use_the_type(self):
        with self.assertRaises(services.NotPermitted) as caught:
            services.prepare_message(actor=self.producer2, content_type=self.pa_a)
        self.assertEqual(caught.exception.code, "not_assigned")
        # Assigned when preparing, unassigned before binding/activating: the check is made every time.
        assign(self.producer2, self.pa_a)
        message = services.prepare_message(actor=self.producer2, content_type=self.pa_a)
        ProducedContentTypeAssignment.objects.filter(user=self.producer2, content_type=self.pa_a).delete()
        with self.assertRaises(services.NotPermitted):
            services.bind_message_media(message, media(self.producer2), actor=self.producer2)
        assign(self.producer2, self.pa_a)
        services.bind_message_media(message, media(self.producer2), actor=self.producer2)
        ProducedContentTypeAssignment.objects.filter(user=self.producer2, content_type=self.pa_a).delete()
        with self.assertRaises(services.NotPermitted) as caught:
            services.activate_message(message, actor=self.producer2)
        self.assertEqual(caught.exception.code, "not_assigned")

    def test_a_producer_cannot_activate_someone_elses_draft(self):
        assign(self.producer2, self.pa_a)
        theirs = self.draft(self.producer2, self.pa_a)
        with self.assertRaises(services.NotPermitted) as caught:
            services.activate_message(theirs, actor=self.producer)
        self.assertEqual(caught.exception.code, "not_your_message")
        self.assertEqual(M.objects.get(pk=theirs.pk).state, "prepared")

    def test_users_without_produce_cannot_use_any_type(self):
        assign(self.host, self.pa_a)                    # an assignment is scope, not a capability
        for user in (self.host, self.nobody, None):
            with self.subTest(user=getattr(user, "username", None)), self.assertRaises(services.NotPermitted):
                services.prepare_message(actor=user, content_type=self.pa_a)

    def test_a_disabled_type_refuses_new_work_even_for_control(self):
        message = self.draft(self.producer, self.pa_a)
        self.pa_a.enabled = False
        self.pa_a.save()
        for actor in (self.producer, self.admin):
            with self.subTest(actor=actor.username):
                with self.assertRaises(services.ContentTypeRefused) as caught:
                    services.prepare_message(actor=actor, content_type=self.pa_a)
                self.assertEqual(caught.exception.code, "content_type_disabled")
        with self.assertRaises(services.ContentTypeRefused):
            services.bind_message_media(message, media(self.admin), actor=self.admin)
        with self.assertRaises(services.ContentTypeRefused):
            services.activate_message(message, actor=self.admin)
        self.assertEqual(M.objects.get(pk=message.pk).state, "prepared")

    def test_the_type_replay_override_is_frozen_at_activation(self):
        quick = content_type("quick_pa", "public_address", replay_interval_seconds=300)
        assign(self.producer, quick)
        message = self.live(self.producer, quick)
        self.assertEqual(message.replay_interval_seconds, 300)
        quick.replay_interval_seconds = 900
        quick.save()
        self.assertEqual(M.objects.get(pk=message.pk).replay_interval_seconds, 300)
        self.assertEqual(self.live(self.producer, self.pa_a).replay_interval_seconds,
                         services.current_policy().default_replay_interval_seconds)

    def test_replay_now_and_policy_stay_control_only(self):
        self.live(self.producer, self.pa_a)
        for action in (lambda: services.request_manual_replay(actor=self.producer, request_token="1" * 32),
                       lambda: services.set_stinger_key("stinger", actor=self.producer),
                       lambda: services.set_limits(actor=self.producer, max_active_messages=10),
                       lambda: services.set_default_replay_interval(600, actor=self.producer)):
            with self.assertRaises(services.NotPermitted):
                action()


class RecallTests(ContentTypeCase):
    def test_the_owner_recalls_their_own_recallable_message(self):
        message = self.live(self.producer, self.pa_b)
        generation = services.current_policy().generation
        recalled = services.clear_message(message, actor=self.producer, reason="event cancelled")
        self.assertEqual((recalled.state, recalled.terminal_by_id), ("cleared", self.producer.pk))
        self.assertEqual(services.current_policy().generation, generation + 1)
        self.assertTrue(ActiveBroadcastMessageEvent.objects.get(message=message, event="cleared")
                        .detail["producer_recall"])

    def test_the_owner_cannot_recall_a_non_recallable_type(self):
        message = self.live(self.producer, self.pa_a)
        with self.assertRaises(services.NotPermitted) as caught:
            services.clear_message(message, actor=self.producer)
        self.assertEqual(caught.exception.code, "not_recallable")
        self.assertEqual(M.objects.get(pk=message.pk).state, "active")

    def test_control_clears_either(self):
        for content_type_ in (self.pa_a, self.pa_b):
            message = self.live(self.producer, content_type_)
            self.assertEqual(services.clear_message(message, actor=self.controller).state, "cleared")

    def test_self_publish_off_does_not_prevent_recall(self):
        message = services.activate_message(self.draft(self.producer, self.reviewed), actor=self.admin)
        self.assertEqual(services.clear_message(message, actor=self.producer).state, "cleared")

    def test_losing_the_assignment_does_not_strand_active_material(self):
        message = self.live(self.producer, self.pa_b)
        ProducedContentTypeAssignment.objects.filter(user=self.producer).delete()
        self.pa_b.enabled = False
        self.pa_b.save()
        self.assertEqual(services.clear_message(message, actor=self.producer).state, "cleared")

    def test_a_producer_never_recalls_someone_elses_message(self):
        assign(self.producer2, self.pa_b)
        theirs = self.live(self.producer2, self.pa_b)
        with self.assertRaises(services.NotPermitted) as caught:
            services.clear_message(theirs, actor=self.producer)
        self.assertEqual(caught.exception.code, "not_your_message")
        self.assertEqual(M.objects.get(pk=theirs.pk).state, "active")


class DeliveryIsolationTests(ContentTypeCase):
    def setUp(self):
        super().setUp()
        kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        target = Category.objects.create(code="AMREG", name="regular", kind=kind)
        self.regular = content_type("usd239min", "regular", target_category=target, output_mode="single")
        self.test_type = content_type("test_audio", "test")
        assign(self.producer, self.regular, self.test_type)

    def test_regular_and_test_types_never_become_active_messages(self):
        for content_type_ in (self.regular, self.test_type, "usd239min", "test_audio"):
            for actor in (self.producer, self.admin):
                with self.subTest(type=str(content_type_), actor=actor.username):
                    with self.assertRaises(services.ContentTypeRefused) as caught:
                        services.prepare_message(actor=actor, content_type=content_type_)
                    self.assertEqual(caught.exception.code, "not_an_active_message_type")
        self.assertFalse(M.objects.filter(content_type__in=[self.regular, self.test_type]).exists())

    def test_an_unknown_type_is_refused(self):
        for value in ("no_such_type", 999999, None):
            with self.subTest(value=value), self.assertRaises(services.ContentTypeRefused) as caught:
                services.prepare_message(actor=self.admin, content_type=value)
            self.assertEqual(caught.exception.code, "unknown_content_type")

    def test_a_test_type_can_never_become_airable(self):
        """Nothing in the domain can carry a test (or regular) type to an airing:
        the only way in is prepare_message, which refuses it, and a message's
        type cannot be changed afterwards (its kind is frozen and so is the
        delivery mode of a referenced type)."""
        message = self.draft(self.producer, self.pa_a)
        self.test_type.delivery_mode = "public_address"         # an unreferenced test type may be re-purposed...
        self.test_type.recallable, self.test_type.producer_self_publish = False, True
        self.test_type.save()
        self.assertEqual(M.objects.get(pk=message.pk).content_type_id, self.pa_a.pk)   # ...existing ones are untouched
        # A raw attempt to point a message at a non-active-message type is refused by
        # the guard (no ORM write path exists); the services expose no such operation.
        from active_messages import guard
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            M.objects.filter(pk=message.pk).update(content_type=self.regular)

    def test_a_mode_changed_type_cannot_activate_an_old_draft(self):
        """Defence in depth below the frozen-mode rule: a draft whose type's
        mode no longer matches its frozen kind is refused."""
        from active_messages.tests.support import raw
        from produced_content.models import ProducedContentType
        message = self.draft(self.producer, self.urgent_a)
        raw(ProducedContentType).filter(pk=self.urgent_a.pk).update(delivery_mode="public_address")
        with self.assertRaises(services.ContentTypeRefused) as caught:
            services.activate_message(message, actor=self.admin)
        self.assertEqual(caught.exception.code, "content_type_mode_changed")


class SchemaTests(ContentTypeCase):
    def test_a_message_type_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        self.draft(self.producer, self.pa_a)
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.pa_a.delete()

    def test_the_database_keeps_one_active_message_per_producer_type(self):
        from unittest import mock
        self.live(self.producer, self.pa_a)
        second = self.draft(self.producer, self.pa_a)
        with mock.patch("active_messages.services._current_active_id", return_value=None), \
                self.assertRaises(IntegrityError), transaction.atomic():
            services.activate_message(second, actor=self.admin)
