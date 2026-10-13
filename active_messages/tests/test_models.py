"""The write guard and the permanent database invariants of active_messages.0001.

Invariants are proven with RAW writes (Django's base QuerySet -- the
raw-SQL-equivalent path): the database itself must refuse, whatever code
writes.
"""
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from active_messages import guard, services
from active_messages.models import (
    ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent, ActiveBroadcastPolicy,
)

from .support import Domain, User, media, raw

SHA = "b" * 64


class GuardTests(Domain, TestCase):
    def setUp(self):
        super().setUp()
        self.message = self.active()
        self.execution = services.materialize_due_execution()
        self.event = ActiveBroadcastMessageEvent.objects.filter(message=self.message).first()

    def test_save_and_delete_always_refuse(self):
        policy = services.current_policy()
        for row in (policy, self.message, self.execution, self.event):
            with self.subTest(model=type(row).__name__):
                with self.assertRaises(guard.ActiveMessageIntegrityError):
                    row.save()
                with self.assertRaises(guard.ActiveMessageIntegrityError):
                    row.delete()

    def test_create_and_get_or_create_refuse(self):
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastMessage.objects.create(kind="urgent", producer_key="user:1", producer_username="x")
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastMessage.objects.get_or_create(kind="urgent", producer_key="user:9", producer_username="y")

    def test_queryset_writes_refuse_including_through_related_managers(self):
        for queryset in (ActiveBroadcastMessage.objects.all(), ActiveBroadcastExecution.objects.all(),
                         ActiveBroadcastMessageEvent.objects.all(), ActiveBroadcastPolicy.objects.all(),
                         self.message.events.all(), self.admin.active_broadcast_messages.all()):
            with self.subTest(queryset=queryset.model.__name__):
                with self.assertRaises(guard.ActiveMessageIntegrityError):
                    queryset.update(id=None)
                with self.assertRaises(guard.ActiveMessageIntegrityError):
                    queryset.delete()
                with self.assertRaises(guard.ActiveMessageIntegrityError):
                    queryset.bulk_update(list(queryset), ["pk"])
                with self.assertRaises(guard.ActiveMessageIntegrityError):
                    queryset.bulk_create([])

    def test_state_cannot_be_changed_through_the_orm(self):
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastMessage.objects.filter(pk=self.message.pk).update(state="cleared")
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastExecution.objects.filter(pk=self.execution.pk).update(state="completed")
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastPolicy.objects.update(generation=0)
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastMessageEvent.objects.update(detail={})

    def test_deleting_a_user_only_nulls_its_links_and_keeps_the_snapshots(self):
        message = self.active(self.producer2)
        key = f"user:{self.producer2.pk}"
        self.producer2.delete()
        message = ActiveBroadcastMessage.objects.get(pk=message.pk)
        self.assertIsNone(message.producer_id)
        self.assertEqual((message.producer_key, message.producer_username, message.state),
                         (key, "producer2", "active"))
        event = ActiveBroadcastMessageEvent.objects.filter(message=message, event="prepared").get()
        self.assertEqual((event.actor_id, event.actor_username), (None, "producer2"))

    def test_bound_media_bytes_cannot_be_purged(self):
        from production.errors import PurgeRefused
        from production.services import retention
        for media_id in (self.message.message_media_id,):
            with self.assertRaises(PurgeRefused) as caught:
                retention.purge_media(media_id)
            self.assertIn(("active_messages.ActiveBroadcastMessage", "message_media", 1), caught.exception.references)


