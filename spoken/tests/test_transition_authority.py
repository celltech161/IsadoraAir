"""Operation-specific write authority (the replacement for the Boolean
``transition_scope``).

A protected SpokenItem / SpokenItemEvent write succeeds only when it is
EXACTLY the write an open, single-use grant describes -- same model, same
row (or the exact new row), same source state, same columns, same values.
Every test uses valid ItemState values, so the state-enum guard is never
what refuses.
"""
import asyncio
import contextvars
import threading
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.db.models.signals import pre_save
from django.test import TestCase
from django.utils import timezone

from spoken import guard, transitions
from spoken.models import ItemState, SpokenItem, SpokenItemEvent

from .test_domain import Fixtures, make_type, media

User = get_user_model()
S = ItemState
Refused = guard.SpokenIntegrityError


def item_snapshot(item):
    return (SpokenItem._base_manager.filter(pk=item.pk).values().get(),
            list(SpokenItemEvent._base_manager.filter(item_id=item.pk).values_list("id", flat=True)))


class GrantFixtures(Fixtures):
    def setUp(self):
        super().setUp()
        self.assertIsNone(guard._grant.get())                   # nothing leaks between test cases
        self.a = transitions.submit(self.item(), media(), actor=self.owner)       # submitted
        self.b = transitions.submit(self.item(), media(), actor=self.owner)       # submitted
        self.now = timezone.now()
        self.plan_values = (("state", S.VALIDATED), ("state_changed_at", self.now), ("validated_at", self.now))

    def grant_for(self, item, values=None):
        return guard.ItemWrite(item_id=item.pk, from_state=S.SUBMITTED, to_state=S.VALIDATED,
                               values=values or self.plan_values)

    def loaded(self, item, **changes):
        row = SpokenItem.objects.get(pk=item.pk)
        for attname, value in {**dict(self.plan_values), **changes}.items():
            setattr(row, attname, value)
        return row

    def assert_refused(self, attempt, item=None):
        before = item_snapshot(item or self.a)
        with self.assertRaises(Refused), transaction.atomic():
            attempt()
        self.assertEqual(item_snapshot(item or self.a), before)

    def event_values(self, item=None, **changes):
        values = {"item_id": (item or self.a).pk, "event": "validated", "from_state": S.SUBMITTED,
                  "to_state": S.VALIDATED, "actor_kind": "system", "actor_id": None, "actor_username": "",
                  "media_id": (item or self.a).submission_media_id, "asset_id": None, "edition_id": None,
                  "detail": {"duration_seconds": "2.000000"}}
        values.update(changes)
        return values


