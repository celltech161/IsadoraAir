"""Operation-specific write authority, sealed through the Django write boundary.

A protected SpokenItem / SpokenItemEvent write happens only as the exact
execution (``guard._execute``) of an open, single-use grant derived from a
deeply immutable TransitionPlan: no model instance is handed to application
code, no model signal fires, and the written row is read back and compared
with the grant before the transaction can commit. Every test uses valid
ItemState values, so the state-enum guard is never what refuses.
"""
import asyncio
import contextvars
import dataclasses
import json
import threading
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.db.models import QuerySet
from django.db.models.signals import post_save, pre_save
from django.test import TestCase
from django.utils import timezone

from spoken import guard, transitions
from spoken.models import ItemState, SpokenItem, SpokenItemEvent

from .support import failing_event_write
from .test_domain import Fixtures, make_type, media

User = get_user_model()
S = ItemState
Refused = guard.SpokenIntegrityError


def item_snapshot(item):
    return (SpokenItem._base_manager.filter(pk=item.pk).values().get(),
            list(SpokenItemEvent._base_manager.filter(item_id=item.pk).values_list("id", flat=True)))


class connected:
    """Connect a signal receiver for the duration of a with-block."""

    def __init__(self, signal, receiver):
        self.signal, self.receiver = signal, receiver

    def __enter__(self):
        self.signal.connect(self.receiver, weak=False)

    def __exit__(self, *exc):
        self.signal.disconnect(self.receiver)


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
                  "detail": transitions._bounded({"duration_seconds": "2.000000"})}
        values.update(changes)
        return values


class SignalBoundaryTests(GrantFixtures):
    """Codex blocker 1: a pre_save receiver mutated an already-authorized
    instance before SQL. Lifecycle writes now expose no instance and emit no
    model signal; anything that alters the payload on its way to SQL is caught
    by the read-back and the whole transition rolls back."""

    def test_pre_save_cannot_tamper_with_an_authorized_item_or_event_write(self):
        seen = []

        def receiver(sender, instance, **kwargs):
            if sender in (SpokenItem, SpokenItemEvent):
                seen.append(sender.__name__)
                if sender is SpokenItem:
                    instance.state, instance.failure_code = S.DELIVERED, "forged"
                else:
                    instance.event, instance.from_state, instance.to_state = "delivered", "", S.DRAFT
                    instance.detail = {"forged": True}
        with connected(pre_save, receiver), connected(post_save, receiver):
            transitions.record_validation(self.a)
        self.assertEqual(seen, [])                                 # no model signal on a lifecycle write
        row = SpokenItem.objects.get(pk=self.a.pk)
        self.assertEqual((row.state, row.failure_code), (S.VALIDATED, ""))
        event = SpokenItemEvent.objects.filter(item=row).latest("id")
        self.assertEqual((event.event, event.from_state, event.to_state, event.detail),
                         ("validated", S.SUBMITTED, S.VALIDATED, {"duration_seconds": "2.000000"}))
        self.assertFalse(SpokenItemEvent.objects.filter(event="delivered").exists())

    def tamper(self, model, method, mutate):
        """Alter the payload inside Django's own write call, i.e. between the
        grant and the SQL -- the last point anything could still interfere."""
        real = getattr(QuerySet, method)

        def wrapped(queryset, *args, **kwargs):
            if queryset.model is model:
                args, kwargs = mutate(args, kwargs)
            return real(queryset, *args, **kwargs)
        return mock.patch.object(QuerySet, method, wrapped)

    def test_an_item_payload_altered_before_sql_is_detected_and_rolled_back(self):
        def forge(args, kwargs):
            return args, {**kwargs, "state": S.DELIVERED, "failure_code": "forged"}
        before = item_snapshot(self.a)
        with self.tamper(SpokenItem, "update", forge), self.assertRaises(Refused):
            transitions.record_validation(self.a)
        self.assertEqual(item_snapshot(self.a), before)             # no state, no event
        self.assertIsNone(guard._grant.get())
        self.assertEqual(transitions.record_validation(self.a).state, S.VALIDATED)    # a normal retry works

    def test_an_event_payload_altered_before_sql_is_detected_and_rolled_back(self):
        def forge(args, kwargs):
            (event,) = args[0]
            event.event, event.from_state, event.to_state, event.detail = "delivered", "", S.DRAFT, {"forged": True}
            return args, kwargs
        before = item_snapshot(self.a)
        with self.tamper(SpokenItemEvent, "bulk_create", forge), self.assertRaises(Refused):
            transitions.record_validation(self.a)
        self.assertEqual(item_snapshot(self.a), before)             # the item write rolled back too
        self.assertFalse(SpokenItemEvent.objects.filter(event="delivered").exists())
        self.assertIsNone(guard._grant.get())
        self.assertEqual(transitions.record_validation(self.a).state, S.VALIDATED)

    def test_post_save_receivers_cannot_issue_lifecycle_writes(self):
        outcomes, fired = {}, []

        def receiver(sender, instance, **kwargs):
            if sender is not SpokenItem or fired:
                return
            fired.append(True)
            for label, attempt in {
                "save submitted -> delivered": lambda: self._forge_save(self.b, state=S.DELIVERED),
                "update -> delivered": lambda: SpokenItem.objects.filter(pk=self.b.pk).update(state=S.DELIVERED),
                "forged event": lambda: SpokenItemEvent.objects.create(**{
                    **self.event_values(self.b), "event": "delivered", "detail": {}}),
                "execute without a grant": guard._execute,
            }.items():
                try:
                    with transaction.atomic():
                        attempt()
                    outcomes[label] = "succeeded"
                except Refused:
                    outcomes[label] = "refused"
        row = SpokenItem.objects.get(pk=self.a.pk)
        row.title = "retitled"
        with connected(post_save, receiver):
            row.save()                                             # an ordinary save does emit post_save
        self.assertEqual(len(outcomes), 4)
        self.assertEqual(outcomes, dict.fromkeys(outcomes, "refused"))
        self.assertEqual(SpokenItem.objects.get(pk=self.b.pk).state, S.SUBMITTED)

    def test_pre_save_cannot_smuggle_protected_values_into_an_ordinary_save(self):
        def receiver(sender, instance, **kwargs):
            if sender is SpokenItem:
                instance.state, instance.failure_code = S.DELIVERED, "forged"
        row = SpokenItem.objects.get(pk=self.a.pk)
        row.title = "retitled"
        with connected(pre_save, receiver):
            row.save()
        stored = SpokenItem.objects.get(pk=self.a.pk)
        self.assertEqual((stored.title, stored.state, stored.failure_code), ("retitled", S.SUBMITTED, ""))

    def _forge_save(self, item, **changes):
        row = SpokenItem.objects.get(pk=item.pk)
        for name, value in changes.items():
            setattr(row, name, value)
        row.save()


