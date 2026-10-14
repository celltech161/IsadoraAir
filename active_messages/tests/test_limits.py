"""The composition limits: operator policy on ActiveBroadcastPolicy within
code-level absolute ceilings. Enforced by the services, the admin form and
the database; output-affecting (one generation step, stale airings
suppressed); never satisfied by truncating media; an active set that no
longer fits fails closed with an operator alert."""
import io
import uuid
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from active_messages import guard, models as domain, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent, \
    ActiveBroadcastPolicy
from production.models import ProductionMedia

from .support import Domain, media, raw, registered_stinger
from .test_concurrency import Sessions

User = get_user_model()


def staff(name):
    return User.objects.create_user(name, is_staff=True)


def register_stinger(testcase, seconds=1.0):
    from production.services import intake, reference_audio
    from spoken.tests import support as audio
    bed = intake.ingest_stream(io.BytesIO(audio.bed(seconds)), kind=ProductionMedia.KIND_BED, validate=True).media
    reference_audio.register("stinger", bed, actor=testcase.admin)
    services.set_stinger_key("stinger", actor=testcase.admin)
    return bed


class DefaultsAndCeilingsTests(Domain, TestCase):
    def test_the_defaults(self):
        policy = self.policy()
        self.assertEqual((policy.max_active_messages, policy.max_message_seconds, policy.max_aggregate_seconds),
                         (16, 300, 600))

    def test_the_absolute_ceilings_bound_the_defaults(self):
        self.assertEqual((domain.ABSOLUTE_MAX_ACTIVE_MESSAGES, domain.ABSOLUTE_MAX_MESSAGE_SECONDS,
                          domain.ABSOLUTE_MAX_AGGREGATE_SECONDS), (24, 600, 1200))
        self.assertLessEqual(domain.DEFAULT_MAX_ACTIVE_MESSAGES, domain.ABSOLUTE_MAX_ACTIVE_MESSAGES)
        self.assertLessEqual(domain.DEFAULT_MAX_MESSAGE_SECONDS, domain.ABSOLUTE_MAX_MESSAGE_SECONDS)
        self.assertLessEqual(domain.DEFAULT_MAX_AGGREGATE_SECONDS, domain.ABSOLUTE_MAX_AGGREGATE_SECONDS)
        self.assertLessEqual(domain.DEFAULT_MAX_MESSAGE_SECONDS, domain.DEFAULT_MAX_AGGREGATE_SECONDS)
        # An airing can never hold more components than the absolute ceiling (database).
        self.assertIn("active_execution_components_bounded",
                      {c.name for c in ActiveBroadcastExecution._meta.constraints})

    def test_lower_valid_administrator_values_apply(self):
        policy = services.set_limits(actor=self.admin, max_active_messages=1, max_message_seconds=1,
                                     max_aggregate_seconds=1)
        self.assertEqual((policy.max_active_messages, policy.max_message_seconds, policy.max_aggregate_seconds),
                         (1, 1, 1))
        policy = services.set_limits(actor=self.admin, max_active_messages=24, max_message_seconds=600,
                                     max_aggregate_seconds=1200)                 # exactly the ceilings: allowed
        self.assertEqual((policy.max_active_messages, policy.max_message_seconds, policy.max_aggregate_seconds),
                         (24, 600, 1200))

    def test_zero_negative_and_non_integer_values_are_refused_by_the_service(self):
        for name in services.LIMIT_CEILINGS:
            for bad in (0, -1, -600, True, False, "60", 60.0, Decimal("60"), 1.5):
                with self.subTest(name=name, value=bad), self.assertRaises(services.InvalidRequest) as caught:
                    services.set_limits(actor=self.admin, **{name: bad})
                self.assertEqual(caught.exception.code, "limit_out_of_range")
        self.assertEqual(self.policy().max_active_messages, 16)

    def test_values_above_the_absolute_ceilings_are_refused_by_the_service(self):
        for name, ceiling in services.LIMIT_CEILINGS.items():
            for bad in (ceiling + 1, ceiling * 10, 2 ** 31):
                with self.subTest(name=name, value=bad), self.assertRaises(services.InvalidRequest):
                    services.set_limits(actor=self.admin, **{name: bad})

    def test_the_per_message_limit_never_exceeds_the_aggregate(self):
        with self.assertRaises(services.InvalidRequest) as caught:
            services.set_limits(actor=self.admin, max_message_seconds=500, max_aggregate_seconds=400)
        self.assertEqual(caught.exception.code, "message_exceeds_aggregate")
        with self.assertRaises(services.InvalidRequest):
            services.set_limits(actor=self.admin, max_aggregate_seconds=299)          # 300 s per message today
        self.assertEqual((self.policy().max_message_seconds, self.policy().max_aggregate_seconds), (300, 600))

    def test_the_database_refuses_out_of_range_limits_whoever_writes(self):
        services.current_policy()
        for values in ({"max_active_messages": 0}, {"max_active_messages": 25}, {"max_message_seconds": 0},
                       {"max_message_seconds": 601}, {"max_aggregate_seconds": 0}, {"max_aggregate_seconds": 1201},
                       {"max_message_seconds": 400, "max_aggregate_seconds": 300}):
            with self.subTest(values), self.assertRaises(IntegrityError), transaction.atomic():
                raw(ActiveBroadcastPolicy).filter(pk=1).update(**values)
        for column in ("max_active_messages", "max_message_seconds", "max_aggregate_seconds"):
            with self.subTest(column=column), self.assertRaises(IntegrityError), transaction.atomic():
                raw(ActiveBroadcastPolicy).filter(pk=1).update(**{column: -5})

    def test_only_control_may_change_limits(self):
        for user in (self.producer, self.host, self.nobody, None):
            with self.subTest(user=getattr(user, "username", None)), self.assertRaises(services.NotPermitted):
                services.set_limits(actor=user, max_active_messages=4)