class DatabaseInvariantTests(Domain, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def insert_message(self, **values):
        columns = dict(id=uuid.uuid4(), kind="urgent", state="prepared", producer_key="user:1",
                       producer_username="someone")
        columns.update(values)
        with transaction.atomic():
            raw(ActiveBroadcastMessage).bulk_create([ActiveBroadcastMessage(**columns)])

    def active_values(self, **values):
        columns = dict(state="active", message_media=media(self.admin), activation_generation=1,
                       activated_at=self.now, replay_interval_seconds=1200, next_due_at=self.now)
        columns.update(values)
        return columns

    def insert_execution(self, **values):
        columns = dict(id=uuid.uuid4(), reason="scheduled", state="pending_render", open_slot=1, policy_generation=1,
                       composition_digest=SHA, composition_snapshot={}, component_count=1, due_at=self.now)
        columns.update(values)
        with transaction.atomic():
            raw(ActiveBroadcastExecution).bulk_create([ActiveBroadcastExecution(**columns)])

    def test_one_active_message_per_producer_and_kind(self):
        self.insert_message(**self.active_values())
        self.insert_message(**self.active_values(kind="public_address"))          # other kind: fine
        self.insert_message(**self.active_values(producer_key="user:2"))          # other producer: fine
        self.insert_message(state="cleared", terminal_at=self.now)                # inactive rows: fine
        with self.assertRaises(IntegrityError):
            self.insert_message(**self.active_values())

    def test_state_shape_constraints(self):
        cases = {
            "unknown state": dict(state="replayed"),
            "unknown kind": dict(kind="emergency"),
            "bad producer key": dict(producer_key="nobody"),
            "path-like producer key": dict(producer_key="user:../x"),
            "empty producer snapshot": dict(producer_username=""),
            "active without media": self.active_values(message_media=None),
            "active without due time": self.active_values(next_due_at=None),
            "active without generation": self.active_values(activation_generation=None),
            "active with terminal time": self.active_values(terminal_at=self.now),
            "prepared with activation": dict(activated_at=self.now),
            "prepared with due time": dict(next_due_at=self.now),
            "cleared without time": dict(state="cleared"),
            "expired never active": dict(state="expired", terminal_at=self.now, expires_at=self.now),
            "interval too short": self.active_values(replay_interval_seconds=59),
            "interval too long": self.active_values(replay_interval_seconds=86401),
        }
        superseded_target = uuid.uuid4()
        self.insert_message(id=superseded_target)
        cases["superseded but not cleared"] = dict(superseded_by_id=superseded_target)
        for label, values in cases.items():
            with self.subTest(label), self.assertRaises(IntegrityError):
                self.insert_message(**values)

    def test_execution_constraints(self):
        cases = {
            "unknown state": dict(state="replayed"),
            "bad digest": dict(composition_digest="ABC"),
            "zero components": dict(component_count=0),
            "too many components": dict(component_count=25),            # the absolute ceiling is 24
            "zero generation": dict(policy_generation=0),
            "ready without its aggregate": dict(state="ready"),
            "terminal still holding the slot": dict(state="suppressed", failure_code="x", terminal_at=self.now),
            "unfinished without the slot": dict(open_slot=None),
            "started without start time": dict(state="started", mix_media=media(), ready_at=self.now,
                                               accepted_at=self.now, claimed_at=self.now),
            "claimed without accept": dict(state="claimed", mix_media=media(), ready_at=self.now, claimed_at=self.now),
            "completed without evidence": dict(state="completed", open_slot=None, terminal_at=self.now,
                                               mix_media=media(), ready_at=self.now, accepted_at=self.now,
                                               claimed_at=self.now, started_at=self.now),
            "air end before start": dict(started_at=self.now, ended_at=self.now - timedelta(seconds=1)),
            "evidence without start": dict(ended_at=self.now),
            "failed without code": dict(state="failed", open_slot=None, terminal_at=self.now),
            "negative duration": dict(state="started", mix_media=media(), ready_at=self.now, accepted_at=self.now,
                                      claimed_at=self.now, started_at=self.now, confirmed_duration_seconds=-1),
        }
        for label, values in cases.items():
            with self.subTest(label), self.assertRaises(IntegrityError):
                self.insert_execution(**values)

    def test_only_one_execution_can_be_unfinished(self):
        self.insert_execution()
        self.insert_execution(state="suppressed", open_slot=None, terminal_at=self.now, failure_code="x")
        with self.assertRaises(IntegrityError):
            self.insert_execution()

    def test_policy_is_a_bounded_singleton(self):
        services.current_policy()
        for values in ({"id": 2}, {"id": 1}):
            with self.subTest(values), self.assertRaises(IntegrityError), transaction.atomic():
                raw(ActiveBroadcastPolicy).bulk_create([ActiveBroadcastPolicy(**values)])
        for values in ({"default_replay_interval_seconds": 10}, {"stinger_key": "../stinger.mp3"}):
            with self.subTest(values), self.assertRaises(IntegrityError), transaction.atomic():
                raw(ActiveBroadcastPolicy).filter(pk=1).update(**values)

    def test_event_invariants(self):
        message = services.prepare_message(actor=self.admin, kind="urgent")
        execution_id = uuid.uuid4()
        self.insert_execution(id=execution_id)
        token = uuid.uuid4()

        def insert(**values):
            columns = dict(event="x", actor_kind="system", generation=0)
            columns.update(values)
            with transaction.atomic():
                raw(ActiveBroadcastMessageEvent).bulk_create([ActiveBroadcastMessageEvent(**columns)])

        insert(execution_id=execution_id, request_token=token)
        insert(event="policy_stinger")
        for label, values in {"no subject": {}, "unknown actor kind": dict(message=message, actor_kind="robot"),
                              "token reused": dict(execution_id=execution_id, request_token=token),
                              "token without execution": dict(message=message, request_token=uuid.uuid4())}.items():
            with self.subTest(label), self.assertRaises(IntegrityError):
                insert(**values)


class UserForeignKeyGuardTests(Domain, TestCase):
    def test_only_a_null_user_update_is_allowed(self):
        message = self.active()
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastMessage.objects.filter(pk=message.pk).update(producer=self.producer)
        with self.assertRaises(guard.ActiveMessageIntegrityError):
            ActiveBroadcastMessage.objects.filter(pk=message.pk).update(producer=None, state="cleared")
        self.assertEqual(ActiveBroadcastMessage.objects.filter(pk=message.pk).update(activated_by=None), 1)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