class PlanImmutabilityTests(GrantFixtures):
    """Codex blocker 2: a genuine plan's nested detail was mutable after
    validation. Plans now hold only immutable pairs; detail is canonical JSON."""

    nested = {"validation": {"checks": ["a", "b"], "score": 1}}

    def test_a_genuine_plan_cannot_be_altered_and_commits_its_validated_detail(self):
        caller_detail = json.loads(json.dumps(self.nested))
        attempts = {}
        real_commit = transitions._commit

        def tampering_commit(row, plan):
            detail = dict(plan.event_values)["detail"]
            for label, attempt in {
                "replace field": lambda: setattr(plan, "event_values", ()),
                "assign into payload": lambda: plan.event_values.__setitem__(0, ("event", "delivered")),
                "mutate detail": lambda: detail.__setitem__("forged_after_validation", True),
                "mutate nested dict": lambda: detail["validation"].update(x=1),
                "mutate nested list": lambda: detail["validation"]["checks"].append("z"),
            }.items():
                try:
                    attempt()
                    attempts[label] = "mutated"
                except (TypeError, AttributeError, dataclasses.FrozenInstanceError):
                    attempts[label] = "impossible"
            caller_detail["validation"]["checks"].append("added after planning")      # the caller's input
            caller_detail["validation"]["score"] = 99
            caller_detail["forged_after_validation"] = True
            return real_commit(row, plan)
        with mock.patch.object(transitions, "_commit", tampering_commit):
            item = transitions._apply(self.a, S.RECALLED, actor=self.manager, detail=caller_detail)
        self.assertEqual(attempts, dict.fromkeys(attempts, "impossible"))
        self.assertEqual(len(attempts), 5)
        event = SpokenItemEvent.objects.filter(item=item).latest("id")
        self.assertEqual(event.detail, self.nested)                      # exactly the validated detail

    def test_plans_refuse_mutable_values(self):
        for label, values in {"dict": (("detail", {"a": 1}),), "list": (("detail", ["a"]),),
                              "not pairs": ({"state": "validated"},), "list payload": [("state", "x")]}.items():
            with self.subTest(case=label), self.assertRaises(TypeError):
                transitions.TransitionPlan(item_id=self.a.pk, from_state=S.SUBMITTED, to_state=S.VALIDATED,
                                           item_values=self.plan_values, event_values=values)

    def test_the_canonical_detail_is_stable(self):
        self.assertEqual(transitions._bounded({"b": 1, "a": {"y": [2, 1], "x": None}}),
                         '{"a":{"x":null,"y":[2,1]},"b":1}')