class OrmProhibitionTests(Domain, TestCase):
    def test_direct_orm_mutation_of_the_limits_is_refused(self):
        policy = services.current_policy()
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastPolicy.objects.update(max_active_messages=4)
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastPolicy.objects.filter(pk=1).update(max_aggregate_seconds=60)
        policy.max_message_seconds = 10
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            policy.save()
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastPolicy.objects.bulk_update([policy], ["max_message_seconds"])
        stored = self.policy()
        self.assertEqual((stored.max_active_messages, stored.max_message_seconds, stored.max_aggregate_seconds),
                         (16, 300, 600))


class GenerationTests(Domain, TestCase):
    def test_a_limit_change_is_one_generation_step_however_many_limits_change(self):
        generation = self.policy().generation
        services.set_limits(actor=self.admin, max_active_messages=8, max_message_seconds=120,
                            max_aggregate_seconds=480)
        self.assertEqual(self.policy().generation, generation + 1)
        services.set_limits(actor=self.admin, max_active_messages=8, max_message_seconds=120)      # no change
        self.assertEqual(self.policy().generation, generation + 1)
        services.set_limits(actor=self.admin, max_aggregate_seconds=500)
        self.assertEqual(self.policy().generation, generation + 2)
        events = list(ActiveBroadcastMessageEvent.objects.filter(event="policy_limits").order_by("id"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].detail["limits"], {"max_active_messages": 8, "max_message_seconds": 120,
                                                      "max_aggregate_seconds": 480})
        self.assertEqual(events[0].detail["previous"], {"max_active_messages": 16, "max_message_seconds": 300,
                                                        "max_aggregate_seconds": 600})
        self.assertEqual(events[1].generation, generation + 2)

    def test_a_limit_change_suppresses_a_still_pending_airing_of_the_old_generation(self):
        self.active()
        pending = services.materialize_due_execution()
        services.set_limits(actor=self.admin, max_active_messages=10)                   # still eligible
        stored = ActiveBroadcastExecution.objects.get(pk=pending.pk)
        self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"))
        rebuilt = services.materialize_due_execution()
        self.assertNotEqual(rebuilt.pk, pending.pk)
        self.assertEqual(rebuilt.policy_generation, self.policy().generation)

    def test_an_engine_held_airing_is_left_to_the_engine(self):
        self.active()
        execution = services.materialize_due_execution()
        self.put_on_air(execution)
        services.set_limits(actor=self.admin, max_active_messages=10)
        self.assertEqual(ActiveBroadcastExecution.objects.get(pk=execution.pk).state, "started")