class CodexBypassRegressionTests(GrantFixtures):
    """Codex's reproduction: while the transition machinery is authorizing a
    legitimate write, unrelated code tries a valid-but-unauthorized transition
    (draft -> delivered, failure_code="forged") and a schema-valid forged
    event. Here that code runs in a pre_save receiver, i.e. INSIDE a real
    transition's save, exactly where the old Boolean scope was active."""

    def test_writes_inside_a_legitimate_transition_cannot_piggyback(self):
        victim = self.item()                                      # draft, 1 event
        attempts, fired = {}, []

        def receiver(sender, instance, **kwargs):
            if sender is not SpokenItem or fired:
                return
            fired.append(True)                                    # once, even if a forgery saves
            for label, attempt in {
                "save delivered+forged": lambda: self._forge_save(victim),
                "update delivered+forged": lambda: SpokenItem.objects.filter(pk=victim.pk).update(
                    state=S.DELIVERED, failure_code="forged"),
                "forged event": lambda: SpokenItemEvent.objects.create(
                    item=victim, event="delivered", from_state=S.DRAFT, to_state=S.DELIVERED,
                    actor_kind="system"),
                "nested grant": lambda: guard._authorized(self.grant_for(victim)).__enter__(),
            }.items():
                try:
                    with transaction.atomic():
                        attempt()
                    attempts[label] = "succeeded"
                except Refused:
                    attempts[label] = "refused"

        pre_save.connect(receiver, weak=False)
        try:
            transitions.record_validation(self.a)                 # a legitimate transition
        finally:
            pre_save.disconnect(receiver)
        self.assertEqual(attempts, dict.fromkeys(attempts, "refused"))
        self.assertEqual(len(attempts), 4)
        victim.refresh_from_db()
        self.assertEqual((victim.state, victim.failure_code, victim.events.count()), (S.DRAFT, "", 1))
        self.assertEqual(SpokenItem.objects.get(pk=self.a.pk).state, S.VALIDATED)    # the real one landed

    def _forge_save(self, victim):
        row = SpokenItem.objects.get(pk=victim.pk)
        row.state, row.failure_code = S.DELIVERED, "forged"
        row.save()

    def test_hand_built_grants_cannot_describe_what_the_table_forbids(self):
        draft = self.item()
        for label, record in {
            "draft -> delivered": guard.ItemWrite(item_id=draft.pk, from_state=S.DRAFT, to_state=S.DELIVERED,
                                                  values=(("state", S.DELIVERED),)),
            "processed -> delivered": guard.ItemWrite(item_id=draft.pk, from_state=S.PROCESSED,
                                                      to_state=S.DELIVERED, values=(("state", S.DELIVERED),)),
            "submit + forged failure": guard.ItemWrite(item_id=draft.pk, from_state=S.DRAFT, to_state=S.SUBMITTED,
                                                       values=(("state", S.SUBMITTED), ("failure_code", "forged"))),
            "state other than target": guard.ItemWrite(item_id=draft.pk, from_state=S.DRAFT, to_state=S.SUBMITTED,
                                                       values=(("state", S.RECALLED),)),
            "delivered event": guard.EventCreate(values=tuple(self.event_values(
                draft, event="delivered", from_state=S.PROCESSED, to_state=S.DELIVERED).items())),
            "event slug mismatch": guard.EventCreate(values=tuple(self.event_values(draft, event="recalled").items())),
            "create non-draft": guard.ItemCreate(item_id=uuid.uuid4(), values=(("state", S.SUBMITTED),
                                                                               ("origin", "submitted"))),
            "create imported": guard.ItemCreate(item_id=uuid.uuid4(), values=(("state", S.DRAFT),
                                                                              ("origin", "imported"))),
            "create with failure": guard.ItemCreate(item_id=uuid.uuid4(), values=(
                ("state", S.DRAFT), ("origin", "submitted"), ("failure_code", "forged"))),
        }.items():
            with self.subTest(grant=label), self.assertRaises(Refused):
                with guard._authorized(record):
                    pass

    def test_hand_built_plans_are_never_committed(self):
        row = SpokenItem.objects.get(pk=self.a.pk)
        forged = transitions.TransitionPlan(
            item_id=row.pk, from_state=S.SUBMITTED, to_state=S.VALIDATED, item_values=self.plan_values,
            event_values=tuple(self.event_values().items()))
        before = item_snapshot(self.a)
        for label, attempt in {"_commit": lambda: transitions._commit(row, forged),
                               "_write_event": lambda: transitions._write_event(forged)}.items():
            with self.subTest(helper=label), self.assertRaises(Refused), transaction.atomic():
                attempt()
        self.assertEqual(item_snapshot(self.a), before)