class CodexBypassRegressionTests(GrantFixtures):
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
        opened = []
        real = guard._authorized

        def spy(record):
            opened.append(type(record).__name__)
            return real(record)
        for label, attempt in {"_commit": lambda: transitions._commit(row, forged),
                               "_write_event": lambda: transitions._write_event(forged)}.items():
            with self.subTest(helper=label), mock.patch.object(guard, "_authorized", spy):
                with self.assertRaises(Refused), transaction.atomic():
                    attempt()
        self.assertEqual(opened, [])                    # refused before any grant is opened
        self.assertEqual(item_snapshot(self.a), before)


class ExactItemWriteTests(GrantFixtures):
    def test_the_grant_executes_exactly_once(self):
        with guard._authorized(self.grant_for(self.a)):
            guard._execute()
            with self.assertRaises(Refused):                    # consumed
                guard._execute()
        row = SpokenItem.objects.get(pk=self.a.pk)
        self.assertEqual((row.state, row.validated_at, row.state_changed_at), (S.VALIDATED, self.now, self.now))
        self.assertEqual(SpokenItem.objects.get(pk=self.b.pk).state, S.SUBMITTED)    # only its own row

    def test_save_never_honors_a_grant(self):
        """Even under an open grant for this very row, save() writes nothing protected."""
        other_media = media()
        changes = {
            "exact plan": {}, "state": {"state": S.DELIVERED}, "failure_code": {"failure_code": "forged"},
            "failure_detail": {"failure_detail": "forged"}, "reviewed_by_id": {"reviewed_by_id": self.manager.pk},
            "reviewed_at": {"reviewed_at": self.now}, "recalled_by_id": {"recalled_by_id": self.manager.pk},
            "recalled_at": {"recalled_at": self.now},
            "lease": {"lease_token": uuid.uuid4(), "lease_expires_at": self.now},
            "rendition_media_id": {"rendition_media_id": other_media.pk},
            "state_changed_at": {"state_changed_at": self.now + timedelta(seconds=1)},
            "terminal_at": {"terminal_at": self.now}, "processing_attempts": {"processing_attempts": 7},
        }
        for label, extra in changes.items():
            with self.subTest(change=label), guard._authorized(self.grant_for(self.a)):
                self.assert_refused(self.loaded(self.a, **extra).save)

    def test_the_grant_needs_the_authorized_source_state(self):
        validated = transitions.record_validation(self.b)          # b is no longer submitted
        with guard._authorized(self.grant_for(validated)):
            self.assert_refused(guard._execute, item=validated)

    def test_another_row_or_a_broader_write_is_refused(self):
        with guard._authorized(self.grant_for(self.a)):
            for target in (self.a, self.b):
                with self.subTest(target=str(target.pk)):
                    self.assert_refused(lambda: SpokenItem.objects.filter(pk=target.pk).update(
                        state=S.VALIDATED, state_changed_at=self.now, validated_at=self.now), item=target)
            self.assert_refused(lambda: SpokenItem.objects.all().update(state=S.VALIDATED))
            self.assert_refused(self.loaded(self.b).save, item=self.b)

    def test_an_item_grant_does_not_create_items_or_events(self):
        with guard._authorized(self.grant_for(self.a)):
            self.assert_refused(lambda: SpokenItem.objects.create(content_type=self.ctype, state=S.DRAFT))
            self.assert_refused(lambda: SpokenItemEvent.objects.create(**{**self.event_values(), "detail": {}}))


class ExactCreationTests(GrantFixtures):
    def creation_grant(self, item_id):
        return guard.ItemCreate(item_id=item_id, values=(
            ("content_type_id", self.ctype.pk), ("origin", "submitted"), ("submitted_by_id", self.owner.pk),
            ("submitted_by_username", "owner"), ("state", S.DRAFT), ("state_changed_at", self.now)))

    def test_a_creation_grant_creates_exactly_one_described_row(self):
        item_id = uuid.uuid4()
        before = SpokenItem.objects.count()
        with guard._authorized(self.creation_grant(item_id)):
            with self.assertRaises(Refused), transaction.atomic():          # save() never creates
                SpokenItem(id=item_id, content_type=self.ctype, state=S.DRAFT).save(force_insert=True)
            guard._execute()
            with self.assertRaises(Refused), transaction.atomic():          # single use
                guard._execute()
        row = SpokenItem.objects.get(pk=item_id)
        self.assertEqual((row.state, row.origin, row.submitted_by_id, row.failure_code, row.validated_at,
                          row.lease_token, row.state_changed_at),
                         (S.DRAFT, "submitted", self.owner.pk, "", None, None, self.now))
        self.assertIsNotNone(row.created_at)                                 # auto_now_add still applied
        self.assertEqual(SpokenItem.objects.count(), before + 1)

    def test_creation_and_its_event_are_atomic(self):
        before = (SpokenItem.objects.count(), SpokenItemEvent.objects.count())
        with failing_event_write(), self.assertRaises(RuntimeError):
            transitions.create_item(self.ctype, actor=self.owner)
        self.assertEqual((SpokenItem.objects.count(), SpokenItemEvent.objects.count()), before)
        self.assertIsNone(guard._grant.get())


