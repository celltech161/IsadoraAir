"""D3C bridge: a regular ProducedContentType -> its explicit, MANAGED
spoken.SpokenContentType backend (produced_content.backend).

Exact mapping, explicit identity (never by key/code), idempotent and
concurrency-safe creation, fail-closed recipes, regular-only links, and no
silent drift (synchronization re-proves the managed fields; Django Admin shows
a managed backend read-only)."""
import threading
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from produced_content import backend, regular
from produced_content.models import ProducedContentType
from spoken import transitions
from spoken.models import (
    CurrentRecallPolicy, DeliveryMode, RecallPolicy, SpokenContentType, SpokenItem, TitlePolicy,
)

from .support import category, no_air_type, regular_type

User = get_user_model()
PRODUCED_TABLE = ProducedContentType._meta.db_table


def backing_of(content_type):
    return ProducedContentType.objects.get(pk=content_type.pk).spoken_content_type


class MappingTests(TestCase):
    def test_single_maps_to_a_current_edition_backend_with_the_exact_managed_configuration(self):
        produced = regular_type("town_news", output_mode="single", recipe_key="news_item", name="Town News",
                                description="Local news", artist_tag="KOGR News")
        backing = backend.ensure_regular_backend(produced)
        self.assertEqual(backing_of(produced).pk, backing.pk)
        self.assertEqual(
            (backing.enabled, backing.name, backing.description, backing.delivery_mode, backing.recipe_key,
             backing.recipe_params, backing.target_category_id, backing.artist_label, backing.requires_review,
             backing.deliver_ready_to_air, backing.on_current_recall, backing.recall_policy, backing.title_policy,
             backing.default_lifetime),
            (True, "Town News", "Local news", DeliveryMode.CURRENT_EDITION, "news_item", {},
             produced.target_category_id, "KOGR News", False, True, CurrentRecallPolicy.RETIRE_ASSET,
             RecallPolicy.OWNER_ANYTIME, TitlePolicy.ITEM_TITLE, None))
        # A generated, durable backend identity -- never the producer-facing key.
        self.assertTrue(backing.code.startswith(backend.BACKEND_CODE_PREFIX))
        self.assertNotIn("town_news", backing.code)

    def test_accumulating_maps_to_an_accumulating_backend_and_the_artist_falls_back_to_the_name(self):
        produced = regular_type("psa", output_mode="accumulating", recipe_key="lcl_psa", name="Local PSA")
        backing = backend.ensure_regular_backend(produced)
        self.assertEqual((backing.delivery_mode, backing.artist_label), (DeliveryMode.ACCUMULATING, "Local PSA"))

    def test_every_d3c_recipe_is_runnable_and_reused_from_the_registry(self):
        from production.services import render
        for index, recipe in enumerate(backend.D3C_RECIPES):
            with self.subTest(recipe=recipe):
                render.get(recipe)                                        # registered by spoken (Phase C)
                produced = regular_type(f"k{index}", recipe_key=recipe)
                self.assertEqual(backend.ensure_regular_backend(produced).recipe_key, recipe)


class SynchronizationTests(TestCase):
    def setUp(self):
        self.produced = regular_type("calendar", recipe_key="cal_events", name="Calendar", artist_tag="KOGR")
        self.backing = backend.ensure_regular_backend(self.produced)

    def test_category_recipe_artist_enabled_and_mode_follow_the_produced_type(self):
        produced = ProducedContentType.objects.get(pk=self.produced.pk)
        produced.name, produced.description = "Community Calendar", "What's on"
        produced.target_category = category("PCCAL")
        produced.recipe_key, produced.artist_tag, produced.output_mode = "3min_ctr", "", "accumulating"
        produced.enabled = False
        produced.save()
        self.assertFalse(backend.health(ProducedContentType.objects.get(pk=produced.pk)).ok)   # drift reported
        backing = backend.ensure_regular_backend(produced)
        self.assertEqual(backing.pk, self.backing.pk)                                         # same backend
        self.assertEqual((backing.name, backing.description, backing.target_category.code, backing.recipe_key,
                          backing.artist_label, backing.delivery_mode, backing.enabled),
                         ("Community Calendar", "What's on", "PCCAL", "3min_ctr", "Community Calendar",
                          DeliveryMode.ACCUMULATING, False))
        self.assertTrue(backend.health(ProducedContentType.objects.get(pk=produced.pk)).ok)

    def test_repeated_synchronization_is_idempotent(self):
        before = SpokenContentType.objects.get(pk=self.backing.pk)
        for _ in range(3):
            self.assertEqual(backend.ensure_regular_backend(self.produced).pk, self.backing.pk)
        after = SpokenContentType.objects.get(pk=self.backing.pk)
        self.assertEqual(SpokenContentType.objects.count(), 1)
        self.assertEqual(after.updated_at, before.updated_at)                 # a no-op writes nothing

    def test_drift_written_behind_the_bridge_is_re_proven_before_production(self):
        tampered = SpokenContentType.objects.get(pk=self.backing.pk)
        tampered.requires_review, tampered.recall_policy = True, RecallPolicy.MANAGERS_ONLY
        tampered.default_lifetime = timedelta(hours=1)
        tampered.save()                                                       # Phase C's own validated save
        health = backend.health(ProducedContentType.objects.get(pk=self.produced.pk))
        self.assertFalse(health.ok)
        self.assertIn("requires_review", health.text)
        backing = backend.ensure_regular_backend(self.produced)
        self.assertEqual((backing.requires_review, backing.recall_policy, backing.default_lifetime),
                         (False, RecallPolicy.OWNER_ANYTIME, None))

    def test_an_ordinary_stale_save_never_clears_the_backend_link(self):
        produced = regular_type("late", recipe_key="lcl_psa")
        stale = ProducedContentType.objects.get(pk=produced.pk)               # an admin form loaded earlier
        backing = backend.ensure_regular_backend(produced)                    # a producer's first submission
        stale.name = "Renamed"
        stale.save()
        self.assertEqual(backing_of(produced).pk, backing.pk)

    def test_synchronize_disables_the_backend_of_a_type_that_is_no_longer_runnable(self):
        produced = ProducedContentType.objects.get(pk=self.produced.pk)
        produced.recipe_key = ""
        produced.save()
        verdict = backend.synchronize(produced)
        self.assertFalse(verdict.ok)
        self.assertIn("no processing recipe", verdict.text)
        self.assertFalse(SpokenContentType.objects.get(pk=self.backing.pk).enabled)       # fail closed
        produced.recipe_key = "cal_events"
        produced.save()
        self.assertTrue(backend.synchronize(produced).ok)
        self.assertTrue(SpokenContentType.objects.get(pk=self.backing.pk).enabled)