class ExactItemWriteTests(GrantFixtures):
    def test_the_exact_planned_write_succeeds_once(self):
        with guard._authorized(self.grant_for(self.a)):
            row = self.loaded(self.a)
            row.save()
            row.validated_at = self.now + timedelta(seconds=5)
            with self.assertRaises(Refused):                    # consumed
                row.save()
        self.assertEqual(SpokenItem.objects.get(pk=self.a.pk).state, S.VALIDATED)

    def test_every_unplanned_protected_column_is_refused(self):
        other_media = media()
        extras = {
            "failure_code": "forged", "failure_detail": "forged", "reviewed_by_id": self.manager.pk,
            "reviewed_by_username": "x", "reviewed_at": self.now, "review_note": "x",
            "recalled_by_id": self.manager.pk, "recalled_at": self.now, "recall_reason": "x",
            "lease_token": uuid.uuid4(), "lease_expires_at": self.now, "rendition_media_id": other_media.pk,
            "submission_media_id": other_media.pk, "terminal_at": self.now, "processed_at": self.now,
            "delivered_at": self.now, "expires_at": self.now, "processing_attempts": 7,
            "origin": "assigned", "submitted_by_id": self.other.pk, "submitted_by_username": "x",
            "content_type_id": make_type(code="elsewhere").pk,
        }
        for attname, value in extras.items():
            with self.subTest(column=attname):
                with guard._authorized(self.grant_for(self.a)):
                    self.assert_refused(self.loaded(self.a, **{attname: value}).save)

    def test_planned_columns_with_other_values_are_refused(self):
        for attname, value in {"state": S.RECALLED, "state_changed_at": self.now + timedelta(seconds=1),
                               "validated_at": self.now + timedelta(seconds=1)}.items():
            with self.subTest(column=attname):
                with guard._authorized(self.grant_for(self.a)):
                    self.assert_refused(self.loaded(self.a, **{attname: value}).save)

    def test_a_planned_column_cannot_be_left_out(self):
        with guard._authorized(self.grant_for(self.a)):
            row = self.loaded(self.a)
            self.assert_refused(lambda: row.save(update_fields=["state", "state_changed_at"]))

    def test_the_grant_needs_the_authorized_source_state(self):
        validated = transitions.record_validation(self.b)          # b is no longer submitted
        with guard._authorized(self.grant_for(validated)):
            self.assert_refused(self.loaded(validated).save, item=validated)

    def test_another_row_is_refused_through_save_and_queryset_update(self):
        with guard._authorized(self.grant_for(self.a)):
            self.assert_refused(self.loaded(self.b).save, item=self.b)
            for target in (self.a, self.b):
                with self.subTest(target=str(target.pk)):
                    self.assert_refused(lambda: SpokenItem.objects.filter(pk=target.pk).update(
                        state=S.VALIDATED, state_changed_at=self.now, validated_at=self.now), item=target)
            self.assert_refused(lambda: SpokenItem.objects.all().update(state=S.VALIDATED))

    def test_an_item_grant_does_not_create_items_or_events(self):
        with guard._authorized(self.grant_for(self.a)):
            self.assert_refused(lambda: SpokenItem.objects.create(content_type=self.ctype, state=S.DRAFT))
            self.assert_refused(lambda: SpokenItemEvent.objects.create(**self.event_values()))