class ExactEventTests(GrantFixtures):
    def test_the_event_grant_inserts_exactly_its_payload_once(self):
        legit = self.event_values()
        with guard._authorized(guard.EventCreate(values=tuple(legit.items()))):
            for attname, value in {"item_id": self.b.pk, "event": "invalid", "from_state": S.DRAFT,
                                   "to_state": S.INVALID, "actor_kind": "worker", "actor_id": self.manager.pk,
                                   "actor_username": "someone", "media_id": media().pk, "asset_id": uuid.uuid4(),
                                   "edition_id": uuid.uuid4(), "detail": {"forged": True}}.items():
                with self.subTest(column=attname):                          # ordinary inserts never pass
                    self.assert_refused(lambda: SpokenItemEvent.objects.create(**{
                        **legit, "detail": json.loads(legit["detail"]), attname: value}))
            event_id = guard._execute()
            self.assert_refused(guard._execute)                              # consumed
        event = SpokenItemEvent.objects.get(pk=event_id)
        self.assertEqual((event.item_id, event.event, event.from_state, event.to_state, event.media_id,
                          event.detail), (self.a.pk, "validated", S.SUBMITTED, S.VALIDATED,
                                          self.a.submission_media_id, {"duration_seconds": "2.000000"}))
        self.assertIsNotNone(event.created_at)
        self.assert_refused(guard._execute)                                  # and closed

    def test_an_event_grant_does_not_authorize_item_writes(self):
        with guard._authorized(guard.EventCreate(values=tuple(self.event_values().items()))):
            self.assert_refused(self.loaded(self.a).save)


class LeakageTests(GrantFixtures):
    def test_grants_close_on_exit_and_on_exceptions(self):
        with guard._authorized(self.grant_for(self.a)):
            pass
        self.assertIsNone(guard._grant.get())
        self.assert_refused(guard._execute)
        with self.assertRaises(ZeroDivisionError):
            with guard._authorized(self.grant_for(self.a)):
                raise ZeroDivisionError
        self.assertIsNone(guard._grant.get())
        self.assert_refused(guard._execute)

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

        def attempt(label):
            try:
                guard._take(guard.ItemWrite)        # the binding itself, independent of the transaction check
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
        self.assertEqual(SpokenItem.objects.get(pk=self.a.pk).state, S.SUBMITTED)

    def test_grants_do_not_reach_other_async_tasks(self):
        async def in_task():
            try:
                guard._take(guard.ItemWrite)        # the binding itself, independent of the transaction check
                return "succeeded"
            except Refused:
                return "refused"

        async def spawns_a_task():
            return await asyncio.get_running_loop().create_task(in_task())
        with guard._authorized(self.grant_for(self.a)):
            self.assertEqual(asyncio.run(in_task()), "refused")             # a task copies the context
            self.assertEqual(asyncio.run(spawns_a_task()), "refused")
            guard._execute()                                                # still usable where it was opened
        self.assertEqual(SpokenItem.objects.get(pk=self.a.pk).state, S.VALIDATED)


class LegitimateLifecycleTests(GrantFixtures):
    """Every C1 transition still works, records exactly its event, and rolls
    back with its event when the event write fails."""

    def assert_step(self, item, call, state, event, stamps=()):
        before = item_snapshot(item)
        with failing_event_write(), self.assertRaises(RuntimeError):
            call()
        self.assertEqual(item_snapshot(item), before)                          # atomic
        self.assertIsNone(guard._grant.get())
        result = call()
        row = result[0] if isinstance(result, tuple) else result
        row = SpokenItem.objects.get(pk=row.pk)
        events = list(SpokenItemEvent.objects.filter(item=row).order_by("id"))
        self.assertEqual(len(events), len(before[1]) + 1)                      # exactly one event
        self.assertEqual((row.state, events[-1].event, events[-1].to_state), (state, event, state))
        self.assertNotEqual(row.state_changed_at, before[0]["state_changed_at"])   # restamped
        self.assertIsNotNone(events[-1].created_at)
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