class IndividualDurationTests(Domain, TestCase):
    def test_binding_refuses_media_longer_than_the_configured_limit_and_never_truncates(self):
        services.set_limits(actor=self.admin, max_message_seconds=60, max_aggregate_seconds=600)
        message = services.prepare_message(actor=self.admin, kind="urgent")
        long = media(self.admin, seconds="60.001000")
        before = ProductionMedia.objects.count()
        with self.assertRaises(services.LimitExceeded) as caught:
            services.bind_message_media(message, long, actor=self.admin)
        self.assertEqual(caught.exception.code, "message_too_long")
        self.assertIn("never truncated", str(caught.exception))
        self.assertEqual(ProductionMedia.objects.count(), before)                       # no derived/cut media
        self.assertEqual(ProductionMedia.objects.get(pk=long.pk).decoded_duration_seconds, Decimal("60.001"))
        self.assertIsNone(ActiveBroadcastMessage.objects.get(pk=message.pk).message_media_id)
        services.bind_message_media(message, media(self.admin, seconds="60.000000"), actor=self.admin)  # = limit: ok

    def test_activation_refuses_a_message_that_no_longer_fits_a_lowered_limit(self):
        message = self.prepared(seconds="120.000000")
        services.set_limits(actor=self.admin, max_message_seconds=100)
        with self.assertRaises(services.CompositionIneligible) as caught:
            services.activate_message(message, actor=self.admin)
        self.assertEqual(caught.exception.code, "message_too_long")
        self.assertEqual(caught.exception.detail["message"], str(message.pk))
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).state, "prepared")

    def test_an_already_active_message_over_a_lowered_limit_fails_closed_with_an_alert(self):
        from monitoring.models import SystemEvent
        message = self.active(seconds="120.000000")
        services.set_limits(actor=self.admin, max_message_seconds=100)
        with self.assertRaises(services.CompositionIneligible) as caught:
            services.materialize_due_execution()
        self.assertEqual(caught.exception.code, "message_too_long")
        self.assertFalse(ActiveBroadcastExecution.objects.exists())
        alert = SystemEvent.objects.get(category="active_messages", level="error")
        self.assertEqual(alert.detail["code"], "message_too_long")
        self.assertEqual(alert.detail["message"], str(message.pk))
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).state, "active")   # not silently cleared


class ActiveCountTests(Domain, TestCase):
    def test_activation_beyond_the_configured_count_is_refused(self):
        services.set_limits(actor=self.admin, max_active_messages=2)
        self.active(self.producer)
        self.active(self.producer2)
        third = self.prepared(self.admin)
        with self.assertRaises(services.CompositionIneligible) as caught:
            services.activate_message(third, actor=self.admin)
        self.assertEqual((caught.exception.code, caught.exception.detail), ("too_many_active", {"active": 3, "limit": 2}))
        self.assertEqual(ActiveBroadcastMessage.objects.filter(state="active").count(), 2)

    def test_a_replacement_at_the_limit_is_still_allowed(self):
        services.set_limits(actor=self.admin, max_active_messages=1)
        old = self.active(self.producer, kind="urgent")
        new = self.active(self.producer, kind="urgent")                      # supersedes: the count stays 1
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=old.pk).state, "cleared")
        self.assertEqual(new.state, "active")

    def test_a_set_over_a_lowered_count_fails_closed_for_scheduled_and_manual_airings(self):
        from monitoring.models import SystemEvent
        self.active(self.producer)
        self.active(self.producer2)
        services.set_limits(actor=self.admin, max_active_messages=1)
        with self.assertRaises(services.CompositionIneligible):
            services.materialize_due_execution()
        token = uuid.uuid4()
        with self.assertRaises(services.CompositionIneligible):
            services.request_manual_replay(actor=self.admin, request_token=token)
        self.assertFalse(ActiveBroadcastExecution.objects.exists())
        self.assertFalse(ActiveBroadcastMessageEvent.objects.filter(request_token=token).exists())
        self.assertTrue(SystemEvent.objects.filter(category="active_messages", level="error").exists())
        report = services.snapshot_active_composition()
        self.assertEqual(report["ineligible"]["code"], "too_many_active")
        # Raising the limit again lets the same set air: nothing was cleared or cut.
        services.set_limits(actor=self.admin, max_active_messages=2)
        self.assertEqual(services.materialize_due_execution().component_count, 2)

    def test_the_expiry_work_of_a_failed_closed_pass_still_commits(self):
        from datetime import timedelta

        from django.utils import timezone
        now = timezone.now()
        expiring = self.active(self.producer, expires_at=now + timedelta(minutes=1), now=now)
        self.active(self.producer2, now=now)
        self.active(self.admin, now=now)
        services.set_limits(actor=self.admin, max_active_messages=1)
        with self.assertRaises(services.CompositionIneligible):
            services.materialize_due_execution(now=now + timedelta(minutes=2))
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=expiring.pk).state, "expired")