class ExactCreationTests(GrantFixtures):
    def creation_grant(self, item_id):
        return guard.ItemCreate(item_id=item_id, values=(
            ("content_type_id", self.ctype.pk), ("origin", "submitted"), ("submitted_by_id", self.owner.pk),
            ("submitted_by_username", "owner"), ("state", S.DRAFT), ("state_changed_at", self.now)))

    def candidate(self, item_id, **overrides):
        values = dict(id=item_id, content_type=self.ctype, origin="submitted", submitted_by=self.owner,
                      submitted_by_username="owner", state=S.DRAFT, state_changed_at=self.now)
        values.update(overrides)
        return SpokenItem(**values)

    def test_a_creation_grant_creates_exactly_one_described_row(self):
        item_id = uuid.uuid4()
        before = SpokenItem.objects.count()
        for label, overrides in {
            "another id": {"id": uuid.uuid4()}, "other type": {"content_type": make_type(code="alt")},
            "other origin": {"origin": "assigned"}, "other submitter": {"submitted_by": self.other},
            "lifecycle timestamp": {"validated_at": self.now}, "failure": {"failure_code": "forged"},
            "lease": {"lease_token": uuid.uuid4(), "lease_expires_at": self.now},
            "other time": {"state_changed_at": self.now + timedelta(seconds=1)},
        }.items():
            with self.subTest(case=label), guard._authorized(self.creation_grant(item_id)):
                with self.assertRaises(Refused), transaction.atomic():
                    self.candidate(item_id, **overrides).save(force_insert=True)
        with guard._authorized(self.creation_grant(item_id)):
            self.candidate(item_id).save(force_insert=True)
            with self.assertRaises(Refused), transaction.atomic():        # single use
                self.candidate(uuid.uuid4()).save(force_insert=True)
        self.assertEqual(SpokenItem.objects.count(), before + 1)

    def test_creation_and_its_event_are_atomic(self):
        before = (SpokenItem.objects.count(), SpokenItemEvent.objects.count())
        with mock.patch.object(type(SpokenItemEvent.objects), "create", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                transitions.create_item(self.ctype, actor=self.owner)
        self.assertEqual((SpokenItem.objects.count(), SpokenItemEvent.objects.count()), before)


class ExactEventTests(GrantFixtures):
    def test_every_altered_event_column_is_refused_and_the_exact_one_inserts_once(self):
        other_media = media()
        legit = self.event_values()
        alterations = {"item_id": self.b.pk, "event": "invalid", "from_state": S.DRAFT, "to_state": S.INVALID,
                       "actor_kind": "worker", "actor_id": self.manager.pk, "actor_username": "someone",
                       "media_id": other_media.pk, "asset_id": uuid.uuid4(), "edition_id": uuid.uuid4(),
                       "detail": {"forged": True}}
        for attname, value in alterations.items():
            with self.subTest(column=attname), guard._authorized(guard.EventCreate(values=tuple(legit.items()))):
                self.assert_refused(lambda: SpokenItemEvent.objects.create(**{**legit, attname: value}))
        with guard._authorized(guard.EventCreate(values=tuple(legit.items()))):
            SpokenItemEvent.objects.create(**legit)
            self.assert_refused(lambda: SpokenItemEvent.objects.create(**legit))      # consumed
        self.assert_refused(lambda: SpokenItemEvent.objects.create(**legit))          # and closed
        self.assertEqual(SpokenItemEvent.objects.filter(item=self.a, event="validated").count(), 1)

    def test_an_event_grant_does_not_authorize_item_writes(self):
        with guard._authorized(guard.EventCreate(values=tuple(self.event_values().items()))):
            self.assert_refused(self.loaded(self.a).save)


class LeakageTests(GrantFixtures):
    def test_grants_close_on_exit_and_on_exceptions(self):
        with guard._authorized(self.grant_for(self.a)):
            pass
        self.assertIsNone(guard._grant.get())
        self.assert_refused(self.loaded(self.a).save)
        with self.assertRaises(ZeroDivisionError):
            with guard._authorized(self.grant_for(self.a)):
                raise ZeroDivisionError
        self.assertIsNone(guard._grant.get())
        self.assert_refused(self.loaded(self.a).save)

    def test_grants_do_not_nest(self):
        with guard._authorized(self.grant_for(self.a)):
            with self.assertRaises(Refused):
                with guard._authorized(self.grant_for(self.b)):
                    pass
            with self.assertRaises(Refused):
                with guard._authorized(guard.EventCreate(values=tuple(self.event_values().items()))):
                    pass
        self.assertIsNone(guard._grant.get())

    def test_grants_do_not_cross_threads(self):
        results = {}
        row = self.loaded(self.a)

        def attempt(label):
            try:
                with connections["default"].cursor() as cursor:    # never wait on the test's row lock
                    cursor.execute("SET lock_timeout = '2s'")
                row.save()
                results[label] = "succeeded"
            except Refused:
                results[label] = "refused"
            except Exception as exc:                            # noqa: BLE001 -- reported below
                results[label] = repr(exc)
            finally:
                connections.close_all()
        with guard._authorized(self.grant_for(self.a)):
            plain = threading.Thread(target=attempt, args=("fresh thread",))
            plain.start()
            plain.join(30)
            context = contextvars.copy_context()                 # even a thread handed the context
            copied = threading.Thread(target=context.run, args=(attempt, "copied context"))
            copied.start()
            copied.join(30)
        self.assertEqual(results, {"fresh thread": "refused", "copied context": "refused"})

    def test_grants_do_not_reach_other_async_tasks(self):
        async def in_task():
            try:
                self.loaded_row.save()
                return "succeeded"
            except Refused:
                return "refused"
        self.loaded_row = self.loaded(self.a)
        async def spawns_a_task():
            return await asyncio.get_running_loop().create_task(in_task())
        with guard._authorized(self.grant_for(self.a)):
            self.assertEqual(asyncio.run(in_task()), "refused")             # a task copies the context
            self.assertEqual(asyncio.run(spawns_a_task()), "refused")
        self.assertEqual(SpokenItem.objects.get(pk=self.a.pk).state, S.SUBMITTED)


class LegitimateLifecycleTests(GrantFixtures):
    """Every C1 transition still works, records exactly its event, and rolls
    back with its event on an injected failure."""

    def assert_step(self, item, call, state, event, stamps=()):
        before = item_snapshot(item)
        with mock.patch.object(type(SpokenItemEvent.objects), "create", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                call()
        self.assertEqual(item_snapshot(item), before)                          # atomic
        result = call()
        row = result[0] if isinstance(result, tuple) else result
        row = SpokenItem.objects.get(pk=row.pk)
        events = list(SpokenItemEvent.objects.filter(item=row).order_by("id"))
        self.assertEqual(len(events), len(before[1]) + 1)                      # exactly one event
        self.assertEqual((row.state, events[-1].event, events[-1].to_state), (state, event, state))
        self.assertNotEqual(row.state_changed_at, before[0]["state_changed_at"])   # restamped
        for stamp in stamps:
            self.assertIsNotNone(getattr(row, stamp), stamp)
        return result

    def test_every_c1_transition(self):
        draft = self.item()
        self.assertEqual((draft.state, draft.events.get().event), (S.DRAFT, "created"))
        self.assert_step(draft, lambda: transitions.submit(draft, media(), actor=self.owner), S.SUBMITTED,
                         "submitted", ("submitted_at",))
        self.assert_step(draft, lambda: transitions.record_validation(draft), S.VALIDATED, "validated",
                         ("validated_at",))
        bad = transitions.submit(self.item(), media(validation_state="invalid", validation_code="corrupt"),
                                 actor=self.owner)
        self.assert_step(bad, lambda: transitions.record_validation(bad), S.INVALID, "invalid", ("terminal_at",))
        item, token = self.assert_step(draft, lambda: transitions.start_processing(draft, lease_seconds=60),
                                       S.PROCESSING, "processing_started", ("lease_token", "processing_started_at"))
        later = timezone.now() + timedelta(hours=2)
        with mock.patch("django.utils.timezone.now", return_value=later):
            item, token = self.assert_step(item, lambda: transitions.start_processing(item, lease_seconds=60),
                                           S.PROCESSING, "processing_reclaimed")
        self.assert_step(item, lambda: transitions.fail_processing(item, lease_token=token, code="render_timeout"),
                         S.FAILED, "failed")
        self.assert_step(item, lambda: transitions.retry(item, actor=self.manager), S.VALIDATED, "retried")
        item, token = transitions.start_processing(item, lease_seconds=60)
        rendition = self.rendition_for(item)
        self.assert_step(item, lambda: self.complete(item, token, rendition), S.PROCESSED, "processed",
                         ("processed_at", "rendition_media_id"))
        self.assert_step(item, lambda: transitions.request_review(item), S.AWAITING_REVIEW, "review_requested")
        self.assert_step(item, lambda: transitions.reject(item, actor=self.manager, note="no"), S.REJECTED,
                         "rejected", ("reviewed_at", "terminal_at"))
        for state_maker in (self.item, self.submitted, self.validated):
            target = state_maker()
            with self.subTest(recall_from=target.state):
                self.assert_step(target, lambda: transitions.recall(target, actor=self.owner, reason="r"),
                                 S.RECALLED, "recalled", ("recalled_at", "terminal_at"))

    def test_projection_transitions_stay_closed(self):
        item = self.processed()
        for target in (S.DELIVERED, S.SUPERSEDED, S.EXPIRED):
            with self.subTest(target=target):
                with self.assertRaises((transitions.ProjectionRequired, transitions.InvalidTransition)):
                    transitions._apply(item, target, actor=self.manager)
        with self.assertRaises(transitions.ProjectionRequired):
            transitions._apply(item, S.DELIVERED, actor=self.manager)
        for source, target in ((S.PROCESSED, S.DELIVERED), (S.DELIVERED, S.SUPERSEDED),
                               (S.SUPERSEDED, S.DELIVERED), (S.DELIVERED, S.RECALLED), (S.DELIVERED, S.EXPIRED)):
            with self.subTest(grant=f"{source}->{target}"), self.assertRaises(Refused):
                with guard._authorized(guard.ItemWrite(item_id=item.pk, from_state=source, to_state=target,
                                                       values=(("state", target),))):
                    pass
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.PROCESSED)

    def test_completion_authorizes_nothing_before_provenance_is_verified(self):
        item, token = self.processing()
        rendition = self.rendition_for(item)
        opened = []
        real = guard._authorized

        def spy(record):
            opened.append(type(record).__name__)
            return real(record)
        with mock.patch.object(guard, "_authorized", spy), \
                mock.patch.object(transitions, "_verify_render",
                                  side_effect=transitions.EvidenceRefused("no", code="render_outdated")):
            with self.assertRaises(transitions.EvidenceRefused):
                self.complete(item, token, rendition)
        self.assertEqual(opened, [])
        with mock.patch.object(guard, "_authorized", spy):
            self.complete(item, token, rendition)
        self.assertEqual(opened, ["ItemWrite", "EventCreate"])


class NoGrantTestIsolation(TestCase):
    def test_no_grant_is_open_outside_the_service(self):
        self.assertIsNone(guard._grant.get())
        owner = User.objects.create_user("o")
        transitions.create_item(make_type(), actor=owner)
        self.assertIsNone(guard._grant.get())