class IdentityTests(TestCase):
    def test_identity_is_the_explicit_link_never_a_matching_key_or_code(self):
        lookalike = SpokenContentType.objects.create(
            code="news_item", name="Standalone", delivery_mode=DeliveryMode.CURRENT_EDITION, recipe_key="lcl_psa",
            enabled=True, target_category=category("PCSTAND"))
        produced = regular_type("news_item", recipe_key="news_item")
        backing = backend.ensure_regular_backend(produced)
        self.assertNotEqual(backing.pk, lookalike.pk)
        self.assertFalse(ProducedContentType.objects.filter(spoken_content_type=lookalike).exists())
        # A standalone Phase-C item is not a produced-content item.
        owner = User.objects.create_user("o")
        standalone = transitions.create_item(lookalike, actor=owner)
        self.assertIsNone(regular.managed_item(standalone.pk))
        self.assertIsNone(regular.produced_type_of(standalone))

    def test_only_the_bridge_links_a_backend(self):
        stray = SpokenContentType.objects.create(code="stray", name="Stray", delivery_mode=DeliveryMode.ACCUMULATING,
                                                 recipe_key="lcl_psa", target_category=category("PCSTRAY"))
        with self.assertRaises(ValidationError):
            ProducedContentType(key="x", name="x", delivery_mode="regular", output_mode="single",
                                target_category=category(), spoken_content_type=stray).save()


class FailClosedTests(TestCase):
    def test_a_blank_or_unknown_recipe_is_not_runnable_and_creates_nothing(self):
        user = User.objects.create_user("c", is_superuser=True)
        for index, recipe in enumerate(["", "spoken_short", "cal_events_v2"]):
            with self.subTest(recipe=recipe):
                produced = regular_type(f"bad{index}", recipe_key=recipe)
                with self.assertRaises(backend.NotRunnable) as caught:
                    backend.ensure_regular_backend(produced)
                self.assertIn("D3C processing recipe" if recipe else "no processing recipe", str(caught.exception))
                with self.assertRaises(regular.Refused):
                    regular.start(user, produced)
                self.assertFalse(backend.health(produced).ok)
        self.assertEqual((SpokenContentType.objects.count(), SpokenItem.objects.count()), (0, 0))

    def test_a_recipe_key_never_names_a_path_script_or_import(self):
        produced = regular_type("safe", recipe_key="lcl_psa")
        for value in ("processors/lcl_psa.py", "spoken.recipes._lcl_psa", "lcl psa; rm -rf /"):
            with self.subTest(value=value):
                produced.recipe_key = value
                with self.assertRaises(ValidationError):
                    produced.full_clean()

    def test_non_regular_types_never_acquire_a_backend(self):
        pa = ProducedContentType.objects.create(key="pa", name="pa", delivery_mode="public_address")
        test = no_air_type("practice")
        for content_type in (pa, test):
            with self.subTest(content_type.delivery_mode), self.assertRaises(backend.NotRegular):
                backend.ensure_regular_backend(content_type)
        self.assertEqual(SpokenContentType.objects.count(), 0)

    def test_a_backed_type_keeps_its_regular_mode(self):
        produced = regular_type("fixed")
        backend.ensure_regular_backend(produced)
        produced = ProducedContentType.objects.get(pk=produced.pk)
        produced.delivery_mode, produced.target_category, produced.output_mode, produced.recipe_key = "test", None, "", ""
        with self.assertRaises(ValidationError) as caught:
            produced.full_clean()
        self.assertIn("delivery_mode", caught.exception.message_dict)
        with self.assertRaises(ValidationError):
            produced.save()