class AggregateDurationTests(Domain, TestCase):
    def test_the_default_aggregate_limit(self):
        self.active(self.producer, seconds="300.000000")
        self.active(self.producer2, seconds="300.000000")                       # exactly 600 s: allowed
        extra = self.prepared(self.admin, seconds="0.500000")
        with self.assertRaises(services.CompositionIneligible) as caught:
            services.activate_message(extra, actor=self.admin)
        self.assertEqual(caught.exception.code, "aggregate_too_long")
        self.assertEqual(caught.exception.detail["seconds"], "600.500000")

    def test_a_lowered_aggregate_fails_an_active_set_closed(self):
        self.active(self.producer, seconds="100.000000")
        self.active(self.producer2, seconds="100.000000")
        services.set_limits(actor=self.admin, max_message_seconds=100, max_aggregate_seconds=150)
        with self.assertRaises(services.CompositionIneligible) as caught:
            services.materialize_due_execution()
        self.assertEqual((caught.exception.code, caught.exception.detail["limit"]), ("aggregate_too_long", 150))

    def test_the_stinger_counts_toward_the_aggregate_only_when_an_urgent_message_brings_it_in(self):
        bed = register_stinger(self, seconds=1.0)
        stinger_seconds = ProductionMedia.objects.get(pk=bed.pk).decoded_duration_seconds
        self.assertGreater(stinger_seconds, 0)
        services.set_limits(actor=self.admin, max_message_seconds=60, max_aggregate_seconds=60)
        # Public address only: no stinger, 59.5 s fits.
        pa = self.active(self.producer, kind="public_address", seconds="59.500000")
        report = services.snapshot_active_composition()
        self.assertEqual((report["ineligible"], report["snapshot"]["stinger_seconds"],
                          report["snapshot"]["accounted_seconds"]), (None, None, "59.500000"))
        services.clear_message(pa, actor=self.admin)
        # Urgent: the same 59.5 s plus the stinger no longer fits.
        urgent = self.prepared(self.producer, kind="urgent", seconds="59.500000")
        with self.assertRaises(services.CompositionIneligible) as caught:
            services.activate_message(urgent, actor=self.admin)
        self.assertEqual(caught.exception.code, "aggregate_too_long")
        self.assertEqual(caught.exception.detail["stinger_seconds"], str(stinger_seconds))
        self.assertEqual(Decimal(caught.exception.detail["seconds"]), Decimal("59.5") + stinger_seconds)

    def test_the_snapshot_records_the_accounted_airtime_outside_the_digest(self):
        bed = register_stinger(self, seconds=1.0)
        stinger_seconds = ProductionMedia.objects.get(pk=bed.pk).decoded_duration_seconds
        self.active(self.producer, kind="urgent", seconds="20.000000")
        self.active(self.producer2, kind="public_address", seconds="10.000000")
        execution = services.materialize_due_execution()
        snapshot = execution.composition_snapshot
        self.assertEqual([c["duration_seconds"] for c in snapshot["components"]], ["20.000000", "10.000000"])
        self.assertEqual(Decimal(snapshot["accounted_seconds"]), Decimal(30) + stinger_seconds)
        from active_messages import composition
        self.assertNotIn("accounted_seconds", composition.identity(snapshot))


