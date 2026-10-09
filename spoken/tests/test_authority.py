"""Adversarial ORM coverage of the spoken authority boundary.

Every ordinary Django write API -- instance save/delete, create,
get_or_create, update_or_create, bulk_create (plain and conflict/upsert),
update, bulk_update, delete, and their async forms -- is tried through the
default manager, the BASE manager and the reverse related managers. Outside
the services each one must be refused before the database changes; reads
keep working. (Raw SQL is outside the threat model.)
"""
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from spoken import guard, transitions
from spoken.configuration import ConfigurationError
from spoken.models import ItemState, SpokenContentType, SpokenItem, SpokenItemEvent

from .test_domain import make_category, make_type, media

User = get_user_model()
S = ItemState


class AuthorityFixtures(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.other = User.objects.create_user("other")
        self.ctype = make_type()
        self.item = transitions.submit(transitions.create_item(self.ctype, actor=self.owner, title="t"), media(),
                                       actor=self.owner)
        self.snapshot = self.state()

    def state(self):
        return (list(SpokenItem._base_manager.order_by("id").values()),
                list(SpokenItemEvent._base_manager.order_by("id").values()),
                list(SpokenContentType._base_manager.order_by("id").values()))

    def assert_refused(self, attempt, error=guard.SpokenIntegrityError):
        with self.assertRaises(error), transaction.atomic():
            attempt()
        self.assertEqual(self.state(), self.snapshot)


class ManagerWiringTests(AuthorityFixtures):
    def test_default_base_and_reverse_managers_are_the_guarded_querysets(self):
        from spoken.models import SpokenContentTypeQuerySet, SpokenItemEventQuerySet, SpokenItemQuerySet
        cases = (
            (SpokenContentType, SpokenContentTypeQuerySet, [make_category().spoken_content_types]),
            (SpokenItem, SpokenItemQuerySet, [self.ctype.items, self.owner.spoken_items,
                                              self.item.submission_media.spoken_submissions]),
            (SpokenItemEvent, SpokenItemEventQuerySet, [self.item.events, self.owner.spoken_item_events]),
        )
        for model, queryset, reverse in cases:
            with self.subTest(model=model.__name__):
                self.assertIsInstance(model._default_manager.all(), queryset)
                self.assertIsInstance(model._base_manager.all(), queryset)
                for manager in reverse:
                    self.assertIsInstance(manager.all(), queryset)

    def test_refusals_are_semantic_not_django_internals(self):
        """Each generic write API is refused by OUR override, before Django's
        own implementation runs -- not because, say, bulk_update() happens to
        call update() internally today."""
        from django.db.models import QuerySet
        loaded = SpokenItem.objects.get(pk=self.item.pk)
        event = SpokenItemEvent.objects.first()
        ctype = SpokenContentType.objects.get(pk=self.ctype.pk)
        cases = {
            "SpokenItem.bulk_create": ("bulk_create", lambda: SpokenItem.objects.bulk_create([SpokenItem()])),
            "SpokenItem.bulk_update user fk": ("bulk_update", lambda: SpokenItem.objects.bulk_update(
                [loaded], ["submitted_by"])),
            "SpokenItem.bulk_update state": ("bulk_update", lambda: SpokenItem.objects.bulk_update(
                [loaded], ["state"])),
            "SpokenItem.delete": ("delete", lambda: SpokenItem.objects.all().delete()),
            "SpokenItem.update": ("update", lambda: SpokenItem.objects.all().update(state=S.DELIVERED)),
            "SpokenItemEvent.bulk_create": ("bulk_create", lambda: SpokenItemEvent.objects.bulk_create([event])),
            "SpokenItemEvent.bulk_update": ("bulk_update", lambda: SpokenItemEvent.objects.bulk_update(
                [event], ["actor"])),
            "SpokenItemEvent.delete": ("delete", lambda: SpokenItemEvent.objects.all().delete()),
            "SpokenItemEvent.update": ("update", lambda: SpokenItemEvent.objects.all().update(event="x")),
            "SpokenContentType.bulk_create": ("bulk_create", lambda: SpokenContentType.objects.bulk_create([ctype])),
            "SpokenContentType.bulk_update": ("bulk_update", lambda: SpokenContentType.objects.bulk_update(
                [ctype], ["name"])),
            "SpokenContentType.update": ("update", lambda: SpokenContentType.objects.all().update(name="x")),
        }
        for label, (method, attempt) in cases.items():
            with self.subTest(api=label), mock.patch.object(QuerySet, method, autospec=True) as django_impl:
                with self.assertRaises(guard.SpokenIntegrityError):
                    attempt()
                django_impl.assert_not_called()

    def test_reads_work_everywhere(self):
        self.assertEqual(SpokenItem._base_manager.get(pk=self.item.pk).state, S.SUBMITTED)
        self.assertEqual(self.ctype.items.count(), 1)
        self.assertEqual(self.item.events.count(), 2)
        self.assertEqual(SpokenItemEvent.objects.select_related("item").first().item.pk, self.item.pk)


class SpokenItemAuthorityTests(AuthorityFixtures):
    def managers(self):
        return {"objects": SpokenItem.objects, "_default_manager": SpokenItem._default_manager,
                "_base_manager": SpokenItem._base_manager, "content_type.items": self.ctype.items,
                "user.spoken_items": self.owner.spoken_items}

    def test_no_creation_path_fabricates_an_item(self):
        forged = dict(state="not_a_state", origin="submitted")
        for name, manager in self.managers().items():
            kw = {} if name == "content_type.items" else {"content_type": self.ctype}
            attempts = {
                "create": lambda: manager.create(**kw, **forged),
                "get_or_create": lambda: manager.get_or_create(title="never", **kw, defaults=forged),
                "update_or_create": lambda: manager.update_or_create(title="never", **kw, defaults=forged),
                "bulk_create": lambda: manager.bulk_create([SpokenItem(content_type=self.ctype, **forged)]),
                "bulk_create ignore": lambda: manager.bulk_create(
                    [SpokenItem(content_type=self.ctype, **forged)], ignore_conflicts=True),
                "bulk_create upsert": lambda: manager.bulk_create(
                    [SpokenItem(pk=self.item.pk, content_type=self.ctype, **forged)], update_conflicts=True,
                    unique_fields=["id"], update_fields=["state"]),
                "abulk_create": lambda: async_to_sync(manager.abulk_create)(
                    [SpokenItem(content_type=self.ctype, **forged)]),
                "acreate": lambda: async_to_sync(manager.acreate)(**kw, **forged),
            }
            for label, attempt in attempts.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        for label, attempt in {
            "save": lambda: SpokenItem(content_type=self.ctype, **forged).save(),
            "save(force_insert)": lambda: SpokenItem(content_type=self.ctype, **forged).save(force_insert=True),
            "valid state, no service": lambda: SpokenItem(content_type=self.ctype, state=S.DRAFT).save(),
        }.items():
            with self.subTest(api=label):
                self.assert_refused(attempt)

    def test_a_pretend_existing_instance_cannot_insert(self):
        ghost = SpokenItem(content_type=self.ctype, state=S.PROCESSED)
        ghost._state.adding = False                                     # UPDATE finds nothing, then INSERT
        self.assert_refused(ghost.save)

    def test_no_mutation_path_changes_lifecycle(self):
        loaded = SpokenItem.objects.get(pk=self.item.pk)
        for name, manager in self.managers().items():
            attempts = {
                "update state": lambda: manager.all().update(state=S.DELIVERED),
                "update invalid state": lambda: manager.all().update(state="not_a_state"),
                "update reassign submitter": lambda: manager.all().update(submitted_by=self.other),
                "update media": lambda: manager.all().update(submission_media=None),
                "update_or_create": lambda: manager.update_or_create(pk=self.item.pk,
                                                                     defaults={"state": S.PROCESSED}),
                "bulk_update state": lambda: manager.bulk_update([loaded], ["state"]),
                "bulk_update user fk": lambda: manager.bulk_update([loaded], ["submitted_by"]),
                "aupdate": lambda: async_to_sync(manager.all().aupdate)(state=S.DELIVERED),
            }
            for label, attempt in attempts.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        loaded.state = "not_a_state"
        self.assert_refused(loaded.save)
        self.assert_refused(lambda: self.other.spoken_items.add(  # reverse add() reassigns the FK
            SpokenItem.objects.get(pk=self.item.pk)))
        other_ctype = make_type(code="other-type")
        self.snapshot = self.state()
        self.assert_refused(lambda: other_ctype.items.add(SpokenItem.objects.get(pk=self.item.pk)))

    def test_no_deletion_path_removes_an_item(self):
        for name, manager in self.managers().items():
            for label, attempt in {"all().delete": lambda: manager.all().delete(),
                                   "filter().delete": lambda: manager.filter(pk=self.item.pk).delete(),
                                   "adelete": lambda: async_to_sync(manager.all().adelete)()}.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        self.assert_refused(SpokenItem.objects.get(pk=self.item.pk).delete)
        self.assertFalse(hasattr(SpokenItem.objects, "delete"))         # queryset-only, as in Django

    def test_an_eventless_item_cannot_be_deleted_either(self):
        # Codex's reproduction: an item with no events (so no PROTECT row
        # stands in the way) must still be undeletable through the ORM.
        with guard.transition_scope():
            bare = SpokenItem.objects.create(content_type=self.ctype, state=S.DRAFT)
        self.snapshot = self.state()
        for name, manager in self.managers().items():
            if name == "user.spoken_items":
                continue                                                # bare has no submitter
            with self.subTest(manager=name):
                self.assert_refused(lambda: manager.filter(pk=bare.pk).delete())
        self.assert_refused(bare.delete)

    def test_inside_the_scope_the_state_must_still_be_an_item_state(self):
        with guard.transition_scope():
            self.assert_refused(lambda: SpokenItem.objects.create(content_type=self.ctype, state="not_a_state"))
            self.assert_refused(lambda: SpokenItem.objects.filter(pk=self.item.pk).update(state="bogus"))
            self.assert_refused(lambda: SpokenItem.objects.bulk_create([SpokenItem(content_type=self.ctype)]))
            self.assert_refused(lambda: SpokenItem.objects.filter(pk=self.item.pk).delete())

    def test_valid_transitions_still_happen_through_the_service_only(self):
        item = transitions.record_validation(self.item)
        self.assertEqual(item.state, S.VALIDATED)
        self.assertEqual(list(item.events.values_list("event", flat=True)), ["created", "submitted", "validated"])

    def test_editorial_text_and_django_set_null_remain_free(self):
        self.ctype.items.update(title="retitled")
        SpokenItem.objects.bulk_update([SpokenItem(pk=self.item.pk, title="bulk", notes="n")], ["title", "notes"])
        self.owner.spoken_items.update(submitted_by=None)               # the SET_NULL shape, nothing more
        row = SpokenItem.objects.get(pk=self.item.pk)
        self.assertEqual((row.title, row.notes, row.submitted_by_id, row.submitted_by_username, row.state),
                         ("bulk", "n", None, "owner", S.SUBMITTED))


class SpokenItemEventAuthorityTests(AuthorityFixtures):
    def managers(self):
        return {"objects": SpokenItemEvent.objects, "_default_manager": SpokenItemEvent._default_manager,
                "_base_manager": SpokenItemEvent._base_manager, "item.events": self.item.events,
                "user.spoken_item_events": self.owner.spoken_item_events}

    def test_no_creation_path_fabricates_history(self):
        forged = dict(event="forged", from_state="draft", to_state="delivered", actor_kind="system")
        for name, manager in self.managers().items():
            kw = {} if name == "item.events" else {"item": self.item}
            attempts = {
                "create": lambda: manager.create(**kw, **forged),
                "get_or_create": lambda: manager.get_or_create(**kw, event="forged", defaults=forged),
                "update_or_create": lambda: manager.update_or_create(**kw, event="forged", defaults=forged),
                "bulk_create": lambda: manager.bulk_create([SpokenItemEvent(item=self.item, **forged)]),
                "bulk_create upsert": lambda: manager.bulk_create(
                    [SpokenItemEvent(item=self.item, **forged)], update_conflicts=True, unique_fields=["id"],
                    update_fields=["event"]),
                "abulk_create": lambda: async_to_sync(manager.abulk_create)(
                    [SpokenItemEvent(item=self.item, **forged)]),
            }
            for label, attempt in attempts.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        self.assert_refused(lambda: SpokenItemEvent(item=self.item, **forged).save())

    def test_history_fields_never_change_and_set_null_stays_narrow(self):
        event = SpokenItemEvent.objects.filter(item=self.item).first()
        other_item = transitions.create_item(self.ctype, actor=self.other)
        self.snapshot = self.state()
        changes = {"event": "x", "item": other_item, "from_state": "x", "to_state": "delivered", "media": None,
                   "asset": None, "edition": None, "detail": {"forged": True}, "created_at": timezone.now(),
                   "actor_kind": "system", "actor_username": "someone", "actor": self.other}
        for name, manager in self.managers().items():
            for column, value in changes.items():
                with self.subTest(manager=name, column=column):
                    self.assert_refused(lambda: manager.all().update(**{column: value}))
                with self.subTest(manager=name, column=f"actor=None + {column}"):
                    if column != "actor":
                        self.assert_refused(lambda: manager.all().update(actor=None, **{column: value}))
            with self.subTest(manager=name, api="bulk_update"):
                self.assert_refused(lambda: manager.bulk_update([event], ["actor"]))
        event.event = "rewritten"
        self.assert_refused(event.save)
        self.assert_refused(lambda: SpokenItemEvent.objects.update_or_create(pk=event.pk,
                                                                            defaults={"detail": {"x": 1}}))

    def test_no_deletion_path_removes_history(self):
        for name, manager in self.managers().items():
            for label, attempt in {"all().delete": lambda: manager.all().delete(),
                                   "adelete": lambda: async_to_sync(manager.all().adelete)()}.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        self.assert_refused(SpokenItemEvent.objects.first().delete)
        with guard.transition_scope():                                  # append-only even for the service
            self.assert_refused(lambda: SpokenItemEvent.objects.all().delete())
            self.assert_refused(lambda: SpokenItemEvent.objects.bulk_create([]))

    def test_user_deletion_still_nulls_the_actor_and_nothing_else(self):
        before = list(SpokenItemEvent.objects.filter(item=self.item).values(
            "id", "event", "from_state", "to_state", "media", "detail", "created_at", "actor_username"))
        self.owner.delete()
        after = list(SpokenItemEvent.objects.filter(item=self.item).values(
            "id", "event", "from_state", "to_state", "media", "detail", "created_at", "actor_username"))
        self.assertEqual(before, after)
        self.assertFalse(SpokenItemEvent.objects.filter(item=self.item, actor__isnull=False).exists())


class ContentTypeWriteAuthorityTests(AuthorityFixtures):
    def bad(self, **overrides):
        values = dict(code="bad-type", name="Bad", delivery_mode="retain_only", recipe_key="no_such_recipe")
        values.update(overrides)
        return SpokenContentType(**values)

    def test_bulk_and_queryset_writes_cannot_skip_validation(self):
        category = make_category()
        loaded = SpokenContentType.objects.get(pk=self.ctype.pk)
        loaded.recipe_key = "no_such_recipe"
        managers = {"objects": SpokenContentType.objects, "_base_manager": SpokenContentType._base_manager,
                    "category.spoken_content_types": category.spoken_content_types}
        for name, manager in managers.items():
            attempts = {
                "bulk_create": lambda: manager.bulk_create([self.bad()]),
                "bulk_create upsert": lambda: manager.bulk_create(
                    [self.bad(code=self.ctype.code)], update_conflicts=True, unique_fields=["code"],
                    update_fields=["recipe_key"]),
                "bulk_update": lambda: manager.bulk_update([loaded], ["recipe_key"]),
                "update recipe": lambda: manager.all().update(recipe_key="no_such_recipe"),
                "update code": lambda: manager.all().update(code="renamed"),
                "update params": lambda: manager.all().update(recipe_params={"bogus": 1}),
                "update enabled": lambda: manager.all().update(enabled=True, target_category=None),
                "aupdate": lambda: async_to_sync(manager.all().aupdate)(max_bytes=0),
            }
            for label, attempt in attempts.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        self.assert_refused(lambda: category.spoken_content_types.clear())

    def test_upsert_helpers_funnel_through_the_validated_locked_save(self):
        unused = SpokenContentType.objects.create(code="unused", name="U", delivery_mode="retain_only",
                                                  recipe_key="lcl_psa")
        self.snapshot = self.state()
        configuration_errors = {
            "unknown recipe": lambda: SpokenContentType.objects.get_or_create(
                code="new-type", defaults=dict(name="N", delivery_mode="retain_only", recipe_key="nope")),
            "parameter schema": lambda: SpokenContentType.objects.update_or_create(
                pk=self.ctype.pk, defaults={"recipe_params": {"bogus": 1}}),
            "media limits": lambda: SpokenContentType.objects.update_or_create(
                pk=self.ctype.pk, defaults={"max_bytes": 10 ** 15}),
            "code shape": lambda: SpokenContentType.objects.update_or_create(
                pk=unused.pk, defaults={"code": "Bad Code!"}),
            "projection category": lambda: SpokenContentType.objects.update_or_create(
                pk=self.ctype.pk, defaults={"target_category": None}),
            "reverse create": lambda: make_category().spoken_content_types.create(
                code="rev", name="R", delivery_mode="retain_only", recipe_key="nope"),
        }
        for label, attempt in configuration_errors.items():
            with self.subTest(path=label):
                self.assert_refused(attempt, ConfigurationError)
        with self.subTest(path="code permanence"):
            self.assert_refused(lambda: SpokenContentType.objects.update_or_create(
                pk=self.ctype.pk, defaults={"code": "renamed"}))

    def test_individual_saves_and_admin_style_edits_still_work(self):
        fresh = SpokenContentType.objects.create(code="fresh", name="Fresh", delivery_mode="retain_only",
                                                 recipe_key="lcl_psa")
        fresh.code, fresh.name = "fresh-renamed", "Renamed while unused"
        fresh.save()
        SpokenContentType.objects.update_or_create(pk=self.ctype.pk, defaults={"name": "Renamed display name"})
        self.assertEqual(SpokenContentType.objects.get(pk=fresh.pk).code, "fresh-renamed")
        self.assertEqual(SpokenContentType.objects.get(pk=self.ctype.pk).name, "Renamed display name")