@override_settings(SECURE_SSL_REDIRECT=False)
class AdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", "root@example.invalid", "x")
        self.client.force_login(self.admin)

    def test_a_managed_backend_is_read_only_and_undeletable_in_admin(self):
        produced = regular_type("admin_news", recipe_key="news_item")
        backing = backend.ensure_regular_backend(produced)
        url = f"/admin/spoken/spokencontenttype/{backing.pk}/change/"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Produced Content")
        self.assertContains(page, "admin_news")
        self.assertNotContains(page, 'name="_save"')                     # view only
        response = self.client.post(url, {"code": backing.code, "name": "Drifted", "delivery_mode": "accumulating",
                                          "recipe_key": "lcl_psa", "recipe_params": "{}", "title_policy": "item_title",
                                          "on_current_recall": "retire_asset", "recall_policy": "managers_only"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SpokenContentType.objects.get(pk=backing.pk).name, produced.name)
        self.assertEqual(self.client.post(f"/admin/spoken/spokencontenttype/{backing.pk}/delete/",
                                          {"post": "yes"}).status_code, 403)

    def test_an_unlinked_spoken_type_stays_ordinary_phase_c_configuration(self):
        standalone = SpokenContentType.objects.create(
            code="standalone", name="Standalone", delivery_mode=DeliveryMode.ACCUMULATING, recipe_key="lcl_psa",
            target_category=category("PCSTAND"))
        page = self.client.get(f"/admin/spoken/spokencontenttype/{standalone.pk}/change/")
        self.assertContains(page, 'name="_save"')

    def test_saving_a_regular_type_in_admin_synchronizes_and_reports_its_backend(self):
        produced = regular_type("admin_cal", recipe_key="cal_events", name="Calendar")
        backing = backend.ensure_regular_backend(produced)
        page = self.client.get(f"/admin/produced_content/producedcontenttype/{produced.pk}/change/")
        self.assertContains(page, backing.code)
        self.assertContains(page, "healthy")
        self.assertNotContains(page, "processing arrives later")
        data = {"key": "admin_cal", "name": "Events Calendar", "description": "", "enabled": "on", "sort_order": "0",
                "delivery_mode": "regular", "target_category": produced.target_category_id, "output_mode": "single",
                "recipe_key": "cal_events", "artist_tag": "", "assignments-TOTAL_FORMS": "0",
                "assignments-INITIAL_FORMS": "0", "assignments-MIN_NUM_FORMS": "0", "assignments-MAX_NUM_FORMS": "1000"}
        response = self.client.post(f"/admin/produced_content/producedcontenttype/{produced.pk}/change/", data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and
                         response.context["adminform"].form.errors)
        backing = SpokenContentType.objects.get(pk=backing.pk)
        self.assertEqual((backing.name, backing.artist_label), ("Events Calendar", "Events Calendar"))
        self.assertEqual(backing_of(produced).pk, backing.pk)
        data["recipe_key"] = ""
        response = self.client.post(f"/admin/produced_content/producedcontenttype/{produced.pk}/change/", data,
                                    follow=True)
        self.assertContains(response, "Producers cannot submit until this is fixed")
        self.assertFalse(SpokenContentType.objects.get(pk=backing.pk).enabled)


class ConcurrentFirstSynchronizationTests(TransactionTestCase):
    """Two producers' first submissions race: exactly ONE backend is created
    (the produced type's row lock serializes them; the waiter sees the link)."""

    serialized_rollback = True        # restore the migrated data afterwards (later serialized classes rely on it)

    def test_concurrent_first_synchronization_creates_one_backend(self):
        produced = regular_type("race", recipe_key="lcl_psa")
        ready, release, results, errors = threading.Event(), threading.Event(), [], []

        def holder():
            try:
                with transaction.atomic():
                    results.append(backend.ensure_regular_backend(produced.pk).pk)
                    ready.set()
                    release.wait(30)
            except BaseException as exc:          # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        def waiter():
            try:
                results.append(backend.ensure_regular_backend(produced.pk).pk)
            except BaseException as exc:          # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        first = threading.Thread(target=holder, daemon=True)
        first.start()
        self.assertTrue(ready.wait(30))
        second = threading.Thread(target=waiter, daemon=True)
        second.start()
        deadline = time.monotonic() + 20
        while True:                                # observe the second transaction waiting for the row lock
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                               "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' AND query ILIKE %s",
                               [f"%{PRODUCED_TABLE}%"])
                if cursor.fetchone()[0]:
                    break
            self.assertLess(time.monotonic(), deadline, "the second synchronization never waited")
            time.sleep(0.005)
        release.set()
        first.join(30)
        second.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(SpokenContentType.objects.count(), 1)
        self.assertEqual(backing_of(produced).pk, results[0])
