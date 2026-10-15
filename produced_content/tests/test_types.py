"""Produced-content types and assignments (iPortal D3A): the four delivery
modes, incompatible field combinations refused by clean() AND the database,
the durable key, assignments as exact scope, and the admin."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import Category, CategoryKind
from produced_content import authority
from produced_content.models import ProducedContentType, ProducedContentTypeAssignment

User = get_user_model()


def category(code="AMNEWS"):
    kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
    return Category.objects.get_or_create(code=code, defaults={"name": code, "kind": kind})[0]


def build(key, mode, **values):
    return ProducedContentType(key=key, name=key, delivery_mode=mode, **values)


class DeliveryModeTests(TestCase):
    def test_all_four_delivery_modes(self):
        rows = {
            "usd239min": build("usd239min", "regular", target_category=category(), output_mode="single",
                               recipe_key="spoken_short", artist_tag="USD 239"),
            "usd239sc": build("usd239sc", "public_address"),
            "urgent_custom": build("urgent_custom", "urgent", recallable=True, producer_self_publish=False,
                                   replay_interval_seconds=900),
            "test_audio": build("test_audio", "test"),
        }
        for key, row in rows.items():
            with self.subTest(key):
                row.full_clean()
                row.save()
        stored = {row.key: row for row in ProducedContentType.objects.filter(key__in=rows)}
        self.assertEqual((stored["usd239min"].output_mode, stored["usd239min"].recallable), ("single", None))
        # Public-address/urgent defaults: self-publish on, producer recall on, replay = the policy default.
        self.assertEqual((stored["usd239sc"].producer_self_publish, stored["usd239sc"].recallable,
                          stored["usd239sc"].replay_interval_seconds), (True, True, None))
        self.assertEqual((stored["urgent_custom"].producer_self_publish, stored["urgent_custom"].recallable,
                          stored["urgent_custom"].replay_interval_seconds), (False, True, 900))
        self.assertEqual((stored["test_audio"].producer_self_publish, stored["test_audio"].recallable,
                          stored["test_audio"].target_category_id), (None, None, None))
        self.assertTrue(stored["usd239sc"].is_active_message_type)
        self.assertFalse(stored["usd239min"].is_active_message_type or stored["test_audio"].is_active_message_type)

    def test_new_pa_and_urgent_types_default_to_self_publish_and_recall(self):
        for mode in ("public_address", "urgent"):
            with self.subTest(mode=mode):
                created = ProducedContentType.objects.create(key=f"new_{mode}", name=mode, delivery_mode=mode)
                stored = ProducedContentType.objects.get(pk=created.pk)
                self.assertEqual((stored.producer_self_publish, stored.recallable), (True, True))
                cleaned = build(f"form_{mode}", mode)
                cleaned.full_clean()                                             # the admin/form path
                self.assertEqual((cleaned.producer_self_publish, cleaned.recallable), (True, True))

    def test_an_explicit_no_is_kept(self):
        for mode in ("public_address", "urgent"):
            with self.subTest(mode=mode):
                row = ProducedContentType.objects.create(key=f"closed_{mode}", name=mode, delivery_mode=mode,
                                                         recallable=False, producer_self_publish=False)
                stored = ProducedContentType.objects.get(pk=row.pk)
                self.assertEqual((stored.producer_self_publish, stored.recallable), (False, False))
        self.assertFalse(ProducedContentType.objects.create(
            key="no_recall", name="x", delivery_mode="public_address", recallable=False).recallable)

    def test_the_migration_compatibility_types_keep_d1_authority(self):
        """Created by active_messages.0003 with explicit values: the model
        defaults never give them producer authority."""
        rows = {row.key: (row.producer_self_publish, row.recallable)
                for row in ProducedContentType.objects.filter(key__in=("urgent_pa", "public_address"))}
        self.assertEqual(rows, {"urgent_pa": (False, False), "public_address": (False, False)})

    def test_incompatible_combinations_are_refused_by_clean(self):
        cat = category()
        cases = {
            "regular without a category": (build("r1", "regular", output_mode="single"), "target_category"),
            "regular without an output mode": (build("r2", "regular", target_category=cat), "output_mode"),
            "regular with recall": (build("r3", "regular", target_category=cat, output_mode="single",
                                          recallable=True), "recallable"),
            "regular with a replay override": (build("r4", "regular", target_category=cat, output_mode="single",
                                                     replay_interval_seconds=600), "replay_interval_seconds"),
            "PA with a category": (build("p1", "public_address", target_category=cat), "target_category"),
            "PA with a recipe": (build("p2", "public_address", recipe_key="news"), "recipe_key"),
            "urgent with an output mode": (build("u1", "urgent", output_mode="single"), "output_mode"),
            "urgent with an artist tag": (build("u2", "urgent", artist_tag="x"), "artist_tag"),
            "test with self-publish": (build("t1", "test", producer_self_publish=True), "producer_self_publish"),
            "test with recall": (build("t2", "test", recallable=False), "recallable"),
            "test with a category": (build("t3", "test", target_category=cat), "target_category"),
            "test with a replay override": (build("t4", "test", replay_interval_seconds=600),
                                            "replay_interval_seconds"),
            "replay below its bound": (build("b1", "urgent", replay_interval_seconds=59), "replay_interval_seconds"),
            "replay above its bound": (build("b2", "urgent", replay_interval_seconds=86401),
                                       "replay_interval_seconds"),
            "an executable recipe": (build("x1", "regular", target_category=cat, output_mode="single",
                                           recipe_key="os.system"), "recipe_key"),
            "a path recipe": (build("x2", "regular", target_category=cat, output_mode="single",
                                    recipe_key="/usr/bin/sox"), "recipe_key"),
            "a key with spaces": (build("bad key", "test"), "key"),
            "an unknown mode": (build("m1", "emergency"), "delivery_mode"),
        }
        for label, (row, field) in cases.items():
            with self.subTest(label), self.assertRaises(ValidationError) as caught:
                row.full_clean()
            self.assertIn(field, caught.exception.message_dict, label)

    def test_the_database_refuses_the_same_combinations(self):
        cat = category()
        cases = {
            "regular without a category": dict(delivery_mode="regular", output_mode="single"),
            "regular with self-publish": dict(delivery_mode="regular", target_category=cat, output_mode="single",
                                              producer_self_publish=True),
            "PA without its settings": dict(delivery_mode="public_address"),
            "PA with a category": dict(delivery_mode="public_address", target_category=cat, recallable=False,
                                       producer_self_publish=True),
            "test that could publish": dict(delivery_mode="test", producer_self_publish=True),
            "test with an output": dict(delivery_mode="test", output_mode="single"),
            "unknown mode": dict(delivery_mode="emergency"),
            "replay out of bounds": dict(delivery_mode="urgent", recallable=False, producer_self_publish=True,
                                         replay_interval_seconds=10),
            "recipe with a dot": dict(delivery_mode="regular", target_category=cat, output_mode="single",
                                      recipe_key="a.b"),
            "key shape": dict(key="Bad Key", delivery_mode="test"),
        }
        for index, (label, values) in enumerate(cases.items()):
            values = {"key": f"db{index}", "name": label, **values}
            with self.subTest(label), self.assertRaises(IntegrityError), transaction.atomic():
                # bulk_create: no save() defaults, no clean() -- only the database decides.
                ProducedContentType.objects.bulk_create([ProducedContentType(**values)])

    def test_the_key_is_durable(self):
        row = build("news_item", "regular", target_category=category(), output_mode="accumulating")
        row.save()
        row.key = "renamed"
        with self.assertRaises(ValidationError):
            row.full_clean()
        with self.assertRaises(ValidationError):
            row.save()
        self.assertTrue(ProducedContentType.objects.filter(key="news_item").exists())
        with self.assertRaises(IntegrityError), transaction.atomic():
            build("news_item", "test").save()

    def test_an_unreferenced_type_may_change_mode_with_consistent_settings(self):
        row = build("cal_events", "test")
        row.save()
        row.delivery_mode, row.target_category, row.output_mode = "regular", category(), "single"
        row.full_clean()
        row.save()
        self.assertEqual(ProducedContentType.objects.get(pk=row.pk).delivery_mode, "regular")


class AssignmentTests(TestCase):
    def setUp(self):
        self.alice, self.bob = User.objects.create_user("alice"), User.objects.create_user("bob")
        self.news = build("news_item", "regular", target_category=category(), output_mode="accumulating")
        self.psa = build("town_notice", "public_address")
        self.urgent = build("urgent_custom", "urgent")
        for row in (self.news, self.psa, self.urgent):
            row.save()

    def test_many_types_per_user_and_many_users_per_type(self):
        for row in (self.news, self.psa, self.urgent):
            ProducedContentTypeAssignment.objects.create(user=self.alice, content_type=row)
        ProducedContentTypeAssignment.objects.create(user=self.bob, content_type=self.psa)
        self.assertEqual(set(authority.assigned_types(self.alice).values_list("key", flat=True)),
                         {"news_item", "town_notice", "urgent_custom"})
        self.assertEqual(set(self.psa.assignments.values_list("user__username", flat=True)), {"alice", "bob"})
        self.assertTrue(authority.is_assigned(self.bob, self.psa))
        self.assertFalse(authority.is_assigned(self.bob, self.urgent))          # exact type, not "some PA/urgent"
        self.assertFalse(authority.is_assigned(None, self.psa))

    def test_one_assignment_per_user_and_type(self):
        ProducedContentTypeAssignment.objects.create(user=self.alice, content_type=self.psa)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProducedContentTypeAssignment.objects.create(user=self.alice, content_type=self.psa)

    def test_disabled_types_are_not_offered(self):
        ProducedContentTypeAssignment.objects.create(user=self.alice, content_type=self.psa)
        self.psa.enabled = False
        self.psa.save()
        self.assertFalse(authority.assigned_types(self.alice).exists())
        self.assertTrue(authority.assigned_types(self.alice, enabled_only=False).exists())

    def test_resolve_is_exact_and_rereads(self):
        self.assertEqual(authority.resolve("town_notice").pk, self.psa.pk)
        self.assertEqual(authority.resolve(self.psa.pk).key, "town_notice")
        stale = ProducedContentType.objects.get(pk=self.psa.pk)
        self.psa.enabled = False
        self.psa.save()
        self.assertFalse(authority.resolve(stale).enabled)
        for unknown in ("missing", 0, None, True, "", object()):
            with self.subTest(unknown=unknown), self.assertRaises(authority.UnknownContentType):
                authority.resolve(unknown)


@override_settings(SECURE_SSL_REDIRECT=False)
class AdminTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", password="x"))

    def test_the_admin_lists_creates_and_refuses_incompatible_settings(self):
        self.assertEqual(self.client.get(reverse("admin:produced_content_producedcontenttype_changelist"))
                         .status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:produced_content_producedcontenttypeassignment_changelist"))
                         .status_code, 200)
        add = reverse("admin:produced_content_producedcontenttype_add")
        self.assertEqual(self.client.get(add).status_code, 200)
        base = {"name": "School closings", "description": "", "enabled": "on", "sort_order": "0",
                "assignments-TOTAL_FORMS": "0", "assignments-INITIAL_FORMS": "0",
                "assignments-MIN_NUM_FORMS": "0", "assignments-MAX_NUM_FORMS": "1000"}
        refused = self.client.post(add, {**base, "key": "usd239sc", "delivery_mode": "public_address",
                                         "output_mode": "single"})
        self.assertEqual(refused.status_code, 200)                               # re-rendered with errors
        self.assertFalse(ProducedContentType.objects.filter(key="usd239sc").exists())
        created = self.client.post(add, {**base, "key": "usd239sc", "delivery_mode": "public_address"})
        self.assertEqual(created.status_code, 302)
        row = ProducedContentType.objects.get(key="usd239sc")
        self.assertEqual((row.producer_self_publish, row.recallable), (True, True))
        change = self.client.get(reverse("admin:produced_content_producedcontenttype_change", args=[row.pk]))
        self.assertEqual(change.status_code, 200)
        self.assertNotContains(change, 'name="key"')                             # the durable key is read-only