@override_settings(SECURE_SSL_REDIRECT=False)
class AdminTests(Domain, TestCase):
    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser("root", password="x")
        self.client.force_login(self.superuser)
        self.url = reverse("admin:active_messages_activebroadcastpolicy_change", args=[1])

    def post(self, **values):
        data = {"max_active_messages": 16, "max_message_seconds": 300, "max_aggregate_seconds": 600,
                "stinger_key": "", "_save": "Save"}
        data.update(values)
        return self.client.post(self.url, data)

    def test_the_changelist_shows_the_policy_with_its_defaults(self):
        response = self.client.get(reverse("admin:active_messages_activebroadcastpolicy_changelist"))
        self.assertEqual(response.status_code, 200)
        page = self.client.get(self.url)
        self.assertContains(page, "max_active_messages")
        self.assertContains(page, "1..24; default 16")

    def test_a_valid_admin_change_goes_through_the_service(self):
        self.client.get(reverse("admin:active_messages_activebroadcastpolicy_changelist"))
        generation = self.policy().generation
        response = self.post(max_active_messages=4, max_message_seconds=90)
        self.assertEqual(response.status_code, 302)
        policy = self.policy()
        self.assertEqual((policy.max_active_messages, policy.max_message_seconds, policy.generation),
                         (4, 90, generation + 1))
        event = ActiveBroadcastMessageEvent.objects.get(event="policy_limits")
        self.assertEqual((event.actor_username, event.detail["limits"]),
                         ("root", {"max_active_messages": 4, "max_message_seconds": 90}))

    def test_admin_stinger_selection_and_blank_both_use_the_service(self):
        registered_stinger(self.superuser)
        self.client.get(reverse("admin:active_messages_activebroadcastpolicy_changelist"))
        generation = self.policy().generation
        with mock.patch.object(services, "set_stinger_key", wraps=services.set_stinger_key) as setter:
            response = self.post(stinger_key="stinger")
        self.assertEqual(response.status_code, 302)
        setter.assert_called_once()
        self.assertEqual((self.policy().stinger_key, self.policy().generation), ("stinger", generation + 1))
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(event="policy_stinger").count(), 1)

        with mock.patch.object(services, "set_stinger_key", wraps=services.set_stinger_key) as setter:
            response = self.post(stinger_key="")
        self.assertEqual(response.status_code, 302)
        setter.assert_called_once()
        self.assertEqual((self.policy().stinger_key, self.policy().generation), ("", generation + 2))

    def test_admin_rejects_an_unregistered_stinger_and_never_writes_the_model_directly(self):
        self.client.get(reverse("admin:active_messages_activebroadcastpolicy_changelist"))
        response = self.post(stinger_key="not-registered")
        self.assertEqual(response.status_code, 200)
        self.assertIn("stinger_key", response.context["adminform"].form.errors)
        self.assertEqual((self.policy().stinger_key, self.policy().generation), ("", 0))

    def test_the_admin_form_refuses_out_of_range_and_inconsistent_values(self):
        self.client.get(reverse("admin:active_messages_activebroadcastpolicy_changelist"))
        generation = self.policy().generation
        for values in ({"max_active_messages": 0}, {"max_active_messages": 25}, {"max_message_seconds": 601},
                       {"max_aggregate_seconds": 1201}, {"max_aggregate_seconds": -1},
                       {"max_message_seconds": 500, "max_aggregate_seconds": 400}):
            with self.subTest(values):
                response = self.post(**values)
                self.assertEqual(response.status_code, 200)              # the form re-rendered with errors
                self.assertTrue(response.context["adminform"].form.errors)
        policy = self.policy()
        self.assertEqual((policy.max_active_messages, policy.max_message_seconds, policy.max_aggregate_seconds,
                          policy.generation), (16, 300, 600, generation))

    def test_other_policy_fields_and_the_domain_rows_are_read_only_in_admin(self):
        self.client.get(reverse("admin:active_messages_activebroadcastpolicy_changelist"))
        self.post(generation=999, native_airing_enabled="on", default_replay_interval_seconds=60)
        policy = self.policy()
        self.assertEqual((policy.generation, policy.native_airing_enabled, policy.stinger_key,
                          policy.default_replay_interval_seconds), (0, False, "", 1200))
        message = self.active()
        change = self.client.post(reverse("admin:active_messages_activebroadcastmessage_change", args=[message.pk]),
                                  {"state": "cleared"})
        self.assertEqual(change.status_code, 403)


class LimitRaceTests(Sessions):
    def test_an_activation_waiting_behind_a_lowered_count_sees_it_and_is_refused(self):
        self.active(self.producer)
        second = self.prepared(self.producer2)
        first, waiting = self.race(lambda: services.set_limits(actor=self.admin, max_active_messages=1),
                                   lambda: services.activate_message(second, actor=self.admin),
                                   "active_messages.services._suppress_stale")
        self.assertIsNone(first["error"])
        self.assertIsInstance(waiting["error"], services.CompositionIneligible)
        self.assertEqual(waiting["error"].code, "too_many_active")
        self.assertEqual(ActiveBroadcastMessage.objects.filter(state="active").count(), 1)

    def test_a_limit_change_waiting_behind_an_activation_applies_after_it_and_fails_the_set_closed(self):
        self.active(self.producer)
        second = self.prepared(self.producer2)
        generation = self.policy().generation
        first, waiting = self.race(lambda: services.activate_message(second, actor=self.admin),
                                   lambda: services.set_limits(actor=self.admin, max_active_messages=1),
                                   "active_messages.services._suppress_stale")
        self.assertIsNone(first["error"])
        self.assertIsNone(waiting["error"])
        self.assertEqual(self.policy().generation, generation + 2)              # activation, then the limit
        with self.assertRaises(services.CompositionIneligible):
            services.materialize_due_execution()

    def test_a_pending_airing_racing_a_limit_change_is_suppressed(self):
        self.active()
        first, waiting = self.race(services.materialize_due_execution,
                                   lambda: services.set_limits(actor=self.admin, max_aggregate_seconds=590),
                                   "active_messages.services._create_execution")
        self.assertIsNone(first["error"])
        self.assertIsNone(waiting["error"])
        self.assertEqual(ActiveBroadcastExecution.objects.get().state, "suppressed")
